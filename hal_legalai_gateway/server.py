"""HAL LegalAI Gateway HTTP + authenticated MCP service (Phase 2)."""

from __future__ import annotations

import logging
import os
import base64
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from starlette.authentication import AuthenticationBackend
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection

from hal_legalai_gateway.config import GatewaySettings, load_settings
from hal_legalai_gateway.health import aggregate_health
from hal_legalai_gateway.mcp_server import (
    CANONICAL_GATEWAY_DISPLAY_NAME,
    canonical_gateway_identity,
    create_mcp_server,
    list_registered_tool_names,
)
from hal_legalai_gateway.forwarding import ToolBinding, forward_mcp_tool
from hal_legalai_gateway.intake_upload import IntakeUploadSessions
from hal_legalai_gateway.request_context import (
    RequestIdMiddleware,
    configure_logging,
    get_correlation_id,
    get_request_id,
)

logger = logging.getLogger(__name__)

REQUIRED_PUBLIC_TOOL_NAMES = frozenset(
    {
        "storage.get_acceptance_contract",
        "storage.archive_acceptance_contract",
        "case.submit",
        "case.status",
        "case.cancel",
        "case.list_artifacts",
    }
)


def required_tool_parity(registered_tools: list[str]) -> dict[str, Any]:
    """Return a stable, non-sensitive public-tool parity record."""
    actual = set(registered_tools)
    missing = sorted(REQUIRED_PUBLIC_TOOL_NAMES - actual)
    return {
        "ok": not missing,
        "required_tools": sorted(REQUIRED_PUBLIC_TOOL_NAMES),
        "missing_tools": missing,
    }

_settings: GatewaySettings | None = None
_mcp: FastMCP | None = None
_registered_tools: list[str] = []
_mcp_http_app: Any = None
_auth_override: AuthProvider | None = None
_intake_upload_sessions: IntakeUploadSessions | None = None

RENNICK_SOURCE_BYTES_MAX = 50 * 1024 * 1024
RENNICK_MANIFEST_BYTES_MAX = 128 * 1024


class _GatewayAuthBackend(AuthenticationBackend):
    """Lazy Bearer backend so auth works after MCP routes are composed into FastAPI.

    FastMCP wraps ``/mcp`` with ``RequireAuthMiddleware``, which expects
    ``AuthenticationMiddleware`` to have populated ``scope["user"]``. Route
    copying alone does not install that middleware on the parent app.
    """

    async def authenticate(self, conn: HTTPConnection):
        provider = _auth_override
        if provider is None and _mcp is not None:
            provider = getattr(_mcp, "auth", None)
        if provider is None:
            return None
        return await BearerAuthBackend(provider).authenticate(conn)




def get_settings() -> GatewaySettings:
    """Return process settings, loading once if lifespan has not run yet."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings

def get_mcp() -> FastMCP | None:
    return _mcp

def reset_settings_for_tests() -> None:
    """Clear cached settings / MCP state (test helper)."""
    global _settings, _mcp, _registered_tools, _mcp_http_app, _auth_override, _intake_upload_sessions
    _settings = None
    _mcp = None
    _registered_tools = []
    _mcp_http_app = None
    _auth_override = None
    _intake_upload_sessions = None


def _upload_sessions() -> IntakeUploadSessions:
    global _intake_upload_sessions
    if _intake_upload_sessions is None:
        _intake_upload_sessions = IntakeUploadSessions(get_settings().jwt_signing_key)
    return _intake_upload_sessions


async def _forward_rennick_pair(source: bytes, manifest: bytes) -> dict[str, Any]:
    """Forward browser-uploaded bytes only on the private Gateway → Bridge hop."""
    settings = get_settings()
    binding = ToolBinding(
        gateway_tool="storage.upload_rennick_case_intake",
        namespace="storage",
        downstream_service="storage",
        downstream_tool="upload_rennick_case_intake",
    )
    downstream = settings.downstream_by_key("storage")
    return await forward_mcp_tool(
        binding=binding,
        arguments={
            "source_bundle_base64": base64.b64encode(source).decode("ascii"),
            "manifest_base64": base64.b64encode(manifest).decode("ascii"),
        },
        base_url=downstream.base_url,
        authorization=settings.bridge_authorization,
        connect_timeout_seconds=settings.connect_timeout_seconds,
        read_timeout_seconds=max(settings.read_timeout_seconds, 300.0),
        mcp_path=settings.mcp_path_for_service("storage"),
        extra_secrets=settings.secret_values_for_redaction(),
    )

def _attach_mcp_routes(application: FastAPI, mcp_app: Any) -> None:
    """Install (or replace) FastMCP routes so lifespan-bound session managers match."""
    application.router.routes = [
        route
        for route in application.router.routes
        if getattr(route, "path", None) not in {
            getattr(mcp_route, "path", None) for mcp_route in mcp_app.routes
        }
    ]
    for route in mcp_app.routes:
        application.router.routes.append(route)


@asynccontextmanager
async def lifespan(application: FastAPI):
    global _settings, _registered_tools, _mcp_http_app, _mcp
    configure_logging()
    # Fail closed on missing GitHub OAuth config / GATEWAY_BRIDGE_AUTHORIZATION.
    _settings = load_settings()
    auth = _auth_override
    _mcp = create_mcp_server(_settings, auth=auth)
    _mcp_http_app = _mcp.http_app(path="/mcp", transport="http")
    _attach_mcp_routes(application, _mcp_http_app)
    _registered_tools = await list_registered_tool_names(_mcp)
    logger.info(
        "HAL LegalAI Gateway starting phase=2 deployed_commit_sha=%s "
        "downstreams=%s registered_tools=%s health_timeout_seconds=%s "
        "connect_timeout_seconds=%s read_timeout_seconds=%s inbound_auth=github_oauth",
        _settings.deployed_commit_sha,
        ",".join(item.key for item in _settings.downstreams),
        ",".join(_registered_tools),
        _settings.health_timeout_seconds,
        _settings.connect_timeout_seconds,
        _settings.read_timeout_seconds,
    )
    async with _mcp_http_app.lifespan(application):
        yield
    logger.info("HAL LegalAI Gateway shutting down")
    _settings = None
    _mcp = None
    _registered_tools = []
    _mcp_http_app = None


def create_app(*, auth_override: AuthProvider | None = None) -> FastAPI:
    """Application factory used by uvicorn and tests.

    ``auth_override`` is for tests only (inject a fixed token verifier). Production
    always uses GitHub OAuth via settings.
    """
    global _auth_override
    _auth_override = auth_override

    application = FastAPI(
        title=CANONICAL_GATEWAY_DISPLAY_NAME,
        description=(
            "Thin authenticated interface consolidation for LegalAI downstream "
            "MCP services. Phase 2 exposes namespaced case/storage/mission/"
            "workflow tools that forward to Bridge, Storage, artifact "
            "retrieval, and Mission Control. Downstream business logic remains "
            "separately deployed. Inbound /mcp uses GitHub OAuth for ChatGPT "
            "Business custom MCP. Canonical plugin identity is "
            f"{CANONICAL_GATEWAY_DISPLAY_NAME}."
        ),
        version="0.2.0",
        lifespan=lifespan,
    )
    # Order: last added is outermost. Request IDs outer; auth populates scope
    # for FastMCP RequireAuthMiddleware on /mcp; /health and /registry stay open.
    application.add_middleware(AuthContextMiddleware)
    application.add_middleware(
        AuthenticationMiddleware,
        backend=_GatewayAuthBackend(),
    )
    application.add_middleware(RequestIdMiddleware)

    @application.get("/")
    async def root(request: Request) -> dict[str, Any]:
        settings = get_settings()
        identity = canonical_gateway_identity(
            public_url=settings.gateway_public_url
        )
        return {
            "service": identity["service_id"],
            "phase": 2,
            "identity": identity,
            "deployed_commit_sha": settings.deployed_commit_sha,
            "request_id": getattr(request.state, "request_id", get_request_id()),
            "correlation_id": getattr(
                request.state, "correlation_id", get_correlation_id()
            ),
            "endpoints": {
                "health": "/health",
                "registry": "/registry",
                "mcp": "/mcp",
            },
            "auth": {
                "inbound": "github_oauth",
                "downstream_bridge": "service_credential",
            },
            "registered_tools": list(_registered_tools),
        }

    @application.get("/.well-known/openid-configuration", include_in_schema=False)
    async def openid_configuration() -> RedirectResponse:
        """Alias OIDC discovery to the OAuth authorization-server metadata.

        ChatGPT's custom-app scanner probes the OIDC well-known path before
        starting OAuth. The Gateway is an OAuth authorization server rather
        than an OpenID Connect identity provider, but its existing OAuth
        metadata has the required authorization, token, and registration
        endpoints.
        """
        base = get_settings().gateway_public_url.rstrip("/")
        return RedirectResponse(
            url=f"{base}/.well-known/oauth-authorization-server",
            status_code=307,
        )

    @application.get("/intake/rennick/upload", include_in_schema=False)
    async def rennick_upload_page(ticket: str) -> HTMLResponse:
        # The ticket is validated by both binary POST routes; never expose any
        # source content in this page or in error responses.
        return HTMLResponse(f'''<!doctype html><title>Rennick intake upload</title>
<main><h2>Rennick intake upload</h2><p>Select the exact ZIP and manifest.</p>
<input id="source" type="file" accept=".zip"><br><input id="manifest" type="file" accept=".json"><br>
<button id="upload">Upload and verify</button><pre id="status"></pre></main>
<script>
const t={ticket!r}, out=document.getElementById('status');
async function put(path,file) {{ const r=await fetch(path+'?ticket='+encodeURIComponent(t),{{method:'POST',body:file}}); if(!r.ok) throw new Error(await r.text()); return r.json(); }}
document.getElementById('upload').onclick=async()=>{{try{{const s=document.getElementById('source').files[0],m=document.getElementById('manifest').files[0];if(!s||!m)throw new Error('Select both files.');out.textContent='Uploading ZIP…';await put('/intake/rennick/source',s);out.textContent='Uploading manifest and verifying…';out.textContent=JSON.stringify(await put('/intake/rennick/manifest',m),null,2)}}catch(e){{out.textContent='Upload failed: '+e.message}}}};
</script>''')

    @application.post("/intake/rennick/source", include_in_schema=False)
    async def rennick_source(ticket: str, request: Request) -> JSONResponse:
        source = await request.body()
        if not source or len(source) > RENNICK_SOURCE_BYTES_MAX:
            return JSONResponse({"ok": False, "error": "invalid_source_size"}, status_code=400)
        if not _upload_sessions().store_source(ticket, source):
            return JSONResponse({"ok": False, "error": "invalid_or_expired_ticket"}, status_code=403)
        return JSONResponse({"ok": True, "source_received": True})

    @application.post("/intake/rennick/manifest", include_in_schema=False)
    async def rennick_manifest(ticket: str, request: Request) -> JSONResponse:
        manifest = await request.body()
        if not manifest or len(manifest) > RENNICK_MANIFEST_BYTES_MAX:
            return JSONResponse({"ok": False, "error": "invalid_manifest_size"}, status_code=400)
        source = _upload_sessions().take_source(ticket)
        if source is None:
            return JSONResponse({"ok": False, "error": "source_missing_or_ticket_expired"}, status_code=400)
        result = await _forward_rennick_pair(source, manifest)
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)

    @application.get("/registry")
    async def registry_endpoint() -> dict[str, Any]:
        """Return the machine-readable logical registry (no live secrets)."""
        settings = get_settings()
        payload = settings.registry.as_public_dict()
        payload["identity"] = canonical_gateway_identity(
            public_url=settings.gateway_public_url
        )
        payload["request_id"] = get_request_id()
        payload["correlation_id"] = get_correlation_id()
        payload["resolved_downstreams"] = {
            item.key: {
                "service_id": item.service_id,
                "configured": item.configured,
                "base_url_env": item.base_url_env,
                "base_url": item.base_url,
                "health_url": item.health_url,
            }
            for item in settings.downstreams
        }
        return payload

    @application.get("/health")
    async def health(request: Request) -> JSONResponse:
        """Gateway liveness plus independent downstream health aggregation.

        Always returns HTTP 200 when the gateway process itself is up so a single
        unhealthy downstream cannot take the gateway (or unrelated capabilities)
        out of Railway rotation. Reports exact registered MCP tool names and the
        Railway-provided commit SHA. Never includes secrets.
        """
        settings = get_settings()
        tools = list(_registered_tools)
        if not tools and _mcp is not None:
            tools = await list_registered_tool_names(_mcp)
        payload = await aggregate_health(settings, registered_tools=tools)
        payload["request_id"] = getattr(
            request.state, "request_id", payload.get("request_id")
        )
        payload["correlation_id"] = getattr(
            request.state, "correlation_id", payload.get("correlation_id")
        )
        payload["deployed_commit_sha"] = settings.deployed_commit_sha
        payload["auth"] = {
            "inbound": "github_oauth",
            "downstream_bridge": "service_credential",
        }
        payload["identity"] = canonical_gateway_identity(
            public_url=settings.gateway_public_url
        )
        payload["required_tool_parity"] = required_tool_parity(tools)
        return JSONResponse(payload)

    @application.get("/ready")
    async def ready() -> JSONResponse:
        """Deployment readiness: fail closed on public-tool registration drift."""
        tools = list(_registered_tools)
        if not tools and _mcp is not None:
            tools = await list_registered_tool_names(_mcp)
        parity = required_tool_parity(tools)
        status_code = 200 if parity["ok"] else 503
        return JSONResponse(
            {
                "service": "hal-legalai-gateway",
                "deployed_commit_sha": get_settings().deployed_commit_sha,
                "required_tool_parity": parity,
            },
            status_code=status_code,
        )

    return application


app = create_app()


def main() -> None:
    """Entry point for local/Railway process start."""
    import uvicorn

    # Fail closed on invalid configuration before binding the port.
    load_settings()
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(
        "hal_legalai_gateway.server:app",
        host="0.0.0.0",
        port=port,
        factory=False,
    )


if __name__ == "__main__":
    main()
