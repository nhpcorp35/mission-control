"""Focused tests for lifespan workflow reconciler (runtime slice 3)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from mission_control.run_registry import RunRegistry, RunStatus
from mission_control.workflow_materializer import (
    MaterializeOutcome,
    materialize_claimed_child,
)
from mission_control.workflow_orchestrator import (
    format_review_verdict_envelope,
)
from mission_control.workflow_reconciler import (
    DEFAULT_RECONCILE_INTERVAL_SECONDS,
    MIN_RECONCILE_INTERVAL_SECONDS,
    POISON_FAILURE_THRESHOLD,
    WorkflowReconciler,
    resolve_reconcile_batch_size,
    resolve_reconcile_interval_seconds,
    resolve_reconcile_max_tick_seconds,
)
from mission_control.workflow_registry import (
    DispatchIntentState,
    StepMaterializationState,
    StepStatus,
    StepType,
    WorkflowPolicySnapshot,
    WorkflowRegistry,
    WorkflowState,
    WorkflowStepSpec,
    is_workflow_orchestration_enabled,
)

_FEATURE_ON = {"MISSION_CONTROL_WORKFLOW_ORCHESTRATION": "true"}
_FEATURE_OFF = {"MISSION_CONTROL_WORKFLOW_ORCHESTRATION": "false"}


def _policy(**overrides) -> WorkflowPolicySnapshot:
    base = dict(
        repository_name="Mission-Control",
        base_branch="main",
        target_branch="wf/reconciler-v1",
        implementation_scope=("mission_control/", "tests/"),
        allow_auto_merge=False,
        allow_auto_deploy=False,
        max_fix_cycles=2,
        max_child_runs=8,
        max_wall_clock_seconds=3600,
        max_credit_units=8,
        credit_unit_per_child_run=1,
    )
    base.update(overrides)
    return WorkflowPolicySnapshot(**base)


def _mission_yaml(*, instructions: str = "implement the slice") -> str:
    return (
        "version: '1.0'\n"
        "mission_id: wf-reconciler\n"
        "title: Reconciler child\n"
        "execution:\n"
        "  agent: cursor\n"
        "  mode: execute\n"
        "permissions:\n"
        "  read: true\n"
        "  create_files: false\n"
        "  modify_files: false\n"
        "  delete_files: false\n"
        "  run_commands: false\n"
        "  stage_changes: false\n"
        "  commit: false\n"
        "  push: false\n"
        f"instructions: {instructions}\n"
        "deliverables: []\n"
    )


def _specs() -> dict[str, WorkflowStepSpec]:
    """Exact templates; fix/re_review must parse as full missions for materialize."""
    review_yaml = _mission_yaml(instructions="emit MC_REVIEW_VERDICT_V1 envelope")
    # Review/re-review stay read-only (materializer + policy gates enforce).
    review_yaml = review_yaml.replace(
        "permissions:\n"
        "  read: true\n"
        "  create_files: false\n"
        "  modify_files: false\n",
        "persistence:\n"
        "  mode: none\n"
        "permissions:\n"
        "  read: true\n"
        "  create_files: false\n"
        "  modify_files: false\n",
    )
    return {
        "implementation": WorkflowStepSpec(
            step_type=StepType.IMPLEMENTATION,
            mission_yaml=_mission_yaml(),
        ),
        "review": WorkflowStepSpec(
            step_type=StepType.REVIEW,
            mission_yaml=review_yaml,
        ),
        "fix": WorkflowStepSpec(
            step_type=StepType.FIX,
            mission_yaml=_mission_yaml(instructions="targeted fix only"),
        ),
        "re_review": WorkflowStepSpec(
            step_type=StepType.RE_REVIEW,
            mission_yaml=review_yaml,
        ),
    }


def _blocked(*findings: str) -> str:
    return format_review_verdict_envelope("blocked", findings)


def _merge_ready() -> str:
    return format_review_verdict_envelope("merge_ready")


class RecordingQueue:
    """Capture enqueue calls without starting workers."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, object]] = []
        self._lock = threading.Lock()

    def enqueue(self, run_id: str, mission: dict, registry: object) -> bool:
        with self._lock:
            getter = getattr(registry, "get_run", None)
            if callable(getter):
                record = getter(run_id)
                if record is not None:
                    status = getattr(record, "status", None)
                    status_value = (
                        status.value if hasattr(status, "value") else str(status)
                    )
                    if status_value in {
                        "running",
                        "completed",
                        "failed",
                        "timed_out",
                    }:
                        return False
            if any(existing == run_id for existing, _, _ in self.calls):
                return False
            self.calls.append((run_id, mission, registry))
            return True

    @property
    def run_ids(self) -> list[str]:
        with self._lock:
            return [run_id for run_id, _, _ in self.calls]


class ReconcilerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._wf_fd, self._wf_path = tempfile.mkstemp(suffix="-wf.db")
        self._run_fd, self._run_path = tempfile.mkstemp(suffix="-run.db")
        os.close(self._wf_fd)
        os.close(self._run_fd)
        self.workflow_registry = WorkflowRegistry(self._wf_path)
        self.run_registry = RunRegistry(self._run_path)
        self.queue = RecordingQueue()
        self.parent_id = str(uuid.uuid4())
        self.reconciler = WorkflowReconciler(
            workflow_registry=self.workflow_registry,
            run_registry=self.run_registry,
            run_queue=self.queue,
            environ=_FEATURE_ON,
            interval_seconds=0.5,
            batch_size=8,
            max_tick_seconds=2.0,
        )

    def tearDown(self) -> None:
        self.reconciler.stop(timeout=2.0)
        self.workflow_registry.close()
        self.run_registry.close()
        os.unlink(self._wf_path)
        os.unlink(self._run_path)

    def _create_workflow(self, **policy_overrides):
        specs = _specs()
        return self.workflow_registry.create_workflow(
            policy=_policy(**policy_overrides),
            implementation=specs["implementation"],
            review=specs["review"],
            fix=specs["fix"],
            re_review=specs["re_review"],
            parent_run_id=self.parent_id,
        )

    def _complete_child(
        self,
        child_run_id: str,
        *,
        status: RunStatus = RunStatus.COMPLETED,
        stdout: str = "done",
        error: str | None = None,
    ) -> None:
        self.run_registry.update_status(child_run_id, RunStatus.RUNNING)
        self.run_registry.store_result(
            child_run_id, stdout=stdout, stderr="", error=error
        )
        self.run_registry.update_status(child_run_id, status)

    def _bootstrap_impl_materialized(self):
        """Claim + materialize implementation via reconciler ticks."""
        wf = self._create_workflow()
        stats = self.reconciler.tick_once()
        self.assertFalse(stats.feature_disabled)
        steps = self.workflow_registry.list_steps(wf.workflow_id)
        self.assertEqual(len(steps), 1)
        impl = steps[0]
        self.assertEqual(impl.step_type, StepType.IMPLEMENTATION)
        self.assertEqual(
            impl.materialization_state, StepMaterializationState.MATERIALIZED
        )
        self.assertIn(impl.child_run_id, self.queue.run_ids)
        return wf, impl


class ProgressionTests(ReconcilerTestCase):
    def test_impl_terminal_creates_review_on_later_tick_without_status_api(
        self,
    ) -> None:
        """Req 1: RunRegistry terminal write alone advances on next tick."""
        wf, impl = self._bootstrap_impl_materialized()
        # Simulate worker execution finishing — no mission.status / HTTP.
        self._complete_child(impl.child_run_id, stdout="implementation done")

        before_steps = self.workflow_registry.list_steps(wf.workflow_id)
        self.assertEqual(len(before_steps), 1)

        stats = self.reconciler.tick_once()
        self.assertGreaterEqual(stats.decisions_applied, 1)

        steps = self.workflow_registry.list_steps(wf.workflow_id)
        self.assertEqual(len(steps), 2)
        review = steps[1]
        self.assertEqual(review.step_type, StepType.REVIEW)
        self.assertIsNotNone(review.child_run_id)
        self.assertEqual(
            review.materialization_state,
            StepMaterializationState.MATERIALIZED,
        )
        self.assertIn(review.child_run_id, self.queue.run_ids)
        # Review run exists in registry (queued), created by reconciler.
        review_run = self.run_registry.get_run(review.child_run_id)
        self.assertIsNotNone(review_run)
        self.assertEqual(review_run.status, RunStatus.QUEUED)

    def test_review_blocked_launches_fix_then_rereview_or_approval(
        self,
    ) -> None:
        """Req 2: review terminal decision auto-launches fix / completes."""
        wf, impl = self._bootstrap_impl_materialized()
        self._complete_child(impl.child_run_id)
        self.reconciler.tick_once()
        review = [
            s
            for s in self.workflow_registry.list_steps(wf.workflow_id)
            if s.step_type is StepType.REVIEW
        ][0]
        self._complete_child(
            review.child_run_id,
            stdout=_blocked("missing unit test for CAS"),
        )
        self.reconciler.tick_once()
        fix = [
            s
            for s in self.workflow_registry.list_steps(wf.workflow_id)
            if s.step_type is StepType.FIX
        ][0]
        self.assertEqual(
            fix.materialization_state, StepMaterializationState.MATERIALIZED
        )
        self._complete_child(fix.child_run_id, stdout="fixed")
        self.reconciler.tick_once()
        rereview = [
            s
            for s in self.workflow_registry.list_steps(wf.workflow_id)
            if s.step_type is StepType.RE_REVIEW
        ][0]
        self._complete_child(rereview.child_run_id, stdout=_merge_ready())
        self.reconciler.tick_once()
        final = self.workflow_registry.get_workflow(wf.workflow_id)
        self.assertEqual(final.state, WorkflowState.NEEDS_APPROVAL)

    def test_review_merge_ready_stops_at_needs_approval(self) -> None:
        wf, impl = self._bootstrap_impl_materialized()
        self._complete_child(impl.child_run_id)
        self.reconciler.tick_once()
        review = self.workflow_registry.list_steps(wf.workflow_id)[1]
        self._complete_child(review.child_run_id, stdout=_merge_ready())
        self.reconciler.tick_once()
        self.assertEqual(
            self.workflow_registry.get_workflow(wf.workflow_id).state,
            WorkflowState.NEEDS_APPROVAL,
        )


class StartupRecoveryTests(ReconcilerTestCase):
    def test_recovers_claimed_unmaterialized_step(self) -> None:
        """Req 3a: claimed but not materialized → materialize on tick."""
        wf = self._create_workflow()
        claim = self.workflow_registry.claim_child_launch(
            workflow_id=wf.workflow_id,
            expected_version=wf.version,
            step_type=StepType.IMPLEMENTATION,
            mission_yaml=_specs()["implementation"].mission_yaml,
            cycle=0,
            attempt=1,
            parent_run_id=self.parent_id,
        )
        self.assertTrue(claim.ok)
        step = claim.step
        self.assertEqual(step.status, StepStatus.CLAIMED)
        self.assertEqual(
            step.materialization_state, StepMaterializationState.CLAIMED
        )
        self.assertIsNone(self.run_registry.get_run(step.child_run_id))

        self.reconciler.tick_once()
        latest = self.workflow_registry.get_step(step.step_id)
        self.assertEqual(
            latest.materialization_state,
            StepMaterializationState.MATERIALIZED,
        )
        self.assertIsNotNone(self.run_registry.get_run(step.child_run_id))
        self.assertIn(step.child_run_id, self.queue.run_ids)

    def test_recovers_materialized_queued_and_expired_lease(self) -> None:
        """Req 3b/c: queued child + expired lease redriven without duplicate."""
        wf, impl = self._bootstrap_impl_materialized()
        intent = self.workflow_registry.get_dispatch_intent(impl.child_run_id)
        self.assertIsNotNone(intent)
        # Force pending with expired lease window.
        with self.workflow_registry._lock:
            self.workflow_registry._conn.execute(
                """
                UPDATE workflow_dispatch_intents
                SET state = ?, lease_owner = 'dead-owner',
                    lease_expires_at = ?, next_attempt_at = ?
                WHERE child_run_id = ?
                """,
                (
                    DispatchIntentState.LEASED.value,
                    (
                        datetime.now(timezone.utc) - timedelta(seconds=60)
                    ).isoformat(),
                    (
                        datetime.now(timezone.utc) - timedelta(seconds=1)
                    ).isoformat(),
                    impl.child_run_id,
                ),
            )
            self.workflow_registry._conn.commit()

        # Empty process queue simulates restart; redrive must re-enqueue once.
        self.queue.calls.clear()
        self.reconciler.tick_once()
        self.assertEqual(self.queue.run_ids.count(impl.child_run_id), 1)
        # Still queued in registry — intent remains redrivable / not acked.
        run = self.run_registry.get_run(impl.child_run_id)
        self.assertEqual(run.status, RunStatus.QUEUED)

        # Advance to running → finalize/ack.
        self.run_registry.update_status(impl.child_run_id, RunStatus.RUNNING)
        self.reconciler.tick_once()
        intent = self.workflow_registry.get_dispatch_intent(impl.child_run_id)
        self.assertEqual(intent.state, DispatchIntentState.ACKED)
        # No second enqueue after execution observed.
        self.assertEqual(self.queue.run_ids.count(impl.child_run_id), 1)
        _ = wf

    def test_recovers_terminal_child_not_yet_reconciled(self) -> None:
        """Req 3d: terminal child written before reconciler saw it."""
        wf, impl = self._bootstrap_impl_materialized()
        self._complete_child(impl.child_run_id)
        # Crash window: terminal in RunRegistry, workflow still has active step.
        active = self.workflow_registry.get_step(impl.step_id)
        self.assertEqual(active.status, StepStatus.QUEUED)
        self.reconciler.tick_once()
        steps = self.workflow_registry.list_steps(wf.workflow_id)
        self.assertEqual(steps[0].status, StepStatus.COMPLETED)
        self.assertEqual(steps[1].step_type, StepType.REVIEW)

    def test_recovers_partial_transition_mark_without_followup_claim(
        self,
    ) -> None:
        """Req 3e: completed step without follow-up claim re-derives launch."""
        wf, impl = self._bootstrap_impl_materialized()
        self._complete_child(impl.child_run_id)
        # Manually mark impl completed without claiming review (crash mid-way).
        current = self.workflow_registry.get_workflow(wf.workflow_id)
        from mission_control.workflow_registry import TransitionReason

        result = self.workflow_registry.apply_cas_transition(
            workflow_id=wf.workflow_id,
            expected_version=current.version,
            to_state=WorkflowState.RUNNING,
            reason=TransitionReason.CHILD_STATUS,
            step_id=impl.step_id,
            child_run_id=impl.child_run_id,
            step_updates={"status": StepStatus.COMPLETED},
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(self.workflow_registry.list_steps(wf.workflow_id)), 1)

        self.reconciler.tick_once()
        steps = self.workflow_registry.list_steps(wf.workflow_id)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[1].step_type, StepType.REVIEW)


class ConcurrencyTests(ReconcilerTestCase):
    def test_two_reconcilers_do_not_duplicate_child_or_enqueue(self) -> None:
        """Req 4: multi-replica CAS — one child, at most one enqueue."""
        wf = self._create_workflow()
        reg_b = WorkflowRegistry(self._wf_path)
        run_b = RunRegistry(self._run_path)
        # Shared process-local queue: duplicate enqueue must be suppressed.
        shared_queue = self.queue
        other = WorkflowReconciler(
            workflow_registry=reg_b,
            run_registry=run_b,
            run_queue=shared_queue,
            environ=_FEATURE_ON,
            interval_seconds=0.5,
            batch_size=8,
            max_tick_seconds=2.0,
        )
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(self.reconciler.tick_once),
                    pool.submit(other.tick_once),
                ]
                for fut in futures:
                    fut.result(timeout=10)

            steps = self.workflow_registry.list_steps(wf.workflow_id)
            self.assertEqual(len(steps), 1)
            child_id = steps[0].child_run_id
            self.assertIsNotNone(child_id)
            # Exactly one registry row and one enqueue on the shared queue.
            self.assertIsNotNone(self.run_registry.get_run(child_id))
            self.assertEqual(shared_queue.run_ids.count(child_id), 1)

            # Complete and race review launch.
            self._complete_child(child_id)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(self.reconciler.tick_once),
                    pool.submit(other.tick_once),
                ]
                for fut in futures:
                    fut.result(timeout=10)
            steps = self.workflow_registry.list_steps(wf.workflow_id)
            reviews = [s for s in steps if s.step_type is StepType.REVIEW]
            self.assertEqual(len(reviews), 1)
            review_id = reviews[0].child_run_id
            self.assertEqual(shared_queue.run_ids.count(review_id), 1)
        finally:
            other.stop(timeout=1.0)
            reg_b.close()
            run_b.close()


class LifecycleTests(unittest.TestCase):
    def test_feature_off_start_creates_no_thread(self) -> None:
        """Req 5: feature-off creates no task/activity."""
        self.assertFalse(is_workflow_orchestration_enabled({}))
        wf_fd, wf_path = tempfile.mkstemp(suffix="-wf.db")
        run_fd, run_path = tempfile.mkstemp(suffix="-run.db")
        os.close(wf_fd)
        os.close(run_fd)
        wf_reg = WorkflowRegistry(wf_path)
        run_reg = RunRegistry(run_path)
        try:
            reconciler = WorkflowReconciler(
                workflow_registry=wf_reg,
                run_registry=run_reg,
                run_queue=RecordingQueue(),
                environ=_FEATURE_OFF,
            )
            self.assertFalse(reconciler.start())
            self.assertFalse(reconciler.is_running)
            stats = reconciler.tick_once()
            self.assertTrue(stats.feature_disabled)
            reconciler.stop(timeout=1.0)
            self.assertFalse(reconciler.is_running)
        finally:
            wf_reg.close()
            run_reg.close()
            os.unlink(wf_path)
            os.unlink(run_path)

    def test_enable_starts_and_shutdown_awaits(self) -> None:
        """Req 5: enable starts; stop cancels and joins cleanly."""
        wf_fd, wf_path = tempfile.mkstemp(suffix="-wf.db")
        run_fd, run_path = tempfile.mkstemp(suffix="-run.db")
        os.close(wf_fd)
        os.close(run_fd)
        wf_reg = WorkflowRegistry(wf_path)
        run_reg = RunRegistry(run_path)
        try:
            reconciler = WorkflowReconciler(
                workflow_registry=wf_reg,
                run_registry=run_reg,
                run_queue=RecordingQueue(),
                environ=_FEATURE_ON,
                interval_seconds=0.5,
            )
            self.assertTrue(reconciler.start())
            self.assertTrue(reconciler.is_running)
            # Second start is refused.
            self.assertFalse(reconciler.start())
            reconciler.stop(timeout=2.0)
            self.assertFalse(reconciler.is_running)
        finally:
            wf_reg.close()
            run_reg.close()
            os.unlink(wf_path)
            os.unlink(run_path)

    def test_api_lifespan_respects_feature_flag(self) -> None:
        """Req 5: lifespan does not start reconciler when flag off."""
        import app.api as api_module
        from fastapi.testclient import TestClient
        from app.api import app

        previous = api_module.workflow_reconciler
        wf_fd, wf_path = tempfile.mkstemp(suffix="-wf.db")
        run_fd, run_path = tempfile.mkstemp(suffix="-run.db")
        os.close(wf_fd)
        os.close(run_fd)
        wf_reg = WorkflowRegistry(wf_path)
        run_reg = RunRegistry(run_path)
        queue = RecordingQueue()
        reconciler = WorkflowReconciler(
            workflow_registry=wf_reg,
            run_registry=run_reg,
            run_queue=queue,
            environ=_FEATURE_OFF,
            interval_seconds=0.5,
        )
        api_module.workflow_reconciler = reconciler
        try:
            with patch.dict(os.environ, _FEATURE_OFF, clear=False):
                with TestClient(app):
                    self.assertFalse(reconciler.is_running)
            self.assertFalse(reconciler.is_running)
        finally:
            api_module.workflow_reconciler = previous
            reconciler.stop(timeout=1.0)
            wf_reg.close()
            run_reg.close()
            os.unlink(wf_path)
            os.unlink(run_path)


class BoundsFairnessTests(ReconcilerTestCase):
    def test_interval_floor_and_defaults(self) -> None:
        """Req 6: interval floor 0.5s, default 5s."""
        self.assertEqual(
            resolve_reconcile_interval_seconds({}),
            DEFAULT_RECONCILE_INTERVAL_SECONDS,
        )
        self.assertEqual(
            resolve_reconcile_interval_seconds(
                {"MISSION_CONTROL_WORKFLOW_RECONCILE_INTERVAL_SECONDS": "0.1"}
            ),
            MIN_RECONCILE_INTERVAL_SECONDS,
        )
        self.assertEqual(
            resolve_reconcile_interval_seconds(
                {"MISSION_CONTROL_WORKFLOW_RECONCILE_INTERVAL_SECONDS": "12"}
            ),
            12.0,
        )
        self.assertEqual(resolve_reconcile_batch_size({}), 16)
        self.assertEqual(resolve_reconcile_max_tick_seconds({}), 2.0)

    def test_batch_and_time_bounds_and_no_tight_loop(self) -> None:
        """Req 6: batch/time bounds; sleep waits (no busy poll)."""
        # Create more workflows than batch size.
        for _ in range(5):
            self._create_workflow()
        tight = WorkflowReconciler(
            workflow_registry=self.workflow_registry,
            run_registry=self.run_registry,
            run_queue=self.queue,
            environ=_FEATURE_ON,
            interval_seconds=0.5,
            batch_size=2,
            max_tick_seconds=2.0,
        )
        stats = tight.tick_once()
        self.assertEqual(stats.workflows_processed, 2)
        self.assertEqual(stats.workflows_considered, 5)

        # Sleep path uses Event.wait with >= floor interval (no busy spin).
        slept: list[float] = []
        original_wait = tight._wake.wait

        def tracking_wait(timeout=None):
            slept.append(float(timeout or 0.0))
            tight._stop.set()
            return original_wait(timeout=0)

        tight._wake.wait = tracking_wait  # type: ignore[method-assign]
        tight._stop.clear()
        tight._run()
        self.assertTrue(slept)
        self.assertGreaterEqual(slept[0], MIN_RECONCILE_INTERVAL_SECONDS)

    def test_fairness_rotates_and_poison_isolation(self) -> None:
        """Req 6: fair ordering + poison isolation across workflows."""
        wfs = [self._create_workflow() for _ in range(3)]
        ids = [w.workflow_id for w in wfs]
        r1 = WorkflowReconciler(
            workflow_registry=self.workflow_registry,
            run_registry=self.run_registry,
            run_queue=self.queue,
            environ=_FEATURE_ON,
            batch_size=3,
            max_tick_seconds=2.0,
            interval_seconds=0.5,
        )
        # Poison workflow 0 so the tick must skip it and still serve others.
        for _ in range(POISON_FAILURE_THRESHOLD):
            r1._note_poison(ids[0])
        self.assertTrue(r1._is_poison_skipped(ids[0]))

        stats = r1.tick_once()
        self.assertGreaterEqual(stats.workflows_skipped_poison, 1)
        self.assertGreaterEqual(stats.workflows_processed, 1)
        self.assertEqual(len(self.workflow_registry.list_steps(ids[0])), 0)
        progressed = sum(
            len(self.workflow_registry.list_steps(wid)) for wid in ids[1:]
        )
        self.assertGreaterEqual(progressed, 1)
        # Fair cursor rotates across ticks (different start offsets).
        order_a = [w.workflow_id for w in r1._fair_order(list(wfs))]
        order_b = [w.workflow_id for w in r1._fair_order(list(wfs))]
        self.assertNotEqual(order_a, order_b)

    def test_infra_error_backoff(self) -> None:
        """Req 6: infrastructure errors set backoff (no tight retry)."""
        self.reconciler._note_infra_failure()
        self.assertGreaterEqual(
            self.reconciler._infra_backoff_seconds, 0.5
        )
        self.reconciler._note_infra_failure()
        self.assertGreaterEqual(
            self.reconciler._infra_backoff_seconds, 1.0
        )
        delay = self.reconciler._sleep_seconds()
        self.assertGreaterEqual(delay, 1.0)


class TerminalStatusTests(ReconcilerTestCase):
    def test_runstatus_completed_failed_timed_out(self) -> None:
        """Req 7: actual RunStatus terminals drive workflow decisions."""
        # completed → review (covered elsewhere); failed → workflow failed
        wf, impl = self._bootstrap_impl_materialized()
        self._complete_child(
            impl.child_run_id,
            status=RunStatus.FAILED,
            stdout="",
            error="boom",
        )
        self.reconciler.tick_once()
        self.assertEqual(
            self.workflow_registry.get_workflow(wf.workflow_id).state,
            WorkflowState.FAILED,
        )

        wf2, impl2 = self._bootstrap_impl_materialized()
        self._complete_child(
            impl2.child_run_id,
            status=RunStatus.TIMED_OUT,
            error="timed_out",
        )
        self.reconciler.tick_once()
        self.assertEqual(
            self.workflow_registry.get_workflow(wf2.workflow_id).state,
            WorkflowState.BLOCKED,
        )
        # RunStatus has no cancelled — cancellation maps via failed path only.
        self.assertNotIn("cancelled", {s.value for s in RunStatus})


class CeilingAndBlockerTests(ReconcilerTestCase):
    def test_child_ceiling_and_repeated_blocker_stop(self) -> None:
        """Req 8: ceilings / repeated blocker / approval boundary."""
        # Child ceiling: max_child_runs=1 → cannot launch review after impl.
        wf = self._create_workflow(max_child_runs=1)
        self.reconciler.tick_once()
        impl = self.workflow_registry.list_steps(wf.workflow_id)[0]
        self._complete_child(impl.child_run_id)
        self.reconciler.tick_once()
        final = self.workflow_registry.get_workflow(wf.workflow_id)
        self.assertEqual(final.state, WorkflowState.BUDGET_EXHAUSTED)

        # Repeated blocker fingerprint → blocked.
        wf2, impl2 = self._bootstrap_impl_materialized()
        self._complete_child(impl2.child_run_id)
        self.reconciler.tick_once()
        review = [
            s
            for s in self.workflow_registry.list_steps(wf2.workflow_id)
            if s.step_type is StepType.REVIEW
        ][0]
        blocked = _blocked("same fingerprint issue")
        self._complete_child(review.child_run_id, stdout=blocked)
        self.reconciler.tick_once()
        fix = [
            s
            for s in self.workflow_registry.list_steps(wf2.workflow_id)
            if s.step_type is StepType.FIX
        ][0]
        self._complete_child(fix.child_run_id, stdout="attempted")
        self.reconciler.tick_once()
        rereview = [
            s
            for s in self.workflow_registry.list_steps(wf2.workflow_id)
            if s.step_type is StepType.RE_REVIEW
        ][0]
        self._complete_child(rereview.child_run_id, stdout=blocked)
        self.reconciler.tick_once()
        self.assertEqual(
            self.workflow_registry.get_workflow(wf2.workflow_id).state,
            WorkflowState.BLOCKED,
        )


class RelatedSuiteSmokeTests(ReconcilerTestCase):
    def test_materializer_still_works_alongside_reconciler(self) -> None:
        """Req 9 (focused): materializer path remains compatible."""
        wf = self._create_workflow()
        claim = self.workflow_registry.claim_child_launch(
            workflow_id=wf.workflow_id,
            expected_version=wf.version,
            step_type=StepType.IMPLEMENTATION,
            mission_yaml=_specs()["implementation"].mission_yaml,
            cycle=0,
            attempt=1,
            parent_run_id=self.parent_id,
        )
        result = materialize_claimed_child(
            workflow_registry=self.workflow_registry,
            run_registry=self.run_registry,
            run_queue=self.queue,
            workflow_id=wf.workflow_id,
            step_id=claim.step.step_id,
            environ=_FEATURE_ON,
            backoff_base_seconds=0.0,
        )
        self.assertIn(
            result.outcome,
            {
                MaterializeOutcome.CREATED,
                MaterializeOutcome.DISPATCH_DEFERRED,
            },
        )


if __name__ == "__main__":
    unittest.main()
