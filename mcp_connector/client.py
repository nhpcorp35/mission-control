from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from mcp_connector.config import Settings
from mcp_connector.errors import MissionControlError
from mission_control.run_registry import is_terminal_status

# ChatGPT MCP tool-call runtime kills long-held calls without a usable
# payload (observed failure when a single wait spanned ~35s). Keep the
# default and hard cap inside a short safe window; callers (HAL) should
# invoke wait_for_run repeatedly until the run is terminal.
MCP_WAIT_DEFAULT_TIMEOUT_SECONDS = 20.0
MCP_WAIT_MIN_TIMEOUT_SECONDS = 0.1
MCP_WAIT_MAX_TIMEOUT_SECONDS = 25.0
MCP_WAIT_DEFAULT_POLL_INTERVAL_SECONDS = 2.0
MCP_WAIT_MIN_POLL_INTERVAL_SECONDS = 0.05
MCP_WAIT_MAX_POLL_INTERVAL_SECONDS = 10.0


def normalize_mcp_wait_timeout(timeout_seconds: float) -> float:
    """Validate and clamp ``timeout_seconds`` for ChatGPT-safe MCP waits.

    Values at or below zero, or below the minimum, are rejected. Values
    above ``MCP_WAIT_MAX_TIMEOUT_SECONDS`` are capped (not rejected) so
    callers that still pass legacy large timeouts remain usable.
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

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/runs/{run_id}")

    async def wait_for_run(
        self,
        run_id: str,
        *,
        timeout_seconds: float = MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = MCP_WAIT_DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> dict[str, Any]:
        """Poll ``get_run`` until the run is terminal or the wait window ends.

        Uses the same Mission Control URL, API key, and request path as
        ``get_run``. Sleeps between polls with a monotonic deadline.

        When the wait window expires while the run is still non-terminal,
        returns a normal structured payload (does not raise) with
        ``wait_expired=True`` plus the latest run fields so MCP clients can
        call again. Terminal runs return the run payload with
        ``wait_expired=False``.
        """
        effective_timeout = normalize_mcp_wait_timeout(timeout_seconds)
        effective_poll = normalize_mcp_wait_poll_interval(
            poll_interval_seconds
        )

        deadline = time.monotonic() + effective_timeout
        latest: dict[str, Any] | None = None
        last_error: MissionControlError | None = None

        while True:
            try:
                payload = await self.get_run(run_id)
            except MissionControlError as exc:
                # Unknown run cannot become available later.
                if exc.status_code == 404:
                    raise
                last_error = exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if latest is not None:
                        return self._wait_expired_payload(
                            latest,
                            timeout_seconds=effective_timeout,
                        )
                    raise MissionControlError(
                        (
                            f"Timed out waiting for run {run_id} after "
                            f"{effective_timeout} seconds"
                        ),
                        details={
                            "run_id": run_id,
                            "timeout_seconds": effective_timeout,
                            "wait_expired": True,
                            "latest": latest,
                            "last_error": last_error.as_dict()["error"],
                        },
                    ) from last_error
                await asyncio.sleep(min(effective_poll, remaining))
                continue

            latest = payload
            last_error = None
            status = payload.get("status")
            if status is not None and is_terminal_status(str(status)):
                return self._wait_terminal_payload(
                    payload,
                    timeout_seconds=effective_timeout,
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._wait_expired_payload(
                    latest,
                    timeout_seconds=effective_timeout,
                )

            await asyncio.sleep(min(effective_poll, remaining))

    @staticmethod
    def _wait_terminal_payload(
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        return {
            **payload,
            "wait_expired": False,
            "timeout_seconds": timeout_seconds,
            "reached_terminal": True,
        }

    @staticmethod
    def _wait_expired_payload(
        latest: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        return {
            **latest,
            "wait_expired": True,
            "timeout_seconds": timeout_seconds,
            "reached_terminal": False,
        }
