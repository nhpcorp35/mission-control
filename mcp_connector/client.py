from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from mcp_connector.config import Settings
from mcp_connector.errors import MissionControlError
from mission_control.run_registry import is_terminal_status


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
        timeout_seconds: float = 900.0,
        poll_interval_seconds: float = 2.0,
    ) -> dict[str, Any]:
        """Poll ``get_run`` until the run is terminal or the timeout expires.

        Uses the same Mission Control URL, API key, and request path as
        ``get_run``. Sleeps between polls with a monotonic deadline.
        """
        if timeout_seconds <= 0:
            raise ValueError(
                "timeout_seconds must be a positive number"
            )
        if poll_interval_seconds <= 0:
            raise ValueError(
                "poll_interval_seconds must be a positive number"
            )

        deadline = time.monotonic() + float(timeout_seconds)
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
                    raise MissionControlError(
                        (
                            f"Timed out waiting for run {run_id} after "
                            f"{timeout_seconds} seconds"
                        ),
                        details={
                            "run_id": run_id,
                            "timeout_seconds": timeout_seconds,
                            "latest": latest,
                            "last_error": last_error.as_dict()["error"],
                        },
                    ) from last_error
                await asyncio.sleep(min(poll_interval_seconds, remaining))
                continue

            latest = payload
            last_error = None
            status = payload.get("status")
            if status is not None and is_terminal_status(str(status)):
                return payload

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MissionControlError(
                    (
                        f"Timed out waiting for run {run_id} after "
                        f"{timeout_seconds} seconds"
                    ),
                    details={
                        "run_id": run_id,
                        "timeout_seconds": timeout_seconds,
                        "latest": latest,
                    },
                )

            await asyncio.sleep(min(poll_interval_seconds, remaining))
