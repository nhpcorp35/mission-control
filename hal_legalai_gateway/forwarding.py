"""Thin MCP tool forwarding via FastMCP Client (no business logic)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from hal_legalai_gateway.auth import (
    redact_secrets,
    service_authorization_header,
)
from hal_legalai_gateway.request_context import get_correlation_id, get_request_id

logger = logging.getLogger(__name__)

STAGE_OK = None
STAGE_UNCONFIGURED = "unconfigured"
STAGE_AUTH = "auth"
STAGE_CONNECT = "connect"
STAGE_TIMEOUT = "timeout"
STAGE_HTTP = "http"
STAGE_PROTOCOL = "protocol"
STAGE_TOOL = "tool"
STAGE_PARSE = "parse"
STAGE_INTERNAL = "internal"


@dataclass(frozen=True)
class ToolBinding:
    """Stable gateway tool → downstream MCP tool mapping."""

    gateway_tool: str
    namespace: str
    downstream_service: str
    downstream_tool: str
    description: str = ""
    notes: str = ""


@dataclass(frozen=True)
class ForwardResult:
    """Structured forward outcome (safe for client return / logs)."""

    ok: bool
    gateway_tool: str
    downstream_service: str
    downstream_tool: str
    request_id: str | None
    correlation_id: str | None
    duration_ms: float
    failure_stage: str | None
    result: Any = None
    error: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "gateway_tool": self.gateway_tool,
            "downstream_service": self.downstream_service,
            "downstream_tool": self.downstream_tool,
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "duration_ms": self.duration_ms,
            "failure_stage": self.failure_stage,
        }
        if self.ok:
            payload["result"] = self.result
        else:
            payload["error"] = self.error or {
                "message": "downstream call failed",
                "stage": self.failure_stage,
            }
        return payload


def _classify_transport_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return STAGE_TIMEOUT
    if isinstance(exc, httpx.ConnectError):
        return STAGE_CONNECT
    if isinstance(exc, httpx.HTTPStatusError):
        return STAGE_HTTP
    if isinstance(exc, (httpx.NetworkError, httpx.TransportError)):
        return STAGE_CONNECT
    name = exc.__class__.__name__
    if "ToolError" in name or "tool" in str(exc).lower():
        return STAGE_TOOL
    if "McpError" in name or "JSONRPC" in name:
        return STAGE_PROTOCOL
    return STAGE_INTERNAL


def _safe_error_message(
    exc: BaseException,
    *,
    extra_secrets: tuple[str, ...] = (),
) -> str:
    """Stringify an error without echoing Authorization / secret material."""
    text = str(exc) or exc.__class__.__name__
    redacted = redact_secrets(text, extra_secrets=extra_secrets)
    lowered = redacted.lower()
    for needle in (
        "bearer ",
        "authorization",
        "api_key",
        "api-key",
        "client_secret",
        "jwt_signing",
        "storage_encryption",
    ):
        if needle in lowered and "[redacted]" not in lowered:
            return exc.__class__.__name__
    return redacted[:500]


def mcp_endpoint_url(base_url: str, mcp_path: str = "/mcp/service") -> str:
    """Join downstream base URL with the Streamable HTTP MCP path."""
    root = base_url.rstrip("/")
    path = mcp_path if mcp_path.startswith("/") else f"/{mcp_path}"
    if root.endswith(path):
        return root
    return f"{root}{path}"


def _httpx_factory(
    *,
    connect_timeout: float,
    read_timeout: float,
) -> Callable[..., httpx.AsyncClient]:
    timeout = httpx.Timeout(
        connect=connect_timeout,
        read=read_timeout,
        write=read_timeout,
        pool=connect_timeout,
    )

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("timeout", timeout)
        return httpx.AsyncClient(**kwargs)

    return factory


async def forward_mcp_tool(
    *,
    binding: ToolBinding,
    arguments: dict[str, Any],
    base_url: str | None,
    authorization: str | None,
    connect_timeout_seconds: float,
    read_timeout_seconds: float,
    mcp_path: str = "/mcp/service",
    require_authorization: bool = True,
    client_factory: Callable[[], Client] | None = None,
    extra_secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Call one downstream MCP tool and return a structured envelope.

    Never raises across the isolation boundary — failures become ``ok=false``
    with an exact ``failure_stage``. Credentials and private payloads are not
    copied into error strings.
    """
    started = time.perf_counter()
    request_id = get_request_id()
    correlation_id = get_correlation_id()

    def _finish(
        *,
        ok: bool,
        failure_stage: str | None,
        result: Any = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
        return ForwardResult(
            ok=ok,
            gateway_tool=binding.gateway_tool,
            downstream_service=binding.downstream_service,
            downstream_tool=binding.downstream_tool,
            request_id=request_id,
            correlation_id=correlation_id,
            duration_ms=duration_ms,
            failure_stage=failure_stage,
            result=result,
            error=error,
        ).as_dict()

    if not base_url:
        return _finish(
            ok=False,
            failure_stage=STAGE_UNCONFIGURED,
            error={
                "message": (
                    f"downstream '{binding.downstream_service}' base URL "
                    "is not configured"
                ),
                "stage": STAGE_UNCONFIGURED,
            },
        )

    if require_authorization and not authorization:
        return _finish(
            ok=False,
            failure_stage=STAGE_AUTH,
            error={
                "message": (
                    "downstream authorization missing: configure "
                    "GATEWAY_BRIDGE_AUTHORIZATION service credential"
                ),
                "stage": STAGE_AUTH,
            },
        )

    headers: dict[str, str] = {
        "Accept": "application/json, text/event-stream",
    }
    if authorization:
        headers["Authorization"] = authorization
    if request_id:
        headers["X-Request-ID"] = request_id
    if correlation_id:
        headers["X-Correlation-ID"] = correlation_id

    url = mcp_endpoint_url(base_url, mcp_path)

    try:
        if client_factory is not None:
            client = client_factory()
        else:
            transport = StreamableHttpTransport(
                url,
                headers=headers,
                httpx_client_factory=_httpx_factory(
                    connect_timeout=connect_timeout_seconds,
                    read_timeout=read_timeout_seconds,
                ),
            )
            client = Client(transport, timeout=read_timeout_seconds)

        async with client:
            raw = await client.call_tool(
                binding.downstream_tool,
                arguments,
                raise_on_error=False,
            )
            if getattr(raw, "is_error", False):
                logger.warning(
                    "downstream tool error gateway_tool=%s downstream=%s tool=%s",
                    binding.gateway_tool,
                    binding.downstream_service,
                    binding.downstream_tool,
                )
                return _finish(
                    ok=False,
                    failure_stage=STAGE_TOOL,
                    error={
                        "message": "downstream tool returned an error",
                        "stage": STAGE_TOOL,
                    },
                )
            data = getattr(raw, "data", None)
            if data is None:
                structured = getattr(raw, "structured_content", None)
                data = structured if structured is not None else getattr(raw, "content", None)
            logger.info(
                "forwarded gateway_tool=%s downstream=%s tool=%s ok=true",
                binding.gateway_tool,
                binding.downstream_service,
                binding.downstream_tool,
            )
            return _finish(ok=True, failure_stage=STAGE_OK, result=data)
    except Exception as exc:  # noqa: BLE001 — isolation boundary
        stage = _classify_transport_error(exc)
        safe = _safe_error_message(exc, extra_secrets=extra_secrets)
        logger.warning(
            "forward failed gateway_tool=%s downstream=%s tool=%s stage=%s error=%s",
            binding.gateway_tool,
            binding.downstream_service,
            binding.downstream_tool,
            stage,
            safe,
        )
        return _finish(
            ok=False,
            failure_stage=stage,
            error={
                "message": safe,
                "stage": stage,
            },
        )


def resolve_authorization_for_service(
    *,
    downstream_service: str,
    bridge_authorization: str | None,
) -> str | None:
    """Bridge/storage/artifacts use the dedicated service credential only.

    Mission Control MCP is gateway-gated; its HTTP API key stays server-side.
    Inbound GitHub OAuth session tokens are never forwarded.
    """
    if downstream_service in {"bridge", "storage", "artifacts"}:
        return service_authorization_header(bridge_authorization)
    return None
