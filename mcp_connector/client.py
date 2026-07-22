from __future__ import annotations

from typing import Any

import httpx

from mcp_connector.config import Settings
from mcp_connector.errors import MissionControlError


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
        timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 1.0,
    ) -> dict[str, Any]:
        # HTTP client timeout must exceed the server-side wait budget.
        http_timeout = max(
            self._settings.request_timeout_seconds,
            float(timeout_seconds) + 30.0,
        )
        return await self._request(
            "POST",
            f"/runs/{run_id}/wait",
            json={
                "timeout_seconds": timeout_seconds,
                "poll_interval_seconds": poll_interval_seconds,
            },
            timeout=http_timeout,
        )
