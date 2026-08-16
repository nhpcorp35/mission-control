"""Lifespan-managed server-side workflow reconciler (orchestration slice 3).

Progresses durable workflows independently of HTTP/MCP/ChatGPT turns.
SQLite CAS / dispatch leases are the multi-replica correctness boundary;
process-local leadership only reduces duplicate work.

Feature flag ``MISSION_CONTROL_WORKFLOW_ORCHESTRATION`` remains off by
default — the worker must not start unless explicitly enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
import random
import sqlite3
import threading
import time
from typing import Any, Mapping

from mission_control.run_registry import RunRecord, RunRegistry, RunStatus
from mission_control.workflow_materializer import (
    MaterializeOutcome,
    SupportsEnqueue,
    materialize_claimed_child,
    redrive_materialized_dispatch,
)
from mission_control.workflow_orchestrator import (
    ChildRunView,
    WorkflowOrchestrator,
)
from mission_control.workflow_registry import (
    DEFAULT_RECONCILE_INTERVAL_SECONDS,
    TERMINAL_WORKFLOW_STATES,
    StepMaterializationState,
    StepStatus,
    WORKFLOW_ORCHESTRATION_ENV,
    WORKFLOW_RECONCILE_INTERVAL_ENV,
    WorkflowRecord,
    WorkflowRegistry,
    WorkflowStepRecord,
    is_terminal_workflow_state,
    is_workflow_orchestration_enabled,
    resolve_reconcile_interval_seconds,
)

# Instance flag so two reconcilers sharing a registry cannot wrap twice.
_WORKFLOW_TERMINAL_LATCH_ATTR = "_mission_control_workflow_terminal_latch"

logger = logging.getLogger(__name__)

# Bounded tick controls (safe defaults; overridable via env).
WORKFLOW_RECONCILE_BATCH_ENV = "MISSION_CONTROL_WORKFLOW_RECONCILE_BATCH_SIZE"
WORKFLOW_RECONCILE_MAX_TICK_ENV = (
    "MISSION_CONTROL_WORKFLOW_RECONCILE_MAX_TICK_SECONDS"
)
DEFAULT_RECONCILE_BATCH_SIZE = 16
DEFAULT_RECONCILE_MAX_TICK_SECONDS = 2.0
MIN_RECONCILE_INTERVAL_SECONDS = 0.5
MIN_RECONCILE_BATCH_SIZE = 1
MAX_RECONCILE_BATCH_SIZE = 64
MIN_RECONCILE_MAX_TICK_SECONDS = 0.05
MAX_RECONCILE_MAX_TICK_SECONDS = 30.0

# Process-local poison isolation (not a correctness boundary).
POISON_FAILURE_THRESHOLD = 3
POISON_SKIP_SECONDS = 30.0

# Infrastructure error backoff (busy-poll prevention).
INFRA_BACKOFF_BASE_SECONDS = 0.5
INFRA_BACKOFF_MAX_SECONDS = 30.0
INFRA_JITTER_RATIO = 0.25


@dataclass
class ReconcileTickStats:
    """Lightweight, secret-safe counters for one bounded tick."""

    workflows_considered: int = 0
    workflows_processed: int = 0
    workflows_skipped_poison: int = 0
    workflows_errors: int = 0
    decisions_applied: int = 0
    materializations: int = 0
    redrives: int = 0
    intents_finalized: int = 0
    timed_out: bool = False
    feature_disabled: bool = False
    infra_error: str | None = None
    elapsed_seconds: float = 0.0


@dataclass
class _PoisonState:
    consecutive_failures: int = 0
    skip_until: float = 0.0


@dataclass
class WorkflowReconcilerCounters:
    """Process-lifetime counters (secret-safe; never log YAML/findings)."""

    ticks: int = 0
    workflows_processed: int = 0
    decisions_applied: int = 0
    materializations: int = 0
    redrives: int = 0
    intents_finalized: int = 0
    workflow_errors: int = 0
    infra_errors: int = 0
    poison_skips: int = 0


def resolve_reconcile_batch_size(
    environ: Mapping[str, str] | None = None,
) -> int:
    env = environ if environ is not None else os.environ
    raw = env.get(WORKFLOW_RECONCILE_BATCH_ENV)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_RECONCILE_BATCH_SIZE
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_RECONCILE_BATCH_SIZE
    return max(MIN_RECONCILE_BATCH_SIZE, min(MAX_RECONCILE_BATCH_SIZE, value))


def resolve_reconcile_max_tick_seconds(
    environ: Mapping[str, str] | None = None,
) -> float:
    env = environ if environ is not None else os.environ
    raw = env.get(WORKFLOW_RECONCILE_MAX_TICK_ENV)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_RECONCILE_MAX_TICK_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_RECONCILE_MAX_TICK_SECONDS
    return max(
        MIN_RECONCILE_MAX_TICK_SECONDS,
        min(MAX_RECONCILE_MAX_TICK_SECONDS, value),
    )


def child_run_view_from_record(record: RunRecord) -> ChildRunView:
    """Project a RunRegistry row into a secret-bounded ChildRunView."""
    status = (
        record.status.value
        if isinstance(record.status, RunStatus)
        else str(record.status)
    )
    return ChildRunView(
        run_id=record.run_id,
        status=status,
        error=record.error,
        stdout=record.stdout or "",
        stderr=record.stderr or "",
        elapsed_seconds=record.elapsed_seconds,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _status_value(status: Any) -> str:
    if isinstance(status, RunStatus):
        return status.value
    if hasattr(status, "value"):
        return str(status.value)
    return str(status)


def _is_execution_observed(record: RunRecord | None) -> bool:
    if record is None:
        return False
    status = _status_value(record.status)
    return status in {"running", "completed", "failed", "timed_out"}


class WorkflowReconciler:
    """Background worker: observe terminals, claim/materialize, redrive.

    Start only when the workflow feature flag is explicitly enabled.
    ``tick_once`` is the deterministic unit used by tests and the loop.
    """

    def __init__(
        self,
        *,
        workflow_registry: WorkflowRegistry,
        run_registry: RunRegistry,
        run_queue: SupportsEnqueue,
        interval_seconds: float | None = None,
        batch_size: int | None = None,
        max_tick_seconds: float | None = None,
        environ: Mapping[str, str] | None = None,
        name: str = "workflow-reconciler",
        orchestrator: WorkflowOrchestrator | None = None,
    ) -> None:
        self._workflow_registry = workflow_registry
        self._run_registry = run_registry
        self._run_queue = run_queue
        self._environ = dict(environ) if environ is not None else None
        self._name = name
        self._orchestrator = orchestrator or WorkflowOrchestrator(
            workflow_registry
        )
        env = self._environ
        raw_interval = (
            float(interval_seconds)
            if interval_seconds is not None
            else resolve_reconcile_interval_seconds(env)
        )
        # Normalize at construction so property/readers see the documented floor,
        # not only the sleep path.
        self._interval_seconds = max(
            MIN_RECONCILE_INTERVAL_SECONDS, raw_interval
        )
        self._batch_size = (
            int(batch_size)
            if batch_size is not None
            else resolve_reconcile_batch_size(env)
        )
        self._max_tick_seconds = (
            float(max_tick_seconds)
            if max_tick_seconds is not None
            else resolve_reconcile_max_tick_seconds(env)
        )
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self._fairness_cursor = 0
        self._poison: dict[str, _PoisonState] = {}
        self._infra_backoff_seconds = 0.0
        self.counters = WorkflowReconcilerCounters()
        self._install_enqueue_before_notification_latch()

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def max_tick_seconds(self) -> float:
        return self._max_tick_seconds

    def kick(self) -> None:
        """Signal that eligible work may exist (non-blocking)."""
        self._wake.set()

    def start(self) -> bool:
        """Start the background loop. Returns False if disabled or running.

        Idempotent and concurrency-safe with ``stop``: a still-alive worker
        (including after a timed-out join) blocks a new start so clearing the
        stop event cannot revive an overlapping loop.
        """
        with self._lifecycle_lock:
            if not is_workflow_orchestration_enabled(self._environ):
                logger.info(
                    (
                        "workflow event=reconciler_skip "
                        "reason=feature_disabled"
                    )
                )
                return False
            existing = self._thread
            if existing is not None and existing.is_alive():
                return False
            # Drop a dead thread reference before starting a replacement.
            self._thread = None
            self._stop.clear()
            self._wake.set()
            thread = threading.Thread(
                target=self._run,
                name=self._name,
                daemon=True,
            )
            self._thread = thread
            thread.start()
            logger.info(
                (
                    "workflow event=reconciler_started interval_s=%s "
                    "batch_size=%s max_tick_s=%s"
                ),
                self._interval_seconds,
                self._batch_size,
                self._max_tick_seconds,
            )
            return True

    def stop(self, *, timeout: float = 5.0) -> None:
        """Cancel the loop and await thread exit (safe if never started).

        On join timeout the thread reference is retained so a later ``start``
        cannot clear the stop event and overlap with the still-running loop.
        Once that thread exits, a subsequent ``start`` may restart cleanly.
        """
        with self._lifecycle_lock:
            self._stop.set()
            self._wake.set()
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lifecycle_lock:
            # Only release the slot when this stop's thread has exited; a
            # timed-out join keeps ``_thread`` so start() stays refused.
            if self._thread is thread and (
                thread is None or not thread.is_alive()
            ):
                self._thread = None
                logger.info("workflow event=reconciler_stopped")

    def tick_once(self) -> ReconcileTickStats:
        """One bounded reconcile pass (serialized)."""
        with self._tick_lock:
            return self._tick_unlocked()

    def _env(self) -> dict[str, str] | None:
        return self._environ

    def _tick_unlocked(self) -> ReconcileTickStats:
        started = time.monotonic()
        stats = ReconcileTickStats()
        if not is_workflow_orchestration_enabled(self._environ):
            stats.feature_disabled = True
            stats.elapsed_seconds = time.monotonic() - started
            return stats

        try:
            workflows = list(self._workflow_registry.list_active_workflows())
        except (sqlite3.Error, OSError) as exc:
            stats.infra_error = type(exc).__name__
            self.counters.infra_errors += 1
            self._note_infra_failure()
            logger.info(
                "workflow event=reconciler_infra_error error_class=%s",
                type(exc).__name__,
            )
            stats.elapsed_seconds = time.monotonic() - started
            return stats

        ordered = self._fair_order(workflows)
        stats.workflows_considered = len(ordered)
        deadline = started + self._max_tick_seconds
        processed_ids: set[str] = set()

        for workflow in ordered:
            if time.monotonic() >= deadline:
                stats.timed_out = True
                break
            if len(processed_ids) >= self._batch_size:
                break
            wid = workflow.workflow_id
            if self._is_poison_skipped(wid):
                stats.workflows_skipped_poison += 1
                self.counters.poison_skips += 1
                continue
            try:
                applied = self._reconcile_one(wid, deadline=deadline)
                stats.workflows_processed += 1
                stats.decisions_applied += applied.get("decisions", 0)
                stats.materializations += applied.get("materializations", 0)
                stats.redrives += applied.get("redrives", 0)
                stats.intents_finalized += applied.get("finalized", 0)
                processed_ids.add(wid)
                self._clear_poison(wid)
            except (sqlite3.Error, OSError) as exc:
                stats.infra_error = type(exc).__name__
                self.counters.infra_errors += 1
                self._note_infra_failure()
                logger.info(
                    (
                        "workflow event=reconciler_infra_error "
                        "workflow_id=%s error_class=%s"
                    ),
                    wid,
                    type(exc).__name__,
                )
                break
            except Exception:  # noqa: BLE001 — isolate per workflow
                stats.workflows_errors += 1
                self.counters.workflow_errors += 1
                self._note_poison(wid)
                logger.info(
                    (
                        "workflow event=reconciler_workflow_error "
                        "workflow_id=%s"
                    ),
                    wid,
                )

        # Redrive any due intents whose workflow was outside this batch.
        if time.monotonic() < deadline and stats.infra_error is None:
            try:
                extra = self._redrive_due_intents(
                    exclude_workflows=processed_ids,
                    deadline=deadline,
                    remaining=max(0, self._batch_size - len(processed_ids)),
                )
                stats.redrives += extra.get("redrives", 0)
                stats.intents_finalized += extra.get("finalized", 0)
            except (sqlite3.Error, OSError) as exc:
                stats.infra_error = type(exc).__name__
                self.counters.infra_errors += 1
                self._note_infra_failure()
                logger.info(
                    "workflow event=reconciler_infra_error error_class=%s",
                    type(exc).__name__,
                )
            except Exception:  # noqa: BLE001
                stats.workflows_errors += 1
                self.counters.workflow_errors += 1
                logger.info("workflow event=reconciler_redrive_error")

        if stats.infra_error is None:
            self._emit_unnotified_terminal_workflows(
                exclude_ids=processed_ids,
                remaining=max(0, self._batch_size),
            )

        if stats.infra_error is None:
            self._infra_backoff_seconds = 0.0

        self.counters.ticks += 1
        self.counters.workflows_processed += stats.workflows_processed
        self.counters.decisions_applied += stats.decisions_applied
        self.counters.materializations += stats.materializations
        self.counters.redrives += stats.redrives
        self.counters.intents_finalized += stats.intents_finalized

        stats.elapsed_seconds = time.monotonic() - started
        logger.info(
            (
                "workflow event=reconciler_tick considered=%s processed=%s "
                "decisions=%s materializations=%s redrives=%s finalized=%s "
                "errors=%s poison_skips=%s timed_out=%s elapsed_s=%.3f"
            ),
            stats.workflows_considered,
            stats.workflows_processed,
            stats.decisions_applied,
            stats.materializations,
            stats.redrives,
            stats.intents_finalized,
            stats.workflows_errors,
            stats.workflows_skipped_poison,
            int(stats.timed_out),
            stats.elapsed_seconds,
        )
        return stats

    def _fair_order(self, workflows: list[Any]) -> list[Any]:
        """Rotate active workflows so poison work cannot starve others."""
        if not workflows:
            self._fairness_cursor = 0
            return []
        n = len(workflows)
        start = self._fairness_cursor % n
        ordered = workflows[start:] + workflows[:start]
        # Advance cursor for the next tick (fair round-robin).
        self._fairness_cursor = (start + 1) % n
        return ordered

    def _reconcile_one(
        self, workflow_id: str, *, deadline: float
    ) -> dict[str, int]:
        out = {
            "decisions": 0,
            "materializations": 0,
            "redrives": 0,
            "finalized": 0,
        }
        steps = self._workflow_registry.list_steps(workflow_id)
        child_runs = self._child_runs_for_steps(steps)
        applied = self._orchestrator.reconcile_workflow(
            workflow_id,
            child_runs=child_runs,
            now=_utc_now(),
        )
        out["decisions"] = len(applied)

        # Refresh steps after claim / mark mutations.
        steps = self._workflow_registry.list_steps(workflow_id)
        for step in steps:
            if time.monotonic() >= deadline:
                break
            if step.status is StepStatus.CLAIMED and (
                step.materialization_state
                is StepMaterializationState.CLAIMED
            ):
                result = materialize_claimed_child(
                    workflow_registry=self._workflow_registry,
                    run_registry=self._run_registry,
                    run_queue=self._run_queue,
                    workflow_id=workflow_id,
                    step_id=step.step_id,
                    environ=self._environ,
                )
                if result.outcome not in {
                    MaterializeOutcome.FEATURE_DISABLED,
                    MaterializeOutcome.NOT_FOUND,
                    MaterializeOutcome.NOT_CLAIMED,
                    MaterializeOutcome.WORKFLOW_TERMINAL,
                }:
                    out["materializations"] += 1
                if result.dispatch_state == "acked" or (
                    result.outcome
                    is MaterializeOutcome.ALREADY_MATERIALIZED
                    and result.reason == "dispatch_acked"
                ):
                    out["finalized"] += 1
                continue

            if (
                step.materialization_state
                is StepMaterializationState.MATERIALIZED
                and step.child_run_id
            ):
                intent = self._workflow_registry.get_dispatch_intent(
                    step.child_run_id
                )
                record = self._run_registry.get_run(step.child_run_id)
                needs_redrive = intent is not None and intent.state.value in {
                    "pending",
                    "leased",
                }
                if not needs_redrive and not (
                    intent is not None
                    and intent.state.value != "acked"
                    and _is_execution_observed(record)
                ):
                    continue
                result = redrive_materialized_dispatch(
                    workflow_registry=self._workflow_registry,
                    run_registry=self._run_registry,
                    run_queue=self._run_queue,
                    workflow_id=workflow_id,
                    step_id=step.step_id,
                    environ=self._environ,
                )
                out["redrives"] += 1
                if result.dispatch_state == "acked" or (
                    result.outcome
                    is MaterializeOutcome.ALREADY_MATERIALIZED
                    and result.reason == "dispatch_acked"
                ):
                    out["finalized"] += 1
        self._emit_durable_workflow_terminal(workflow_id)
        return out

    def _redrive_due_intents(
        self,
        *,
        exclude_workflows: set[str],
        deadline: float,
        remaining: int,
    ) -> dict[str, int]:
        out = {"redrives": 0, "finalized": 0}
        if remaining <= 0:
            return out
        due = self._workflow_registry.list_redrivable_dispatch_intents(
            limit=remaining
        )
        seen_workflows: set[str] = set()
        for intent in due:
            if time.monotonic() >= deadline:
                break
            if intent.workflow_id in exclude_workflows:
                continue
            if intent.workflow_id in seen_workflows:
                continue
            if self._is_poison_skipped(intent.workflow_id):
                continue
            seen_workflows.add(intent.workflow_id)
            try:
                result = redrive_materialized_dispatch(
                    workflow_registry=self._workflow_registry,
                    run_registry=self._run_registry,
                    run_queue=self._run_queue,
                    workflow_id=intent.workflow_id,
                    step_id=intent.step_id,
                    child_run_id=intent.child_run_id,
                    environ=self._environ,
                )
                out["redrives"] += 1
                if result.dispatch_state == "acked" or (
                    result.outcome
                    is MaterializeOutcome.ALREADY_MATERIALIZED
                    and result.reason == "dispatch_acked"
                ):
                    out["finalized"] += 1
                self._clear_poison(intent.workflow_id)
            except Exception:  # noqa: BLE001
                self._note_poison(intent.workflow_id)
                logger.info(
                    (
                        "workflow event=reconciler_workflow_error "
                        "workflow_id=%s phase=redrive"
                    ),
                    intent.workflow_id,
                )
        return out

    def _child_runs_for_steps(
        self, steps: list[WorkflowStepRecord]
    ) -> dict[str, ChildRunView]:
        views: dict[str, ChildRunView] = {}
        for step in steps:
            child_run_id = step.child_run_id
            if not child_run_id:
                continue
            record = self._run_registry.get_run(child_run_id)
            if record is None:
                continue
            views[child_run_id] = child_run_view_from_record(record)
        return views

    def _install_enqueue_before_notification_latch(self) -> None:
        """Enqueue the durable terminal row before the registry latch CAS.

        Orchestrator still calls ``mark_notification_emitted``; wrapping
        that API keeps enqueue/mark restart-safe without schema changes.
        """
        registry = self._workflow_registry
        if getattr(registry, _WORKFLOW_TERMINAL_LATCH_ATTR, False):
            return
        original = registry.mark_notification_emitted
        reconciler = self

        def mark_notification_emitted(
            workflow_id: str, *, expected_version: int
        ):
            workflow = registry.get_workflow(workflow_id)
            reconciler._enqueue_workflow_terminal(workflow)
            return original(
                workflow_id, expected_version=expected_version
            )

        registry.mark_notification_emitted = mark_notification_emitted
        setattr(registry, _WORKFLOW_TERMINAL_LATCH_ATTR, True)

    def _notification_outbox(self):
        return self._run_registry._get_notification_outbox()

    def _enqueue_workflow_terminal(
        self, workflow: WorkflowRecord | None
    ) -> None:
        if workflow is None:
            return
        if not is_terminal_workflow_state(workflow.state):
            return
        self._notification_outbox().maybe_enqueue_workflow_terminal(workflow)

    def _emit_durable_workflow_terminal(self, workflow_id: str) -> None:
        """Enqueue first, then latch. Safe after restarts and races."""
        workflow = self._workflow_registry.get_workflow(workflow_id)
        if workflow is None:
            return
        if not is_terminal_workflow_state(workflow.state):
            return
        self._enqueue_workflow_terminal(workflow)
        latest = self._workflow_registry.get_workflow(workflow_id)
        if latest is None or latest.notification_emitted:
            return
        self._workflow_registry.mark_notification_emitted(
            workflow_id,
            expected_version=latest.version,
        )

    def _unnotified_terminal_workflow_ids(self, *, limit: int) -> list[str]:
        """Discover terminal workflows whose alert latch is still clear."""
        if limit <= 0:
            return []
        states = tuple(sorted(TERMINAL_WORKFLOW_STATES))
        placeholders = ",".join("?" for _ in states)
        try:
            conn = sqlite3.connect(self._workflow_registry.db_path)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout = 5000")
                rows = conn.execute(
                    f"""
                    SELECT workflow_id FROM workflows
                    WHERE notification_emitted = 0
                      AND state IN ({placeholders})
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (*states, int(limit)),
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return []
        return [str(row["workflow_id"]) for row in rows]

    def _emit_unnotified_terminal_workflows(
        self, *, exclude_ids: set[str], remaining: int
    ) -> None:
        if remaining <= 0:
            return
        for workflow_id in self._unnotified_terminal_workflow_ids(
            limit=remaining
        ):
            if workflow_id in exclude_ids:
                continue
            try:
                self._emit_durable_workflow_terminal(workflow_id)
            except Exception:  # noqa: BLE001 — isolate per workflow
                self._note_poison(workflow_id)
                logger.info(
                    (
                        "workflow event=reconciler_workflow_error "
                        "workflow_id=%s phase=terminal_notify"
                    ),
                    workflow_id,
                )

    def _is_poison_skipped(self, workflow_id: str) -> bool:
        state = self._poison.get(workflow_id)
        if state is None:
            return False
        if state.consecutive_failures < POISON_FAILURE_THRESHOLD:
            return False
        return time.monotonic() < state.skip_until

    def _note_poison(self, workflow_id: str) -> None:
        state = self._poison.setdefault(workflow_id, _PoisonState())
        state.consecutive_failures += 1
        if state.consecutive_failures >= POISON_FAILURE_THRESHOLD:
            state.skip_until = time.monotonic() + POISON_SKIP_SECONDS
            logger.info(
                (
                    "workflow event=reconciler_poison_skip workflow_id=%s "
                    "failures=%s skip_s=%s"
                ),
                workflow_id,
                state.consecutive_failures,
                POISON_SKIP_SECONDS,
            )

    def _clear_poison(self, workflow_id: str) -> None:
        self._poison.pop(workflow_id, None)

    def _note_infra_failure(self) -> None:
        if self._infra_backoff_seconds <= 0:
            self._infra_backoff_seconds = INFRA_BACKOFF_BASE_SECONDS
        else:
            self._infra_backoff_seconds = min(
                INFRA_BACKOFF_MAX_SECONDS,
                self._infra_backoff_seconds * 2.0,
            )

    def _sleep_seconds(self) -> float:
        base = max(MIN_RECONCILE_INTERVAL_SECONDS, self._interval_seconds)
        delay = max(base, self._infra_backoff_seconds)
        jitter = delay * INFRA_JITTER_RATIO * random.random()
        return delay + jitter

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick_once()
            self._wake.clear()
            self._wake.wait(timeout=self._sleep_seconds())


def build_default_workflow_reconciler(
    *,
    run_registry: RunRegistry,
    run_queue: SupportsEnqueue,
    workflow_registry: WorkflowRegistry | None = None,
    environ: Mapping[str, str] | None = None,
) -> WorkflowReconciler:
    """Construct a reconciler using the shared Mission Control DB path."""
    registry = workflow_registry or WorkflowRegistry()
    return WorkflowReconciler(
        workflow_registry=registry,
        run_registry=run_registry,
        run_queue=run_queue,
        environ=environ,
    )


__all__ = [
    "DEFAULT_RECONCILE_BATCH_SIZE",
    "DEFAULT_RECONCILE_INTERVAL_SECONDS",
    "DEFAULT_RECONCILE_MAX_TICK_SECONDS",
    "MIN_RECONCILE_INTERVAL_SECONDS",
    "POISON_FAILURE_THRESHOLD",
    "POISON_SKIP_SECONDS",
    "ReconcileTickStats",
    "WORKFLOW_ORCHESTRATION_ENV",
    "WORKFLOW_RECONCILE_BATCH_ENV",
    "WORKFLOW_RECONCILE_INTERVAL_ENV",
    "WORKFLOW_RECONCILE_MAX_TICK_ENV",
    "WorkflowReconciler",
    "WorkflowReconcilerCounters",
    "build_default_workflow_reconciler",
    "child_run_view_from_record",
    "resolve_reconcile_batch_size",
    "resolve_reconcile_interval_seconds",
    "resolve_reconcile_max_tick_seconds",
]
