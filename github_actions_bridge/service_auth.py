"""Bridge auth surfaces: public GitHub OAuth + service-only TokenVerifier MCP.

FastMCP 2.x does not reliably support a custom CompositeAuthProvider that mixes
GitHub OAuth discovery with a static service bearer on the same ``/mcp`` route
(OAuth ``required_scopes`` and discovery metadata break Gateway service calls).

Supported two-surface design (no FastMCP 3 migration):

* Public ``/mcp`` — GitHub OAuth only (unchanged ChatGPT / operator clients).
* Service ``/mcp/service`` — ``BRIDGE_SERVICE_TOKEN`` via TokenVerifier / static
  bearer only. Fail closed. No GitHub OAuth discovery on this path.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Any
from urllib.parse import urlparse

from fastmcp.server.auth import AccessToken, AuthProvider, TokenVerifier
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_protected_resource_routes,
)
from starlette.authentication import AuthenticationBackend
from starlette.requests import HTTPConnection
from starlette.routing import Route

SERVICE_CLIENT_ID = "hal-gateway-service"
BRIDGE_SERVICE_TOKEN_ENV = "BRIDGE_SERVICE_TOKEN"
DEFAULT_PUBLIC_MCP_PATH = "/mcp"
DEFAULT_SERVICE_MCP_PATH = "/mcp/service"
# ChatGPT plugin identity for the existing bridge-backed record. Resource URL
# stays {BRIDGE_PUBLIC_URL}/mcp — only the human-readable RFC 9728 name is
# stamped so in-place Refresh keeps binding to this origin.
CANONICAL_GATEWAY_DISPLAY_NAME = "HAL LegalAI Gateway"
PLUGIN_REFRESH_PROTECTED_RESOURCE_PATH = (
    "/.well-known/oauth-protected-resource" + DEFAULT_PUBLIC_MCP_PATH
)

logger = logging.getLogger(__name__)


def normalize_bearer_token(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip()
    return cleaned or None


def token_fingerprint(value: str) -> str:
    """Short SHA-256 fingerprint for diagnostics; never log raw token values."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:12]


def _log_service_verify(
    *,
    accepted: bool,
    missing_bearer: bool,
    provided_len: int,
    expected_len: int,
    fingerprint_match: bool,
    provided_fp: str | None = None,
    expected_fp: str | None = None,
) -> None:
    """Secret-safe verifier boundary diagnostics (booleans/lengths/fingerprints)."""
    logger.warning(
        "service_token_verify accepted=%s missing_bearer=%s provided_len=%s "
        "expected_len=%s fingerprint_match=%s provided_fp=%s expected_fp=%s",
        accepted,
        missing_bearer,
        provided_len,
        expected_len,
        fingerprint_match,
        provided_fp or "-",
        expected_fp or "-",
    )


class ServiceTokenVerifier(TokenVerifier):
    """Constant-time verifier for a dedicated non-expiring service credential.

    Supported FastMCP 2.x ``TokenVerifier`` (static bearer) auth provider — use
    on the service-only MCP path, never composed into the public OAuth surface.
    """

    def __init__(
        self,
        expected_token: str,
        *,
        client_id: str = SERVICE_CLIENT_ID,
        required_scopes: list[str] | None = None,
    ) -> None:
        super().__init__(required_scopes=required_scopes)
        expected = normalize_bearer_token(expected_token)
        if not expected:
            raise ValueError("service token must be non-empty")
        self._expected = expected
        self._client_id = client_id
        self._expected_fp = token_fingerprint(expected)

    async def verify_token(self, token: str) -> AccessToken | None:
        provided = normalize_bearer_token(token)
        if not provided:
            _log_service_verify(
                accepted=False,
                missing_bearer=True,
                provided_len=0,
                expected_len=len(self._expected),
                fingerprint_match=False,
                expected_fp=self._expected_fp,
            )
            return None
        if not secrets.compare_digest(provided, self._expected):
            _log_service_verify(
                accepted=False,
                missing_bearer=False,
                provided_len=len(provided),
                expected_len=len(self._expected),
                fingerprint_match=False,
                provided_fp=token_fingerprint(provided),
                expected_fp=self._expected_fp,
            )
            return None
        return AccessToken(
            token=provided,
            client_id=self._client_id,
            scopes=list(self.required_scopes or []),
            expires_at=None,
            claims={
                "token_use": "service",
                "client_id": self._client_id,
            },
        )


class FailClosedTokenVerifier(TokenVerifier):
    """Reject every bearer token (service surface when ``BRIDGE_SERVICE_TOKEN`` unset)."""

    def __init__(self) -> None:
        super().__init__(required_scopes=None)

    async def verify_token(self, token: str) -> AccessToken | None:
        provided = normalize_bearer_token(token)
        missing = not provided
        _log_service_verify(
            accepted=False,
            missing_bearer=missing,
            provided_len=0 if missing else len(provided or ""),
            expected_len=0,
            fingerprint_match=False,
            provided_fp=None if missing else token_fingerprint(provided or ""),
        )
        return None


class PathAwareBearerBackend(AuthenticationBackend):
    """Route AuthenticationMiddleware to OAuth or service TokenVerifier by path.

    The service MCP path never consults GitHub OAuth verification.
    """

    def __init__(
        self,
        *,
        oauth: AuthProvider,
        service: TokenVerifier,
        service_mcp_path: str = DEFAULT_SERVICE_MCP_PATH,
    ) -> None:
        self._oauth_backend = BearerAuthBackend(oauth)
        self._service_backend = BearerAuthBackend(service)
        path = service_mcp_path if service_mcp_path.startswith("/") else f"/{service_mcp_path}"
        self._service_mcp_path = path.rstrip("/") or DEFAULT_SERVICE_MCP_PATH

    def _is_service_path(self, path: str) -> bool:
        return path == self._service_mcp_path or path.startswith(
            f"{self._service_mcp_path}/"
        )

    async def authenticate(self, conn: HTTPConnection) -> Any:
        path = conn.scope.get("path") or ""
        if self._is_service_path(path):
            return await self._service_backend.authenticate(conn)
        return await self._oauth_backend.authenticate(conn)


def is_service_access_token(token: AccessToken | None) -> bool:
    if token is None:
        return False
    claims = token.claims or {}
    return (
        claims.get("token_use") == "service"
        or claims.get("client_id") == SERVICE_CLIENT_ID
        or token.client_id == SERVICE_CLIENT_ID
    )


def build_service_auth_provider(
    service_token: str | None,
) -> TokenVerifier:
    """Build the service-only MCP TokenVerifier (fail closed when unset)."""
    cleaned = normalize_bearer_token(service_token)
    if not cleaned:
        return FailClosedTokenVerifier()
    return ServiceTokenVerifier(cleaned)


def compose_dual_mcp_http_app(
    mcp: Any,
    *,
    oauth_auth: AuthProvider,
    service_auth: TokenVerifier,
    public_mcp_path: str = DEFAULT_PUBLIC_MCP_PATH,
    service_mcp_path: str = DEFAULT_SERVICE_MCP_PATH,
    json_response: bool = False,
) -> Any:
    """Compose public OAuth ``/mcp`` and service-only TokenVerifier MCP apps.

    OAuth discovery/routes stay on the public surface only. The service path is
    protected solely by ``service_auth`` and never mounts GitHub OAuth metadata.
    """
    from contextlib import asynccontextmanager

    from fastmcp.server.http import RequestContextMiddleware, create_streamable_http_app
    from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.authentication import AuthenticationMiddleware

    public_path = (
        public_mcp_path if public_mcp_path.startswith("/") else f"/{public_mcp_path}"
    )
    service_path = (
        service_mcp_path
        if service_mcp_path.startswith("/")
        else f"/{service_mcp_path}"
    )
    public_path = public_path.rstrip("/") or DEFAULT_PUBLIC_MCP_PATH
    service_path = service_path.rstrip("/") or DEFAULT_SERVICE_MCP_PATH

    public_http = create_streamable_http_app(
        mcp,
        public_path,
        auth=oauth_auth,
        json_response=json_response,
    )
    service_http = create_streamable_http_app(
        mcp,
        service_path,
        auth=service_auth,
        json_response=json_response,
    )

    routes = list(public_http.routes)
    routes.extend(
        route
        for route in service_http.routes
        if getattr(route, "path", None) == service_path
    )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with public_http.lifespan(app):
            async with service_http.lifespan(app):
                yield

    middleware = [
        Middleware(RequestContextMiddleware),
        Middleware(
            AuthenticationMiddleware,
            backend=PathAwareBearerBackend(
                oauth=oauth_auth,
                service=service_auth,
                service_mcp_path=service_path,
            ),
        ),
        Middleware(AuthContextMiddleware),
    ]
    return Starlette(routes=routes, lifespan=lifespan, middleware=middleware)


def plugin_refresh_mcp_url(public_url: str) -> str:
    """Exact Streamable HTTP URL ChatGPT Refresh recaches for this plugin."""
    return f"{public_url.rstrip('/')}{DEFAULT_PUBLIC_MCP_PATH}"


def plugin_refresh_protected_resource_path() -> str:
    """RFC 9728 metadata path for the public ``/mcp`` resource."""
    return PLUGIN_REFRESH_PROTECTED_RESOURCE_PATH


def replace_canonical_protected_resource_routes(
    provider: AuthProvider,
    routes: list[Any],
) -> list[Any]:
    """Stamp RFC 9728 ``resource_name`` without changing the resource URL."""
    resource_url = getattr(provider, "_resource_url", None)
    if resource_url is None:
        return routes
    issuer = getattr(provider, "issuer_url", None) or getattr(
        provider, "base_url", None
    )
    if issuer is None:
        return routes
    metadata_path = urlparse(str(build_resource_metadata_url(resource_url))).path
    kept = [
        route
        for route in routes
        if not (
            isinstance(route, Route) and getattr(route, "path", None) == metadata_path
        )
    ]
    registration = getattr(provider, "client_registration_options", None)
    scopes = None
    if registration is not None:
        scopes = getattr(registration, "valid_scopes", None)
    if not scopes:
        scopes = list(getattr(provider, "required_scopes", None) or [])
    named = create_protected_resource_routes(
        resource_url=resource_url,
        authorization_servers=[issuer],
        scopes_supported=list(scopes) if scopes else None,
        resource_name=CANONICAL_GATEWAY_DISPLAY_NAME,
    )
    return kept + list(named)


def stamp_canonical_protected_resource_identity(
    provider: AuthProvider,
) -> AuthProvider:
    """Ensure OAuth protected-resource metadata names HAL LegalAI Gateway.

    FastMCP's GitHubProvider omits ``resource_name``. ChatGPT Refresh uses MCP
    ``tools/list`` plus this RFC 9728 field; the resource URL (plugin key) is
    left unchanged so the existing bridge-backed record can recache in place.
    """
    original_get_routes = provider.get_routes

    def get_routes(mcp_path: str | None = None) -> list[Any]:
        return replace_canonical_protected_resource_routes(
            provider,
            original_get_routes(mcp_path),
        )

    provider.get_routes = get_routes  # type: ignore[method-assign]
    return provider
