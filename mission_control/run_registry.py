"""SQLite-backed run registry for asynchronous mission execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import logging
import os
from pathlib import Path
import sqlite3
import threading
import uuid

from mission_control.run_result import (
    StructuredRunResult,
    deserialize_structured_result,
    serialize_structured_result,
)

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "./data/mission-control.db"
INTERRUPTED_RUN_ERROR = "Run interrupted by service restart."

_RUNS_TABLE = "runs"

TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "timed_out",
    }
)

# Backward-compatible private alias for internal call sites.
_TERMINAL_STATUSES = TERMINAL_STATUSES


class RunStatus(str, Enum):
    """Lifecycle statuses for a registered run."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


def is_terminal_status(status: RunStatus | str) -> bool:
    """Return True when ``status`` is a terminal run lifecycle status."""
    if isinstance(status, RunStatus):
        return status.value in TERMINAL_STATUSES
    return str(status) in TERMINAL_STATUSES


@dataclass
class RunRecord:
    """Snapshot of a single mission run persisted in SQLite."""

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
    result: StructuredRunResult | None = None


def resolve_db_path() -> str:
    """Return the configured SQLite database path."""
    return os.environ.get("MISSION_CONTROL_DB_PATH", DEFAULT_DB_PATH)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ensure_db_parent(db_path: str) -> None:
    Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _row_to_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["run_id"],
        status=RunStatus(row["status"]),
        created_at=_parse_dt(row["created_at"]) or _utc_now(),
        started_at=_parse_dt(row["started_at"]),
        completed_at=_parse_dt(row["completed_at"]),
        elapsed_seconds=row["elapsed_seconds"],
        stdout=row["stdout"] or "",
        stderr=row["stderr"] or "",
        error=row["error"],
        return_code=row["return_code"],
        commit_sha=row["commit_sha"],
        result=deserialize_structured_result(
            row["result_json"] if "result_json" in row.keys() else None
        ),
    )


class RunRegistry:
    """Thread-safe run registry backed by SQLite."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = os.path.abspath(
            os.path.expanduser(db_path or resolve_db_path())
        )
        self._lock = threading.Lock()
        _ensure_db_parent(self._db_path)
        self._conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    @property
    def db_path(self) -> str:
        return self._db_path

    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_RUNS_TABLE} (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    elapsed_seconds REAL,
                    stdout TEXT NOT NULL DEFAULT '',
                    stderr TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    return_code INTEGER,
                    commit_sha TEXT,
                    result_json TEXT
                )
                """
            )
            columns = {
                row[1]
                for row in self._conn.execute(
                    f"PRAGMA table_info({_RUNS_TABLE})"
                )
            }
            if "return_code" not in columns:
                self._conn.execute(
                    f"ALTER TABLE {_RUNS_TABLE} ADD COLUMN return_code INTEGER"
                )
            if "result_json" not in columns:
                self._conn.execute(
                    f"ALTER TABLE {_RUNS_TABLE} ADD COLUMN result_json TEXT"
                )
            self._conn.commit()

    def recover_interrupted_runs(self) -> int:
        """Mark queued or running runs failed after a service restart."""
        now = _utc_now()
        recovered = 0
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT run_id, started_at
                FROM {_RUNS_TABLE}
                WHERE status IN (?, ?)
                """,
                (RunStatus.QUEUED.value, RunStatus.RUNNING.value),
            ).fetchall()

            for row in rows:
                started_at = _parse_dt(row["started_at"])
                elapsed_seconds = None
                if started_at is not None:
                    elapsed_seconds = (now - started_at).total_seconds()

                self._conn.execute(
                    f"""
                    UPDATE {_RUNS_TABLE}
                    SET status = ?,
                        completed_at = ?,
                        elapsed_seconds = ?,
                        error = ?
                    WHERE run_id = ?
                    """,
                    (
                        RunStatus.FAILED.value,
                        _format_dt(now),
                        elapsed_seconds,
                        INTERRUPTED_RUN_ERROR,
                        row["run_id"],
                    ),
                )
                recovered += 1

            if recovered:
                self._conn.commit()
                logger.info(
                    "Recovered %s interrupted run(s) from %s",
                    recovered,
                    self._db_path,
                )

        return recovered

    def count_runs(self) -> int:
        """Return the number of persisted run records."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS count FROM {_RUNS_TABLE}"
            ).fetchone()
            return int(row["count"])

    def _list_run_ids_unlocked(self) -> list[str]:
        rows = self._conn.execute(
            f"SELECT run_id FROM {_RUNS_TABLE} ORDER BY created_at"
        ).fetchall()
        return [row["run_id"] for row in rows]

    def diagnostic_state(self) -> tuple[int, list[str]]:
        """Return ``(count, run_ids)`` for lifecycle logs (no secrets)."""
        with self._lock:
            keys = self._list_run_ids_unlocked()
        return len(keys), keys

    def _fetch_row(self, run_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            f"SELECT * FROM {_RUNS_TABLE} WHERE run_id = ?",
            (run_id,),
        ).fetchone()

    def _persist_record(self, record: RunRecord) -> None:
        self._conn.execute(
            f"""
            INSERT INTO {_RUNS_TABLE} (
                run_id,
                status,
                created_at,
                started_at,
                completed_at,
                elapsed_seconds,
                stdout,
                stderr,
                error,
                return_code,
                commit_sha,
                result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status = excluded.status,
                created_at = excluded.created_at,
                started_at = excluded.started_at,
                completed_at = excluded.completed_at,
                elapsed_seconds = excluded.elapsed_seconds,
                stdout = excluded.stdout,
                stderr = excluded.stderr,
                error = excluded.error,
                return_code = excluded.return_code,
                commit_sha = excluded.commit_sha,
                result_json = excluded.result_json
            """,
            (
                record.run_id,
                record.status.value,
                _format_dt(record.created_at),
                _format_dt(record.started_at),
                _format_dt(record.completed_at),
                record.elapsed_seconds,
                record.stdout,
                record.stderr,
                record.error,
                record.return_code,
                record.commit_sha,
                serialize_structured_result(record.result),
            ),
        )
        self._conn.commit()

    def create_run(self) -> RunRecord:
        """Create a new run in ``queued`` status with a UUID4 ``run_id``."""
        record = RunRecord(
            run_id=str(uuid.uuid4()),
            status=RunStatus.QUEUED,
            created_at=_utc_now(),
        )
        with self._lock:
            self._persist_record(record)
            keys = self._list_run_ids_unlocked()
            count = len(keys)
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
            row = self._fetch_row(run_id)
            if row is None:
                return None
            return _row_to_record(row)

    def update_status(
        self,
        run_id: str,
        status: RunStatus,
    ) -> RunRecord | None:
        """Update run status and related timestamps."""
        with self._lock:
            row = self._fetch_row(run_id)
            if row is None:
                return None

            record = _row_to_record(row)
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

            self._persist_record(record)
            keys = self._list_run_ids_unlocked()
            count = len(keys)

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
        result: StructuredRunResult | None = None,
    ) -> RunRecord | None:
        """Store execution output fields on an existing run."""
        with self._lock:
            row = self._fetch_row(run_id)
            if row is None:
                return None

            record = _row_to_record(row)
            record.stdout = stdout
            record.stderr = stderr
            record.error = error
            record.return_code = return_code
            if commit_sha is not None:
                record.commit_sha = commit_sha
            if result is not None:
                record.result = result
            self._persist_record(record)
            return record

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()
