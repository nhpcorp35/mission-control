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
    deployed_commit_sha: str

    def downstream_by_key(self, key: str) -> ResolvedDownstream:
        for item in self.downstreams:
            if item.key == key:
                return item
        raise KeyError(key)


def get_deployed_commit_sha() -> str:
    """Return the explicit deployment commit SHA, or a safe unknown fallback."""
    value = (os.environ.get(DEPLOYED_COMMIT_SHA_ENV) or "").strip()
    return value if value else UNKNOWN_DEPLOYED_COMMIT_SHA


def _parse_timeout(raw: str | None) -> float:
    if raw is None or not str(raw).strip():
        return DEFAULT_HEALTH_TIMEOUT_SECONDS
    try:
        value = float(str(raw).strip())
    except ValueError as exc:
        raise RuntimeError(
            f"{HEALTH_TIMEOUT_ENV} must be numeric"
        ) from exc
    if value < MIN_HEALTH_TIMEOUT_SECONDS:
        raise RuntimeError(
            f"{HEALTH_TIMEOUT_ENV} must be >= {MIN_HEALTH_TIMEOUT_SECONDS}"
        )
    if value > MAX_HEALTH_TIMEOUT_SECONDS:
        raise RuntimeError(
            f"{HEALTH_TIMEOUT_ENV} must be <= {MAX_HEALTH_TIMEOUT_SECONDS}"
        )
    return value


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


def load_settings(
    *,
    environ: dict[str, str] | None = None,
    registry: GatewayRegistry | None = None,
) -> GatewaySettings:
    """Load registry + env, validate, and resolve downstream URLs."""
    env = environ if environ is not None else dict(os.environ)
    loaded_registry = registry or load_registry()
    timeout = _parse_timeout(env.get(HEALTH_TIMEOUT_ENV))

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

    return GatewaySettings(
        registry=loaded_registry,
        downstreams=tuple(downstreams),
        health_timeout_seconds=timeout,
        deployed_commit_sha=get_deployed_commit_sha()
        if environ is None
        else (
            (env.get(DEPLOYED_COMMIT_SHA_ENV) or "").strip()
            or UNKNOWN_DEPLOYED_COMMIT_SHA
        ),
    )
