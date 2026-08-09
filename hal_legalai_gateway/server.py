"""HAL LegalAI Gateway HTTP + authenticated MCP service (Phase 2)."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastmcp import FastMCP

from hal_legalai_gateway.config import GatewaySettings, load_settings
from hal_legalai_gateway.auth import GatewayAPIKeyMiddleware
from hal_legalai_gateway.health import aggregate_health
from hal_legalai_gateway.mcp_server import (
    create_mcp_server,
    list_registered_tool_names,
)
from hal_legalai_gateway.request_context import (
    RequestIdMiddleware,
    configure_logging,
    get_correlation_id,
    get_request_id,
)

logger = logging.getLogger(__name__)

_settings: GatewaySettings | None = None
_mcp: FastMCP | None = None
_registered_tools: list[str] = []
_mcp_http_app: Any = None


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
    global _settings, _mcp, _registered_tools, _mcp_http_app
    _settings = None
    _mcp = None
    _registered_tools = []
    _mcp_http_app = None


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
    # Fail closed on missing GATEWAY_API_KEY / GATEWAY_BRIDGE_AUTHORIZATION.
    _settings = load_settings()
    _mcp = create_mcp_server(_settings)
    _mcp_http_app = _mcp.http_app(path="/mcp", transport="http")
    _attach_mcp_routes(application, _mcp_http_app)
    _registered_tools = await list_registered_tool_names(_mcp)
    logger.info(
        "HAL LegalAI Gateway starting phase=2 deployed_commit_sha=%s "
        "downstreams=%s registered_tools=%s health_timeout_seconds=%s "
        "connect_timeout_seconds=%s read_timeout_seconds=%s",
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


def create_app() -> FastAPI:
    """Application factory used by uvicorn and tests."""
    application = FastAPI(
        title="HAL LegalAI Gateway",
        description=(
            "Thin authenticated interface consolidation for LegalAI downstream "
            "MCP services. Phase 2 exposes namespaced case/storage/mission tools "
            "that forward to Bridge, Storage, artifact retrieval, and Mission "
            "Control. Downstream business logic remains separately deployed."
        ),
        version="0.2.0",
        lifespan=lifespan,
    )
    # Order: request IDs outermost, then MCP Bearer gate for /mcp only.
    application.add_middleware(
        GatewayAPIKeyMiddleware,
        api_key_provider=lambda: get_settings().gateway_api_key,
    )
    application.add_middleware(RequestIdMiddleware)

    @application.get("/")
    async def root(request: Request) -> dict[str, Any]:
        settings = get_settings()
        return {
            "service": "hal-legalai-gateway",
            "phase": 2,
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
            "registered_tools": list(_registered_tools),
        }

    @application.get("/registry")
    async def registry_endpoint() -> dict[str, Any]:
        """Return the machine-readable logical registry (no live secrets)."""
        settings = get_settings()
        payload = settings.registry.as_public_dict()
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
        Railway-provided commit SHA.
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
        return JSONResponse(payload)

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
