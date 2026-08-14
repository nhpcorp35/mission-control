"""SQLite-backed run registry for asynchronous mission execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any
import uuid

import yaml

from mission_control.run_result import (
    StructuredRunResult,
    deserialize_structured_result,
    serialize_structured_result,
)

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "./data/mission-control.db"
INTERRUPTED_RUN_ERROR = "Run interrupted by service restart."

_RUNS_TABLE = "runs"
_STARTUP_RECOVERY_LEASE_TABLE = "startup_recovery_leases"
STARTUP_RECOVERY_LEASE_NAME = "interrupted_run_recovery"
STARTUP_RECOVERY_LEASE_TTL_SECONDS = 30
_SQLITE_BUSY_TIMEOUT_MS = 5000

# Live observability: heartbeat cadence while agent execution is active.
HEARTBEAT_INTERVAL_SECONDS = 5.0

# Bounded platform-authored progress (never raw model output / secrets).
_PROGRESS_ALLOWED_KEYS = frozenset({"step", "detail"})
_PROGRESS_STEP_MAX_LEN = 64
_PROGRESS_DETAIL_MAX_LEN = 160
_SECRETISH_RE = re.compile(
    r"(?i)\b(token|secret|password|api[_-]?key|authorization|bearer)\b|"
    r"\b[A-Za-z0-9_-]{24,}\b"
)

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


class RunPhase(str, Enum):
    """Authoritative platform execution phase for live mission status."""

    QUEUED = "queued"
    WORKSPACE_PREPARATION = "workspace_preparation"
    AGENT_EXECUTION = "agent_execution"
    VERIFICATION = "verification"
    PERSISTENCE = "persistence"
    CLEANUP = "cleanup"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_PHASES = frozenset(
    {
        RunPhase.COMPLETED.value,
        RunPhase.FAILED.value,
    }
)

_ACTIVE_PHASES = frozenset(
    {
        RunPhase.QUEUED.value,
        RunPhase.WORKSPACE_PREPARATION.value,
        RunPhase.AGENT_EXECUTION.value,
        RunPhase.VERIFICATION.value,
        RunPhase.PERSISTENCE.value,
        RunPhase.CLEANUP.value,
    }
)


def is_terminal_status(status: RunStatus | str) -> bool:
    """Return True when ``status`` is a terminal run lifecycle status."""
    if isinstance(status, RunStatus):
        return status.value in TERMINAL_STATUSES
    return str(status) in TERMINAL_STATUSES


def is_terminal_phase(phase: RunPhase | str | None) -> bool:
    """Return True when ``phase`` is a terminal observability phase."""
    if phase is None:
        return False
    if isinstance(phase, RunPhase):
        return phase.value in TERMINAL_PHASES
    return str(phase) in TERMINAL_PHASES


def _bound_text(value: str, max_len: int) -> str:
    text = " ".join(value.split())
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3] + "..."


def _redact_progress_text(value: str) -> str:
    """Strip secret-ish tokens from platform progress detail."""
    return _SECRETISH_RE.sub("[redacted]", value)


def platform_progress(*, step: str, detail: str) -> dict[str, str]:
    """Build a small, redacted, platform-authored progress object."""
    return {
        "step": _bound_text(str(step), _PROGRESS_STEP_MAX_LEN),
        "detail": _bound_text(
            _redact_progress_text(str(detail)),
            _PROGRESS_DETAIL_MAX_LEN,
        ),
    }


def sanitize_progress(value: object | None) -> dict[str, str] | None:
    """Normalize stored/API progress; drop unknown keys and bound values."""
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    step = value.get("step")
    detail = value.get("detail")
    if not isinstance(step, str) or not isinstance(detail, str):
        return None
    # Reject unexpected keys by reconstruction (allowlist only).
    _ = {k: value.get(k) for k in value if k in _PROGRESS_ALLOWED_KEYS}
    return platform_progress(step=step, detail=detail)


def serialize_progress(value: dict[str, str] | None) -> str | None:
    cleaned = sanitize_progress(value)
    if cleaned is None:
        return None
    return json.dumps(cleaned, separators=(",", ":"), sort_keys=True)


def deserialize_progress(raw: str | None) -> dict[str, str] | None:
    if raw is None or raw == "":
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return sanitize_progress(parsed)


def phase_for_status(status: RunStatus) -> RunPhase:
    """Best-effort phase for legacy rows or status-only callers."""
    if status is RunStatus.QUEUED:
        return RunPhase.QUEUED
    if status is RunStatus.RUNNING:
        return RunPhase.AGENT_EXECUTION
    if status is RunStatus.COMPLETED:
        return RunPhase.COMPLETED
    return RunPhase.FAILED


def _parse_phase(value: str | None, status: RunStatus) -> RunPhase:
    if value:
        try:
            return RunPhase(value)
        except ValueError:
            pass
    return phase_for_status(status)


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
    mission_yaml: str | None = None
    retried_from: str | None = None
    phase: RunPhase = RunPhase.QUEUED
    phase_started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    progress: dict[str, str] | None = None

    @property
    def queued_at(self) -> datetime:
        """Alias of ``created_at`` for callers expecting queue-time naming."""
        return self.created_at


class ReservedRunOutcome(str, Enum):
    """Deterministic outcomes for caller-reserved run ID materialization."""

    CREATED = "created"
    RECOVERED_IDEMPOTENTLY = "recovered_idempotently"
    CONFLICT = "conflict"


# Stable conflict classes for fail-closed reserved-id materialization.
CONFLICT_INVALID_RUN_ID = "invalid_run_id"
CONFLICT_NONCANONICAL_RUN_ID = "noncanonical_run_id"
CONFLICT_MISSING_BINDING_IDENTITY = "missing_binding_identity"
CONFLICT_MISSION_YAML_MISMATCH = "mission_yaml_mismatch"
CONFLICT_PERMISSIONS_MISMATCH = "permissions_mismatch"
CONFLICT_EXECUTION_MISMATCH = "execution_mismatch"
CONFLICT_REPOSITORY_MISMATCH = "repository_mismatch"
CONFLICT_OWNERSHIP_MISMATCH = "ownership_mismatch"
CONFLICT_OWNERSHIP_ALIAS_CONFLICT = "ownership_alias_conflict"
CONFLICT_EXISTING_RUN_COLLISION = "existing_run_collision"


@dataclass(frozen=True)
class ReservedRunCreateResult:
    """Result of creating or recovering a caller-reserved run ID.

    ``outcome`` is one of :class:`ReservedRunOutcome`. On conflict,
    ``record`` is the existing immutable row (when present) and
    ``conflict_class`` names the fail-closed reason. Never log
    ``mission_yaml`` from this object.
    """

    outcome: ReservedRunOutcome
    record: RunRecord | None = None
    conflict_class: str | None = None


def require_canonical_run_uuid(run_id: str) -> str:
    """Return ``run_id`` when it is a canonical hyphenated UUID string.

    Raises ``ValueError`` with a stable conflict-class message for
    malformed or non-canonical forms (uppercase, braces, URN, compact hex).
    """
    if not isinstance(run_id, str) or not run_id:
        raise ValueError(CONFLICT_INVALID_RUN_ID)
    try:
        parsed = uuid.UUID(run_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(CONFLICT_INVALID_RUN_ID) from exc
    canonical = str(parsed)
    if run_id != canonical:
        raise ValueError(CONFLICT_NONCANONICAL_RUN_ID)
    return canonical


def _is_blank(value: str | None) -> bool:
    """True when ``value`` is missing or whitespace-only."""
    return value is None or str(value).strip() == ""


def normalize_ownership_id(value: str | None) -> str | None:
    """Strip ownership ids; blank / whitespace-only becomes ``None``."""
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped if stripped else None


def normalize_optional_mission_yaml(value: str | None) -> str | None:
    """Preserve exact YAML text; blank / whitespace-only becomes ``None``."""
    if value is None:
        return None
    if str(value).strip() == "":
        return None
    return value


def binding_identity_complete(
    mission_yaml: str | None,
    retried_from: str | None,
) -> bool:
    """True when both immutable binding fields are nonblank."""
    return not _is_blank(mission_yaml) and not _is_blank(retried_from)


def resolve_run_registry_ownership(
    *,
    retried_from: str | None = None,
    parent_run_id: str | None = None,
) -> str | None:
    """Resolve canonical RunRegistry ownership (``retried_from``).

    Workflow materialization may speak ``parent_run_id``; RunRegistry stores
    ``retried_from``. This is the single alias-translation point: identical
    nonblank values collapse, blanks normalize to ``None``, and conflicting
    aliases fail closed.
    """
    ownership = normalize_ownership_id(retried_from)
    parent = normalize_ownership_id(parent_run_id)
    if ownership is not None and parent is not None and ownership != parent:
        raise ValueError(CONFLICT_OWNERSHIP_ALIAS_CONFLICT)
    return ownership if ownership is not None else parent


def require_reserved_binding_identity(
    mission_yaml: str | None,
    retried_from: str | None,
) -> tuple[str, str]:
    """Require nonblank mission YAML + ownership for reserved creates.

    Returns ``(mission_yaml, normalized_retried_from)``. Raises ``ValueError``
    with ``missing_binding_identity`` when either field is omitted, empty, or
    whitespace-only.
    """
    yaml_text = normalize_optional_mission_yaml(mission_yaml)
    ownership = normalize_ownership_id(retried_from)
    if yaml_text is None or ownership is None:
        raise ValueError(CONFLICT_MISSING_BINDING_IDENTITY)
    return yaml_text, ownership


def _mission_mapping(mission_yaml: str | None) -> dict[str, Any]:
    """Best-effort parse of mission YAML for conflict classification only."""
    if mission_yaml is None or str(mission_yaml).strip() == "":
        return {}
    try:
        data = yaml.safe_load(mission_yaml)
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _mapping_section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if isinstance(value, dict):
        return value
    return {}


def reserved_run_identity_matches(
    existing: RunRecord,
    *,
    mission_yaml: str | None,
    retried_from: str | None,
) -> bool:
    """True when immutable mission identity + launch metadata match exactly.

    Incomplete / blank bindings never match — legacy unowned rows cannot be
    claimed via idempotent recovery.
    """
    if not binding_identity_complete(mission_yaml, retried_from):
        return False
    if not binding_identity_complete(
        existing.mission_yaml, existing.retried_from
    ):
        return False
    return (
        existing.mission_yaml == mission_yaml
        and normalize_ownership_id(existing.retried_from)
        == normalize_ownership_id(retried_from)
    )


def classify_reserved_run_conflict(
    existing: RunRecord,
    *,
    mission_yaml: str | None,
    retried_from: str | None,
) -> str:
    """Classify why a reserved-id create cannot recover idempotently.

    Precedence favors ownership and launch-authority fields before a
    generic mission YAML mismatch. Unrelated rows (e.g. default
    ``create_run()`` allocations with no mission YAML) report
    ``existing_run_collision``.
    """
    if reserved_run_identity_matches(
        existing, mission_yaml=mission_yaml, retried_from=retried_from
    ):
        raise ValueError("identity matches; not a conflict")

    existing_incomplete = not binding_identity_complete(
        existing.mission_yaml, existing.retried_from
    )
    expected_complete = binding_identity_complete(mission_yaml, retried_from)
    if existing_incomplete and expected_complete:
        return CONFLICT_EXISTING_RUN_COLLISION
    if existing_incomplete or not expected_complete:
        # Blank / legacy / omitted identity must never be claimed.
        return CONFLICT_EXISTING_RUN_COLLISION

    if normalize_ownership_id(existing.retried_from) != normalize_ownership_id(
        retried_from
    ):
        return CONFLICT_OWNERSHIP_MISMATCH

    expected = _mission_mapping(mission_yaml)
    observed = _mission_mapping(existing.mission_yaml)

    exp_repo = _mapping_section(expected, "repository")
    obs_repo = _mapping_section(observed, "repository")
    if exp_repo.get("name") != obs_repo.get("name"):
        return CONFLICT_REPOSITORY_MISMATCH

    if expected.get("permissions") != observed.get("permissions"):
        return CONFLICT_PERMISSIONS_MISMATCH

    exp_exec = _mapping_section(expected, "execution")
    obs_exec = _mapping_section(observed, "execution")
    if exp_exec.get("agent") != obs_exec.get("agent") or exp_exec.get(
        "mode"
    ) != obs_exec.get("mode"):
        return CONFLICT_EXECUTION_MISMATCH

    if existing.mission_yaml != mission_yaml:
        return CONFLICT_MISSION_YAML_MISMATCH

    return CONFLICT_EXISTING_RUN_COLLISION


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
    keys = row.keys()
    status = RunStatus(row["status"])
    phase_raw = row["phase"] if "phase" in keys else None
    phase = _parse_phase(phase_raw, status)
    phase_started_at = (
        _parse_dt(row["phase_started_at"]) if "phase_started_at" in keys else None
    )
    if phase_started_at is None:
        phase_started_at = _parse_dt(row["started_at"]) or _parse_dt(
            row["created_at"]
        )
    heartbeat_at = (
        _parse_dt(row["heartbeat_at"]) if "heartbeat_at" in keys else None
    )
    progress = (
        deserialize_progress(row["progress_json"])
        if "progress_json" in keys
        else None
    )
    if progress is None:
        progress = platform_progress(
            step=phase.value,
            detail=f"Legacy run record in phase {phase.value}",
        )
    return RunRecord(
        run_id=row["run_id"],
        status=status,
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
            row["result_json"] if "result_json" in keys else None
        ),
        mission_yaml=row["mission_yaml"] if "mission_yaml" in keys else None,
        retried_from=row["retried_from"] if "retried_from" in keys else None,
        phase=phase,
        phase_started_at=phase_started_at,
        heartbeat_at=heartbeat_at,
        progress=progress,
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
        self._conn.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
        self._ensure_schema()
        # Phase 2C: durable notification outbox shares this registry DB.
        # Imported lazily to avoid import cycles at module load.
        self._notification_outbox = None

    @property
    def db_path(self) -> str:
        return self._db_path

    def _get_notification_outbox(self):
        """Return the Phase 2C outbox bound to this registry DB."""
        if self._notification_outbox is None:
            from mission_control.notifications import NotificationOutbox

            self._notification_outbox = NotificationOutbox(self._db_path)
        return self._notification_outbox

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
                    result_json TEXT,
                    mission_yaml TEXT,
                    retried_from TEXT,
                    phase TEXT,
                    phase_started_at TEXT,
                    heartbeat_at TEXT,
                    progress_json TEXT
                )
                """
            )
            self._conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_STARTUP_RECOVERY_LEASE_TABLE} (
                    name TEXT PRIMARY KEY,
                    owner_token TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
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
            if "mission_yaml" not in columns:
                self._conn.execute(
                    f"ALTER TABLE {_RUNS_TABLE} ADD COLUMN mission_yaml TEXT"
                )
            if "retried_from" not in columns:
                self._conn.execute(
                    f"ALTER TABLE {_RUNS_TABLE} ADD COLUMN retried_from TEXT"
                )
            if "phase" not in columns:
                self._conn.execute(
                    f"ALTER TABLE {_RUNS_TABLE} ADD COLUMN phase TEXT"
                )
            if "phase_started_at" not in columns:
                self._conn.execute(
                    f"ALTER TABLE {_RUNS_TABLE} ADD COLUMN phase_started_at TEXT"
                )
            if "heartbeat_at" not in columns:
                self._conn.execute(
                    f"ALTER TABLE {_RUNS_TABLE} ADD COLUMN heartbeat_at TEXT"
                )
            if "progress_json" not in columns:
                self._conn.execute(
                    f"ALTER TABLE {_RUNS_TABLE} ADD COLUMN progress_json TEXT"
                )
            self._conn.commit()

    def _try_acquire_startup_recovery_lease_unlocked(self) -> str | None:
        """Acquire the cross-process startup recovery lease, or return None."""
        token = f"{os.getpid()}:{uuid.uuid4()}"
        now = _utc_now()
        expires_at = now + timedelta(seconds=STARTUP_RECOVERY_LEASE_TTL_SECONDS)
        try:
            # End any implicit transaction so BEGIN IMMEDIATE can take a
            # reserved lock across processes sharing this database file.
            self._conn.commit()
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                f"""
                SELECT owner_token, expires_at
                FROM {_STARTUP_RECOVERY_LEASE_TABLE}
                WHERE name = ?
                """,
                (STARTUP_RECOVERY_LEASE_NAME,),
            ).fetchone()
            if row is not None:
                lease_expires = _parse_dt(row["expires_at"])
                if lease_expires is not None and lease_expires > now:
                    self._conn.rollback()
                    return None

            self._conn.execute(
                f"""
                INSERT INTO {_STARTUP_RECOVERY_LEASE_TABLE} (
                    name,
                    owner_token,
                    acquired_at,
                    expires_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    owner_token = excluded.owner_token,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at
                """,
                (
                    STARTUP_RECOVERY_LEASE_NAME,
                    token,
                    _format_dt(now),
                    _format_dt(expires_at),
                ),
            )
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            try:
                self._conn.rollback()
            except sqlite3.Error:
                pass
            logger.debug(
                "Startup recovery lease acquire failed for %s: %s",
                self._db_path,
                exc,
            )
            return None

        logger.info(
            "Acquired startup recovery lease for %s (owner=%s)",
            self._db_path,
            token,
        )
        return token

    def _release_startup_recovery_lease_unlocked(self, owner_token: str) -> None:
        """Release a startup recovery lease owned by ``owner_token``."""
        try:
            self._conn.commit()
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                f"""
                DELETE FROM {_STARTUP_RECOVERY_LEASE_TABLE}
                WHERE name = ? AND owner_token = ?
                """,
                (STARTUP_RECOVERY_LEASE_NAME, owner_token),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            try:
                self._conn.rollback()
            except sqlite3.Error:
                pass
            logger.warning(
                "Failed to release startup recovery lease for %s: %s",
                self._db_path,
                exc,
            )

    def _recover_interrupted_runs_unlocked(self) -> tuple[int, list[str]]:
        """Mark queued/running runs failed. Caller must hold ``self._lock``.

        Returns ``(recovered_count, recovered_run_ids)``.
        """
        now = _utc_now()
        recovered = 0
        recovered_ids: list[str] = []
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

            progress = serialize_progress(
                platform_progress(
                    step=RunPhase.FAILED.value,
                    detail="Run interrupted by service restart",
                )
            )
            self._conn.execute(
                f"""
                UPDATE {_RUNS_TABLE}
                SET status = ?,
                    completed_at = ?,
                    elapsed_seconds = ?,
                    error = ?,
                    phase = ?,
                    phase_started_at = ?,
                    heartbeat_at = ?,
                    progress_json = ?
                WHERE run_id = ?
                  AND status IN (?, ?)
                """,
                (
                    RunStatus.FAILED.value,
                    _format_dt(now),
                    elapsed_seconds,
                    INTERRUPTED_RUN_ERROR,
                    RunPhase.FAILED.value,
                    _format_dt(now),
                    _format_dt(now),
                    progress,
                    row["run_id"],
                    RunStatus.QUEUED.value,
                    RunStatus.RUNNING.value,
                ),
            )
            recovered += 1
            recovered_ids.append(row["run_id"])

        if recovered:
            self._conn.commit()
            logger.info(
                "Recovered %s interrupted run(s) from %s",
                recovered,
                self._db_path,
            )

        return recovered, recovered_ids

    def recover_interrupted_runs(self) -> int:
        """Mark queued or running runs failed after a service restart.

        Exactly one process across replicas sharing this SQLite database
        performs recovery. Non-owners skip cleanly and return ``0``. A
        time-bounded lease ensures a crashed owner cannot block recovery
        permanently.
        """
        recovered_ids: list[str] = []
        with self._lock:
            owner_token = self._try_acquire_startup_recovery_lease_unlocked()
            if owner_token is None:
                logger.info(
                    (
                        "Skipping interrupted-run recovery for %s; "
                        "another process holds the startup recovery lease"
                    ),
                    self._db_path,
                )
                return 0

            try:
                recovered, recovered_ids = (
                    self._recover_interrupted_runs_unlocked()
                )
            finally:
                self._release_startup_recovery_lease_unlocked(owner_token)

        # Phase 2C: emit recovery/terminal after releasing the registry lock
        # so the outbox connection never contends with an open write txn.
        if recovered_ids:
            outbox = self._get_notification_outbox()
            for run_id in recovered_ids:
                record = self.get_run(run_id)
                if record is None:
                    continue
                outbox.maybe_enqueue_recovery(record)
                outbox.maybe_enqueue_terminal(record)

        return len(recovered_ids)

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
                result_json,
                mission_yaml,
                retried_from,
                phase,
                phase_started_at,
                heartbeat_at,
                progress_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                result_json = excluded.result_json,
                phase = excluded.phase,
                phase_started_at = excluded.phase_started_at,
                heartbeat_at = excluded.heartbeat_at,
                progress_json = excluded.progress_json
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
                record.mission_yaml,
                record.retried_from,
                record.phase.value,
                _format_dt(record.phase_started_at),
                _format_dt(record.heartbeat_at),
                serialize_progress(record.progress),
            ),
        )
        self._conn.commit()

    def _insert_new_run_unlocked(self, record: RunRecord) -> bool:
        """Insert ``record`` once. Return False on primary-key conflict.

        Uses ``BEGIN IMMEDIATE`` so concurrent connections serialize the
        create-or-observe path. Never updates or recycles an existing row.
        """
        try:
            self._conn.commit()
            self._conn.execute("BEGIN IMMEDIATE")
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
                    result_json,
                    mission_yaml,
                    retried_from,
                    phase,
                    phase_started_at,
                    heartbeat_at,
                    progress_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    record.mission_yaml,
                    record.retried_from,
                    record.phase.value,
                    _format_dt(record.phase_started_at),
                    _format_dt(record.heartbeat_at),
                    serialize_progress(record.progress),
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            try:
                self._conn.rollback()
            except sqlite3.Error:
                pass
            return False
        except sqlite3.Error:
            try:
                self._conn.rollback()
            except sqlite3.Error:
                pass
            raise

    def _new_queued_record(
        self,
        *,
        run_id: str,
        mission_yaml: str | None,
        retried_from: str | None,
    ) -> RunRecord:
        now = _utc_now()
        return RunRecord(
            run_id=run_id,
            status=RunStatus.QUEUED,
            created_at=now,
            mission_yaml=mission_yaml,
            retried_from=retried_from,
            phase=RunPhase.QUEUED,
            phase_started_at=now,
            heartbeat_at=now,
            progress=platform_progress(
                step=RunPhase.QUEUED.value,
                detail="Waiting for execution slot",
            ),
        )

    def _log_run_created(self, record: RunRecord, *, event: str) -> None:
        with self._lock:
            keys = self._list_run_ids_unlocked()
            count = len(keys)
        logger.info(
            (
                "lifecycle run_id=%s event=%s status=%s "
                "phase=%s api_pid=%s registry_id=%s registry_count=%s "
                "registry_keys=%s"
            ),
            record.run_id,
            event,
            record.status.value,
            record.phase.value,
            os.getpid(),
            id(self),
            count,
            keys,
        )

    def _create_reserved_run(
        self,
        *,
        run_id: str,
        mission_yaml: str | None,
        retried_from: str | None,
    ) -> ReservedRunCreateResult:
        """Materialize a caller-reserved UUID without mutating existing rows."""
        try:
            canonical_id = require_canonical_run_uuid(run_id)
        except ValueError as exc:
            conflict_class = str(exc) or CONFLICT_INVALID_RUN_ID
            logger.info(
                (
                    "lifecycle run_id=%s event=reserved_run_conflict "
                    "conflict_class=%s api_pid=%s registry_id=%s"
                ),
                run_id if isinstance(run_id, str) else "<invalid>",
                conflict_class,
                os.getpid(),
                id(self),
            )
            return ReservedRunCreateResult(
                outcome=ReservedRunOutcome.CONFLICT,
                record=None,
                conflict_class=conflict_class,
            )

        try:
            bound_yaml, bound_ownership = require_reserved_binding_identity(
                mission_yaml, retried_from
            )
        except ValueError as exc:
            conflict_class = str(exc) or CONFLICT_MISSING_BINDING_IDENTITY
            logger.info(
                (
                    "lifecycle run_id=%s event=reserved_run_conflict "
                    "conflict_class=%s api_pid=%s registry_id=%s"
                ),
                canonical_id,
                conflict_class,
                os.getpid(),
                id(self),
            )
            return ReservedRunCreateResult(
                outcome=ReservedRunOutcome.CONFLICT,
                record=None,
                conflict_class=conflict_class,
            )

        record = self._new_queued_record(
            run_id=canonical_id,
            mission_yaml=bound_yaml,
            retried_from=bound_ownership,
        )
        with self._lock:
            inserted = self._insert_new_run_unlocked(record)
            if inserted:
                existing = None
            else:
                row = self._fetch_row(canonical_id)
                existing = _row_to_record(row) if row is not None else None

        if inserted:
            self._log_run_created(record, event="reserved_run_created")
            return ReservedRunCreateResult(
                outcome=ReservedRunOutcome.CREATED,
                record=record,
            )

        if existing is None:
            logger.info(
                (
                    "lifecycle run_id=%s event=reserved_run_conflict "
                    "conflict_class=%s api_pid=%s registry_id=%s"
                ),
                canonical_id,
                CONFLICT_EXISTING_RUN_COLLISION,
                os.getpid(),
                id(self),
            )
            return ReservedRunCreateResult(
                outcome=ReservedRunOutcome.CONFLICT,
                record=None,
                conflict_class=CONFLICT_EXISTING_RUN_COLLISION,
            )

        if reserved_run_identity_matches(
            existing,
            mission_yaml=bound_yaml,
            retried_from=bound_ownership,
        ):
            logger.info(
                (
                    "lifecycle run_id=%s event=reserved_run_recovered "
                    "status=%s api_pid=%s registry_id=%s"
                ),
                existing.run_id,
                existing.status.value,
                os.getpid(),
                id(self),
            )
            return ReservedRunCreateResult(
                outcome=ReservedRunOutcome.RECOVERED_IDEMPOTENTLY,
                record=existing,
            )

        conflict_class = classify_reserved_run_conflict(
            existing,
            mission_yaml=bound_yaml,
            retried_from=bound_ownership,
        )
        logger.info(
            (
                "lifecycle run_id=%s event=reserved_run_conflict "
                "conflict_class=%s api_pid=%s registry_id=%s"
            ),
            existing.run_id,
            conflict_class,
            os.getpid(),
            id(self),
        )
        return ReservedRunCreateResult(
            outcome=ReservedRunOutcome.CONFLICT,
            record=existing,
            conflict_class=conflict_class,
        )

    def create_run(
        self,
        *,
        mission_yaml: str | None = None,
        retried_from: str | None = None,
        run_id: str | None = None,
    ) -> RunRecord | ReservedRunCreateResult:
        """Create a new run in ``queued`` status.

        When ``run_id`` is omitted, allocates a fresh UUID4 and returns a
        :class:`RunRecord` (existing callers unchanged). Blank mission /
        ownership strings normalize to ``None``.

        When ``run_id`` is supplied, materializes that caller-reserved
        canonical UUID and returns a :class:`ReservedRunCreateResult`
        discriminating ``created``, ``recovered_idempotently``, or
        ``conflict``. Reserved creates require nonblank ``mission_yaml`` and
        ``retried_from``; existing rows are never overwritten or recycled.
        """
        if run_id is not None:
            return self._create_reserved_run(
                run_id=run_id,
                mission_yaml=mission_yaml,
                retried_from=retried_from,
            )

        record = self._new_queued_record(
            run_id=str(uuid.uuid4()),
            mission_yaml=normalize_optional_mission_yaml(mission_yaml),
            retried_from=normalize_ownership_id(retried_from),
        )
        with self._lock:
            self._persist_record(record)
        self._log_run_created(record, event="run_record_created")
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
        """Update run status and related timestamps.

        Terminal statuses are monotonic: once a run is completed, failed, or
        timed_out, later workers cannot regress it to a running status or
        overwrite it with a different terminal status.
        """
        notify_phase_previous: str | None = None
        notify_terminal = False
        with self._lock:
            row = self._fetch_row(run_id)
            if row is None:
                return None

            record = _row_to_record(row)
            if is_terminal_status(record.status):
                return record

            previous_phase = record.phase.value
            now = _utc_now()
            record.status = status

            if status is RunStatus.RUNNING and record.started_at is None:
                record.started_at = now
                record.heartbeat_at = now

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
                terminal_phase = (
                    RunPhase.COMPLETED
                    if status is RunStatus.COMPLETED
                    else RunPhase.FAILED
                )
                if record.phase is not terminal_phase:
                    notify_phase_previous = previous_phase
                    record.phase = terminal_phase
                    record.phase_started_at = now
                record.heartbeat_at = now
                if record.progress is None or record.progress.get("step") != (
                    terminal_phase.value
                ):
                    detail = (
                        "Run completed"
                        if terminal_phase is RunPhase.COMPLETED
                        else "Run failed"
                    )
                    record.progress = platform_progress(
                        step=terminal_phase.value,
                        detail=detail,
                    )
                notify_terminal = True

            keys = self._list_run_ids_unlocked()
            count = len(keys)
            snapshot = record
            event = (
                "final_status_update"
                if status.value in _TERMINAL_STATUSES
                else "status_update"
            )
            # Emit lifecycle instrumentation under the write lock before
            # persistence makes the status visible and before durable
            # notification enqueue (which may be delayed) can race observers.
            logger.info(
                (
                    "lifecycle run_id=%s event=%s status=%s phase=%s "
                    "api_pid=%s registry_id=%s registry_count=%s "
                    "registry_keys=%s"
                ),
                run_id,
                event,
                status.value,
                snapshot.phase.value,
                os.getpid(),
                id(self),
                count,
                keys,
            )
            if status.value in _TERMINAL_STATUSES:
                logger.info(
                    (
                        "lifecycle run_id=%s event=finished status=%s "
                        "has_error=%s api_pid=%s registry_id=%s "
                        "registry_count=%s registry_keys=%s"
                    ),
                    run_id,
                    status.value,
                    bool(snapshot.error),
                    os.getpid(),
                    id(self),
                    count,
                    keys,
                )
            self._persist_record(record)

        # Phase 2C notifications outside the registry write lock.
        outbox = self._get_notification_outbox()
        if notify_phase_previous is not None:
            outbox.maybe_enqueue_phase_change(
                snapshot, previous_phase=notify_phase_previous
            )
        if notify_terminal:
            outbox.maybe_enqueue_terminal(snapshot)

        return snapshot

    def set_phase(
        self,
        run_id: str,
        phase: RunPhase,
        *,
        progress: dict[str, str] | None = None,
    ) -> RunRecord | None:
        """Advance the authoritative platform phase for a non-terminal run.

        Terminal phases and terminal statuses cannot regress to active phases.
        """
        phase_changed = False
        previous_phase: str | None = None
        with self._lock:
            row = self._fetch_row(run_id)
            if row is None:
                return None

            record = _row_to_record(row)
            if is_terminal_status(record.status) or is_terminal_phase(
                record.phase
            ):
                # Stale worker: do not overwrite a newer terminal state.
                return record

            if phase.value in _ACTIVE_PHASES or phase.value in TERMINAL_PHASES:
                now = _utc_now()
                previous_phase = record.phase.value
                if record.phase is not phase:
                    phase_changed = True
                    record.phase = phase
                    record.phase_started_at = now
                record.heartbeat_at = now
                if progress is not None:
                    record.progress = sanitize_progress(progress)
                elif record.progress is None:
                    record.progress = platform_progress(
                        step=phase.value,
                        detail=f"Entered phase {phase.value}",
                    )
                self._persist_record(record)

            snapshot = record

        if phase_changed:
            self._get_notification_outbox().maybe_enqueue_phase_change(
                snapshot, previous_phase=previous_phase
            )

        logger.info(
            "lifecycle run_id=%s event=phase_update phase=%s",
            run_id,
            phase.value,
        )
        return snapshot

    def touch_heartbeat(self, run_id: str) -> RunRecord | None:
        """Refresh ``heartbeat_at`` while a non-terminal run is active.

        Heartbeat refreshes never enqueue notification events.
        """
        with self._lock:
            row = self._fetch_row(run_id)
            if row is None:
                return None

            record = _row_to_record(row)
            if is_terminal_status(record.status) or is_terminal_phase(
                record.phase
            ):
                return record

            record.heartbeat_at = _utc_now()
            self._persist_record(record)
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
            if self._notification_outbox is not None:
                try:
                    self._notification_outbox.close()
                except Exception:  # noqa: BLE001
                    pass
                self._notification_outbox = None
            self._conn.close()
