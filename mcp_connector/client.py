from __future__ import annotations

from typing import Any

import httpx

from mcp_connector.config import Settings
from mcp_connector.errors import MissionControlError
from mission_control.monitoring import (
    MONITOR_CURSOR_MAX_CHARS,
    normalize_monitor_cursor,
)

# Honor caller-requested wait budgets end-to-end (aligned with
# POST /runs/{run_id}/wait). Default stays short for interactive clients;
# values up to MCP_WAIT_MAX_TIMEOUT_SECONDS are not artificially cut off
# at ~25s. Platform edge proxies may still close long idle HTTP tool
# responses — see MISSION_CONTROL_API.md.
MCP_WAIT_DEFAULT_TIMEOUT_SECONDS = 20.0
MCP_WAIT_MIN_TIMEOUT_SECONDS = 0.1
MCP_WAIT_MAX_TIMEOUT_SECONDS = 3600.0
MCP_WAIT_DEFAULT_POLL_INTERVAL_SECONDS = 2.0
MCP_WAIT_MIN_POLL_INTERVAL_SECONDS = 0.05
MCP_WAIT_MAX_POLL_INTERVAL_SECONDS = 10.0
# Re-export for connector/gateway callers and tests.
MCP_MONITOR_CURSOR_MAX_CHARS = MONITOR_CURSOR_MAX_CHARS


def normalize_mcp_wait_timeout(timeout_seconds: float) -> float:
    """Validate and clamp ``timeout_seconds`` for MCP ``wait_for_run``.

    Values at or below zero, or below the minimum, are rejected. Values
    above ``MCP_WAIT_MAX_TIMEOUT_SECONDS`` are capped (not rejected) so
    oversized requests still wait for the maximum supported window.
    """
    value = float(timeout_seconds)
    if value <= 0:
        raise ValueError("timeout_seconds must be a positive number")
    if value < MCP_WAIT_MIN_TIMEOUT_SECONDS:
        raise ValueError(
            "timeout_seconds must be >= "
            f"{MCP_WAIT_MIN_TIMEOUT_SECONDS}"
        )
    if value > MCP_WAIT_MAX_TIMEOUT_SECONDS:
        return MCP_WAIT_MAX_TIMEOUT_SECONDS
    return value


def normalize_mcp_wait_poll_interval(poll_interval_seconds: float) -> float:
    """Validate and clamp ``poll_interval_seconds`` for MCP waits."""
    value = float(poll_interval_seconds)
    if value <= 0:
        raise ValueError(
            "poll_interval_seconds must be a positive number"
        )
    if value < MCP_WAIT_MIN_POLL_INTERVAL_SECONDS:
        raise ValueError(
            "poll_interval_seconds must be >= "
            f"{MCP_WAIT_MIN_POLL_INTERVAL_SECONDS}"
        )
    if value > MCP_WAIT_MAX_POLL_INTERVAL_SECONDS:
        return MCP_WAIT_MAX_POLL_INTERVAL_SECONDS
    return value


def normalize_mcp_wait_cursor(cursor: str | None) -> str | None:
    """Validate optional wait cursor size before forwarding to Mission Control."""
    return normalize_monitor_cursor(cursor)

class MissionControlClient:
    """Thin asynchronous client for the Mission Control REST API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": (
                f"Bearer {self._settings.mission_control_api_key}"
            ),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        request_timeout = (
            self._settings.request_timeout_seconds
            if timeout is None
            else timeout
        )
        try:
            async with httpx.AsyncClient(
                base_url=self._settings.mission_control_url,
                headers=self._headers(),
                timeout=request_timeout,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    json=json,
                )
        except httpx.TimeoutException as exc:
            raise MissionControlError(
                "Mission Control did not respond before the timeout"
            ) from exc
        except httpx.RequestError as exc:
            raise MissionControlError(
                f"Could not reach Mission Control: {exc}"
            ) from exc

        try:
            body: Any = response.json()
        except ValueError:
            body = {"raw_response": response.text[:4000]}

        if response.is_error:
            raise MissionControlError(
                "Mission Control request failed",
                status_code=response.status_code,
                details=body,
            )

        if not isinstance(body, dict):
            raise MissionControlError(
                "Mission Control returned a non-object JSON response",
                status_code=response.status_code,
                details=body,
            )

        return body

    async def submit_run(self, mission_yaml: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/runs",
            json={"mission_yaml": mission_yaml},
        )

    async def submit_structured_run(
        self,
        *,
        mission_id: str,
        title: str,
        instructions: str,
        deliverables: list[Any],
        create_files: bool,
        modify_files: bool,
        persistence_mode: str | None = None,
        repository_name: str = "Mission-Control",
        repository_path: str = ".",
        base_branch: str = "main",
        run_commands: bool = True,
        platform_push_approved: bool | None = None,
        allow_automatic_platform_push: bool = False,
        approval: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mission_id": mission_id,
            "title": title,
            "instructions": instructions,
            "deliverables": deliverables,
            "create_files": create_files,
            "modify_files": modify_files,
            "repository_name": repository_name,
            "repository_path": repository_path,
            "base_branch": base_branch,
            "run_commands": run_commands,
            "allow_automatic_platform_push": (
                allow_automatic_platform_push
            ),
        }
        # Omit unset persistence_mode so the API/builder can infer push for
        # mutations and none for read-only; explicit values stay authoritative.
        if persistence_mode is not None:
            payload["persistence_mode"] = persistence_mode
        # Omit unset flat approval so nested-only approval is not conflicted
        # by an invented default false.
        if platform_push_approved is not None:
            payload["platform_push_approved"] = platform_push_approved
        if approval is not None:
            payload["approval"] = approval
        return await self._request(
            "POST",
            "/runs/structured",
            json=payload,
        )

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/runs/{run_id}")

    async def run_repository_command(
        self,
        *,
        repository: str,
        ref: str,
        argv: list[str],
        working_directory: str = ".",
        timeout_seconds: float = 300.0,
        allowed_env_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Execute an allowlisted repository command (POST /repository-commands)."""
        # Bound httpx timeout above the command timeout so the API can respond.
        request_timeout = max(float(timeout_seconds) + 30.0, 60.0)
        return await self._request(
            "POST",
            "/repository-commands",
            json={
                "repository": repository,
                "ref": ref,
                "argv": argv,
                "working_directory": working_directory,
                "timeout_seconds": timeout_seconds,
                "allowed_env_names": list(allowed_env_names or []),
            },
            timeout=request_timeout,
        )

    async def submit_and_wait(
        self,
        mission_yaml: str,
        *,
        timeout_seconds: float = MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = MCP_WAIT_DEFAULT_POLL_INTERVAL_SECONDS,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Submit exact mission YAML, then wait via ``wait_for_run``.

        Reuses the authenticated ``submit_run`` and ``wait_for_run`` paths.
        Wait parameter validation/clamping matches ``wait_for_run`` and runs
        before submission so an invalid timeout does not queue a run.

        On structured submission failure (``ok: false``, no ``run_id``),
        returns that payload without entering the wait loop. On success,
        returns the ``wait_for_run`` payload (accepted ``run_id`` plus the
        final authoritative run fields, including Phase 2B monitoring fields
        and ``wait_expired`` when the caller-requested wait window expires).
        """
        # Validate wait bounds before submit so bad timeouts never queue a run.
        effective_timeout = normalize_mcp_wait_timeout(timeout_seconds)
        effective_poll = normalize_mcp_wait_poll_interval(
            poll_interval_seconds
        )
        effective_cursor = normalize_mcp_wait_cursor(cursor)

        submitted = await self.submit_run(mission_yaml)
        run_id = submitted.get("run_id")
        if submitted.get("ok") is False or not run_id:
            return submitted

        return await self.wait_for_run(
            str(run_id),
            timeout_seconds=effective_timeout,
            poll_interval_seconds=effective_poll,
            cursor=effective_cursor,
        )

    async def wait_for_run(
        self,
        run_id: str,
        *,
        timeout_seconds: float = MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = MCP_WAIT_DEFAULT_POLL_INTERVAL_SECONDS,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Forward to Mission Control ``POST /runs/{run_id}/wait``.

        Mission Control is the source of truth for Phase 2B monitoring fields
        (``heartbeat_health``, ``stale_heartbeat``, ``monitoring_history``,
        ``cursor``, ``stale_threshold_seconds``). This client does not
        fabricate monitoring fields; it validates bounds then returns the
        authoritative wait payload unchanged.

        Optional ``cursor`` resumes bounded history after ``wait_expired``.
        Oversized cursors are rejected before the request is sent. Wait
        expiry never mutates or cancels the run.
        """
        effective_timeout = normalize_mcp_wait_timeout(timeout_seconds)
        effective_poll = normalize_mcp_wait_poll_interval(
            poll_interval_seconds
        )
        effective_cursor = normalize_mcp_wait_cursor(cursor)

        body: dict[str, Any] = {
            "timeout_seconds": effective_timeout,
            "poll_interval_seconds": effective_poll,
        }
        if effective_cursor is not None:
            body["cursor"] = effective_cursor

        # Bound httpx timeout above the wait budget so Mission Control can
        # return wait_expired within the caller-selected duration.
        request_timeout = max(effective_timeout + 30.0, 60.0)
        return await self._request(
            "POST",
            f"/runs/{run_id}/wait",
            json=body,
            timeout=request_timeout,
        )
