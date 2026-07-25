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


EXPECTED_TOOL_NAMES = (
    "submit_run",
    "submit_structured_run",
    "get_run",
    "wait_for_run",
    "submit_and_wait",
)

settings = Settings.from_env()
client = MissionControlClient(settings)

mcp = FastMCP(
    "Mission Control",
    instructions=(
        "Submit Mission Control missions as structured fields "
        "(prefer submit_structured_run for routine execute missions) or as "
        "raw YAML (submit_run), retrieve asynchronous run status, and wait "
        "for runs to reach a terminal state. For exact YAML end-to-end in "
        "one tool call, use submit_and_wait (submit_run + wait_for_run). "
        "Intended HAL flow: submit_and_wait with exact YAML, or "
        "submit_structured_run (or submit_run) then wait_for_run until "
        "status is terminal (or repeat when wait_expired is true), then "
        "inspect status, summary, result.persistence, and commit_sha. "
        "Prefer summary / result.persistence / commit_sha over agent stdout "
        "for persistence claims (platform persistence runs after the agent "
        "completes). wait_for_run / submit_and_wait default timeout is "
        f"{MCP_WAIT_DEFAULT_TIMEOUT_SECONDS:g}s; requested timeouts are "
        f"honored up to {MCP_WAIT_MAX_TIMEOUT_SECONDS:g}s. When "
        "wait_expired is true, call wait_for_run again with the same run_id."
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
async def submit_structured_run(
    mission_id: str,
    title: str,
    instructions: str,
    deliverables: list[str],
    create_files: bool,
    modify_files: bool,
    persistence_mode: str = "none",
    repository_name: str = "Mission-Control",
    repository_path: str = ".",
    base_branch: str = "main",
    run_commands: bool = True,
    platform_push_approved: bool = False,
    allow_automatic_platform_push: bool = False,
) -> dict[str, Any]:
    """Submit a mission via structured fields (POST /runs/structured).

    Prefer this for routine execute missions. Mission Control builds Mission
    Spec v1.0 YAML with safe defaults and queues it through the same async
    pipeline as submit_run. Raw YAML submit_run remains available when exact
    document control is required.
    """
    try:
        if not mission_id.strip():
            raise ValueError("mission_id must not be empty")
        if not title.strip():
            raise ValueError("title must not be empty")
        if not instructions.strip():
            raise ValueError("instructions must not be empty")
        if not isinstance(deliverables, list):
            raise ValueError("deliverables must be a list")

        result = await client.submit_structured_run(
            mission_id=mission_id,
            title=title,
            instructions=instructions,
            deliverables=deliverables,
            create_files=create_files,
            modify_files=modify_files,
            persistence_mode=persistence_mode,
            repository_name=repository_name,
            repository_path=repository_path,
            base_branch=base_branch,
            run_commands=run_commands,
            platform_push_approved=platform_push_approved,
            allow_automatic_platform_push=allow_automatic_platform_push,
        )
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
    """Wait for a run to reach a terminal status.

    Polls through the same authenticated get_run path until the run is
    terminal (completed, failed, or timed_out) or timeout_seconds elapses.
    Returns immediately when already terminal. poll_interval_seconds
    controls delay between get_run polls.

    Default timeout_seconds is 20. Requested timeouts are honored up to
    3600s (aligned with POST /runs/{run_id}/wait); larger values are
    capped. Zero/negative values are rejected. When wait_expired is true,
    call wait_for_run again with the same run_id.

    On terminal status returns ok=true with run fields, wait_expired=false,
    and timeout_seconds (effective). When the wait window expires while
    still queued/running, returns ok=true with the latest run fields,
    wait_expired=true, and timeout_seconds — not a transport/tool error.
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


@mcp.tool()
async def submit_and_wait(
    mission_yaml: str,
    timeout_seconds: float = MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = MCP_WAIT_DEFAULT_POLL_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Submit exact mission YAML and wait for a terminal run state.

    One-shot HAL path: reuses authenticated submit_run then wait_for_run
    (same timeout_seconds / poll_interval_seconds validation and limits as
    wait_for_run). Returns the accepted run_id and final authoritative run
    payload. Submission failures return the existing structured submission
    error without waiting. When wait_expired is true, resume with
    wait_for_run using the same run_id.
    """
    try:
        if not mission_yaml.strip():
            raise ValueError("mission_yaml must not be empty")

        result = await client.submit_and_wait(
            mission_yaml,
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
