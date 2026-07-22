"""Process-local in-memory run registry for asynchronous mission execution.

This registry keeps run records in the current process only. Records are not
written to disk, Redis, or any shared store. Restarting the process discards
all state. It is intentionally independent of FastAPI and Cursor execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import logging
import os
import threading
import uuid

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "timed_out",
    }
)


class RunStatus(str, Enum):
    """Lifecycle statuses for a registered run."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass
class RunRecord:
    """Snapshot of a single mission run held in process memory.

    Records are retained for the lifetime of the process. Terminal statuses
    (completed, failed, timed_out) update the existing entry in place and
    never remove it from the registry.
    """

    run_id: str
    status: RunStatus
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    elapsed_seconds: float | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    return_code: int | None = None
    commit_sha: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunRegistry:
    """Thread-safe, process-local, non-persistent run registry.

    All run state lives in this process's memory and is lost on restart.
    Unknown run IDs return ``None`` rather than raising.
    """

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._lock = threading.Lock()

    def diagnostic_state(self) -> tuple[int, list[str]]:
        """Return ``(count, run_ids)`` for lifecycle logs (no secrets)."""
        with self._lock:
            keys = list(self._runs.keys())
        return len(keys), keys

    def create_run(self) -> RunRecord:
        """Create a new run in ``queued`` status with a UUID4 ``run_id``."""
        record = RunRecord(
            run_id=str(uuid.uuid4()),
            status=RunStatus.QUEUED,
            created_at=_utc_now(),
        )
        with self._lock:
            self._runs[record.run_id] = record
            count = len(self._runs)
            keys = list(self._runs.keys())
        logger.info(
            (
                "lifecycle run_id=%s event=run_record_created status=%s "
                "api_pid=%s registry_id=%s registry_count=%s registry_keys=%s"
            ),
            record.run_id,
            record.status.value,
            os.getpid(),
            id(self),
            count,
            keys,
        )
        return record

    def get_run(self, run_id: str) -> RunRecord | None:
        """Return the run record for ``run_id``, or ``None`` if unknown."""
        with self._lock:
            return self._runs.get(run_id)

    def update_status(
        self,
        run_id: str,
        status: RunStatus,
    ) -> RunRecord | None:
        """Update run status and related timestamps.

        Mutates the existing registry entry in place and never removes it.
        Returns ``None`` when ``run_id`` is unknown.
        """
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None

            now = _utc_now()
            record.status = status

            if status is RunStatus.RUNNING and record.started_at is None:
                record.started_at = now

            if status in (
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.TIMED_OUT,
            ):
                record.completed_at = now
                if record.started_at is not None:
                    record.elapsed_seconds = (
                        record.completed_at - record.started_at
                    ).total_seconds()

            count = len(self._runs)
            keys = list(self._runs.keys())

        event = (
            "final_status_update"
            if status.value in _TERMINAL_STATUSES
            else "status_update"
        )
        logger.info(
            (
                "lifecycle run_id=%s event=%s status=%s "
                "api_pid=%s registry_id=%s registry_count=%s registry_keys=%s"
            ),
            run_id,
            event,
            status.value,
            os.getpid(),
            id(self),
            count,
            keys,
        )
        return record

    def store_result(
        self,
        run_id: str,
        *,
        stdout: str = "",
        stderr: str = "",
        error: str | None = None,
        return_code: int | None = None,
        commit_sha: str | None = None,
    ) -> RunRecord | None:
        """Store execution output fields on an existing run.

        Updates the existing record in place; never removes it. Returns
        ``None`` when ``run_id`` is unknown. Does not change status; callers
        should use :meth:`update_status` for lifecycle transitions.
        """
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None

            record.stdout = stdout
            record.stderr = stderr
            record.error = error
            record.return_code = return_code
            if commit_sha is not None:
                record.commit_sha = commit_sha
            return record