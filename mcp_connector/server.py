from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette

from mcp_connector.client import (
    MCP_WAIT_DEFAULT_POLL_INTERVAL_SECONDS,
    MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
    MCP_WAIT_MAX_TIMEOUT_SECONDS,
    MissionControlClient,
)
from mcp_connector.config import Settings
from mcp_connector.errors import MissionControlError


EXPECTED_TOOL_NAMES = ("submit_run", "get_run", "wait_for_run")

settings = Settings.from_env()
client = MissionControlClient(settings)

mcp = FastMCP(
    "Mission Control",
    instructions=(
        "Submit Mission Control YAML, retrieve asynchronous run status, "
        "and wait for runs to reach a terminal state. Intended HAL flow: "
        "submit_run, then call wait_for_run repeatedly (no user prompting) "
        "until status is terminal or wait_expired stays relevant, then "
        "inspect status/output/commit_sha. Each wait_for_run uses a short "
        f"ChatGPT-safe default window ({MCP_WAIT_DEFAULT_TIMEOUT_SECONDS:g}s, "
        f"capped at {MCP_WAIT_MAX_TIMEOUT_SECONDS:g}s); when wait_expired is "
        "true, call wait_for_run again with the same run_id."
    ),
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8001")),
    json_response=True,
)


def _tool_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, MissionControlError):
        return exc.as_dict()

    return {
        "ok": False,
        "error": {
            "message": str(exc),
            "status_code": None,
            "details": None,
        },
    }


@mcp.tool()
async def submit_run(mission_yaml: str) -> dict[str, Any]:
    """Submit an exact Mission Control YAML document."""
    try:
        if not mission_yaml.strip():
            raise ValueError("mission_yaml must not be empty")

        result = await client.submit_run(mission_yaml)
        return {"ok": True, **result}
    except Exception as exc:
        return _tool_error(exc)


@mcp.tool()
async def get_run(run_id: str) -> dict[str, Any]:
    """Retrieve the current state of a Mission Control run."""
    try:
        if not run_id.strip():
            raise ValueError("run_id must not be empty")

        result = await client.get_run(run_id)
        return {"ok": True, **result}
    except Exception as exc:
        return _tool_error(exc)


@mcp.tool()
async def wait_for_run(
    run_id: str,
    timeout_seconds: float = MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = MCP_WAIT_DEFAULT_POLL_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Wait briefly for a run to reach a terminal status (ChatGPT-safe).

    Polls through the same authenticated get_run path until the run is
    terminal (completed, failed, or timed_out) or the short wait window
    elapses. Returns immediately when already terminal.

    HAL should call this tool repeatedly without user prompting until the
    run is terminal. Each call uses a conservative default window
    (timeout_seconds=20) so a single MCP tool call stays within ChatGPT
    runtime limits. Values above 25s are capped to 25s; zero/negative
    values are rejected.

    On terminal status returns ok=true with run fields, wait_expired=false,
    and timeout_seconds (effective). When the wait window expires while
    still queued/running, returns ok=true with the latest run fields,
    wait_expired=true, and timeout_seconds — not a transport/tool error —
    so HAL can call wait_for_run again with the same run_id.
    """
    try:
        if not run_id.strip():
            raise ValueError("run_id must not be empty")

        result = await client.wait_for_run(
            run_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        return {"ok": True, **result}
    except Exception as exc:
        return _tool_error(exc)


def create_http_app() -> Starlette:
    """Build the Railway/public MCP HTTP app.

    ChatGPT custom apps should use Streamable HTTP at ``/mcp``. Legacy SSE at
    ``/sse`` (plus ``/messages``) is also mounted so existing ``/sse`` URLs keep
    discovering the same tools.
    """
    streamable_app = mcp.streamable_http_app()
    sse_app = mcp.sse_app()

    routes = list(streamable_app.routes)
    seen_paths = {getattr(route, "path", None) for route in routes}
    for route in sse_app.routes:
        path = getattr(route, "path", None)
        if path in seen_paths:
            continue
        routes.append(route)
        seen_paths.add(path)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with mcp.session_manager.run():
            yield

    return Starlette(
        debug=mcp.settings.debug,
        routes=routes,
        lifespan=lifespan,  # type: ignore[arg-type]
    )


def main() -> None:
    """Start the MCP HTTP server (Railway ``SERVICE_MODE=mcp`` entrypoint)."""
    app = create_http_app()
    uvicorn.run(
        app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
