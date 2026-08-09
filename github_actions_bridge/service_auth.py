"""Narrow service-to-service auth for Gateway → Bridge cutover.

Preserves direct GitHub OAuth for interactive Bridge clients while accepting a
dedicated non-expiring ``BRIDGE_SERVICE_TOKEN`` (matched by the Gateway's
``GATEWAY_BRIDGE_AUTHORIZATION``). User OAuth session tokens are not reused as
generic downstream secrets.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastmcp.server.auth import AccessToken, AuthProvider, TokenVerifier

SERVICE_CLIENT_ID = "hal-gateway-service"
BRIDGE_SERVICE_TOKEN_ENV = "BRIDGE_SERVICE_TOKEN"


def normalize_bearer_token(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip()
    return cleaned or None


class ServiceTokenVerifier(TokenVerifier):
    """Constant-time verifier for a dedicated non-expiring service credential."""

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

    async def verify_token(self, token: str) -> AccessToken | None:
        provided = normalize_bearer_token(token)
        if not provided or not secrets.compare_digest(provided, self._expected):
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


class CompositeAuthProvider(AuthProvider):
    """GitHub OAuth routes/discovery plus optional service-token verification."""

    def __init__(
        self,
        oauth: AuthProvider,
        *extra_verifiers: TokenVerifier,
    ) -> None:
        base_url = getattr(oauth, "base_url", None)
        required_scopes = list(getattr(oauth, "required_scopes", None) or [])
        super().__init__(base_url=base_url, required_scopes=required_scopes)
        self._oauth = oauth
        self._extra_verifiers = extra_verifiers

    async def verify_token(self, token: str) -> AccessToken | None:
        for verifier in self._extra_verifiers:
            result = await verifier.verify_token(token)
            if result is not None:
                return result
        return await self._oauth.verify_token(token)

    def set_mcp_path(self, mcp_path: str | None) -> None:
        super().set_mcp_path(mcp_path)
        setter = getattr(self._oauth, "set_mcp_path", None)
        if callable(setter):
            setter(mcp_path)

    def get_routes(self, mcp_path: str | None = None) -> list[Any]:
        return self._oauth.get_routes(mcp_path=mcp_path)

    def get_well_known_routes(self, mcp_path: str | None = None) -> list[Any]:
        getter = getattr(self._oauth, "get_well_known_routes", None)
        if callable(getter):
            return getter(mcp_path=mcp_path)
        return super().get_well_known_routes(mcp_path=mcp_path)


def is_service_access_token(token: AccessToken | None) -> bool:
    if token is None:
        return False
    claims = token.claims or {}
    return (
        claims.get("token_use") == "service"
        or claims.get("client_id") == SERVICE_CLIENT_ID
        or token.client_id == SERVICE_CLIENT_ID
    )


def build_bridge_auth_provider(
    oauth: AuthProvider,
    *,
    service_token: str | None,
) -> AuthProvider:
    """Compose GitHub OAuth with an optional dedicated service credential."""
    cleaned = normalize_bearer_token(service_token)
    if not cleaned:
        return oauth
    return CompositeAuthProvider(oauth, ServiceTokenVerifier(cleaned))
