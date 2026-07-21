from __future__ import annotations

from typing import Any


class MissionControlError(RuntimeError):
    """A normalized error returned by the Mission Control REST API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "message": str(self),
                "status_code": self.status_code,
                "details": self.details,
            },
        }
