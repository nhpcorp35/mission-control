"""Gateway configuration: env resolution, timeouts, and fail-closed validation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from hal_legalai_gateway.auth import (
    ALLOWED_GITHUB_LOGIN_ENV,
    BRIDGE_AUTHORIZATION_ENV,
    DEFAULT_ALLOWED_GITHUB_LOGIN,
    GATEWAY_PUBLIC_URL_ENV,
    GITHUB_OAUTH_CLIENT_ID_ENV,
    GITHUB_OAUTH_CLIENT_SECRET_ENV,
    JWT_SIGNING_KEY_ENV,
    REDIS_HOST_ENV,
    REDIS_PORT_ENV,
    STORAGE_ENCRYPTION_KEY_ENV,
    normalize_bearer_token,
)
from hal_legalai_gateway.registry import GatewayRegistry, load_registry

DEPLOYED_COMMIT_SHA_ENV = "RAILWAY_GIT_COMMIT_SHA"
UNKNOWN_DEPLOYED_COMMIT_SHA = "unknown"

DEFAULT_HEALTH_TIMEOUT_SECONDS = 5.0
MIN_HEALTH_TIMEOUT_SECONDS = 0.1
MAX_HEALTH_TIMEOUT_SECONDS = 30.0
HEALTH_TIMEOUT_ENV = "GATEWAY_HEALTH_TIMEOUT_SECONDS"

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 30.0
MIN_IO_TIMEOUT_SECONDS = 0.1
MAX_CONNECT_TIMEOUT_SECONDS = 30.0
MAX_READ_TIMEOUT_SECONDS = 120.0
CONNECT_TIMEOUT_ENV = "GATEWAY_CONNECT_TIMEOUT_SECONDS"
READ_TIMEOUT_ENV = "GATEWAY_READ_TIMEOUT_SECONDS"

MCP_PATH_ENV = "GATEWAY_MCP_PATH"
# Bridge/Storage/Artifacts use the service-only MCP path (TokenVerifier bearer).
DEFAULT_MCP_PATH = "/mcp/service"
# Mission Control MCP remains on the public Streamable HTTP path.
DEFAULT_MISSION_CONTROL_MCP_PATH = "/mcp"
MISSION_CONTROL_MCP_PATH_ENV = "GATEWAY_MISSION_CONTROL_MCP_PATH"
DEFAULT_REDIS_PORT = 6379


@dataclass(frozen=True)
class ResolvedDownstream:
    """Runtime URL binding for one registry downstream service."""

    key: str
    service_id: str
    display_name: str
    base_url: str | None
    health_url: str | None
    base_url_env: str
    configured: bool


@dataclass(frozen=True)
class GatewaySettings:
    """Validated gateway runtime settings."""

    registry: GatewayRegistry
    downstreams: tuple[ResolvedDownstream, ...]
    health_timeout_seconds: float
    connect_timeout_seconds: float
    read_timeout_seconds: float
    deployed_commit_sha: str
    github_oauth_client_id: str
    github_oauth_client_secret: str
    gateway_public_url: str
    jwt_signing_key: str
    redis_host: str
    redis_port: int
    storage_encryption_key: str
    bridge_authorization: str
    allowed_github_login: str
    mcp_path: str
    mission_control_mcp_path: str

    def downstream_by_key(self, key: str) -> ResolvedDownstream:
        for item in self.downstreams:
            if item.key == key:
                return item
        raise KeyError(key)

    def mcp_path_for_service(self, downstream_service: str) -> str:
        """Return the Streamable HTTP path for a downstream service.

        Bridge / Storage / Artifacts use the service-only TokenVerifier path.
        Mission Control keeps its public ``/mcp`` path.
        """
        if downstream_service in {"bridge", "storage", "artifacts"}:
            return self.mcp_path
        return self.mission_control_mcp_path

    def secret_values_for_redaction(self) -> tuple[str, ...]:
        """Return configured secrets that must never appear in logs/errors/health."""
        return tuple(
            value
            for value in (
                self.github_oauth_client_secret,
                self.jwt_signing_key,
                self.storage_encryption_key,
                self.bridge_authorization,
                normalize_bearer_token(self.bridge_authorization) or "",
            )
            if value
        )


def get_deployed_commit_sha() -> str:
    """Return the explicit deployment commit SHA, or a safe unknown fallback."""
    value = (os.environ.get(DEPLOYED_COMMIT_SHA_ENV) or "").strip()
    return value if value else UNKNOWN_DEPLOYED_COMMIT_SHA


def _parse_bounded_timeout(
    raw: str | None,
    *,
    env_name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except ValueError as exc:
        raise RuntimeError(f"{env_name} must be numeric") from exc
    if value < minimum:
        raise RuntimeError(f"{env_name} must be >= {minimum}")
    if value > maximum:
        raise RuntimeError(f"{env_name} must be <= {maximum}")
    return value


def _parse_timeout(raw: str | None) -> float:
    return _parse_bounded_timeout(
        raw,
        env_name=HEALTH_TIMEOUT_ENV,
        default=DEFAULT_HEALTH_TIMEOUT_SECONDS,
        minimum=MIN_HEALTH_TIMEOUT_SECONDS,
        maximum=MAX_HEALTH_TIMEOUT_SECONDS,
    )


def validate_http_base_url(url: str, *, env_name: str) -> str:
    """Require an absolute http(s) URL without a trailing slash."""
    cleaned = url.strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError(
            f"{env_name} must be an absolute http(s) URL"
        )
    if not parsed.netloc:
        raise RuntimeError(
            f"{env_name} must include a host"
        )
    if parsed.params or parsed.query or parsed.fragment:
        raise RuntimeError(
            f"{env_name} must not include params, query, or fragment"
        )
    return cleaned


def resolve_downstream_base_url(
    *,
    env_name: str,
    default_base_url: str | None,
    environ: dict[str, str] | None = None,
) -> str | None:
    """Resolve a downstream base URL from env, then registry default."""
    env = environ if environ is not None else os.environ
    raw = (env.get(env_name) or "").strip()
    if raw:
        return validate_http_base_url(raw, env_name=env_name)
    if default_base_url:
        return validate_http_base_url(
            default_base_url, env_name=f"default:{env_name}"
        )
    return None


def build_health_url(base_url: str | None, health_path: str) -> str | None:
    if not base_url:
        return None
    path = health_path if health_path.startswith("/") else f"/{health_path}"
    return f"{base_url.rstrip('/')}{path}"


def _require_secret(env: dict[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is required (fail-closed): set the Railway variable before start"
        )
    return value


def _parse_mcp_path(raw: str | None, *, env_name: str, default: str) -> str:
    value = (raw or "").strip() or default
    if not value.startswith("/"):
        raise RuntimeError(f"{env_name} must start with '/'")
    if "?" in value or "#" in value:
        raise RuntimeError(f"{env_name} must not include query or fragment")
    return value.rstrip("/") or "/"


def _parse_redis_port(raw: str | None) -> int:
    if raw is None or not str(raw).strip():
        return DEFAULT_REDIS_PORT
    try:
        port = int(str(raw).strip())
    except ValueError as exc:
        raise RuntimeError(f"{REDIS_PORT_ENV} must be an integer") from exc
    if port < 1 or port > 65535:
        raise RuntimeError(f"{REDIS_PORT_ENV} must be between 1 and 65535")
    return port


def load_settings(
    *,
    environ: dict[str, str] | None = None,
    registry: GatewayRegistry | None = None,
) -> GatewaySettings:
    """Load registry + env, validate, and resolve downstream URLs.

    Fail closed when inbound GitHub OAuth configuration or the dedicated
    Bridge service authorization is missing. ``GATEWAY_BRIDGE_AUTHORIZATION``
    must be a non-expiring service credential that matches Bridge
    ``BRIDGE_SERVICE_TOKEN`` — never a copied user OAuth bearer.
    """
    env = environ if environ is not None else dict(os.environ)
    loaded_registry = registry or load_registry()
    timeout = _parse_timeout(env.get(HEALTH_TIMEOUT_ENV))
    connect_timeout = _parse_bounded_timeout(
        env.get(CONNECT_TIMEOUT_ENV),
        env_name=CONNECT_TIMEOUT_ENV,
        default=DEFAULT_CONNECT_TIMEOUT_SECONDS,
        minimum=MIN_IO_TIMEOUT_SECONDS,
        maximum=MAX_CONNECT_TIMEOUT_SECONDS,
    )
    read_timeout = _parse_bounded_timeout(
        env.get(READ_TIMEOUT_ENV),
        env_name=READ_TIMEOUT_ENV,
        default=DEFAULT_READ_TIMEOUT_SECONDS,
        minimum=MIN_IO_TIMEOUT_SECONDS,
        maximum=MAX_READ_TIMEOUT_SECONDS,
    )
    if read_timeout < connect_timeout:
        raise RuntimeError(
            f"{READ_TIMEOUT_ENV} must be >= {CONNECT_TIMEOUT_ENV}"
        )

    github_oauth_client_id = _require_secret(env, GITHUB_OAUTH_CLIENT_ID_ENV)
    github_oauth_client_secret = _require_secret(
        env, GITHUB_OAUTH_CLIENT_SECRET_ENV
    )
    gateway_public_url = validate_http_base_url(
        _require_secret(env, GATEWAY_PUBLIC_URL_ENV),
        env_name=GATEWAY_PUBLIC_URL_ENV,
    )
    jwt_signing_key = _require_secret(env, JWT_SIGNING_KEY_ENV)
    redis_host = _require_secret(env, REDIS_HOST_ENV)
    redis_port = _parse_redis_port(env.get(REDIS_PORT_ENV))
    storage_encryption_key = _require_secret(env, STORAGE_ENCRYPTION_KEY_ENV)
    bridge_authorization = _require_secret(env, BRIDGE_AUTHORIZATION_ENV)
    if normalize_bearer_token(bridge_authorization) is None:
        raise RuntimeError(
            f"{BRIDGE_AUTHORIZATION_ENV} is required (fail-closed): "
            "set a dedicated non-expiring service credential"
        )
    allowed_github_login = (
        (env.get(ALLOWED_GITHUB_LOGIN_ENV) or "").strip()
        or DEFAULT_ALLOWED_GITHUB_LOGIN
    )
    mcp_path = _parse_mcp_path(
        env.get(MCP_PATH_ENV),
        env_name=MCP_PATH_ENV,
        default=DEFAULT_MCP_PATH,
    )
    mission_control_mcp_path = _parse_mcp_path(
        env.get(MISSION_CONTROL_MCP_PATH_ENV),
        env_name=MISSION_CONTROL_MCP_PATH_ENV,
        default=DEFAULT_MISSION_CONTROL_MCP_PATH,
    )

    downstreams: list[ResolvedDownstream] = []
    for key in loaded_registry.service_keys():
        service = loaded_registry.services[key]
        base_url = resolve_downstream_base_url(
            env_name=service.base_url_env,
            default_base_url=service.default_base_url,
            environ=env,
        )
        downstreams.append(
            ResolvedDownstream(
                key=key,
                service_id=service.service_id,
                display_name=service.display_name,
                base_url=base_url,
                health_url=build_health_url(base_url, service.health_path),
                base_url_env=service.base_url_env,
                configured=base_url is not None,
            )
        )

    deployed = (
        get_deployed_commit_sha()
        if environ is None
        else (
            (env.get(DEPLOYED_COMMIT_SHA_ENV) or "").strip()
            or UNKNOWN_DEPLOYED_COMMIT_SHA
        )
    )

    return GatewaySettings(
        registry=loaded_registry,
        downstreams=tuple(downstreams),
        health_timeout_seconds=timeout,
        connect_timeout_seconds=connect_timeout,
        read_timeout_seconds=read_timeout,
        deployed_commit_sha=deployed,
        github_oauth_client_id=github_oauth_client_id,
        github_oauth_client_secret=github_oauth_client_secret,
        gateway_public_url=gateway_public_url,
        jwt_signing_key=jwt_signing_key,
        redis_host=redis_host,
        redis_port=redis_port,
        storage_encryption_key=storage_encryption_key,
        bridge_authorization=bridge_authorization,
        allowed_github_login=allowed_github_login,
        mcp_path=mcp_path,
        mission_control_mcp_path=mission_control_mcp_path,
    )
