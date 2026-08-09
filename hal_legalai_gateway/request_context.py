"""Correlation / request ID foundation for gateway request handling and logs."""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

REQUEST_ID_HEADER = "X-Request-ID"
CORRELATION_ID_HEADER = "X-Correlation-ID"

_request_id: ContextVar[str | None] = ContextVar("gateway_request_id", default=None)
_correlation_id: ContextVar[str | None] = ContextVar(
    "gateway_correlation_id", default=None
)


def new_request_id() -> str:
    return str(uuid.uuid4())


def get_request_id() -> str | None:
    return _request_id.get()


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def bind_request_ids(*, request_id: str, correlation_id: str) -> tuple:
    """Bind IDs into contextvars; return tokens for reset."""
    return (
        _request_id.set(request_id),
        _correlation_id.set(correlation_id),
    )


def reset_request_ids(tokens: tuple) -> None:
    request_token, correlation_token = tokens
    _request_id.reset(request_token)
    _correlation_id.reset(correlation_token)


def resolve_incoming_ids(request: Request) -> tuple[str, str]:
    """Derive request and correlation IDs from inbound headers."""
    incoming_request = (request.headers.get(REQUEST_ID_HEADER) or "").strip()
    incoming_correlation = (
        request.headers.get(CORRELATION_ID_HEADER) or ""
    ).strip()
    request_id = incoming_request or new_request_id()
    correlation_id = incoming_correlation or request_id
    return request_id, correlation_id


class RequestContextFilter(logging.Filter):
    """Inject request/correlation IDs into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        record.correlation_id = get_correlation_id() or "-"
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """Configure process logging with request-id fields."""
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s "
                "[request_id=%(request_id)s correlation_id=%(correlation_id)s] "
                "%(name)s: %(message)s"
            )
        )
        handler.addFilter(RequestContextFilter())
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.addFilter(RequestContextFilter())
    root.setLevel(level)
    # Ensure filter exists even when handlers were pre-installed without it.
    for handler in root.handlers:
        if not any(isinstance(f, RequestContextFilter) for f in handler.filters):
            handler.addFilter(RequestContextFilter())


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach request/correlation IDs to context, logs, and response headers."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: Callable
    ) -> Response:
        request_id, correlation_id = resolve_incoming_ids(request)
        tokens = bind_request_ids(
            request_id=request_id, correlation_id=correlation_id
        )
        request.state.request_id = request_id
        request.state.correlation_id = correlation_id
        try:
            response = await call_next(request)
        finally:
            reset_request_ids(tokens)
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response
