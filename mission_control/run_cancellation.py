"""Authorized run cancellation, stale-heartbeat recovery, and watchdog."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any

from mission_control.monitoring import (
    HEARTBEAT_STALE_THRESHOLD_SECONDS,
    classify_heartbeat_health,
)
from mission_control.run_registry import (
    RunRecord,
    RunRegistry,
    RunStatus,
    is_terminal_status,
)

logger = logging.getLogger(__name__)

# Graceful cancel: SIGTERM to process group, then SIGKILL after this bound.
CANCEL_GRACE_SECONDS = 10.0
WATCHDOG_POLL_INTERVAL_SECONDS = 15.0
STALE_HEARTBEAT_WATCHDOG_TIMEOUT_SECONDS = HEARTBEAT_STALE_THRESHOLD_SECONDS

STALE_HEARTBEAT_FAILURE_PREFIX = "Run failed: stale heartbeat"
CANCELLED_BY_OPERATOR_ERROR = "Run cancelled by operator"
CANCELLED_ALREADY_TERMINAL = "run_already_terminal"
CANCELLED_ALREADY_CANCELLED = "run_already_cancelled"

_DIAGNOSTICS_ALLOWED_KEYS = frozenset(
    {
        "action",
        "source",
        "requested_at",
        "recovery_at",
        "last_heartbeat_at",
        "termination",
        "previous_status",
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def stale_heartbeat_failure_reason(
    *,
    last_heartbeat_at: datetime | str | None,
    recovery_at: datetime | None = None,
) -> str:
    """Build a durable stale-heartbeat terminal error string."""
    clock = recovery_at or _utc_now()
    hb_text = "absent"
    if last_heartbeat_at is not None:
        if isinstance(last_heartbeat_at, datetime):
            hb_text = _format_dt(last_heartbeat_at)
        else:
            hb_text = str(last_heartbeat_at)
    return (
        f"{STALE_HEARTBEAT_FAILURE_PREFIX} "
        f"(last_heartbeat_at={hb_text}; recovered_at={_format_dt(clock)})"
    )


def _is_stale_running_run(
    record: RunRecord,
    *,
    now: datetime,
    stale_threshold_seconds: float,
) -> bool:
    """True when a running run's heartbeat is absent or older than threshold."""
    if record.status is not RunStatus.RUNNING:
        return False
    heartbeat_at = record.heartbeat_at
    if heartbeat_at is None:
        return True
    age = (now - heartbeat_at).total_seconds()
    return age > stale_threshold_seconds


def redact_cancellation_diagnostics(raw: dict[str, Any]) -> dict[str, str]:
    """Return a bounded, redacted diagnostics object for API/storage."""
    cleaned: dict[str, str] = {}
    for key in _DIAGNOSTICS_ALLOWED_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        text = " ".join(str(value).split())
        if len(text) > 160:
            text = text[:157] + "..."
        cleaned[key] = text
    return cleaned


@dataclass(frozen=True)
class CancelRunResult:
    ok: bool
    run_id: str
    status: str
    already_terminal: bool = False
    diagnostics: dict[str, str] = field(default_factory=dict)
    code: str | None = None


class ActiveExecutionRegistry:
    """Process-local registry of active Cursor subprocesses by run_id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, subprocess.Popen[str]] = {}

    def register(self, run_id: str, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            self._entries[str(run_id)] = proc

    def unregister(self, run_id: str) -> None:
        with self._lock:
            self._entries.pop(str(run_id), None)

    def get(self, run_id: str) -> subprocess.Popen[str] | None:
        with self._lock:
            return self._entries.get(str(run_id))

    def terminate_process_group(
        self,
        run_id: str,
        *,
        grace_seconds: float = CANCEL_GRACE_SECONDS,
    ) -> str:
        """Terminate the registered subprocess tree; return termination summary."""
        with self._lock:
            proc = self._entries.get(str(run_id))
        if proc is None or proc.pid is None:
            return "no_active_subprocess"
        pid = proc.pid
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return "subprocess_already_exited"
        except (PermissionError, OSError):
            try:
                proc.terminate()
            except ProcessLookupError:
                return "subprocess_already_exited"
        deadline = time.monotonic() + max(0.1, grace_seconds)
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return "terminated_gracefully"
            time.sleep(0.1)
        from mission_control.executor import _terminate_process_tree

        _terminate_process_tree(proc)
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        return "terminated_forced"


# Shared registry wired by app startup and executor subprocess creation.
active_execution_registry = ActiveExecutionRegistry()


def cancel_run(
    registry: RunRegistry,
    run_id: str,
    *,
    source: str = "operator",
    run_queue: Any | None = None,
) -> CancelRunResult:
    """Idempotently cancel a non-terminal run and terminate its subprocess."""
    record = registry.get_run(run_id)
    if record is None:
        return CancelRunResult(
            ok=False,
            run_id=run_id,
            status="unknown",
            code="run_not_found",
        )

    now = _utc_now()
    diagnostics = redact_cancellation_diagnostics(
        {
            "action": "cancel",
            "source": source,
            "requested_at": _format_dt(now),
            "previous_status": record.status.value,
        }
    )

    if record.status is RunStatus.CANCELLED:
        return CancelRunResult(
            ok=True,
            run_id=run_id,
            status=RunStatus.CANCELLED.value,
            already_terminal=True,
            diagnostics=diagnostics,
            code=CANCELLED_ALREADY_CANCELLED,
        )

    if is_terminal_status(record.status):
        return CancelRunResult(
            ok=True,
            run_id=run_id,
            status=record.status.value,
            already_terminal=True,
            diagnostics=diagnostics,
            code=CANCELLED_ALREADY_TERMINAL,
        )

    termination = "not_applicable"
    if record.status is RunStatus.RUNNING:
        termination = active_execution_registry.terminate_process_group(run_id)
        diagnostics = redact_cancellation_diagnostics(
            {**diagnostics, "termination": termination}
        )
    elif record.status is RunStatus.QUEUED and run_queue is not None:
        remover = getattr(run_queue, "remove_pending", None)
        if callable(remover):
            remover(run_id)

    updated = registry.cancel_run(
        run_id,
        error=CANCELLED_BY_OPERATOR_ERROR,
        diagnostics=diagnostics,
        source=source,
    )
    if updated is None:
        latest = registry.get_run(run_id)
        status = latest.status.value if latest is not None else "unknown"
        return CancelRunResult(
            ok=False,
            run_id=run_id,
            status=status,
            diagnostics=diagnostics,
            code="cancel_race",
        )

    return CancelRunResult(
        ok=True,
        run_id=run_id,
        status=updated.status.value,
        diagnostics=diagnostics,
    )


def recover_stale_run(
    registry: RunRegistry,
    run_id: str,
    *,
    source: str = "operator_recovery",
    run_queue: Any | None = None,
    recovery_at: datetime | None = None,
) -> CancelRunResult:
    """Operator recovery: terminate stale worker and terminalize with stale heartbeat."""
    record = registry.get_run(run_id)
    if record is None:
        return CancelRunResult(
            ok=False,
            run_id=run_id,
            status="unknown",
            code="run_not_found",
        )

    clock = recovery_at or _utc_now()
    last_hb = record.heartbeat_at
    diagnostics = redact_cancellation_diagnostics(
        {
            "action": "recover_stale",
            "source": source,
            "requested_at": _format_dt(clock),
            "last_heartbeat_at": (
                _format_dt(last_hb) if isinstance(last_hb, datetime) else last_hb
            ),
            "previous_status": record.status.value,
        }
    )

    if is_terminal_status(record.status):
        return CancelRunResult(
            ok=True,
            run_id=run_id,
            status=record.status.value,
            already_terminal=True,
            diagnostics=diagnostics,
            code=CANCELLED_ALREADY_TERMINAL,
        )

    health = classify_heartbeat_health(record, now=clock)
    stale_by_status = _is_stale_running_run(
        record,
        now=clock,
        stale_threshold_seconds=STALE_HEARTBEAT_WATCHDOG_TIMEOUT_SECONDS,
    )
    if (
        not stale_by_status
        and health.value not in {"stale", "absent"}
        and record.status is RunStatus.RUNNING
    ):
        return CancelRunResult(
            ok=False,
            run_id=run_id,
            status=record.status.value,
            diagnostics=diagnostics,
            code="heartbeat_not_stale",
        )

    termination = "not_applicable"
    if record.status is RunStatus.RUNNING:
        termination = active_execution_registry.terminate_process_group(run_id)
    elif record.status is RunStatus.QUEUED and run_queue is not None:
        remover = getattr(run_queue, "remove_pending", None)
        if callable(remover):
            remover(run_id)

    diagnostics = redact_cancellation_diagnostics(
        {**diagnostics, "termination": termination, "recovery_at": _format_dt(clock)}
    )
    error = stale_heartbeat_failure_reason(
        last_heartbeat_at=last_hb,
        recovery_at=clock,
    )
    updated = registry.terminalize_stale_heartbeat(
        run_id,
        error=error,
        diagnostics=diagnostics,
        source=source,
        recovery_at=clock,
    )
    if updated is None:
        latest = registry.get_run(run_id)
        status = latest.status.value if latest is not None else "unknown"
        return CancelRunResult(
            ok=False,
            run_id=run_id,
            status=status,
            diagnostics=diagnostics,
            code="recovery_race",
        )

    return CancelRunResult(
        ok=True,
        run_id=run_id,
        status=updated.status.value,
        diagnostics=diagnostics,
    )


class HeartbeatWatchdog:
    """Background stale-heartbeat recovery for active runs."""

    def __init__(
        self,
        registry: RunRegistry,
        *,
        run_queue: Any | None = None,
        stale_threshold_seconds: float = STALE_HEARTBEAT_WATCHDOG_TIMEOUT_SECONDS,
        poll_interval_seconds: float = WATCHDOG_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._registry = registry
        self._run_queue = run_queue
        self._stale_threshold_seconds = stale_threshold_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def stale_threshold_seconds(self) -> float:
        return self._stale_threshold_seconds

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="mission-control-heartbeat-watchdog",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self._poll_interval_seconds):
            try:
                self.tick()
            except Exception:
                logger.exception("heartbeat watchdog tick failed")

    def tick(self) -> int:
        """Scan running runs once; return count terminalized."""
        now = _utc_now()
        terminalized = 0
        records = self._registry.list_active_runs()
        for record in records:
            if record.status is not RunStatus.RUNNING:
                continue
            if not _is_stale_running_run(
                record,
                now=now,
                stale_threshold_seconds=self._stale_threshold_seconds,
            ):
                continue
            result = recover_stale_run(
                self._registry,
                record.run_id,
                source="heartbeat_watchdog",
                run_queue=self._run_queue,
                recovery_at=now,
            )
            if result.ok and not result.already_terminal:
                terminalized += 1
                logger.info(
                    "heartbeat_watchdog terminalized run_id=%s status=%s",
                    record.run_id,
                    result.status,
                )
        return terminalized
