"""Independent downstream health probes with failure-stage isolation."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from hal_legalai_gateway.config import GatewaySettings, ResolvedDownstream
from hal_legalai_gateway.request_context import get_correlation_id, get_request_id

logger = logging.getLogger(__name__)

STATUS_HEALTHY = "healthy"
STATUS_UNHEALTHY = "unhealthy"
STATUS_UNCONFIGURED = "unconfigured"

STAGE_OK = None
STAGE_UNCONFIGURED = "unconfigured"
STAGE_CONNECT = "connect"
STAGE_TIMEOUT = "timeout"
STAGE_HTTP = "http"
STAGE_PARSE = "parse"
STAGE_INTERNAL = "internal"


def _classify_transport_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return STAGE_TIMEOUT
    if isinstance(exc, httpx.ConnectError):
        return STAGE_CONNECT
    if isinstance(exc, (httpx.NetworkError, httpx.TransportError)):
        return STAGE_CONNECT
    return STAGE_INTERNAL


async def probe_downstream(
    downstream: ResolvedDownstream,
    *,
    timeout_seconds: float,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Probe one downstream. Never raises — failures become structured status."""
    started = time.perf_counter()
    base: dict[str, Any] = {
        "key": downstream.key,
        "service_id": downstream.service_id,
        "display_name": downstream.display_name,
        "base_url": downstream.base_url,
        "health_url": downstream.health_url,
        "base_url_env": downstream.base_url_env,
        "status": STATUS_UNHEALTHY,
        "latency_ms": None,
        "failure_stage": STAGE_INTERNAL,
        "http_status": None,
        "error": None,
    }

    if not downstream.configured or not downstream.health_url:
        base.update(
            {
                "status": STATUS_UNCONFIGURED,
                "failure_stage": STAGE_UNCONFIGURED,
                "latency_ms": 0.0,
                "error": (
                    f"{downstream.base_url_env} is not configured and no "
                    "registry default_base_url is available"
                ),
            }
        )
        return base

    owns_client = client is None
    http_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds)
    )
    headers = {
        "Accept": "application/json",
    }
    request_id = get_request_id()
    correlation_id = get_correlation_id()
    if request_id:
        headers["X-Request-ID"] = request_id
    if correlation_id:
        headers["X-Correlation-ID"] = correlation_id

    try:
        response = await http_client.get(downstream.health_url, headers=headers)
        latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
        base["latency_ms"] = latency_ms
        base["http_status"] = response.status_code
        if response.status_code >= 400:
            base.update(
                {
                    "status": STATUS_UNHEALTHY,
                    "failure_stage": STAGE_HTTP,
                    "error": f"health endpoint returned HTTP {response.status_code}",
                }
            )
            return base
        # Prefer JSON but accept any 2xx as healthy for heterogeneous services.
        content_type = (response.headers.get("content-type") or "").lower()
        body: Any = None
        if "application/json" in content_type:
            try:
                body = response.json()
            except ValueError:
                base.update(
                    {
                        "status": STATUS_UNHEALTHY,
                        "failure_stage": STAGE_PARSE,
                        "error": "health endpoint returned invalid JSON",
                    }
                )
                return base
        base.update(
            {
                "status": STATUS_HEALTHY,
                "failure_stage": STAGE_OK,
                "error": None,
                "body_preview": _body_preview(body),
            }
        )
        return base
    except Exception as exc:  # noqa: BLE001 — isolation boundary
        latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
        stage = _classify_transport_error(exc)
        logger.warning(
            "downstream health probe failed key=%s stage=%s error=%s",
            downstream.key,
            stage,
            exc,
        )
        base.update(
            {
                "status": STATUS_UNHEALTHY,
                "failure_stage": stage,
                "latency_ms": latency_ms,
                "error": str(exc) or exc.__class__.__name__,
            }
        )
        return base
    finally:
        if owns_client:
            await http_client.aclose()


def _body_preview(body: Any) -> Any:
    if body is None:
        return None
    if isinstance(body, dict):
        # Keep preview small and non-sensitive.
        preview = {}
        for key in ("status", "ok", "service", "deployed_commit_sha"):
            if key in body:
                preview[key] = body[key]
        return preview or {"keys": sorted(str(k) for k in list(body)[:8])}
    return {"type": type(body).__name__}


async def aggregate_health(
    settings: GatewaySettings,
    *,
    registered_tools: list[str] | None = None,
) -> dict[str, Any]:
    """Check every configured downstream independently.

    One failure never prevents other probes from completing. Namespace
    capability availability is derived only from that namespace's downstream.
    """
    timeout = settings.health_timeout_seconds

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        results = await asyncio.gather(
            *[
                probe_downstream(
                    downstream, timeout_seconds=timeout, client=client
                )
                for downstream in settings.downstreams
            ]
        )

    by_key = {item["key"]: item for item in results}
    capabilities: dict[str, Any] = {}
    for name, namespace in sorted(settings.registry.namespaces.items()):
        downstream_key = namespace.downstream_service
        probe = by_key.get(downstream_key)
        available = bool(probe and probe.get("status") == STATUS_HEALTHY)
        capabilities[name] = {
            "available": available,
            "downstream_service": downstream_key,
            "tools": list(namespace.tools),
            "reason": None
            if available
            else (
                None
                if probe is None
                else probe.get("failure_stage") or probe.get("status")
            ),
        }

    # Artifact-routed tools remain available only when artifacts downstream is healthy.
    artifacts_probe = by_key.get("artifacts")
    artifacts_available = bool(
        artifacts_probe and artifacts_probe.get("status") == STATUS_HEALTHY
    )
    for route in settings.registry.tool_routes:
        if route.downstream_service != "artifacts":
            continue
        capabilities.setdefault(
            "artifacts",
            {
                "available": artifacts_available,
                "downstream_service": "artifacts",
                "tools": [],
                "reason": None
                if artifacts_available
                else (
                    None
                    if artifacts_probe is None
                    else artifacts_probe.get("failure_stage")
                    or artifacts_probe.get("status")
                ),
            },
        )
        capabilities["artifacts"]["tools"] = sorted(
            set(capabilities["artifacts"]["tools"]) | {route.tool}
        )

    unhealthy = [
        item
        for item in results
        if item["status"] != STATUS_HEALTHY
    ]
    gateway_status = "ok" if not unhealthy else "degraded"

    tools = list(registered_tools) if registered_tools is not None else []
    return {
        "ok": True,
        "status": gateway_status,
        "service": "hal-legalai-gateway",
        "phase": 2,
        "deployed_commit_sha": settings.deployed_commit_sha,
        "registered_tools": tools,
        "request_id": get_request_id(),
        "correlation_id": get_correlation_id(),
        "health_timeout_seconds": timeout,
        "connect_timeout_seconds": settings.connect_timeout_seconds,
        "read_timeout_seconds": settings.read_timeout_seconds,
        "downstream": {item["key"]: item for item in results},
        "capabilities": capabilities,
    }
