"""FIFO single-active execution queue for asynchronous mission runs.

The queue is process-local and in-memory only. Pending and active work is
lost if the process restarts. At most one Cursor execution runs at a time;
additional accepted runs wait in FIFO order.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# execute(run_id, mission, registry)
ExecuteFn = Callable[[str, dict, Any], None]


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

    def enqueue(self, run_id: str, mission: dict, registry: Any) -> None:
        """Accept a run for FIFO execution.

        Does not start Cursor immediately when another run is already active.
        ``registry`` is captured at enqueue time so workers stay isolated from
        later process-global registry replacements (e.g. in tests).
        """
        with self._cond:
            if self._execute_fn is None:
                raise RuntimeError("RunQueue.configure() must be called first")
            self._pending.append((run_id, mission, registry))
            depth = len(self._pending)
            active = self._active_run_id
            logger.info(
                "lifecycle run_id=%s event=queued queue_depth=%s active_run_id=%s",
                run_id,
                depth,
                active,
            )
            self._ensure_worker_locked()
            self._cond.notify()

    def reset(self) -> None:
        """Drop pending work and clear active state (for tests)."""
        with self._cond:
            self._pending.clear()
            self._active_run_id = None
            self._cond.notify_all()

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

    def is_active(self) -> bool:
        with self._lock:
            return self._active_run_id is not None

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="mission-control-run-queue",
            daemon=True,
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            with self._cond:
                while not self._pending and not self._stopped:
                    self._cond.wait()
                if self._stopped and not self._pending:
                    self._active_run_id = None
                    return
                run_id, mission, registry = self._pending.popleft()
                self._active_run_id = run_id
                execute_fn = self._execute_fn

            assert execute_fn is not None
            logger.info(
                "lifecycle run_id=%s event=dequeued queue_depth=%s",
                run_id,
                self.pending_count(),
            )
            try:
                execute_fn(run_id, mission, registry)
            except Exception:
                logger.exception(
                    "lifecycle run_id=%s event=worker_error",
                    run_id,
                )
            finally:
                with self._cond:
                    self._active_run_id = None
                    self._cond.notify_all()
