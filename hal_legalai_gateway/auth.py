"""Gateway authentication: inbound GitHub OAuth + downstream service credentials.

ChatGPT Business custom MCP expects the same FastMCP ``GitHubProvider`` OAuth
pattern used by the Bridge. A static inbound API key is not suitable.

Gateway → Bridge/Storage/Artifacts uses a dedicated non-expiring service
credential (``GATEWAY_BRIDGE_AUTHORIZATION``). The inbound GitHub OAuth session
token is never forwarded downstream: it is a different audience and expires with
the user session.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from cryptography.fernet import Fernet
from fastmcp.server.auth import AccessToken, AuthProvider, TokenVerifier
from fastmcp.server.auth.providers.github import GitHubProvider
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

GATEWAY_PUBLIC_URL_ENV = "GATEWAY_PUBLIC_URL"
GITHUB_OAUTH_CLIENT_ID_ENV = "GITHUB_OAUTH_CLIENT_ID"
GITHUB_OAUTH_CLIENT_SECRET_ENV = "GITHUB_OAUTH_CLIENT_SECRET"
JWT_SIGNING_KEY_ENV = "JWT_SIGNING_KEY"
REDIS_HOST_ENV = "REDIS_HOST"
REDIS_PORT_ENV = "REDIS_PORT"
STORAGE_ENCRYPTION_KEY_ENV = "STORAGE_ENCRYPTION_KEY"
BRIDGE_AUTHORIZATION_ENV = "GATEWAY_BRIDGE_AUTHORIZATION"
ALLOWED_GITHUB_LOGIN_ENV = "ALLOWED_GITHUB_LOGIN"
DEFAULT_ALLOWED_GITHUB_LOGIN = "nhpcorp35"
SERVICE_CLIENT_ID = "hal-gateway-service"

_SECRET_PATTERN = re.compile(
    r"(?i)(bearer\s+)(\S+)|(authorization[\"']?\s*[:=]\s*[\"']?)([^\s\"']+)"
)


def normalize_bearer_token(value: str | None) -> str | None:
    """Return the raw token, stripping an optional ``Bearer `` prefix."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip()
    return cleaned or None


def format_authorization_header(value: str) -> str:
    """Ensure a value is an ``Authorization`` Bearer header."""
    cleaned = value.strip()
    if cleaned.lower().startswith("bearer "):
        return cleaned
    return f"Bearer {cleaned}"


def tokens_match(provided: str, expected: str) -> bool:
    """Constant-time token comparison."""
    if not provided or not expected:
        return False
    return secrets.compare_digest(provided, expected)


def redact_secrets(text: str, *, extra_secrets: tuple[str, ...] = ()) -> str:
    """Remove bearer tokens and known secrets from log/error strings."""
    if not text:
        return text
    redacted = _SECRET_PATTERN.sub(
        lambda m: (m.group(1) or m.group(3) or "") + "[REDACTED]",
        text,
    )
    for secret in extra_secrets:
        raw = normalize_bearer_token(secret) or ""
        if raw and raw in redacted:
            redacted = redacted.replace(raw, "[REDACTED]")
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


class ServiceTokenVerifier(TokenVerifier):
    """Verify a dedicated non-expiring service-to-service Bearer credential.

    Unlike user GitHub OAuth tokens, this credential is provisioned as a Railway
    secret and does not expire with a user session.
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

    async def verify_token(self, token: str) -> AccessToken | None:
        provided = normalize_bearer_token(token)
        if provided is None or not tokens_match(provided, self._expected):
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
    """Supported composite: OAuth discovery/routes + extra token verifiers.

    Used on the Bridge so direct GitHub OAuth keeps working while the Gateway
    authenticates with a dedicated service credential.
    """

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

    def get_routes(self, mcp_path: str | None = None) -> list:
        return self._oauth.get_routes(mcp_path=mcp_path)

    def get_well_known_routes(self, mcp_path: str | None = None) -> list:
        getter = getattr(self._oauth, "get_well_known_routes", None)
        if callable(getter):
            return getter(mcp_path=mcp_path)
        return super().get_well_known_routes(mcp_path=mcp_path)


class FixedTokenAuthProvider(AuthProvider):
    """Test/helper auth that accepts one pre-issued Bearer token.

    Production inbound auth uses ``GitHubProvider``; this exists so unit tests
    can exercise authenticated MCP calls without a live GitHub OAuth dance.
    """

    def __init__(
        self,
        token: str,
        *,
        client_id: str = "test-gateway-client",
        claims: dict[str, Any] | None = None,
        scopes: list[str] | None = None,
    ) -> None:
        super().__init__()
        expected = normalize_bearer_token(token)
        if not expected:
            raise ValueError("fixed token must be non-empty")
        self._expected = expected
        self._client_id = client_id
        self._claims = dict(claims or {})
        self._scopes = list(scopes or [])

    async def verify_token(self, token: str) -> AccessToken | None:
        provided = normalize_bearer_token(token)
        if provided is None or not tokens_match(provided, self._expected):
            return None
        return AccessToken(
            token=provided,
            client_id=self._client_id,
            scopes=list(self._scopes),
            expires_at=None,
            claims=dict(self._claims),
        )


def build_github_oauth_provider(
    *,
    client_id: str,
    client_secret: str,
    public_url: str,
    jwt_signing_key: str,
    redis_host: str,
    redis_port: int,
    storage_encryption_key: str,
) -> GitHubProvider:
    """Build the same FastMCP GitHub OAuth provider pattern as the Bridge."""
    return GitHubProvider(
        client_id=client_id,
        client_secret=client_secret,
        base_url=public_url.rstrip("/"),
        jwt_signing_key=jwt_signing_key,
        client_storage=FernetEncryptionWrapper(
            key_value=RedisStore(
                host=redis_host,
                port=redis_port,
            ),
            fernet=Fernet(storage_encryption_key.encode()),
        ),
    )


def service_authorization_header(service_authorization: str | None) -> str | None:
    """Format the dedicated gateway→bridge service credential for MCP calls.

    Never accepts or returns an inbound user OAuth session token.
    """
    if not service_authorization:
        return None
    cleaned = service_authorization.strip()
    if not cleaned:
        return None
    return format_authorization_header(cleaned)


def is_service_access_token(token: AccessToken | None) -> bool:
    if token is None:
        return False
    claims = token.claims or {}
    return (
        claims.get("token_use") == "service"
        or claims.get("client_id") == SERVICE_CLIENT_ID
        or token.client_id == SERVICE_CLIENT_ID
    )
