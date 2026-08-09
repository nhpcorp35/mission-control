"""HAL LegalAI Gateway HTTP service (Phase 1 skeleton)."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from hal_legalai_gateway.config import GatewaySettings, load_settings
from hal_legalai_gateway.health import aggregate_health
from hal_legalai_gateway.request_context import (
    RequestIdMiddleware,
    configure_logging,
    get_correlation_id,
    get_request_id,
)

logger = logging.getLogger(__name__)

_settings: GatewaySettings | None = None


def get_settings() -> GatewaySettings:
    """Return process settings, loading once if lifespan has not run yet."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings_for_tests() -> None:
    """Clear cached settings (test helper)."""
    global _settings
    _settings = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _settings
    configure_logging()
    _settings = load_settings()
    logger.info(
        "HAL LegalAI Gateway starting deployed_commit_sha=%s "
        "downstreams=%s health_timeout_seconds=%s",
        _settings.deployed_commit_sha,
        ",".join(item.key for item in _settings.downstreams),
        _settings.health_timeout_seconds,
    )
    yield
    logger.info("HAL LegalAI Gateway shutting down")
    _settings = None


app = FastAPI(
    title="HAL LegalAI Gateway",
    description=(
        "Thin interface consolidation for LegalAI downstream services. "
        "Phase 1 exposes registry metadata and independent health aggregation. "
        "Bridge, Storage, Mission Control, and artifact retrieval remain "
        "separately deployed."
    ),
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(RequestIdMiddleware)


@app.get("/")
async def root(request: Request) -> dict[str, Any]:
    settings = get_settings()
    return {
        "service": "hal-legalai-gateway",
        "phase": 1,
        "deployed_commit_sha": settings.deployed_commit_sha,
        "request_id": getattr(request.state, "request_id", get_request_id()),
        "correlation_id": getattr(
            request.state, "correlation_id", get_correlation_id()
        ),
        "endpoints": {
            "health": "/health",
            "registry": "/registry",
        },
    }


@app.get("/registry")
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


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    """Gateway liveness plus independent downstream health aggregation.

    Always returns HTTP 200 when the gateway process itself is up so a single
    unhealthy downstream cannot take the gateway (or unrelated capabilities)
    out of Railway rotation. Per-downstream status, latency, and failure_stage
    are reported independently; capabilities reflect only their own downstream.
    """
    settings = get_settings()
    payload = await aggregate_health(settings)
    payload["request_id"] = getattr(
        request.state, "request_id", payload.get("request_id")
    )
    payload["correlation_id"] = getattr(
        request.state, "correlation_id", payload.get("correlation_id")
    )
    # Exact Railway-provided SHA (or explicit unknown fallback).
    payload["deployed_commit_sha"] = settings.deployed_commit_sha
    return JSONResponse(payload)


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
