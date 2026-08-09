"""Inbound gateway authentication (fail-closed API key).

Downstream Bridge MCP uses GitHub OAuth (FastMCP GitHubProvider). That is not
compatible with a shared gateway API key, so Bridge calls use either:

1. Caller-supplied ``X-Downstream-Authorization`` after the gateway API key is
   validated (preferred when the caller holds a Bridge-compatible token), or
2. Explicit service-to-service ``GATEWAY_BRIDGE_AUTHORIZATION``.

Mission Control MCP (mcp_connector) authenticates to the Mission Control HTTP
API with a server-side key and does not require a caller Bearer on the MCP
wire. Gateway API key enforcement is therefore the write gate for mission.*
tools.
"""

from __future__ import annotations

import secrets
from typing import Any, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

GATEWAY_API_KEY_ENV = "GATEWAY_API_KEY"
DOWNSTREAM_AUTHORIZATION_HEADER = "X-Downstream-Authorization"


def tokens_match(provided: str, expected: str) -> bool:
    """Constant-time API key comparison."""
    if not provided or not expected:
        return False
    return secrets.compare_digest(provided, expected)


def extract_bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    value = authorization_header.strip()
    if not value.lower().startswith("bearer "):
        return None
    token = value[7:].strip()
    return token or None


class GatewayAPIKeyMiddleware(BaseHTTPMiddleware):
    """Require ``GATEWAY_API_KEY`` Bearer auth for the MCP endpoint only.

    ``/health`` and ``/registry`` stay unauthenticated for Railway probes.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        api_key_provider: Callable[[], str],
        protected_prefixes: tuple[str, ...] = ("/mcp",),
    ) -> None:
        super().__init__(app)
        self._api_key_provider = api_key_provider
        self._protected_prefixes = protected_prefixes

    def _is_protected(self, path: str) -> bool:
        normalized = path if path != "/" else path
        for prefix in self._protected_prefixes:
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return True
        return False

    async def dispatch(
        self, request: Request, call_next: Callable
    ) -> Response:
        if not self._is_protected(request.url.path):
            return await call_next(request)

        expected = (self._api_key_provider() or "").strip()
        if not expected:
            return JSONResponse(
                {
                    "error": "invalid_token",
                    "error_description": (
                        f"{GATEWAY_API_KEY_ENV} is not configured"
                    ),
                },
                status_code=503,
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = extract_bearer_token(request.headers.get("Authorization"))
        if token is None or not tokens_match(token, expected):
            return JSONResponse(
                {
                    "error": "invalid_token",
                    "error_description": (
                        "Authentication failed. Provide "
                        f"Authorization: Bearer <{GATEWAY_API_KEY_ENV}>."
                    ),
                },
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


def extract_downstream_authorization(
    headers: Any,
    *,
    service_authorization: str | None,
) -> str | None:
    """Resolve Bridge-compatible Authorization after gateway auth succeeded.

    Prefer a caller-supplied downstream header when present; otherwise use the
    configured service-to-service value. Never returns the gateway API key.
    """
    incoming = ""
    if headers is not None:
        try:
            incoming = (headers.get(DOWNSTREAM_AUTHORIZATION_HEADER) or "").strip()
        except Exception:  # noqa: BLE001 — headers may be plain mapping or Starlette
            incoming = ""
    if incoming:
        if incoming.lower().startswith("bearer "):
            return incoming
        return f"Bearer {incoming}"
    if service_authorization:
        cleaned = service_authorization.strip()
        if not cleaned:
            return None
        if cleaned.lower().startswith("bearer "):
            return cleaned
        return f"Bearer {cleaned}"
    return None
