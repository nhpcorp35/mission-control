"""Gateway configuration: env resolution, timeouts, and fail-closed validation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

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

GATEWAY_API_KEY_ENV = "GATEWAY_API_KEY"
BRIDGE_AUTHORIZATION_ENV = "GATEWAY_BRIDGE_AUTHORIZATION"
MCP_PATH_ENV = "GATEWAY_MCP_PATH"
DEFAULT_MCP_PATH = "/mcp"


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
    gateway_api_key: str
    bridge_authorization: str
    mcp_path: str

    def downstream_by_key(self, key: str) -> ResolvedDownstream:
        for item in self.downstreams:
            if item.key == key:
                return item
        raise KeyError(key)


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


def _parse_mcp_path(raw: str | None) -> str:
    value = (raw or "").strip() or DEFAULT_MCP_PATH
    if not value.startswith("/"):
        raise RuntimeError(f"{MCP_PATH_ENV} must start with '/'")
    if "?" in value or "#" in value:
        raise RuntimeError(f"{MCP_PATH_ENV} must not include query or fragment")
    return value.rstrip("/") or "/"


def load_settings(
    *,
    environ: dict[str, str] | None = None,
    registry: GatewayRegistry | None = None,
) -> GatewaySettings:
    """Load registry + env, validate, and resolve downstream URLs.

    Fail closed when inbound API key or Bridge service authorization is missing.
    Bridge GitHub OAuth is not compatible with GATEWAY_API_KEY, so explicit
    GATEWAY_BRIDGE_AUTHORIZATION (or per-call X-Downstream-Authorization) is
    required for case/storage/artifacts forwarding.
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

    gateway_api_key = _require_secret(env, GATEWAY_API_KEY_ENV)
    bridge_authorization = _require_secret(env, BRIDGE_AUTHORIZATION_ENV)
    mcp_path = _parse_mcp_path(env.get(MCP_PATH_ENV))

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
        gateway_api_key=gateway_api_key,
        bridge_authorization=bridge_authorization,
        mcp_path=mcp_path,
    )
