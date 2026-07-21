from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    mission_control_url: str
    mission_control_api_key: str
    request_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "Settings":
        url = os.environ.get("MISSION_CONTROL_URL", "").strip().rstrip("/")
        api_key = os.environ.get("MISSION_CONTROL_API_KEY", "").strip()

        if not url:
            raise RuntimeError("MISSION_CONTROL_URL is required")

        if not api_key:
            raise RuntimeError("MISSION_CONTROL_API_KEY is required")

        try:
            timeout = float(
                os.environ.get("MISSION_CONTROL_TIMEOUT_SECONDS", "30")
            )
        except ValueError as exc:
            raise RuntimeError(
                "MISSION_CONTROL_TIMEOUT_SECONDS must be numeric"
            ) from exc

        if timeout <= 0:
            raise RuntimeError(
                "MISSION_CONTROL_TIMEOUT_SECONDS must be greater than zero"
            )

        return cls(
            mission_control_url=url,
            mission_control_api_key=api_key,
            request_timeout_seconds=timeout,
        )
