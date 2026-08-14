"""SQLite-backed durable workflow registry (orchestration v1).

Stores immutable workflow identities, CAS-versioned state, step records,
and auditable transition history. Child-run launch uses a durable
idempotency key plus a pre-assigned ``child_run_id`` so reconciliation
after restart cannot double-submit the same step.

This module does not enqueue Cursor executions; callers materialize the
reserved ``child_run_id`` into the existing run registry / queue via the
future ``RunRegistry.create_run(run_id=...)`` contract (see
``reserved_child_run_materialization_spec``).

Schema is versioned. Unsupported newer schema versions fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "./data/mission-control.db"
_SQLITE_BUSY_TIMEOUT_MS = 5000

_WORKFLOWS_TABLE = "workflows"
_WORKFLOW_STEPS_TABLE = "workflow_steps"
_WORKFLOW_TRANSITIONS_TABLE = "workflow_transitions"
_SCHEMA_META_TABLE = "workflow_schema_meta"

# Monotonic schema version for additive migrations.
WORKFLOW_SCHEMA_VERSION = 2

# Environment flag — disabled by default (production must not enable yet).
WORKFLOW_ORCHESTRATION_ENV = "MISSION_CONTROL_WORKFLOW_ORCHESTRATION"
WORKFLOW_RECONCILE_INTERVAL_ENV = "MISSION_CONTROL_WORKFLOW_RECONCILE_INTERVAL_SECONDS"
DEFAULT_RECONCILE_INTERVAL_SECONDS = 5.0

# Future RunRegistry.create_run(run_id=...) compatibility (not wired yet).
RESERVED_CHILD_RUN_ID_CONTRACT_VERSION = 1


class WorkflowSchemaUnsupportedError(RuntimeError):
    """Raised when the on-disk schema is newer than this binary supports."""


class WorkflowState(str, Enum):
    """Lifecycle states for a durable workflow."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    NEEDS_APPROVAL = "needs_approval"
    BLOCKED = "blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_WORKFLOW_STATES = frozenset(
    {
        WorkflowState.COMPLETED.value,
        WorkflowState.NEEDS_APPROVAL.value,
        WorkflowState.BLOCKED.value,
        WorkflowState.BUDGET_EXHAUSTED.value,
        WorkflowState.FAILED.value,
        WorkflowState.CANCELLED.value,
    }
)

# Terminal states that should produce one actionable operator alert.
ACTIONABLE_WORKFLOW_ALERT_STATES = frozenset(
    {
        WorkflowState.NEEDS_APPROVAL.value,
        WorkflowState.COMPLETED.value,
        WorkflowState.BLOCKED.value,
        WorkflowState.BUDGET_EXHAUSTED.value,
        WorkflowState.FAILED.value,
    }
)


class StepType(str, Enum):
    """Canonical v1 step kinds."""

    IMPLEMENTATION = "implementation"
    REVIEW = "review"
    FIX = "fix"
    RE_REVIEW = "re_review"


class StepStatus(str, Enum):
    """Per-step durable status."""

    PENDING = "pending"
    CLAIMED = "claimed"  # child_run_id reserved; awaiting materialization
    QUEUED = "queued"  # materialized into run registry / queue
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class StepMaterializationState(str, Enum):
    """Explicit claim vs materialization lifecycle for orphan recovery."""

    CLAIMED = "claimed"
    MATERIALIZED = "materialized"


class TransitionReason(str, Enum):
    """Machine-readable reasons recorded on audit transitions."""

    SUBMITTED = "submitted"
    CHILD_LAUNCHED = "child_launched"
    CHILD_BOUND = "child_bound"
    CHILD_STATUS = "child_status"
    POLICY_GATE = "policy_gate"
    VERDICT = "verdict"
    BUDGET = "budget"
    CANCELLED = "cancelled"
    RECONCILE = "reconcile"
    INTERVENTION = "intervention"
    ERROR = "error"
    SCHEMA_MIGRATE = "schema_migrate"


def is_terminal_workflow_state(state: WorkflowState | str) -> bool:
    if isinstance(state, WorkflowState):
        return state.value in TERMINAL_WORKFLOW_STATES
    return str(state) in TERMINAL_WORKFLOW_STATES


def is_workflow_orchestration_enabled(
    environ: dict[str, str] | None = None,
) -> bool:
    """Return True only when the feature flag is explicitly enabled."""
    env = environ if environ is not None else os.environ
    raw = str(env.get(WORKFLOW_ORCHESTRATION_ENV, "")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def resolve_reconcile_interval_seconds(
    environ: dict[str, str] | None = None,
) -> float:
    env = environ if environ is not None else os.environ
    raw = env.get(WORKFLOW_RECONCILE_INTERVAL_ENV)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_RECONCILE_INTERVAL_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_RECONCILE_INTERVAL_SECONDS
    return max(0.5, value)


def resolve_workflow_db_path() -> str:
    return os.environ.get("MISSION_CONTROL_DB_PATH", DEFAULT_DB_PATH)


def reserved_child_run_materialization_spec(
    *,
    child_run_id: str,
    mission_yaml: str,
    parent_run_id: str | None = None,
    retried_from: str | None = None,
) -> dict[str, Any]:
    """Describe reserved-id materialization for future RunRegistry wiring.

    Does **not** call ``RunRegistry.create_run``. Ownership uses the
    RunRegistry-canonical ``retried_from`` field. Workflow callers may pass
    ``parent_run_id``; alias translation lives in
    :func:`mission_control.run_registry.resolve_run_registry_ownership`.
    Conflicting ``parent_run_id`` / ``retried_from`` values fail closed.
    """
    from mission_control.run_registry import resolve_run_registry_ownership

    ownership = resolve_run_registry_ownership(
        parent_run_id=parent_run_id,
        retried_from=retried_from,
    )
    return {
        "contract_version": RESERVED_CHILD_RUN_ID_CONTRACT_VERSION,
        "run_id": child_run_id,
        "mission_yaml": mission_yaml,
        "retried_from": ownership,
        "status": "queued",
        "note": (
            "RunRegistry.create_run(run_id=..., mission_yaml=..., "
            "retried_from=...)"
        ),
    }


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


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _loads(raw: str | None, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


@dataclass(frozen=True)
class WorkflowPolicySnapshot:
    """Immutable authorization + ceiling snapshot captured at submit time.

    Merge/deploy and other destructive authorities default to deny. Never
    infer authority from mission prose — only these explicit fields.
    """

    repository_name: str
    base_branch: str
    target_branch: str
    implementation_scope: tuple[str, ...]
    allow_auto_merge: bool = False
    allow_auto_deploy: bool = False
    allow_destructive_actions: bool = False
    allow_permission_expansion: bool = False
    allow_database_migrations: bool = False
    allow_secret_changes: bool = False
    allow_scope_or_repo_changes: bool = False
    max_fix_cycles: int = 2
    max_child_runs: int = 8
    max_wall_clock_seconds: int = 6 * 60 * 60
    # Conservative unit counter until exact credits are available.
    max_credit_units: int = 8
    credit_unit_per_child_run: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository_name": self.repository_name,
            "base_branch": self.base_branch,
            "target_branch": self.target_branch,
            "implementation_scope": list(self.implementation_scope),
            "allow_auto_merge": self.allow_auto_merge,
            "allow_auto_deploy": self.allow_auto_deploy,
            "allow_destructive_actions": self.allow_destructive_actions,
            "allow_permission_expansion": self.allow_permission_expansion,
            "allow_database_migrations": self.allow_database_migrations,
            "allow_secret_changes": self.allow_secret_changes,
            "allow_scope_or_repo_changes": self.allow_scope_or_repo_changes,
            "max_fix_cycles": self.max_fix_cycles,
            "max_child_runs": self.max_child_runs,
            "max_wall_clock_seconds": self.max_wall_clock_seconds,
            "max_credit_units": self.max_credit_units,
            "credit_unit_per_child_run": self.credit_unit_per_child_run,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowPolicySnapshot:
        scope = data.get("implementation_scope") or []
        if not isinstance(scope, list):
            scope = []
        return cls(
            repository_name=str(data.get("repository_name") or ""),
            base_branch=str(data.get("base_branch") or ""),
            target_branch=str(data.get("target_branch") or ""),
            implementation_scope=tuple(str(p) for p in scope),
            allow_auto_merge=bool(data.get("allow_auto_merge", False)),
            allow_auto_deploy=bool(data.get("allow_auto_deploy", False)),
            allow_destructive_actions=bool(
                data.get("allow_destructive_actions", False)
            ),
            allow_permission_expansion=bool(
                data.get("allow_permission_expansion", False)
            ),
            allow_database_migrations=bool(
                data.get("allow_database_migrations", False)
            ),
            allow_secret_changes=bool(data.get("allow_secret_changes", False)),
            allow_scope_or_repo_changes=bool(
                data.get("allow_scope_or_repo_changes", False)
            ),
            max_fix_cycles=int(data.get("max_fix_cycles", 2)),
            max_child_runs=int(data.get("max_child_runs", 8)),
            max_wall_clock_seconds=int(
                data.get("max_wall_clock_seconds", 6 * 60 * 60)
            ),
            max_credit_units=int(data.get("max_credit_units", 8)),
            credit_unit_per_child_run=int(
                data.get("credit_unit_per_child_run", 1)
            ),
        )


@dataclass(frozen=True)
class WorkflowStepSpec:
    """Exact mission template for a step (no prose inference)."""

    step_type: StepType
    mission_yaml: str
    # Optional machine label for audit / fingerprints.
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_type": self.step_type.value,
            "mission_yaml": self.mission_yaml,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowStepSpec:
        return cls(
            step_type=StepType(str(data["step_type"])),
            mission_yaml=str(data.get("mission_yaml") or ""),
            label=(
                str(data["label"])
                if data.get("label") is not None
                else None
            ),
        )


@dataclass
class WorkflowStepRecord:
    """Durable workflow step row."""

    step_id: str
    workflow_id: str
    step_type: StepType
    status: StepStatus
    attempt: int
    cycle: int
    idempotency_key: str | None
    child_run_id: str | None
    parent_run_id: str | None
    mission_yaml: str
    policy_snapshot: dict[str, Any]
    last_decision: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    blocker_fingerprint: str | None = None
    materialization_state: StepMaterializationState = (
        StepMaterializationState.CLAIMED
    )


@dataclass
class WorkflowRecord:
    """Durable workflow row."""

    workflow_id: str
    state: WorkflowState
    version: int
    policy_snapshot: WorkflowPolicySnapshot
    step_specs: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    parent_run_id: str | None = None
    current_step_id: str | None = None
    fix_cycle_count: int = 0
    child_run_count: int = 0
    credit_units_used: int = 0
    # Reserved for future exact usage metering.
    credit_usage_actual: float | None = None
    last_decision: dict[str, Any] | None = None
    last_blocker_fingerprint: str | None = None
    error: str | None = None
    notification_emitted: bool = False


@dataclass
class WorkflowTransitionRecord:
    """Auditable state / decision transition."""

    transition_id: str
    workflow_id: str
    from_state: str
    to_state: str
    reason: str
    detail: dict[str, Any]
    created_at: datetime
    step_id: str | None = None
    child_run_id: str | None = None
    version_after: int | None = None


@dataclass
class CasResult:
    """Outcome of a compare-and-swap workflow mutation."""

    ok: bool
    workflow: WorkflowRecord | None
    conflict: bool = False
    error: str | None = None


@dataclass
class LaunchClaimResult:
    """Result of an at-most-once child launch claim."""

    ok: bool
    already_claimed: bool
    child_run_id: str | None
    idempotency_key: str | None
    workflow: WorkflowRecord | None
    step: WorkflowStepRecord | None
    conflict: bool = False
    error: str | None = None
    policy_audit: dict[str, Any] | None = None


def make_idempotency_key(
    workflow_id: str,
    step_type: StepType | str,
    cycle: int,
    attempt: int,
) -> str:
    kind = step_type.value if isinstance(step_type, StepType) else str(step_type)
    return f"{workflow_id}:{kind}:c{cycle}:a{attempt}"


class WorkflowRegistry:
    """Thread-safe SQLite workflow registry with CAS and audit history."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = os.path.abspath(
            os.path.expanduser(db_path or resolve_workflow_db_path())
        )
        self._lock = threading.RLock()
        _ensure_db_parent(self._db_path)
        self._conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            timeout=_SQLITE_BUSY_TIMEOUT_MS / 1000.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema()

    @property
    def db_path(self) -> str:
        return self._db_path

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._read_schema_version_unlocked()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _read_schema_version_unlocked(self) -> int:
        row = self._conn.execute(
            f"""
            SELECT value FROM {_SCHEMA_META_TABLE}
            WHERE key = 'schema_version'
            """
        ).fetchone()
        if row is None:
            return 0
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return 0

    def _write_schema_version_unlocked(self, version: int) -> None:
        self._conn.execute(
            f"""
            INSERT INTO {_SCHEMA_META_TABLE} (key, value)
            VALUES ('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(version),),
        )

    def _table_columns_unlocked(self, table: str) -> set[str]:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r["name"]) for r in rows}

    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_SCHEMA_META_TABLE} (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                self._conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_WORKFLOWS_TABLE} (
                        workflow_id TEXT PRIMARY KEY,
                        state TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        policy_json TEXT NOT NULL,
                        step_specs_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        started_at TEXT,
                        completed_at TEXT,
                        parent_run_id TEXT,
                        current_step_id TEXT,
                        fix_cycle_count INTEGER NOT NULL DEFAULT 0,
                        child_run_count INTEGER NOT NULL DEFAULT 0,
                        credit_units_used INTEGER NOT NULL DEFAULT 0,
                        credit_usage_actual REAL,
                        last_decision_json TEXT,
                        last_blocker_fingerprint TEXT,
                        error TEXT,
                        notification_emitted INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                self._conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_WORKFLOW_STEPS_TABLE} (
                        step_id TEXT PRIMARY KEY,
                        workflow_id TEXT NOT NULL,
                        step_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        cycle INTEGER NOT NULL,
                        idempotency_key TEXT,
                        child_run_id TEXT,
                        parent_run_id TEXT,
                        mission_yaml TEXT NOT NULL,
                        policy_json TEXT NOT NULL,
                        last_decision_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        started_at TEXT,
                        completed_at TEXT,
                        error TEXT,
                        blocker_fingerprint TEXT,
                        materialization_state TEXT NOT NULL DEFAULT 'claimed',
                        UNIQUE (idempotency_key)
                    )
                    """
                )
                self._conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_WORKFLOW_TRANSITIONS_TABLE} (
                        transition_id TEXT PRIMARY KEY,
                        workflow_id TEXT NOT NULL,
                        step_id TEXT,
                        child_run_id TEXT,
                        from_state TEXT NOT NULL,
                        to_state TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        detail_json TEXT NOT NULL,
                        version_after INTEGER,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                self._conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_workflow_steps_workflow
                    ON {_WORKFLOW_STEPS_TABLE}(workflow_id)
                    """
                )
                self._conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_workflow_transitions_workflow
                    ON {_WORKFLOW_TRANSITIONS_TABLE}(workflow_id, created_at)
                    """
                )

                current = self._read_schema_version_unlocked()
                if current > WORKFLOW_SCHEMA_VERSION:
                    self._conn.rollback()
                    raise WorkflowSchemaUnsupportedError(
                        f"workflow schema version {current} is newer than "
                        f"supported {WORKFLOW_SCHEMA_VERSION}"
                    )
                if current < 1:
                    # Fresh or pre-versioned install → treat as v1 baseline.
                    self._write_schema_version_unlocked(1)
                    current = 1
                if current < 2:
                    self._migrate_to_v2_unlocked()
                    self._write_schema_version_unlocked(2)
                    current = 2
                if current != WORKFLOW_SCHEMA_VERSION:
                    self._conn.rollback()
                    raise WorkflowSchemaUnsupportedError(
                        f"workflow schema version {current} unsupported"
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _migrate_to_v2_unlocked(self) -> None:
        """Additive v2: materialization_state + UNIQUE(child_run_id)."""
        cols = self._table_columns_unlocked(_WORKFLOW_STEPS_TABLE)
        if "materialization_state" not in cols:
            self._conn.execute(
                f"""
                ALTER TABLE {_WORKFLOW_STEPS_TABLE}
                ADD COLUMN materialization_state TEXT NOT NULL DEFAULT 'claimed'
                """
            )
        # Repair duplicate child_run_id values before UNIQUE index.
        dup_rows = self._conn.execute(
            f"""
            SELECT child_run_id, COUNT(*) AS c
            FROM {_WORKFLOW_STEPS_TABLE}
            WHERE child_run_id IS NOT NULL
            GROUP BY child_run_id
            HAVING c > 1
            """
        ).fetchall()
        for dup in dup_rows:
            child_id = dup["child_run_id"]
            rows = self._conn.execute(
                f"""
                SELECT step_id FROM {_WORKFLOW_STEPS_TABLE}
                WHERE child_run_id = ?
                ORDER BY created_at ASC, step_id ASC
                """,
                (child_id,),
            ).fetchall()
            # Keep the first; null out the rest (repair) so UNIQUE can apply.
            for row in rows[1:]:
                self._conn.execute(
                    f"""
                    UPDATE {_WORKFLOW_STEPS_TABLE}
                    SET child_run_id = NULL,
                        error = COALESCE(error, 'duplicate_child_run_id_repaired')
                    WHERE step_id = ?
                    """,
                    (row["step_id"],),
                )
        self._conn.execute(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_steps_child_run_id
            ON {_WORKFLOW_STEPS_TABLE}(child_run_id)
            """
        )
        # Backfill: legacy queued steps without materialization were claims.
        self._conn.execute(
            f"""
            UPDATE {_WORKFLOW_STEPS_TABLE}
            SET materialization_state = 'claimed'
            WHERE materialization_state IS NULL OR materialization_state = ''
            """
        )

    def _row_to_workflow(self, row: sqlite3.Row) -> WorkflowRecord:
        return WorkflowRecord(
            workflow_id=row["workflow_id"],
            state=WorkflowState(row["state"]),
            version=int(row["version"]),
            policy_snapshot=WorkflowPolicySnapshot.from_dict(
                _loads(row["policy_json"], {})
            ),
            step_specs=_loads(row["step_specs_json"], {}),
            created_at=_parse_dt(row["created_at"]) or _utc_now(),
            updated_at=_parse_dt(row["updated_at"]) or _utc_now(),
            started_at=_parse_dt(row["started_at"]),
            completed_at=_parse_dt(row["completed_at"]),
            parent_run_id=row["parent_run_id"],
            current_step_id=row["current_step_id"],
            fix_cycle_count=int(row["fix_cycle_count"] or 0),
            child_run_count=int(row["child_run_count"] or 0),
            credit_units_used=int(row["credit_units_used"] or 0),
            credit_usage_actual=row["credit_usage_actual"],
            last_decision=_loads(row["last_decision_json"], None),
            last_blocker_fingerprint=row["last_blocker_fingerprint"],
            error=row["error"],
            notification_emitted=bool(row["notification_emitted"]),
        )

    def _row_to_step(self, row: sqlite3.Row) -> WorkflowStepRecord:
        keys = row.keys()
        mat_raw = (
            row["materialization_state"]
            if "materialization_state" in keys
            else StepMaterializationState.CLAIMED.value
        )
        try:
            mat = StepMaterializationState(str(mat_raw or "claimed"))
        except ValueError:
            mat = StepMaterializationState.CLAIMED
        status_raw = row["status"]
        # Legacy rows may still say queued for unmaterialized claims.
        if (
            status_raw == StepStatus.QUEUED.value
            and mat is StepMaterializationState.CLAIMED
        ):
            # Preserve QUEUED if already materialized naming; treat as CLAIMED
            # only when materialization_state says claimed.
            status = StepStatus.CLAIMED
        else:
            status = StepStatus(status_raw)
        return WorkflowStepRecord(
            step_id=row["step_id"],
            workflow_id=row["workflow_id"],
            step_type=StepType(row["step_type"]),
            status=status,
            attempt=int(row["attempt"]),
            cycle=int(row["cycle"]),
            idempotency_key=row["idempotency_key"],
            child_run_id=row["child_run_id"],
            parent_run_id=row["parent_run_id"],
            mission_yaml=row["mission_yaml"] or "",
            policy_snapshot=_loads(row["policy_json"], {}),
            last_decision=_loads(row["last_decision_json"], None),
            created_at=_parse_dt(row["created_at"]) or _utc_now(),
            updated_at=_parse_dt(row["updated_at"]) or _utc_now(),
            started_at=_parse_dt(row["started_at"]),
            completed_at=_parse_dt(row["completed_at"]),
            error=row["error"],
            blocker_fingerprint=row["blocker_fingerprint"],
            materialization_state=mat,
        )

    def _row_to_transition(
        self, row: sqlite3.Row
    ) -> WorkflowTransitionRecord:
        return WorkflowTransitionRecord(
            transition_id=row["transition_id"],
            workflow_id=row["workflow_id"],
            from_state=row["from_state"],
            to_state=row["to_state"],
            reason=row["reason"],
            detail=_loads(row["detail_json"], {}),
            created_at=_parse_dt(row["created_at"]) or _utc_now(),
            step_id=row["step_id"],
            child_run_id=row["child_run_id"],
            version_after=(
                int(row["version_after"])
                if row["version_after"] is not None
                else None
            ),
        )

    def _fetch_workflow_unlocked(
        self, workflow_id: str
    ) -> WorkflowRecord | None:
        row = self._conn.execute(
            f"SELECT * FROM {_WORKFLOWS_TABLE} WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_workflow(row)

    def _fetch_step_unlocked(self, step_id: str) -> WorkflowStepRecord | None:
        row = self._conn.execute(
            f"SELECT * FROM {_WORKFLOW_STEPS_TABLE} WHERE step_id = ?",
            (step_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_step(row)

    def _list_steps_unlocked(
        self, workflow_id: str
    ) -> list[WorkflowStepRecord]:
        rows = self._conn.execute(
            f"""
            SELECT * FROM {_WORKFLOW_STEPS_TABLE}
            WHERE workflow_id = ?
            ORDER BY created_at ASC, step_id ASC
            """,
            (workflow_id,),
        ).fetchall()
        return [self._row_to_step(row) for row in rows]

    def _append_transition_unlocked(
        self,
        *,
        workflow_id: str,
        from_state: str,
        to_state: str,
        reason: str,
        detail: dict[str, Any],
        version_after: int | None,
        step_id: str | None = None,
        child_run_id: str | None = None,
    ) -> WorkflowTransitionRecord:
        now = _utc_now()
        record = WorkflowTransitionRecord(
            transition_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            detail=detail,
            created_at=now,
            step_id=step_id,
            child_run_id=child_run_id,
            version_after=version_after,
        )
        self._conn.execute(
            f"""
            INSERT INTO {_WORKFLOW_TRANSITIONS_TABLE} (
                transition_id, workflow_id, step_id, child_run_id,
                from_state, to_state, reason, detail_json,
                version_after, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.transition_id,
                record.workflow_id,
                record.step_id,
                record.child_run_id,
                record.from_state,
                record.to_state,
                record.reason,
                _dumps(record.detail),
                record.version_after,
                _format_dt(record.created_at),
            ),
        )
        return record

    def create_workflow(
        self,
        *,
        policy: WorkflowPolicySnapshot,
        implementation: WorkflowStepSpec,
        review: WorkflowStepSpec,
        fix: WorkflowStepSpec | None = None,
        re_review: WorkflowStepSpec | None = None,
        parent_run_id: str | None = None,
        workflow_id: str | None = None,
    ) -> WorkflowRecord:
        """Create a workflow in ``pending`` with immutable policy + templates."""
        if not policy.repository_name or not policy.target_branch:
            raise ValueError(
                "policy.repository_name and policy.target_branch are required"
            )
        if not implementation.mission_yaml.strip():
            raise ValueError("implementation mission_yaml is required")
        if not review.mission_yaml.strip():
            raise ValueError("review mission_yaml is required")
        if implementation.step_type is not StepType.IMPLEMENTATION:
            raise ValueError("implementation spec must use step_type=implementation")
        if review.step_type is not StepType.REVIEW:
            raise ValueError("review spec must use step_type=review")

        specs: dict[str, Any] = {
            "implementation": implementation.to_dict(),
            "review": review.to_dict(),
        }
        if fix is not None:
            if fix.step_type is not StepType.FIX:
                raise ValueError("fix spec must use step_type=fix")
            specs["fix"] = fix.to_dict()
        if re_review is not None:
            if re_review.step_type is not StepType.RE_REVIEW:
                raise ValueError("re_review spec must use step_type=re_review")
            specs["re_review"] = re_review.to_dict()

        now = _utc_now()
        wid = workflow_id or str(uuid.uuid4())
        record = WorkflowRecord(
            workflow_id=wid,
            state=WorkflowState.PENDING,
            version=1,
            policy_snapshot=policy,
            step_specs=specs,
            created_at=now,
            updated_at=now,
            parent_run_id=parent_run_id,
            last_decision={"action": "submitted"},
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    f"""
                    INSERT INTO {_WORKFLOWS_TABLE} (
                        workflow_id, state, version, policy_json,
                        step_specs_json, created_at, updated_at,
                        started_at, completed_at, parent_run_id,
                        current_step_id, fix_cycle_count, child_run_count,
                        credit_units_used, credit_usage_actual,
                        last_decision_json, last_blocker_fingerprint,
                        error, notification_emitted
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL,
                        0, 0, 0, NULL, ?, NULL, NULL, 0
                    )
                    """,
                    (
                        record.workflow_id,
                        record.state.value,
                        record.version,
                        _dumps(policy.to_dict()),
                        _dumps(specs),
                        _format_dt(record.created_at),
                        _format_dt(record.updated_at),
                        parent_run_id,
                        _dumps(record.last_decision),
                    ),
                )
                self._append_transition_unlocked(
                    workflow_id=wid,
                    from_state=WorkflowState.PENDING.value,
                    to_state=WorkflowState.PENDING.value,
                    reason=TransitionReason.SUBMITTED.value,
                    detail={"action": "submitted"},
                    version_after=1,
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        logger.info(
            "workflow event=created workflow_id=%s state=%s version=%s",
            wid,
            record.state.value,
            record.version,
        )
        return record

    def get_workflow(self, workflow_id: str) -> WorkflowRecord | None:
        with self._lock:
            return self._fetch_workflow_unlocked(workflow_id)

    def list_steps(self, workflow_id: str) -> list[WorkflowStepRecord]:
        with self._lock:
            return self._list_steps_unlocked(workflow_id)

    def get_step(self, step_id: str) -> WorkflowStepRecord | None:
        with self._lock:
            return self._fetch_step_unlocked(step_id)

    def get_step_by_child_run(
        self, child_run_id: str
    ) -> WorkflowStepRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT * FROM {_WORKFLOW_STEPS_TABLE}
                WHERE child_run_id = ?
                """,
                (child_run_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_step(row)

    def list_active_workflows(self) -> list[WorkflowRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM {_WORKFLOWS_TABLE}
                WHERE state IN (?, ?)
                ORDER BY created_at ASC
                """,
                (WorkflowState.PENDING.value, WorkflowState.RUNNING.value),
            ).fetchall()
            return [self._row_to_workflow(row) for row in rows]

    def get_history(
        self, workflow_id: str
    ) -> list[WorkflowTransitionRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM {_WORKFLOW_TRANSITIONS_TABLE}
                WHERE workflow_id = ?
                ORDER BY created_at ASC, transition_id ASC
                """,
                (workflow_id,),
            ).fetchall()
            return [self._row_to_transition(row) for row in rows]

    def claim_child_launch(
        self,
        *,
        workflow_id: str,
        expected_version: int,
        step_type: StepType,
        mission_yaml: str,
        cycle: int,
        attempt: int,
        parent_run_id: str | None,
        idempotency_key: str | None = None,
        decision: dict[str, Any] | None = None,
        policy_gate: Callable[..., tuple[str | None, dict[str, Any]]]
        | None = None,
    ) -> LaunchClaimResult:
        """Reserve a child run id under CAS + durable idempotency key.

        Restart-safe: repeating the same key returns the same
        ``child_run_id`` without creating another step.

        Policy gates run inside the same ``BEGIN IMMEDIATE`` transaction
        immediately before the claim write; audit evidence is persisted.
        """
        # Import lazily to avoid circular import at module load.
        if policy_gate is None:
            from mission_control.workflow_orchestrator import (
                enforce_launch_policy_gates,
            )

            policy_gate = enforce_launch_policy_gates

        key = idempotency_key or make_idempotency_key(
            workflow_id, step_type, cycle, attempt
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                workflow = self._fetch_workflow_unlocked(workflow_id)
                if workflow is None:
                    self._conn.rollback()
                    return LaunchClaimResult(
                        ok=False,
                        already_claimed=False,
                        child_run_id=None,
                        idempotency_key=key,
                        workflow=None,
                        step=None,
                        error="workflow_not_found",
                    )
                if is_terminal_workflow_state(workflow.state):
                    self._conn.rollback()
                    return LaunchClaimResult(
                        ok=False,
                        already_claimed=False,
                        child_run_id=None,
                        idempotency_key=key,
                        workflow=workflow,
                        step=None,
                        error="workflow_terminal",
                    )
                if workflow.version != expected_version:
                    self._conn.rollback()
                    return LaunchClaimResult(
                        ok=False,
                        already_claimed=False,
                        child_run_id=None,
                        idempotency_key=key,
                        workflow=workflow,
                        step=None,
                        conflict=True,
                        error="version_conflict",
                    )

                existing = self._conn.execute(
                    f"""
                    SELECT * FROM {_WORKFLOW_STEPS_TABLE}
                    WHERE idempotency_key = ?
                    """,
                    (key,),
                ).fetchone()
                if existing is not None:
                    step = self._row_to_step(existing)
                    self._conn.commit()
                    return LaunchClaimResult(
                        ok=True,
                        already_claimed=True,
                        child_run_id=step.child_run_id,
                        idempotency_key=key,
                        workflow=workflow,
                        step=step,
                    )

                # Budget ceilings at the claim boundary (same semantics as
                # orchestrator): child_run_count >= max OR credit overflow.
                policy = workflow.policy_snapshot
                next_children = workflow.child_run_count + 1
                next_credits = (
                    workflow.credit_units_used
                    + int(policy.credit_unit_per_child_run)
                )
                if next_children > policy.max_child_runs or (
                    next_credits > policy.max_credit_units
                ):
                    self._conn.rollback()
                    return LaunchClaimResult(
                        ok=False,
                        already_claimed=False,
                        child_run_id=None,
                        idempotency_key=key,
                        workflow=workflow,
                        step=None,
                        error="budget_ceiling",
                    )
                if (
                    workflow.credit_usage_actual is not None
                    and float(workflow.credit_usage_actual)
                    >= float(policy.max_credit_units)
                ):
                    self._conn.rollback()
                    return LaunchClaimResult(
                        ok=False,
                        already_claimed=False,
                        child_run_id=None,
                        idempotency_key=key,
                        workflow=workflow,
                        step=None,
                        error="budget_ceiling_actual",
                    )

                denial, policy_audit = policy_gate(
                    policy=workflow.policy_snapshot,
                    step_type=step_type,
                    mission_yaml=mission_yaml,
                )
                if denial:
                    # Persist audit evidence without claiming a child.
                    now = _utc_now()
                    new_version = workflow.version + 1
                    cur = self._conn.execute(
                        f"""
                        UPDATE {_WORKFLOWS_TABLE}
                        SET state = ?,
                            version = ?,
                            updated_at = ?,
                            completed_at = ?,
                            error = ?,
                            last_decision_json = ?
                        WHERE workflow_id = ? AND version = ?
                        """,
                        (
                            WorkflowState.BLOCKED.value,
                            new_version,
                            _format_dt(now),
                            _format_dt(now),
                            denial,
                            _dumps(
                                {
                                    "action": "policy_violation",
                                    "policy_audit": policy_audit,
                                }
                            ),
                            workflow_id,
                            expected_version,
                        ),
                    )
                    if int(cur.rowcount or 0) < 1:
                        self._conn.rollback()
                        latest = self._fetch_workflow_unlocked(workflow_id)
                        return LaunchClaimResult(
                            ok=False,
                            already_claimed=False,
                            child_run_id=None,
                            idempotency_key=key,
                            workflow=latest,
                            step=None,
                            conflict=True,
                            error="version_conflict",
                            policy_audit=policy_audit,
                        )
                    self._append_transition_unlocked(
                        workflow_id=workflow_id,
                        from_state=workflow.state.value,
                        to_state=WorkflowState.BLOCKED.value,
                        reason=TransitionReason.POLICY_GATE.value,
                        detail={
                            "cause": denial,
                            "policy_audit": policy_audit,
                        },
                        version_after=new_version,
                    )
                    self._conn.commit()
                    latest = self._fetch_workflow_unlocked(workflow_id)
                    return LaunchClaimResult(
                        ok=False,
                        already_claimed=False,
                        child_run_id=None,
                        idempotency_key=key,
                        workflow=latest,
                        step=None,
                        error=denial,
                        policy_audit=policy_audit,
                    )

                now = _utc_now()
                step_id = str(uuid.uuid4())
                child_run_id = str(uuid.uuid4())
                policy_json = _dumps(workflow.policy_snapshot.to_dict())
                decision_payload = dict(decision or {})
                decision_payload.setdefault("action", "launch_child")
                decision_payload.setdefault("step_type", step_type.value)
                decision_payload["policy_audit"] = policy_audit
                # Compatibility payload for future create_run(run_id=...).
                decision_payload["reserved_run"] = (
                    reserved_child_run_materialization_spec(
                        child_run_id=child_run_id,
                        mission_yaml=mission_yaml,
                        parent_run_id=parent_run_id,
                    )
                )
                new_version = workflow.version + 1
                credit_delta = int(
                    workflow.policy_snapshot.credit_unit_per_child_run
                )
                new_child_count = workflow.child_run_count + 1
                new_credits = workflow.credit_units_used + credit_delta
                started_at = workflow.started_at or now
                state = WorkflowState.RUNNING

                self._conn.execute(
                    f"""
                    INSERT INTO {_WORKFLOW_STEPS_TABLE} (
                        step_id, workflow_id, step_type, status, attempt,
                        cycle, idempotency_key, child_run_id, parent_run_id,
                        mission_yaml, policy_json, last_decision_json,
                        created_at, updated_at, started_at, completed_at,
                        error, blocker_fingerprint, materialization_state
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL,
                        NULL, NULL, ?
                    )
                    """,
                    (
                        step_id,
                        workflow_id,
                        step_type.value,
                        StepStatus.CLAIMED.value,
                        attempt,
                        cycle,
                        key,
                        child_run_id,
                        parent_run_id,
                        mission_yaml,
                        policy_json,
                        _dumps(decision_payload),
                        _format_dt(now),
                        _format_dt(now),
                        _format_dt(now),
                        StepMaterializationState.CLAIMED.value,
                    ),
                )
                cur = self._conn.execute(
                    f"""
                    UPDATE {_WORKFLOWS_TABLE}
                    SET state = ?,
                        version = ?,
                        updated_at = ?,
                        started_at = ?,
                        current_step_id = ?,
                        child_run_count = ?,
                        credit_units_used = ?,
                        last_decision_json = ?
                    WHERE workflow_id = ? AND version = ?
                    """,
                    (
                        state.value,
                        new_version,
                        _format_dt(now),
                        _format_dt(started_at),
                        step_id,
                        new_child_count,
                        new_credits,
                        _dumps(decision_payload),
                        workflow_id,
                        expected_version,
                    ),
                )
                if int(cur.rowcount or 0) < 1:
                    self._conn.rollback()
                    latest = self._fetch_workflow_unlocked(workflow_id)
                    return LaunchClaimResult(
                        ok=False,
                        already_claimed=False,
                        child_run_id=None,
                        idempotency_key=key,
                        workflow=latest,
                        step=None,
                        conflict=True,
                        error="version_conflict",
                    )
                self._append_transition_unlocked(
                    workflow_id=workflow_id,
                    from_state=workflow.state.value,
                    to_state=state.value,
                    reason=TransitionReason.CHILD_LAUNCHED.value,
                    detail={
                        "step_type": step_type.value,
                        "idempotency_key": key,
                        "child_run_id": child_run_id,
                        "cycle": cycle,
                        "attempt": attempt,
                        "materialization_state": (
                            StepMaterializationState.CLAIMED.value
                        ),
                        "policy_audit": policy_audit,
                    },
                    version_after=new_version,
                    step_id=step_id,
                    child_run_id=child_run_id,
                )
                self._conn.commit()
                workflow = self._fetch_workflow_unlocked(workflow_id)
                step = self._fetch_step_unlocked(step_id)
                logger.info(
                    (
                        "workflow event=child_launch_claimed workflow_id=%s "
                        "step_id=%s child_run_id=%s step_type=%s "
                        "idempotency_key=%s version=%s"
                    ),
                    workflow_id,
                    step_id,
                    child_run_id,
                    step_type.value,
                    key,
                    new_version,
                )
                return LaunchClaimResult(
                    ok=True,
                    already_claimed=False,
                    child_run_id=child_run_id,
                    idempotency_key=key,
                    workflow=workflow,
                    step=step,
                    policy_audit=policy_audit,
                )
            except Exception:
                self._conn.rollback()
                raise

    def mark_step_materialized(
        self,
        *,
        workflow_id: str,
        expected_version: int,
        step_id: str,
        child_status: str = "queued",
    ) -> CasResult:
        """Mark a claimed step as materialized into the run registry.

        Compare-and-swap on both workflow ``version`` and step
        ``materialization_state='claimed'`` so concurrent materializers
        cannot double-bind the same logical child. Already-materialized
        steps return ``ok=False`` / ``error='already_materialized'``
        without bumping the workflow version.
        """
        status = (
            StepStatus.RUNNING
            if child_status == "running"
            else StepStatus.QUEUED
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                workflow = self._fetch_workflow_unlocked(workflow_id)
                if workflow is None:
                    self._conn.rollback()
                    return CasResult(
                        ok=False, workflow=None, error="workflow_not_found"
                    )
                if workflow.version != expected_version:
                    self._conn.rollback()
                    return CasResult(
                        ok=False,
                        workflow=workflow,
                        conflict=True,
                        error="version_conflict",
                    )
                if is_terminal_workflow_state(workflow.state):
                    self._conn.rollback()
                    return CasResult(
                        ok=False,
                        workflow=workflow,
                        error="workflow_terminal",
                    )

                step = self._fetch_step_unlocked(step_id)
                if step is None or step.workflow_id != workflow_id:
                    self._conn.rollback()
                    return CasResult(
                        ok=False,
                        workflow=workflow,
                        error="step_not_found",
                    )
                if (
                    step.materialization_state
                    is StepMaterializationState.MATERIALIZED
                ):
                    self._conn.rollback()
                    return CasResult(
                        ok=False,
                        workflow=workflow,
                        error="already_materialized",
                    )
                if (
                    step.materialization_state
                    is not StepMaterializationState.CLAIMED
                    or step.status is not StepStatus.CLAIMED
                ):
                    self._conn.rollback()
                    return CasResult(
                        ok=False,
                        workflow=workflow,
                        error="not_claimed",
                    )

                now = _utc_now()
                new_version = workflow.version + 1
                cur = self._conn.execute(
                    f"""
                    UPDATE {_WORKFLOW_STEPS_TABLE}
                    SET status = ?,
                        materialization_state = ?,
                        updated_at = ?
                    WHERE step_id = ?
                      AND materialization_state = ?
                      AND status = ?
                    """,
                    (
                        status.value,
                        StepMaterializationState.MATERIALIZED.value,
                        _format_dt(now),
                        step_id,
                        StepMaterializationState.CLAIMED.value,
                        StepStatus.CLAIMED.value,
                    ),
                )
                if int(cur.rowcount or 0) < 1:
                    self._conn.rollback()
                    latest = self._fetch_workflow_unlocked(workflow_id)
                    return CasResult(
                        ok=False,
                        workflow=latest,
                        conflict=True,
                        error="already_materialized",
                    )

                wcur = self._conn.execute(
                    f"""
                    UPDATE {_WORKFLOWS_TABLE}
                    SET state = ?,
                        version = ?,
                        updated_at = ?,
                        last_decision_json = ?
                    WHERE workflow_id = ? AND version = ?
                    """,
                    (
                        WorkflowState.RUNNING.value,
                        new_version,
                        _format_dt(now),
                        _dumps(
                            {
                                "action": "child_bound",
                                "materialization_state": (
                                    StepMaterializationState.MATERIALIZED.value
                                ),
                                "child_status": child_status,
                            }
                        ),
                        workflow_id,
                        expected_version,
                    ),
                )
                if int(wcur.rowcount or 0) < 1:
                    self._conn.rollback()
                    latest = self._fetch_workflow_unlocked(workflow_id)
                    return CasResult(
                        ok=False,
                        workflow=latest,
                        conflict=True,
                        error="version_conflict",
                    )

                self._append_transition_unlocked(
                    workflow_id=workflow_id,
                    from_state=workflow.state.value,
                    to_state=WorkflowState.RUNNING.value,
                    reason=TransitionReason.CHILD_BOUND.value,
                    detail={
                        "materialization_state": (
                            StepMaterializationState.MATERIALIZED.value
                        ),
                        "child_status": child_status,
                    },
                    version_after=new_version,
                    step_id=step_id,
                    child_run_id=step.child_run_id,
                )
                self._conn.commit()
                updated = self._fetch_workflow_unlocked(workflow_id)
                logger.info(
                    (
                        "workflow event=child_bound workflow_id=%s "
                        "step_id=%s child_run_id=%s version=%s"
                    ),
                    workflow_id,
                    step_id,
                    step.child_run_id,
                    new_version,
                )
                return CasResult(ok=True, workflow=updated)
            except Exception:
                self._conn.rollback()
                raise


    def apply_cas_transition(
        self,
        *,
        workflow_id: str,
        expected_version: int,
        to_state: WorkflowState,
        reason: TransitionReason | str,
        detail: dict[str, Any] | None = None,
        step_updates: dict[str, Any] | None = None,
        workflow_updates: dict[str, Any] | None = None,
        step_id: str | None = None,
        child_run_id: str | None = None,
    ) -> CasResult:
        """Atomically advance workflow state when ``version`` matches."""
        reason_value = (
            reason.value if isinstance(reason, TransitionReason) else str(reason)
        )
        detail = detail or {}
        workflow_updates = dict(workflow_updates or {})
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                workflow = self._fetch_workflow_unlocked(workflow_id)
                if workflow is None:
                    self._conn.rollback()
                    return CasResult(
                        ok=False, workflow=None, error="workflow_not_found"
                    )
                if workflow.version != expected_version:
                    self._conn.rollback()
                    return CasResult(
                        ok=False,
                        workflow=workflow,
                        conflict=True,
                        error="version_conflict",
                    )
                if (
                    is_terminal_workflow_state(workflow.state)
                    and to_state is not workflow.state
                ):
                    # Allow cancel only from non-terminal; reject other mutations.
                    if workflow.state is not WorkflowState.CANCELLED:
                        self._conn.rollback()
                        return CasResult(
                            ok=False,
                            workflow=workflow,
                            error="workflow_terminal",
                        )

                now = _utc_now()
                new_version = workflow.version + 1
                completed_at = workflow.completed_at
                if is_terminal_workflow_state(to_state) and completed_at is None:
                    completed_at = now

                last_decision = workflow_updates.pop(
                    "last_decision", detail or workflow.last_decision
                )
                error = workflow_updates.pop("error", workflow.error)
                fix_cycle_count = workflow_updates.pop(
                    "fix_cycle_count", workflow.fix_cycle_count
                )
                last_blocker = workflow_updates.pop(
                    "last_blocker_fingerprint",
                    workflow.last_blocker_fingerprint,
                )
                notification_emitted = workflow_updates.pop(
                    "notification_emitted",
                    workflow.notification_emitted,
                )
                current_step_id = workflow_updates.pop(
                    "current_step_id", workflow.current_step_id
                )
                credit_usage_actual = workflow_updates.pop(
                    "credit_usage_actual", workflow.credit_usage_actual
                )

                cur = self._conn.execute(
                    f"""
                    UPDATE {_WORKFLOWS_TABLE}
                    SET state = ?,
                        version = ?,
                        updated_at = ?,
                        completed_at = ?,
                        current_step_id = ?,
                        fix_cycle_count = ?,
                        last_decision_json = ?,
                        last_blocker_fingerprint = ?,
                        error = ?,
                        notification_emitted = ?,
                        credit_usage_actual = ?
                    WHERE workflow_id = ? AND version = ?
                    """,
                    (
                        to_state.value,
                        new_version,
                        _format_dt(now),
                        _format_dt(completed_at),
                        current_step_id,
                        int(fix_cycle_count),
                        _dumps(last_decision) if last_decision is not None else None,
                        last_blocker,
                        error,
                        1 if notification_emitted else 0,
                        credit_usage_actual,
                        workflow_id,
                        expected_version,
                    ),
                )
                if int(cur.rowcount or 0) < 1:
                    self._conn.rollback()
                    latest = self._fetch_workflow_unlocked(workflow_id)
                    return CasResult(
                        ok=False,
                        workflow=latest,
                        conflict=True,
                        error="version_conflict",
                    )

                if step_id and step_updates:
                    self._apply_step_updates_unlocked(
                        step_id=step_id,
                        updates=step_updates,
                        now=now,
                    )

                self._append_transition_unlocked(
                    workflow_id=workflow_id,
                    from_state=workflow.state.value,
                    to_state=to_state.value,
                    reason=reason_value,
                    detail=detail,
                    version_after=new_version,
                    step_id=step_id,
                    child_run_id=child_run_id,
                )
                self._conn.commit()
                updated = self._fetch_workflow_unlocked(workflow_id)
                logger.info(
                    (
                        "workflow event=transition workflow_id=%s "
                        "from=%s to=%s reason=%s version=%s"
                    ),
                    workflow_id,
                    workflow.state.value,
                    to_state.value,
                    reason_value,
                    new_version,
                )
                return CasResult(ok=True, workflow=updated)
            except Exception:
                self._conn.rollback()
                raise

    def _apply_step_updates_unlocked(
        self,
        *,
        step_id: str,
        updates: dict[str, Any],
        now: datetime,
    ) -> None:
        step = self._fetch_step_unlocked(step_id)
        if step is None:
            return
        status = updates.get("status", step.status)
        if isinstance(status, StepStatus):
            status_value = status.value
        else:
            status_value = str(status)
        error = updates.get("error", step.error)
        last_decision = updates.get("last_decision", step.last_decision)
        blocker = updates.get("blocker_fingerprint", step.blocker_fingerprint)
        mat = updates.get("materialization_state", step.materialization_state)
        if isinstance(mat, StepMaterializationState):
            mat_value = mat.value
        else:
            mat_value = str(mat)
        started_at = step.started_at
        completed_at = step.completed_at
        if status_value == StepStatus.RUNNING.value and started_at is None:
            started_at = now
        if status_value in {
            StepStatus.COMPLETED.value,
            StepStatus.FAILED.value,
            StepStatus.TIMED_OUT.value,
            StepStatus.CANCELLED.value,
        }:
            completed_at = completed_at or now
        self._conn.execute(
            f"""
            UPDATE {_WORKFLOW_STEPS_TABLE}
            SET status = ?,
                updated_at = ?,
                started_at = ?,
                completed_at = ?,
                error = ?,
                last_decision_json = ?,
                blocker_fingerprint = ?,
                materialization_state = ?
            WHERE step_id = ?
            """,
            (
                status_value,
                _format_dt(now),
                _format_dt(started_at),
                _format_dt(completed_at),
                error,
                _dumps(last_decision) if last_decision is not None else None,
                blocker,
                mat_value,
                step_id,
            ),
        )

    def sync_step_from_child_status(
        self,
        *,
        workflow_id: str,
        expected_version: int,
        step_id: str,
        child_status: str,
        error: str | None = None,
    ) -> CasResult:
        """Mark a step running/terminal from child run status (CAS)."""
        status_map = {
            "queued": StepStatus.QUEUED,
            "running": StepStatus.RUNNING,
            "completed": StepStatus.COMPLETED,
            "failed": StepStatus.FAILED,
            "timed_out": StepStatus.TIMED_OUT,
            "cancelled": StepStatus.CANCELLED,
        }
        step_status = status_map.get(child_status)
        if step_status is None:
            return CasResult(
                ok=False,
                workflow=self.get_workflow(workflow_id),
                error="unknown_child_status",
            )
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            return CasResult(ok=False, workflow=None, error="workflow_not_found")
        to_state = workflow.state
        if to_state is WorkflowState.PENDING:
            to_state = WorkflowState.RUNNING
        step_updates: dict[str, Any] = {
            "status": step_status,
            "error": error,
            "last_decision": {"child_status": child_status},
        }
        if child_status in {"queued", "running"}:
            step_updates["materialization_state"] = (
                StepMaterializationState.MATERIALIZED
            )
        return self.apply_cas_transition(
            workflow_id=workflow_id,
            expected_version=expected_version,
            to_state=to_state,
            reason=TransitionReason.CHILD_STATUS,
            detail={"child_status": child_status},
            step_id=step_id,
            step_updates=step_updates,
            child_run_id=None,
        )

    def cancel_workflow(
        self,
        workflow_id: str,
        *,
        expected_version: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> CasResult:
        """Cancel a non-terminal workflow (CAS when version supplied)."""
        with self._lock:
            workflow = self._fetch_workflow_unlocked(workflow_id)
        if workflow is None:
            return CasResult(ok=False, workflow=None, error="workflow_not_found")
        if is_terminal_workflow_state(workflow.state):
            return CasResult(
                ok=False, workflow=workflow, error="workflow_terminal"
            )
        version = (
            expected_version
            if expected_version is not None
            else workflow.version
        )
        return self.apply_cas_transition(
            workflow_id=workflow_id,
            expected_version=version,
            to_state=WorkflowState.CANCELLED,
            reason=TransitionReason.CANCELLED,
            detail=detail or {"action": "cancel"},
            workflow_updates={
                "last_decision": {"action": "cancel"},
                "error": "cancelled_by_operator",
            },
        )

    def mark_notification_emitted(
        self,
        workflow_id: str,
        *,
        expected_version: int,
    ) -> CasResult:
        """Record that the single actionable workflow alert was emitted."""
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            return CasResult(ok=False, workflow=None, error="workflow_not_found")
        return self.apply_cas_transition(
            workflow_id=workflow_id,
            expected_version=expected_version,
            to_state=workflow.state,
            reason=TransitionReason.RECONCILE,
            detail={"notification_emitted": True},
            workflow_updates={"notification_emitted": True},
        )

    def set_credit_usage_actual(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        credit_usage_actual: float,
    ) -> CasResult:
        """Record exact credit usage for actual-credit ceiling enforcement."""
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            return CasResult(ok=False, workflow=None, error="workflow_not_found")
        return self.apply_cas_transition(
            workflow_id=workflow_id,
            expected_version=expected_version,
            to_state=workflow.state,
            reason=TransitionReason.BUDGET,
            detail={"credit_usage_actual": credit_usage_actual},
            workflow_updates={"credit_usage_actual": float(credit_usage_actual)},
        )
