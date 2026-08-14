"""Phase-aware mission monitoring for wait / submit_and_wait (Phase 2B).

Server-side helpers that classify heartbeat health, build a bounded safe
monitoring history, and encode a resumable cursor. Wait timeout is distinct
from mission ``timed_out`` and never mutates or cancels a run.
"""

from __future__ import annotations

import base64
import json
import math
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from mission_control.run_registry import (
    HEARTBEAT_INTERVAL_SECONDS,
    RunPhase,
    RunRecord,
    RunStatus,
    is_terminal_status,
    sanitize_progress,
)

# Poll cadence for HAL/Unified server-side waits (~25s). Callers may override
# within existing wait poll bounds.
DEFAULT_MONITOR_POLL_INTERVAL_SECONDS = 25.0

# Stale threshold must be safely larger than the live heartbeat cadence (5s).
# Eighteen intervals (90s) reduces false stale alerts under scheduling jitter
# while remaining well below typical operator wait budgets. Operators may pass
# an explicit stale_threshold_seconds override on observe/enqueue helpers.
HEARTBEAT_STALE_THRESHOLD_SECONDS = HEARTBEAT_INTERVAL_SECONDS * 18.0  # 90s


def validate_stale_threshold_seconds(value: float) -> float:
    """Return ``value`` when it is finite and strictly positive.

    Invalid overrides must fail closed before any outbox/queue mutation.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "stale_threshold_seconds must be a finite number > 0"
        ) from exc
    if not math.isfinite(seconds) or seconds <= 0.0:
        raise ValueError(
            "stale_threshold_seconds must be finite and strictly positive"
        )
    return seconds


# Bound monitoring payload size (events are already redacted/sanitized).
MONITORING_HISTORY_MAX_EVENTS = 32

# Opaque resumable cursor is base64url(json). A full 32-event history stays
# well under this; reject larger inputs before decode/forward (DoS hardening).
MONITOR_CURSOR_MAX_CHARS = 16_384

# Monitoring treats cancelled as terminal even if the registry enum lacks it yet.
MONITORING_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "timed_out",
        "cancelled",
    }
)

_CURSOR_VERSION = 1
_EVENT_ALLOWED_KEYS = frozenset(
    {"at", "status", "phase", "progress", "heartbeat_health"}
)


def normalize_monitor_cursor(cursor: str | None) -> str | None:
    """Return a stripped cursor or ``None``; reject oversized inputs."""
    if cursor is None:
        return None
    text = str(cursor).strip()
    if not text:
        return None
    if len(text) > MONITOR_CURSOR_MAX_CHARS:
        raise ValueError(
            "cursor exceeds maximum length of "
            f"{MONITOR_CURSOR_MAX_CHARS} characters"
        )
    return text


class HeartbeatHealth(str, Enum):
    """Heartbeat health relative to the stale threshold."""

    HEALTHY = "healthy"
    STALE = "stale"
    ABSENT = "absent"
    NOT_APPLICABLE = "not_applicable"
    TERMINAL = "terminal"


def is_monitoring_terminal(status: RunStatus | str | None) -> bool:
    """Return True for terminal lifecycle outcomes observed by monitoring."""
    if status is None:
        return False
    if isinstance(status, RunStatus):
        value = status.value
    else:
        value = str(status)
    if value in MONITORING_TERMINAL_STATUSES:
        return True
    return is_terminal_status(status)


def _status_value(status: RunStatus | str) -> str:
    if isinstance(status, RunStatus):
        return status.value
    return str(status)


def _phase_value(phase: RunPhase | str | None) -> str:
    if phase is None:
        return RunPhase.QUEUED.value
    if isinstance(phase, RunPhase):
        return phase.value
    return str(phase)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def classify_heartbeat_health(
    record: RunRecord | Mapping[str, Any],
    *,
    now: datetime | None = None,
    stale_threshold_seconds: float = HEARTBEAT_STALE_THRESHOLD_SECONDS,
) -> HeartbeatHealth:
    """Classify heartbeat health without mutating the run.

    ``not_applicable`` covers queued runs (agent heartbeat cadence not active).
    ``absent`` covers non-queued active runs with no ``heartbeat_at``.
    ``stale`` means ``heartbeat_at`` is older than the documented threshold.
    Terminal statuses always classify as ``terminal``.
    """
    threshold = validate_stale_threshold_seconds(stale_threshold_seconds)
    if isinstance(record, Mapping):
        status = record.get("status")
        phase = record.get("phase")
        heartbeat_at = record.get("heartbeat_at")
    else:
        status = record.status
        phase = record.phase
        heartbeat_at = record.heartbeat_at

    if is_monitoring_terminal(status):
        return HeartbeatHealth.TERMINAL

    phase_value = _phase_value(phase)
    if phase_value == RunPhase.QUEUED.value or _status_value(status) == (
        RunStatus.QUEUED.value
    ):
        return HeartbeatHealth.NOT_APPLICABLE

    if heartbeat_at is None:
        return HeartbeatHealth.ABSENT

    clock = _as_utc(now or datetime.now(timezone.utc))
    hb = heartbeat_at
    if isinstance(hb, str):
        try:
            hb = datetime.fromisoformat(hb)
        except ValueError:
            return HeartbeatHealth.ABSENT
    if not isinstance(hb, datetime):
        return HeartbeatHealth.ABSENT

    age = (clock - _as_utc(hb)).total_seconds()
    if age > threshold:
        return HeartbeatHealth.STALE
    return HeartbeatHealth.HEALTHY


def _safe_progress(progress: object | None) -> dict[str, str] | None:
    return sanitize_progress(progress)


def build_monitoring_event(
    record: RunRecord,
    *,
    now: datetime | None = None,
    stale_threshold_seconds: float = HEARTBEAT_STALE_THRESHOLD_SECONDS,
) -> dict[str, Any]:
    """Build one safe monitoring event (no stdout/stderr/prompts/secrets)."""
    clock = _as_utc(now or datetime.now(timezone.utc))
    health = classify_heartbeat_health(
        record,
        now=clock,
        stale_threshold_seconds=stale_threshold_seconds,
    )
    progress = _safe_progress(record.progress)
    return {
        "at": clock.isoformat().replace("+00:00", "Z"),
        "status": _status_value(record.status),
        "phase": _phase_value(record.phase),
        "progress": progress,
        "heartbeat_health": health.value,
    }


def _event_dedupe_key(event: Mapping[str, Any]) -> tuple[Any, ...]:
    progress = event.get("progress")
    if isinstance(progress, Mapping):
        progress_key = (progress.get("step"), progress.get("detail"))
    else:
        progress_key = (None, None)
    return (
        event.get("status"),
        event.get("phase"),
        progress_key,
        event.get("heartbeat_health"),
    )


def sanitize_monitoring_event(value: object | None) -> dict[str, Any] | None:
    """Drop unknown keys and re-sanitize progress for cursor/history replay."""
    if not isinstance(value, dict):
        return None
    status = value.get("status")
    phase = value.get("phase")
    health = value.get("heartbeat_health")
    at = value.get("at")
    if not isinstance(status, str) or not isinstance(phase, str):
        return None
    if not isinstance(health, str) or not isinstance(at, str):
        return None
    try:
        HeartbeatHealth(health)
    except ValueError:
        return None
    progress = _safe_progress(value.get("progress"))
    event = {
        "at": at[:64],
        "status": status[:64],
        "phase": phase[:64],
        "progress": progress,
        "heartbeat_health": health,
    }
    # Reconstruct from allowlist only (never carry stdout/stderr/etc.).
    _ = {k: value.get(k) for k in value if k in _EVENT_ALLOWED_KEYS}
    return event


def append_monitoring_event(
    history: list[dict[str, Any]],
    event: Mapping[str, Any],
    *,
    max_events: int = MONITORING_HISTORY_MAX_EVENTS,
) -> list[dict[str, Any]]:
    """Append ``event`` when phase/progress/health meaningfully changed."""
    cleaned = sanitize_monitoring_event(dict(event))
    if cleaned is None:
        return list(history)
    if history and _event_dedupe_key(history[-1]) == _event_dedupe_key(cleaned):
        return list(history)
    next_history = list(history) + [cleaned]
    if max_events < 1:
        return []
    if len(next_history) > max_events:
        return next_history[-max_events:]
    return next_history


def encode_monitor_cursor(history: list[dict[str, Any]]) -> str:
    """Encode bounded monitoring history into a resumable opaque cursor."""
    safe_history: list[dict[str, Any]] = []
    for item in history[-MONITORING_HISTORY_MAX_EVENTS:]:
        cleaned = sanitize_monitoring_event(item)
        if cleaned is not None:
            safe_history.append(cleaned)
    payload = {"v": _CURSOR_VERSION, "history": safe_history}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_monitor_cursor(cursor: str | None) -> list[dict[str, Any]]:
    """Decode a monitor cursor; invalid/empty cursors resume with no history.

    Oversized cursors raise ``ValueError`` before any decode work (DoS bound).
    """
    text = normalize_monitor_cursor(cursor)
    if text is None:
        return []
    pad = "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(text + pad)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        return []
    history = payload.get("history")
    if not isinstance(history, list):
        return []
    out: list[dict[str, Any]] = []
    for item in history:
        cleaned = sanitize_monitoring_event(item)
        if cleaned is not None:
            out.append(cleaned)
    return out[-MONITORING_HISTORY_MAX_EVENTS:]


def observe_run(
    record: RunRecord,
    history: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    stale_threshold_seconds: float = HEARTBEAT_STALE_THRESHOLD_SECONDS,
    max_events: int = MONITORING_HISTORY_MAX_EVENTS,
) -> tuple[list[dict[str, Any]], HeartbeatHealth, dict[str, Any]]:
    """Observe a run snapshot: update history and return health + latest event."""
    event = build_monitoring_event(
        record,
        now=now,
        stale_threshold_seconds=stale_threshold_seconds,
    )
    health = HeartbeatHealth(event["heartbeat_health"])
    next_history = append_monitoring_event(
        history, event, max_events=max_events
    )
    return next_history, health, event
