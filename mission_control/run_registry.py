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
# Quarantined legacy attribution. Must never be written to runs.error.
# Retained only so detectors/tests can identify stale binaries or forks.
INTERRUPTED_RUN_ERROR = "Run interrupted by service restart."
OWNER_LOST_RUN_ERROR = "Run execution owner lost; lease expired."
# Distinct refusal string when a writer attempts the quarantined legacy error.
LEGACY_INTERRUPT_REFUSED_ERROR = (
    "Platform refused legacy service-restart interruption attribution."
)

# Bumped when owner-loss terminalization / provenance guards change.
# Logged at API startup so deployed-image vs source mismatches are visible.
STARTUP_RECOVERY_POLICY_VERSION = "owner-lost-lease-v2"
_PROCESS_INSTANCE_ID = f"{os.getpid()}:{uuid.uuid4().hex[:12]}"

_RUNS_TABLE = "runs"
_STARTUP_RECOVERY_LEASE_TABLE = "startup_recovery_leases"
STARTUP_RECOVERY_LEASE_NAME = "interrupted_run_recovery"
STARTUP_RECOVERY_LEASE_TTL_SECONDS = 30
_SQLITE_BUSY_TIMEOUT_MS = 5000

# Live observability: heartbeat cadence while agent execution is active.
HEARTBEAT_INTERVAL_SECONDS = 5.0
# Bounded grace after last heartbeat / claim before a running run may be
# terminalized as owner-lost. Aligns with HEARTBEAT_STALE_THRESHOLD_SECONDS.
# Must stay strictly greater than HEARTBEAT_INTERVAL_SECONDS so a healthy
# owner refreshing on the normal cadence is never owner-lost by recovery.
EXECUTION_LEASE_GRACE_SECONDS = HEARTBEAT_INTERVAL_SECONDS * 18.0  # 90s
# Mild replica/NTP skew: heartbeats up to this far in the future normalize to
# "fresh" (age 0). Beyond this, the clock is fail-safe rejected so corrupt
# far-future values cannot pin dead work indefinitely, while small skew cannot
# false-terminalize a healthy owner.
EXECUTION_LEASE_FUTURE_SKEW_TOLERANCE_SECONDS = 30.0
# Observed post-hotfix false-interrupt window (run 019bf53f…); must remain
# strictly less than EXECUTION_LEASE_GRACE_SECONDS.
OBSERVED_FALSE_INTERRUPT_SECONDS = 30.0

# Bounded platform-authored progress (never raw model output / secrets).
_PROGRESS_ALLOWED_KEYS = frozenset({"step", "detail"})
_PROGRESS_PROVENANCE_KEY = "provenance"
_PROGRESS_STEP_MAX_LEN = 64
_PROGRESS_DETAIL_MAX_LEN = 160
_PROVENANCE_VALUE_MAX_LEN = 96
_PROVENANCE_ALLOWED_KEYS = frozenset(
    {
        "event",
        "source",
        "execution_owner",
        "process_instance_id",
        "build_marker",
        "policy_version",
    }
)
_SECRETISH_RE = re.compile(
    r"(?i)(token|secret|password|api[_-]?key|authorization|bearer)|"
    r"[A-Za-z0-9_-]{24,}"
)
# Durable build provenance: full git SHA or documented short (7–12) hex only.
_COMMIT_SHA_FULL_RE = re.compile(r"^[0-9a-f]{40}$")
_COMMIT_SHA_SHORT_RE = re.compile(r"^[0-9a-f]{7,12}$")
_BUILD_FINGERPRINT_LEN = 12
_BUILD_MARKER_UNKNOWN = "unknown"
_BUILD_MARKER_ENV_KEYS = (
    "RAILWAY_GIT_COMMIT_SHA",
    "RAILWAY_GIT_COMMIT",
    "GIT_COMMIT_SHA",
    "MISSION_CONTROL_BUILD_SHA",
)
# Shared SQLite invariant: refuse quarantined legacy interrupt attribution.
_LEGACY_INTERRUPT_INSERT_TRIGGER = "runs_block_legacy_interrupt_insert"
_LEGACY_INTERRUPT_UPDATE_TRIGGER = "runs_block_legacy_interrupt_update"
_LEGACY_INTERRUPT_SQL_CANON = INTERRUPTED_RUN_ERROR.replace("'", "''")


def _python_strip_whitespace_codepoints() -> tuple[int, ...]:
    """Code points removed by ``str.strip()`` (Unicode whitespace / isspace).

    BOM (U+FEFF) and zero-width lookalikes are intentionally excluded: Python
    ``str.strip()`` does not treat them as whitespace, so SQLite must not either.
    """
    return tuple(i for i in range(0x110000) if chr(i).isspace())


def _sql_trim_chars_expr(codepoints: tuple[int, ...]) -> str:
    """Deterministic SQLite ``trim`` second-arg expression from code points."""
    return "char(" + ", ".join(str(cp) for cp in codepoints) + ")"


# Single canonical list: application strip() and SQLite triggers share policy.
_PYTHON_STRIP_WHITESPACE_CODEPOINTS = _python_strip_whitespace_codepoints()
_SQL_STRIP_CHARS = _sql_trim_chars_expr(_PYTHON_STRIP_WHITESPACE_CODEPOINTS)

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


def sanitize_build_fingerprint(value: object | None) -> str | None:
    """Accept only validated git SHA material; return a bounded non-secret fingerprint.

    Full 40-hex SHAs are shortened to the first 12 hex characters. The documented
    short fallback accepts 7–12 hex characters as-is. Arbitrary / high-entropy
    values are rejected so they never persist as build markers.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if _COMMIT_SHA_FULL_RE.fullmatch(text):
        return text[:_BUILD_FINGERPRINT_LEN]
    if _COMMIT_SHA_SHORT_RE.fullmatch(text):
        return text
    return None


def deployed_build_marker() -> str | None:
    """Return a validated deploy fingerprint when the host provides a real git SHA."""
    for key in _BUILD_MARKER_ENV_KEYS:
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        fingerprint = sanitize_build_fingerprint(raw)
        if fingerprint is not None:
            return fingerprint
    return None


def bound_terminal_provenance(
    value: object | None,
) -> dict[str, str] | None:
    """Allowlist and bound platform terminal-mutation provenance."""
    if not isinstance(value, dict):
        return None
    cleaned: dict[str, str] = {}
    for key in _PROVENANCE_ALLOWED_KEYS:
        raw = value.get(key)
        if raw is None:
            continue
        if key == "build_marker":
            text = str(raw).strip()
            if text == _BUILD_MARKER_UNKNOWN:
                cleaned[key] = _BUILD_MARKER_UNKNOWN
                continue
            fingerprint = sanitize_build_fingerprint(text)
            if fingerprint is not None:
                cleaned[key] = fingerprint
            # Invalid / high-entropy markers are dropped (not secret-redacted).
            continue
        text = _bound_text(_redact_progress_text(str(raw)), _PROVENANCE_VALUE_MAX_LEN)
        if text:
            cleaned[key] = text
    return cleaned or None


def build_terminal_provenance(
    *,
    event: str,
    source: str,
    execution_owner: str | None,
) -> dict[str, str]:
    """Bounded non-secret provenance for platform-authored terminal mutations."""
    marker = deployed_build_marker() or _BUILD_MARKER_UNKNOWN
    owner = execution_owner or "none"
    return bound_terminal_provenance(
        {
            "event": event,
            "source": source,
            "execution_owner": owner,
            "process_instance_id": _PROCESS_INSTANCE_ID,
            "build_marker": marker,
            "policy_version": STARTUP_RECOVERY_POLICY_VERSION,
        }
    ) or {}


def is_legacy_interrupt_error(error: str | None) -> bool:
    """True when ``error`` is the quarantined restart string (strip/casefold)."""
    if error is None:
        return False
    return error.strip().casefold() == INTERRUPTED_RUN_ERROR.strip().casefold()


def refuse_legacy_interrupt_error(error: str | None) -> str | None:
    """Block quarantined service-restart attribution on any error write path."""
    if error is None:
        return None
    if is_legacy_interrupt_error(error):
        logger.error(
            (
                "rejected_legacy_interrupt_error policy_version=%s "
                "process_instance_id=%s build_marker=%s"
            ),
            STARTUP_RECOVERY_POLICY_VERSION,
            _PROCESS_INSTANCE_ID,
            deployed_build_marker() or _BUILD_MARKER_UNKNOWN,
        )
        return LEGACY_INTERRUPT_REFUSED_ERROR
    return error


def startup_recovery_module_identity() -> str:
    """Package/module identity for diagnostics (never an absolute host path)."""
    stem = Path(__file__).stem
    if __package__:
        return f"{__package__}.{stem}"
    return stem


def startup_recovery_policy_diagnostics() -> dict[str, object]:
    """Runtime identity for loaded recovery policy (startup assertion / logs)."""
    return {
        "policy_version": STARTUP_RECOVERY_POLICY_VERSION,
        "module_path": startup_recovery_module_identity(),
        "grace_seconds": EXECUTION_LEASE_GRACE_SECONDS,
        "observed_false_interrupt_seconds": OBSERVED_FALSE_INTERRUPT_SECONDS,
        "legacy_interrupt_quarantined": True,
        "owner_lost_error": OWNER_LOST_RUN_ERROR,
        "process_instance_id": _PROCESS_INSTANCE_ID,
        "build_marker": deployed_build_marker() or _BUILD_MARKER_UNKNOWN,
    }

def sanitize_progress(value: object | None) -> dict[str, str] | None:
    """Normalize stored/API progress; drop unknown keys and bound values.

    Provenance is stripped here so public/monitoring surfaces stay step/detail
    only. Durable provenance is restored by ``deserialize_progress``.
    """
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

def serialize_progress(value: dict | None) -> str | None:
    cleaned = sanitize_progress(value)
    if cleaned is None:
        return None
    payload: dict[str, object] = dict(cleaned)
    if isinstance(value, dict):
        provenance = bound_terminal_provenance(value.get(_PROGRESS_PROVENANCE_KEY))
        if provenance is not None:
            payload[_PROGRESS_PROVENANCE_KEY] = provenance
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)

def deserialize_progress(raw: str | None) -> dict | None:
    if raw is None or raw == "":
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    cleaned = sanitize_progress(parsed)
    if cleaned is None:
        return None
    if isinstance(parsed, dict):
        provenance = bound_terminal_provenance(parsed.get(_PROGRESS_PROVENANCE_KEY))
        if provenance is not None:
            return {**cleaned, _PROGRESS_PROVENANCE_KEY: provenance}
    return cleaned

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
    progress: dict | None = None
    execution_owner: str | None = None

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
    """Parse a persisted UTC timestamp.

    Malformed values return ``None`` (never raise) so one corrupt row cannot
    abort registry reads or a startup recovery pass.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def _dt_raw_is_malformed(raw: object | None) -> bool:
    """True when a persisted timestamp is present but not parseable as datetime."""
    if raw is None:
        return False
    if not isinstance(raw, str):
        return True
    if raw.strip() == "":
        return False
    return _parse_dt(raw) is None

def _authoritative_lease_raw_malformed(
    raw_heartbeat: object | None,
    raw_started: object | None,
) -> bool:
    """Fail-safe: corrupt authoritative lease clock is not a valid lease.

    Heartbeat is authoritative when present (non-empty). Otherwise started_at
    is the claim/lease fallback. A malformed authoritative clock must not be
    treated as healthy and must not abort sibling-row recovery.
    """
    if raw_heartbeat is not None and not (
        isinstance(raw_heartbeat, str) and raw_heartbeat.strip() == ""
    ):
        return _dt_raw_is_malformed(raw_heartbeat)
    return _dt_raw_is_malformed(raw_started)

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
    execution_owner = (
        row["execution_owner"] if "execution_owner" in keys else None
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
        execution_owner=execution_owner,
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
                    progress_json TEXT,
                    execution_owner TEXT
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
            if "execution_owner" not in columns:
                self._conn.execute(
                    f"ALTER TABLE {_RUNS_TABLE} ADD COLUMN execution_owner TEXT"
                )
            self._install_legacy_interrupt_guards_unlocked()
            self._conn.commit()

    def _install_legacy_interrupt_guards_unlocked(self) -> None:
        """Idempotent SQLite triggers blocking legacy interrupt error writes.

        DROP+CREATE is safe across replicas/startup and refreshes the body when
        the refusal message or strip charset changes. Normalization uses the
        shared ``_SQL_STRIP_CHARS`` expression (Python ``str.strip()`` whitespace)
        plus ``lower()`` so direct SQL matches ``strip().casefold()`` for the
        ASCII quarantined string. Historical rows that already store the
        quarantined string remain readable; only NEW writes of that string
        (INSERT or error-changing UPDATE) are aborted atomically.
        """
        refused = LEGACY_INTERRUPT_REFUSED_ERROR.replace("'", "''")
        canon = _LEGACY_INTERRUPT_SQL_CANON
        strip_chars = _SQL_STRIP_CHARS
        self._conn.execute(
            f"DROP TRIGGER IF EXISTS {_LEGACY_INTERRUPT_INSERT_TRIGGER}"
        )
        self._conn.execute(
            f"DROP TRIGGER IF EXISTS {_LEGACY_INTERRUPT_UPDATE_TRIGGER}"
        )
        self._conn.execute(
            f"""
            CREATE TRIGGER {_LEGACY_INTERRUPT_INSERT_TRIGGER}
            BEFORE INSERT ON {_RUNS_TABLE}
            WHEN NEW.error IS NOT NULL
             AND lower(trim(NEW.error, {strip_chars}))
                 = lower(trim('{canon}', {strip_chars}))
            BEGIN
              SELECT RAISE(ABORT, '{refused}');
            END
            """
        )
        # Fire only when error is newly set/changed to the quarantined string so
        # historical rows can still be updated for unrelated columns/repairs.
        self._conn.execute(
            f"""
            CREATE TRIGGER {_LEGACY_INTERRUPT_UPDATE_TRIGGER}
            BEFORE UPDATE OF error ON {_RUNS_TABLE}
            WHEN NEW.error IS NOT NULL
             AND lower(trim(NEW.error, {strip_chars}))
                 = lower(trim('{canon}', {strip_chars}))
             AND (
                  OLD.error IS NULL
                  OR lower(trim(OLD.error, {strip_chars}))
                     != lower(trim(NEW.error, {strip_chars}))
             )
            BEGIN
              SELECT RAISE(ABORT, '{refused}');
            END
            """
        )

    def _execution_lease_age_seconds(
        self,
        record: RunRecord,
        *,
        now: datetime | None = None,
    ) -> float | None:
        """Seconds since last durable lease clock sample, or None if unknown.

        Future clocks within ``EXECUTION_LEASE_FUTURE_SKEW_TOLERANCE_SECONDS``
        normalize to age ``0.0``. Further-future values return ``None`` so
        callers treat them as an invalid/expired lease (fail-safe: do not pin
        dead work). All comparisons use UTC-aware datetimes.
        """
        clock = now or _utc_now()
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)
        else:
            clock = clock.astimezone(timezone.utc)
        lease_at = record.heartbeat_at or record.started_at
        if lease_at is None:
            return None
        if lease_at.tzinfo is None:
            lease_at = lease_at.replace(tzinfo=timezone.utc)
        else:
            lease_at = lease_at.astimezone(timezone.utc)
        age = (clock - lease_at).total_seconds()
        if age >= 0.0:
            return age
        if -age <= EXECUTION_LEASE_FUTURE_SKEW_TOLERANCE_SECONDS:
            return 0.0
        return None

    def _execution_lease_is_valid(
        self,
        record: RunRecord,
        *,
        now: datetime | None = None,
        grace_seconds: float = EXECUTION_LEASE_GRACE_SECONDS,
    ) -> bool:
        """True when a running run's heartbeat/lease is within grace."""
        if record.status is not RunStatus.RUNNING:
            return False
        age = self._execution_lease_age_seconds(record, now=now)
        if age is None:
            # Absent lease clock, unparseable/rejected future clock, or
            # claim/running persistence never landed a sample: not valid.
            return False
        return age <= grace_seconds

    def _new_execution_owner_token(self) -> str:
        return f"{os.getpid()}:{uuid.uuid4().hex[:12]}"

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

    def _terminalize_owner_lost_cas_unlocked(
        self,
        *,
        run_id: str,
        execution_owner: str | None,
        raw_heartbeat: object | None,
        raw_started: object | None,
        started_at: datetime | None,
        now: datetime,
        age: float | None,
        lease_clock_malformed: bool,
    ) -> bool:
        """CAS-terminalize one running run as owner-lost with provenance.

        The only registry path allowed to fail active work for lease/owner
        loss. Never writes ``INTERRUPTED_RUN_ERROR``. Returns True when the
        CAS update claimed the row.
        """
        elapsed_seconds = None
        if started_at is not None:
            elapsed_seconds = (now - started_at).total_seconds()

        provenance = build_terminal_provenance(
            event="terminalize_owner_lost",
            source="startup_recovery.owner_lease_cas",
            execution_owner=execution_owner,
        )
        progress_payload: dict = platform_progress(
            step=RunPhase.FAILED.value,
            detail="Execution owner lost; lease expired",
        )
        progress_payload[_PROGRESS_PROVENANCE_KEY] = provenance
        progress = serialize_progress(progress_payload)

        # Hard invariant: owner-loss never uses the quarantined legacy string.
        if OWNER_LOST_RUN_ERROR == INTERRUPTED_RUN_ERROR:
            raise RuntimeError(
                "OWNER_LOST_RUN_ERROR must not equal quarantined "
                "INTERRUPTED_RUN_ERROR"
            )

        # CAS on exact observed lease clocks: if the owner refreshes
        # heartbeat (or repairs clocks) after this read, rowcount=0.
        cursor = self._conn.execute(
            f"""
            UPDATE {_RUNS_TABLE}
            SET status = ?,
                completed_at = ?,
                elapsed_seconds = ?,
                error = ?,
                phase = ?,
                phase_started_at = ?,
                heartbeat_at = ?,
                progress_json = ?,
                execution_owner = NULL
            WHERE run_id = ?
              AND status = ?
              AND heartbeat_at IS ?
              AND started_at IS ?
            """,
            (
                RunStatus.FAILED.value,
                _format_dt(now),
                elapsed_seconds,
                OWNER_LOST_RUN_ERROR,
                RunPhase.FAILED.value,
                _format_dt(now),
                _format_dt(now),
                progress,
                run_id,
                RunStatus.RUNNING.value,
                raw_heartbeat,
                raw_started,
            ),
        )
        if cursor.rowcount != 1:
            logger.info(
                (
                    "startup_recovery decision=skip_race run_id=%s "
                    "execution_owner=%s policy_version=%s"
                ),
                run_id,
                execution_owner,
                STARTUP_RECOVERY_POLICY_VERSION,
            )
            return False

        logger.info(
            (
                "startup_recovery decision=terminalize_owner_lost "
                "run_id=%s status=%s execution_owner=%s "
                "lease_age_seconds=%s grace_seconds=%s "
                "lease_clock_malformed=%s policy_version=%s "
                "process_instance_id=%s build_marker=%s source=%s"
            ),
            run_id,
            RunStatus.RUNNING.value,
            execution_owner,
            None if age is None else round(age, 3),
            EXECUTION_LEASE_GRACE_SECONDS,
            lease_clock_malformed,
            STARTUP_RECOVERY_POLICY_VERSION,
            provenance.get("process_instance_id"),
            provenance.get("build_marker"),
            provenance.get("source"),
        )
        return True

    def _recover_interrupted_runs_unlocked(self) -> tuple[int, list[str]]:
        """Terminalize dead running owners only. Caller holds ``self._lock``.

        Queued / unclaimed runs are left queued so they can be re-enqueued.
        Running runs with a fresh execution lease/heartbeat are left alone so
        another healthy replica is not interrupted. Returns
        ``(terminalized_count, terminalized_run_ids)``.

        Terminalizing UPDATE uses a heartbeat/started CAS against the exact
        observed lease clock values so a late owner heartbeat between read and
        update cannot false-fail the run. Malformed lease timestamps are
        isolated per row (fail-safe expired) with secret-safe diagnostics.
        """
        now = _utc_now()
        recovered = 0
        recovered_ids: list[str] = []
        rows = self._conn.execute(
            f"""
            SELECT *
            FROM {_RUNS_TABLE}
            WHERE status IN (?, ?)
            """,
            (RunStatus.QUEUED.value, RunStatus.RUNNING.value),
        ).fetchall()

        for row in rows:
            run_id_for_log = "?"
            try:
                keys = row.keys()
                run_id_for_log = str(row["run_id"])
                raw_heartbeat = (
                    row["heartbeat_at"] if "heartbeat_at" in keys else None
                )
                raw_started = row["started_at"] if "started_at" in keys else None
                record = _row_to_record(row)
                if record.status is RunStatus.QUEUED:
                    logger.info(
                        (
                            "startup_recovery decision=leave_queued run_id=%s "
                            "status=%s execution_owner=%s"
                        ),
                        record.run_id,
                        record.status.value,
                        record.execution_owner,
                    )
                    continue

                heartbeat_malformed = _dt_raw_is_malformed(raw_heartbeat)
                started_malformed = _dt_raw_is_malformed(raw_started)
                lease_clock_malformed = _authoritative_lease_raw_malformed(
                    raw_heartbeat,
                    raw_started,
                )

                if lease_clock_malformed:
                    age = None
                    lease_valid = False
                    logger.warning(
                        (
                            "startup_recovery decision=malformed_lease_clock "
                            "run_id=%s status=%s heartbeat_malformed=%s "
                            "started_malformed=%s policy=fail_safe_expired"
                        ),
                        record.run_id,
                        record.status.value,
                        heartbeat_malformed,
                        started_malformed,
                    )
                else:
                    age = self._execution_lease_age_seconds(record, now=now)
                    lease_valid = self._execution_lease_is_valid(
                        record, now=now
                    )
                    if (
                        age is None
                        and (record.heartbeat_at is not None
                             or record.started_at is not None)
                    ):
                        # Parsed clock present but rejected (e.g. far-future).
                        logger.warning(
                            (
                                "startup_recovery decision="
                                "reject_future_lease_clock "
                                "run_id=%s status=%s "
                                "skew_tolerance_seconds=%s "
                                "policy=fail_safe_expired"
                            ),
                            record.run_id,
                            record.status.value,
                            EXECUTION_LEASE_FUTURE_SKEW_TOLERANCE_SECONDS,
                        )

                if lease_valid:
                    logger.info(
                        (
                            "startup_recovery decision=leave_running_healthy "
                            "run_id=%s status=%s execution_owner=%s "
                            "lease_age_seconds=%s grace_seconds=%s"
                        ),
                        record.run_id,
                        record.status.value,
                        record.execution_owner,
                        None if age is None else round(age, 3),
                        EXECUTION_LEASE_GRACE_SECONDS,
                    )
                    continue

                if not self._terminalize_owner_lost_cas_unlocked(
                    run_id=record.run_id,
                    execution_owner=record.execution_owner,
                    raw_heartbeat=raw_heartbeat,
                    raw_started=raw_started,
                    started_at=record.started_at,
                    now=now,
                    age=age,
                    lease_clock_malformed=lease_clock_malformed,
                ):
                    continue

                recovered += 1
                recovered_ids.append(record.run_id)
            except Exception as exc:
                # Isolate unexpected row failures; never abort the pass.
                logger.warning(
                    (
                        "startup_recovery decision=skip_row_exception "
                        "run_id=%s error_type=%s"
                    ),
                    run_id_for_log,
                    type(exc).__name__,
                )
                continue

        if recovered:
            self._conn.commit()
            logger.info(
                "Terminalized %s owner-lost run(s) from %s",
                recovered,
                self._db_path,
            )

        return recovered, recovered_ids

    def recover_interrupted_runs(self) -> int:
        """Recover runs after process/replica startup.

        Exactly one process across replicas sharing this SQLite database
        performs recovery. Non-owners skip cleanly and return ``0``. A
        time-bounded lease ensures a crashed owner cannot block recovery
        permanently.

        Queued/unclaimed runs are never failed by startup alone. Running
        runs are terminalized only when the durable execution lease
        (heartbeat) is expired beyond ``EXECUTION_LEASE_GRACE_SECONDS``.
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

    def list_requeueable_queued_runs(self) -> list[tuple[str, str]]:
        """Return ``(run_id, mission_yaml)`` for queued runs that can execute.

        Startup recovery leaves these rows queued; the local run queue must
        re-enqueue them. Duplicate local enqueues are safe when paired with
        ``try_claim_run`` (exactly-once execution).
        """
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT run_id, mission_yaml
                FROM {_RUNS_TABLE}
                WHERE status = ?
                ORDER BY created_at ASC
                """,
                (RunStatus.QUEUED.value,),
            ).fetchall()
            candidates: list[tuple[str, str]] = []
            for row in rows:
                mission_yaml = row["mission_yaml"]
                if not mission_yaml:
                    logger.info(
                        (
                            "startup_recovery decision=skip_requeue_no_yaml "
                            "run_id=%s"
                        ),
                        row["run_id"],
                    )
                    continue
                candidates.append((row["run_id"], mission_yaml))
            return candidates

    def try_claim_run(
        self,
        run_id: str,
        *,
        owner_token: str | None = None,
    ) -> RunRecord | None:
        """Atomically claim a queued run for execution.

        Returns the claimed ``running`` record, or ``None`` when another
        owner holds a valid lease / the run is not claimable. Idempotent
        when ``owner_token`` already owns the running row.
        """
        token = owner_token or self._new_execution_owner_token()
        with self._lock:
            now = _utc_now()
            try:
                self._conn.commit()
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    pass
                logger.debug(
                    "try_claim_run begin failed run_id=%s: %s",
                    run_id,
                    exc,
                )
                return None

            row = self._fetch_row(run_id)
            if row is None:
                self._conn.rollback()
                return None

            record = _row_to_record(row)
            if is_terminal_status(record.status):
                self._conn.rollback()
                logger.info(
                    (
                        "startup_recovery decision=claim_rejected_terminal "
                        "run_id=%s status=%s"
                    ),
                    run_id,
                    record.status.value,
                )
                return None

            if record.status is RunStatus.RUNNING:
                if record.execution_owner == token:
                    self._conn.commit()
                    return record
                if self._execution_lease_is_valid(record, now=now):
                    # Valid lease (including legacy owner-NULL rows) is not
                    # stealable by an arbitrary claimer.
                    self._conn.rollback()
                    logger.info(
                        (
                            "startup_recovery decision=claim_rejected_leased "
                            "run_id=%s execution_owner=%s"
                        ),
                        run_id,
                        record.execution_owner,
                    )
                    return None
                self._conn.rollback()
                logger.info(
                    (
                        "startup_recovery decision=claim_rejected_expired "
                        "run_id=%s execution_owner=%s"
                    ),
                    run_id,
                    record.execution_owner,
                )
                return None

            if record.status is not RunStatus.QUEUED:
                self._conn.rollback()
                return None

            cursor = self._conn.execute(
                f"""
                UPDATE {_RUNS_TABLE}
                SET status = ?,
                    started_at = COALESCE(started_at, ?),
                    heartbeat_at = ?,
                    execution_owner = ?
                WHERE run_id = ?
                  AND status = ?
                """,
                (
                    RunStatus.RUNNING.value,
                    _format_dt(now),
                    _format_dt(now),
                    token,
                    run_id,
                    RunStatus.QUEUED.value,
                ),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                logger.info(
                    (
                        "startup_recovery decision=claim_rejected_race "
                        "run_id=%s"
                    ),
                    run_id,
                )
                return None
            self._conn.commit()
            row = self._fetch_row(run_id)
            if row is None:
                return None
            snapshot = _row_to_record(row)

        logger.info(
            (
                "lifecycle run_id=%s event=claimed status=%s "
                "execution_owner=%s api_pid=%s"
            ),
            run_id,
            snapshot.status.value,
            snapshot.execution_owner,
            os.getpid(),
        )
        return snapshot

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
                progress_json,
                execution_owner
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                mission_yaml = excluded.mission_yaml,
                retried_from = excluded.retried_from,
                phase = excluded.phase,
                phase_started_at = excluded.phase_started_at,
                heartbeat_at = excluded.heartbeat_at,
                progress_json = excluded.progress_json,
                execution_owner = excluded.execution_owner
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
                record.execution_owner,
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
                if record.execution_owner is None:
                    record.execution_owner = self._new_execution_owner_token()

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
                record.execution_owner = None
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
            record.error = refuse_legacy_interrupt_error(error)
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
