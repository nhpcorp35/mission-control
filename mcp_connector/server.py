from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from mcp_connector.client import MissionControlClient
from mcp_connector.config import Settings
from mcp_connector.errors import MissionControlError


settings = Settings.from_env()
client = MissionControlClient(settings)

mcp = FastMCP(
    "Mission Control",
    instructions=(
        "Submit Mission Control YAML, retrieve asynchronous run status, "
        "and wait for runs to reach a terminal state. Intended HAL flow: "
        "submit_run, then wait_for_run, then inspect status/output/commit_sha."
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
    timeout_seconds: float = 900.0,
    poll_interval_seconds: float = 2.0,
) -> dict[str, Any]:
    """Wait until a run reaches a terminal status or the timeout expires.

    Polls through the same authenticated get_run path until the run is
    terminal (completed, failed, or timed_out) or timeout_seconds elapses.
    Returns immediately when already terminal. On success the payload shape
    matches get_run. On wait timeout returns a structured error with run_id,
    timeout_seconds, and the latest successful payload when available.
    Defaults: timeout_seconds=900, poll_interval_seconds=2.
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


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
