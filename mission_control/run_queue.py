"""FIFO single-active execution queue for asynchronous mission runs.

The queue is process-local and in-memory only. Pending and active work is
lost if the process restarts. At most one Cursor execution runs at a time;
additional accepted runs wait in FIFO order.

``enqueue`` is idempotent per ``run_id`` against process-local queue state
and authoritative registry status: suppress when already queued/active in
this process, or when the registry reports running/terminal. Callers that
need crash recovery across restarts must keep a durable dispatch intent
outside this process-local queue.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

# execute(run_id, mission, registry)
ExecuteFn = Callable[[str, dict, Any], None]

_TERMINAL_OR_ACTIVE_REGISTRY = frozenset(
    {
        "running",
        "completed",
        "failed",
        "timed_out",
        "cancelled",
    }
)


def _registry_log_fields(registry: Any) -> tuple[int | None, int | None, list[str] | None]:
    """Best-effort registry identity/count/keys without assuming a concrete type."""
    if registry is None:
        return None, None, None
    registry_id = id(registry)
    diagnostic = getattr(registry, "diagnostic_state", None)
    if callable(diagnostic):
        count, keys = diagnostic()
        return registry_id, count, keys
    return registry_id, None, None


def _registry_blocks_enqueue(registry: Any, run_id: str) -> bool:
    """Return True when authoritative registry state must suppress enqueue."""
    if registry is None:
        return False
    getter = getattr(registry, "get_run", None)
    if not callable(getter):
        return False
    record = getter(run_id)
    if record is None:
        return False
    status = getattr(record, "status", None)
    if status is None:
        return False
    status_value = status.value if hasattr(status, "value") else str(status)
    return status_value in _TERMINAL_OR_ACTIVE_REGISTRY


class RunQueue:
    """Serialize Cursor executions: one active run, FIFO for the rest."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._pending: deque[tuple[str, dict, Any]] = deque()
        self._active_run_id: str | None = None
        self._worker: threading.Thread | None = None
        self._execute_fn: ExecuteFn | None = None
        self._stopped = False

    def configure(self, execute_fn: ExecuteFn) -> None:
        """Set the callable invoked for each dequeued run."""
        with self._lock:
            self._execute_fn = execute_fn

    def enqueue(self, run_id: str, mission: dict, registry: Any) -> bool:
        """Accept a run for FIFO execution.

        Returns True when newly accepted into the process-local pending
        queue. Returns False when suppressed because the run is already
        pending/active in this process, or registry status is
        running/terminal. Does not start Cursor immediately when another
        run is already active. ``registry`` is captured at enqueue time so
        workers stay isolated from later process-global registry
        replacements (e.g. in tests).
        """
        with self._cond:
            if self._execute_fn is None:
                raise RuntimeError("RunQueue.configure() must be called first")
            if self._active_run_id == run_id:
                logger.info(
                    (
                        "lifecycle run_id=%s event=enqueue_suppressed "
                        "reason=already_active api_pid=%s"
                    ),
                    run_id,
                    os.getpid(),
                )
                return False
            if any(pending_id == run_id for pending_id, _, _ in self._pending):
                logger.info(
                    (
                        "lifecycle run_id=%s event=enqueue_suppressed "
                        "reason=already_queued api_pid=%s"
                    ),
                    run_id,
                    os.getpid(),
                )
                return False
            if _registry_blocks_enqueue(registry, run_id):
                logger.info(
                    (
                        "lifecycle run_id=%s event=enqueue_suppressed "
                        "reason=registry_active_or_terminal api_pid=%s"
                    ),
                    run_id,
                    os.getpid(),
                )
                return False
            self._pending.append((run_id, mission, registry))
            depth = len(self._pending)
            active = self._active_run_id
            registry_id, registry_count, registry_keys = _registry_log_fields(
                registry
            )
            logger.info(
                (
                    "lifecycle run_id=%s event=queued queue_depth=%s "
                    "active_run_id=%s api_pid=%s registry_id=%s "
                    "registry_count=%s registry_keys=%s"
                ),
                run_id,
                depth,
                active,
                os.getpid(),
                registry_id,
                registry_count,
                registry_keys,
            )
            scheduled_new = self._ensure_worker_locked()
            logger.info(
                (
                    "lifecycle run_id=%s event=worker_scheduled "
                    "new_worker=%s worker_alive=%s api_pid=%s "
                    "registry_id=%s registry_count=%s"
                ),
                run_id,
                scheduled_new,
                bool(self._worker is not None and self._worker.is_alive()),
                os.getpid(),
                registry_id,
                registry_count,
            )
            self._cond.notify()
            return True

    def reset(self) -> None:
        """Drop pending work and clear active state (for tests)."""
        self.stop()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the worker thread and drop pending work.

        Used by tests (and optional process shutdown) so queue workers do not
        linger and consume RLIMIT_NPROC / thread slots across the suite.
        """
        with self._cond:
            self._stopped = True
            self._pending.clear()
            self._active_run_id = None
            worker = self._worker
            self._cond.notify_all()
        if worker is not None and worker.is_alive():
            worker.join(timeout=timeout)
        with self._cond:
            if self._worker is worker:
                self._worker = None
            # Allow a later enqueue() on the same instance to start a new worker.
            self._stopped = False

    @property
    def active_run_id(self) -> str | None:
        with self._lock:
            return self._active_run_id

    def pending_run_ids(self) -> list[str]:
        with self._lock:
            return [run_id for run_id, _, _ in self._pending]

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def remove_pending(self, run_id: str) -> bool:
        """Drop a queued run from the pending deque (idempotent)."""
        with self._cond:
            before = len(self._pending)
            self._pending = deque(
                (item for item in self._pending if item[0] != run_id)
            )
            removed = len(self._pending) != before
            if removed:
                logger.info(
                    (
                        "lifecycle run_id=%s event=removed_from_pending "
                        "api_pid=%s"
                    ),
                    run_id,
                    os.getpid(),
                )
            return removed

    def is_active(self) -> bool:
        with self._lock:
            return self._active_run_id is not None

    def _ensure_worker_locked(self) -> bool:
        if self._worker is not None and self._worker.is_alive():
            return False
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="mission-control-run-queue",
            daemon=True,
        )
        self._worker.start()
        return True

    def _release_worker_if_current_locked(self) -> None:
        """Clear ``_worker`` under the condition lock when this thread exits.

        Must run while holding ``_cond`` so a concurrent ``enqueue()`` either
        still sees a live worker (and leaves work in ``_pending``) or sees
        ``None`` and starts a replacement — never drops a queued run.
        """
        if self._worker is threading.current_thread():
            self._worker = None

    def _worker_loop(self) -> None:
        while True:
            with self._cond:
                while not self._pending and not self._stopped:
                    self._cond.wait()
                if not self._pending:
                    # Stopped, or idle after notify with an empty queue.
                    self._active_run_id = None
                    self._release_worker_if_current_locked()
                    return
                run_id, mission, registry = self._pending.popleft()
                self._active_run_id = run_id
                execute_fn = self._execute_fn

            assert execute_fn is not None
            registry_id, registry_count, registry_keys = _registry_log_fields(
                registry
            )
            logger.info(
                (
                    "lifecycle run_id=%s event=dequeued queue_depth=%s "
                    "api_pid=%s registry_id=%s registry_count=%s"
                ),
                run_id,
                self.pending_count(),
                os.getpid(),
                registry_id,
                registry_count,
            )
            logger.info(
                (
                    "lifecycle run_id=%s event=worker_entered "
                    "api_pid=%s registry_id=%s registry_count=%s "
                    "registry_keys=%s"
                ),
                run_id,
                os.getpid(),
                registry_id,
                registry_count,
                registry_keys,
            )
            try:
                execute_fn(run_id, mission, registry)
            except Exception:
                logger.exception(
                    (
                        "lifecycle run_id=%s event=worker_error "
                        "api_pid=%s registry_id=%s"
                    ),
                    run_id,
                    os.getpid(),
                    registry_id,
                )
            finally:
                with self._cond:
                    self._active_run_id = None
                    if self._pending and not self._stopped:
                        self._cond.notify_all()
                    else:
                        # Exit when idle so tests/full suites do not accumulate
                        # daemon worker threads (each counts toward NPROC).
                        self._release_worker_if_current_locked()
                        self._cond.notify_all()
                        return
