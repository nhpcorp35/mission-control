"""Focused tests for crash-safe workflow child materialization (slice 2)."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
import uuid

from mission_control.run_registry import (
    CONFLICT_MISSION_YAML_MISMATCH,
    CONFLICT_OWNERSHIP_MISMATCH,
    RunRegistry,
)
from mission_control.workflow_materializer import (
    MaterializeCrashHooks,
    MaterializeOutcome,
    POISON_CAS_MAX_ATTEMPTS,
    _poison,
    materialize_claimed_child,
    redrive_materialized_dispatch,
)
from mission_control.workflow_orchestrator import WorkflowOrchestrator
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
from mission_control.run_queue import RunQueue
from mission_control.run_registry import RunStatus

_FEATURE_ON = {"MISSION_CONTROL_WORKFLOW_ORCHESTRATION": "true"}


def _policy(**overrides) -> WorkflowPolicySnapshot:
    base = dict(
        repository_name="Mission-Control",
        base_branch="main",
        target_branch="wf/orchestrator-v1",
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


def _mission_yaml(
    *,
    repository_name: str | None = None,
    create_files: bool = False,
    instructions: str = "implement the slice",
) -> str:
    """Build a claim-safe mission body.

    Avoid nested ``repository: / name:`` mappings — the authority-injection
    scanner treats multiline ``repository:`` blocks as a mismatched name.
    Optional ``repository_name`` is a single-line key for denial tests.
    """
    create = "true" if create_files else "false"
    repo_line = (
        f"repository_name: {repository_name}\n"
        if repository_name is not None
        else ""
    )
    return (
        "version: '1.0'\n"
        "mission_id: wf-materialize\n"
        "title: Materialize child\n"
        f"{repo_line}"
        "execution:\n"
        "  agent: cursor\n"
        "  mode: execute\n"
        "permissions:\n"
        "  read: true\n"
        f"  create_files: {create}\n"
        "  modify_files: false\n"
        "  delete_files: false\n"
        "  run_commands: false\n"
        "  stage_changes: false\n"
        "  commit: false\n"
        "  push: false\n"
        f"instructions: {instructions}\n"
        "deliverables: []\n"
    )


def _specs(mission_yaml: str | None = None) -> dict[str, WorkflowStepSpec]:
    impl_yaml = mission_yaml or _mission_yaml()
    return {
        "implementation": WorkflowStepSpec(
            step_type=StepType.IMPLEMENTATION,
            mission_yaml=impl_yaml,
        ),
        "review": WorkflowStepSpec(
            step_type=StepType.REVIEW,
            mission_yaml=(
                "mission: review\n"
                "permissions:\n  create_files: false\n"
                "  modify_files: false\n"
                "persistence:\n  mode: none\n"
                "instructions: emit MC_REVIEW_VERDICT_V1 envelope\n"
            ),
        ),
        "fix": WorkflowStepSpec(
            step_type=StepType.FIX,
            mission_yaml="mission: fix\ninstructions: targeted fix only\n",
        ),
        "re_review": WorkflowStepSpec(
            step_type=StepType.RE_REVIEW,
            mission_yaml=(
                "mission: re-review\n"
                "persistence:\n  mode: none\n"
                "permissions:\n  create_files: false\n"
                "  modify_files: false\n"
                "instructions: emit MC_REVIEW_VERDICT_V1 envelope\n"
            ),
        ),
    }


class RecordingQueue:
    """Capture enqueue calls without starting workers."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, object]] = []
        self._lock = threading.Lock()
        self._failures_remaining: int = 0

    def enqueue(self, run_id: str, mission: dict, registry: object) -> bool:
        with self._lock:
            if self._failures_remaining > 0:
                self._failures_remaining -= 1
                raise RuntimeError("enqueue_boom")
            if any(existing == run_id for existing, _, _ in self.calls):
                return False
            # Mirror RunQueue registry suppress rules for durable redrive tests.
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
            self.calls.append((run_id, mission, registry))
            return True

    def fail_next(self, count: int = 1) -> None:
        with self._lock:
            self._failures_remaining = count

    @property
    def run_ids(self) -> list[str]:
        with self._lock:
            return [run_id for run_id, _, _ in self.calls]


class MaterializeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._wf_fd, self._wf_path = tempfile.mkstemp(suffix="-wf.db")
        self._run_fd, self._run_path = tempfile.mkstemp(suffix="-run.db")
        os.close(self._wf_fd)
        os.close(self._run_fd)
        self.workflow_registry = WorkflowRegistry(self._wf_path)
        self.run_registry = RunRegistry(self._run_path)
        self.queue = RecordingQueue()
        self.orch = WorkflowOrchestrator(self.workflow_registry)
        self.parent_id = str(uuid.uuid4())

    def tearDown(self) -> None:
        self.workflow_registry.close()
        self.run_registry.close()
        os.unlink(self._wf_path)
        os.unlink(self._run_path)

    def _create_and_claim(
        self,
        *,
        parent_run_id: str | None = ...,
        mission_yaml: str | None = None,
    ):
        parent = self.parent_id if parent_run_id is ... else parent_run_id
        specs = _specs(mission_yaml)
        wf = self.workflow_registry.create_workflow(
            policy=_policy(),
            implementation=specs["implementation"],
            review=specs["review"],
            fix=specs["fix"],
            re_review=specs["re_review"],
            parent_run_id=parent,
        )
        claim = self.workflow_registry.claim_child_launch(
            workflow_id=wf.workflow_id,
            expected_version=wf.version,
            step_type=StepType.IMPLEMENTATION,
            mission_yaml=specs["implementation"].mission_yaml,
            cycle=0,
            attempt=1,
            parent_run_id=parent,
        )
        self.assertTrue(claim.ok, claim.error)
        assert claim.step is not None
        assert claim.workflow is not None
        return claim.workflow, claim.step

    def _materialize(self, workflow_id: str, step_id: str, **kwargs):
        return materialize_claimed_child(
            workflow_registry=self.workflow_registry,
            run_registry=self.run_registry,
            run_queue=self.queue,
            workflow_id=workflow_id,
            step_id=step_id,
            environ=_FEATURE_ON,
            backoff_base_seconds=0.0,
            **kwargs,
        )

    def _assert_one_row_at_most_one_enqueue(self, child_run_id: str) -> None:
        self.assertEqual(self.run_registry.count_runs(), 1)
        fetched = self.run_registry.get_run(child_run_id)
        self.assertIsNotNone(fetched)
        self.assertLessEqual(len(self.queue.calls), 1)
        if self.queue.calls:
            self.assertEqual(self.queue.run_ids, [child_run_id])

    def _assert_intent_state(
        self, child_run_id: str, expected: DispatchIntentState
    ) -> None:
        intent = self.workflow_registry.get_dispatch_intent(child_run_id)
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.state, expected)


class FeatureOffTests(MaterializeTestCase):
    def test_feature_disabled_by_default(self) -> None:
        self.assertFalse(is_workflow_orchestration_enabled({}))
        wf, step = self._create_and_claim()
        result = materialize_claimed_child(
            workflow_registry=self.workflow_registry,
            run_registry=self.run_registry,
            run_queue=self.queue,
            workflow_id=wf.workflow_id,
            step_id=step.step_id,
        )
        self.assertEqual(result.outcome, MaterializeOutcome.FEATURE_DISABLED)
        self.assertEqual(self.run_registry.count_runs(), 0)
        self.assertEqual(self.queue.calls, [])
        latest = self.workflow_registry.get_step(step.step_id)
        assert latest is not None
        self.assertEqual(
            latest.materialization_state, StepMaterializationState.CLAIMED
        )


class CreatedPathTests(MaterializeTestCase):
    def test_created_marks_and_enqueues_once(self) -> None:
        wf, step = self._create_and_claim()
        exact_yaml = step.mission_yaml
        result = self._materialize(wf.workflow_id, step.step_id)
        self.assertEqual(result.outcome, MaterializeOutcome.CREATED)
        self.assertTrue(result.enqueued)
        self.assertEqual(result.child_run_id, step.child_run_id)
        self._assert_one_row_at_most_one_enqueue(step.child_run_id)

        record = self.run_registry.get_run(step.child_run_id)
        assert record is not None
        self.assertEqual(record.mission_yaml, exact_yaml)
        self.assertEqual(record.retried_from, self.parent_id)

        latest = self.workflow_registry.get_step(step.step_id)
        assert latest is not None
        self.assertEqual(latest.status, StepStatus.QUEUED)
        self.assertEqual(
            latest.materialization_state, StepMaterializationState.MATERIALIZED
        )
        self.assertEqual(self.queue.calls[0][1]["mission_id"], "wf-materialize")
        self._assert_intent_state(step.child_run_id, DispatchIntentState.ACKED)

    def test_created_then_reconcile_sees_bound_step(self) -> None:
        wf, step = self._create_and_claim()
        result = self._materialize(wf.workflow_id, step.step_id)
        self.assertEqual(result.outcome, MaterializeOutcome.CREATED)
        self.assertTrue(result.enqueued)
        latest = self.workflow_registry.get_step(step.step_id)
        assert latest is not None
        self.assertEqual(
            latest.materialization_state, StepMaterializationState.MATERIALIZED
        )
        self._assert_one_row_at_most_one_enqueue(step.child_run_id)


class IdempotentRecoveryTests(MaterializeTestCase):
    def test_idempotent_retry_after_success(self) -> None:
        wf, step = self._create_and_claim()
        first = self._materialize(wf.workflow_id, step.step_id)
        self.assertEqual(first.outcome, MaterializeOutcome.CREATED)
        second = self._materialize(wf.workflow_id, step.step_id)
        self.assertEqual(
            second.outcome, MaterializeOutcome.ALREADY_MATERIALIZED
        )
        self.assertFalse(second.enqueued)
        self._assert_one_row_at_most_one_enqueue(step.child_run_id)
        self._assert_intent_state(step.child_run_id, DispatchIntentState.ACKED)

    def test_recover_after_create_before_mark(self) -> None:
        wf, step = self._create_and_claim()
        # Simulate crash after create: reserved row exists, step still claimed.
        created = self.run_registry.create_run(
            run_id=step.child_run_id,
            mission_yaml=step.mission_yaml,
            retried_from=self.parent_id,
        )
        self.assertEqual(created.outcome.value, "created")
        latest = self.workflow_registry.get_step(step.step_id)
        assert latest is not None
        self.assertEqual(
            latest.materialization_state, StepMaterializationState.CLAIMED
        )

        result = self._materialize(wf.workflow_id, step.step_id)
        self.assertEqual(
            result.outcome, MaterializeOutcome.RECOVERED_IDEMPOTENTLY
        )
        self.assertTrue(result.enqueued)
        self._assert_one_row_at_most_one_enqueue(step.child_run_id)
        bound = self.workflow_registry.get_step(step.step_id)
        assert bound is not None
        self.assertEqual(
            bound.materialization_state, StepMaterializationState.MATERIALIZED
        )


class MismatchPoisonTests(MaterializeTestCase):
    def test_mismatched_existing_run_poisons(self) -> None:
        wf, step = self._create_and_claim()
        other_yaml = _mission_yaml(instructions="different mission body")
        self.run_registry.create_run(
            run_id=step.child_run_id,
            mission_yaml=other_yaml,
            retried_from=self.parent_id,
        )
        result = self._materialize(wf.workflow_id, step.step_id)
        self.assertEqual(result.outcome, MaterializeOutcome.CONFLICT)
        self.assertEqual(result.reason, CONFLICT_MISSION_YAML_MISMATCH)
        self.assertFalse(result.enqueued)
        self.assertEqual(self.queue.calls, [])
        self.assertEqual(self.run_registry.count_runs(), 1)
        poisoned = self.workflow_registry.get_workflow(wf.workflow_id)
        assert poisoned is not None
        self.assertEqual(poisoned.state, WorkflowState.BLOCKED)
        self.assertEqual(poisoned.error, CONFLICT_MISSION_YAML_MISMATCH)

    def test_ownership_mismatch_poisons(self) -> None:
        wf, step = self._create_and_claim()
        self.run_registry.create_run(
            run_id=step.child_run_id,
            mission_yaml=step.mission_yaml,
            retried_from=str(uuid.uuid4()),
        )
        result = self._materialize(wf.workflow_id, step.step_id)
        self.assertEqual(result.outcome, MaterializeOutcome.CONFLICT)
        self.assertEqual(result.reason, CONFLICT_OWNERSHIP_MISMATCH)
        self.assertEqual(self.queue.calls, [])
        poisoned = self.workflow_registry.get_workflow(wf.workflow_id)
        assert poisoned is not None
        self.assertEqual(poisoned.state, WorkflowState.BLOCKED)


class MissingParentTests(MaterializeTestCase):
    def test_missing_parent_binding_rejects_before_create(self) -> None:
        wf, step = self._create_and_claim(parent_run_id=None)
        result = self._materialize(wf.workflow_id, step.step_id)
        self.assertEqual(
            result.outcome, MaterializeOutcome.MISSING_PARENT_BINDING
        )
        self.assertEqual(self.run_registry.count_runs(), 0)
        self.assertEqual(self.queue.calls, [])
        latest = self.workflow_registry.get_step(step.step_id)
        assert latest is not None
        self.assertEqual(
            latest.materialization_state, StepMaterializationState.CLAIMED
        )


class PolicyDenialTests(MaterializeTestCase):
    def test_final_policy_denial_before_create(self) -> None:
        wf, step = self._create_and_claim()
        # Tamper stored YAML after claim: repository authority injection.
        poisoned_yaml = _mission_yaml(repository_name="other-repo")
        with self.workflow_registry._lock:
            self.workflow_registry._conn.execute(
                """
                UPDATE workflow_steps
                SET mission_yaml = ?
                WHERE step_id = ?
                """,
                (poisoned_yaml, step.step_id),
            )
            self.workflow_registry._conn.commit()

        result = self._materialize(wf.workflow_id, step.step_id)
        self.assertEqual(result.outcome, MaterializeOutcome.POLICY_DENIED)
        self.assertEqual(result.reason, "repository_mismatch")
        self.assertEqual(self.run_registry.count_runs(), 0)
        self.assertEqual(self.queue.calls, [])
        blocked = self.workflow_registry.get_workflow(wf.workflow_id)
        assert blocked is not None
        self.assertEqual(blocked.state, WorkflowState.BLOCKED)


class ConcurrentMaterializerTests(MaterializeTestCase):
    def test_concurrent_materializers_one_row_one_enqueue(self) -> None:
        wf, step = self._create_and_claim()
        barrier = threading.Barrier(2)
        results: list = []
        lock = threading.Lock()

        def worker() -> None:
            workflow_reg = WorkflowRegistry(self._wf_path)
            run_reg = RunRegistry(self._run_path)
            try:
                barrier.wait(timeout=5)

                def after_gate() -> None:
                    # Serialize only the entry race; DB provides correctness.
                    pass

                result = materialize_claimed_child(
                    workflow_registry=workflow_reg,
                    run_registry=run_reg,
                    run_queue=self.queue,
                    workflow_id=wf.workflow_id,
                    step_id=step.step_id,
                    environ=_FEATURE_ON,
                    hooks=MaterializeCrashHooks(before_create=after_gate),
                )
                with lock:
                    results.append(result)
            finally:
                workflow_reg.close()
                run_reg.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())

        self.assertEqual(len(results), 2)
        enqueued = [r for r in results if r.enqueued]
        self.assertEqual(len(enqueued), 1)
        outcomes = {r.outcome for r in results}
        self.assertTrue(
            outcomes
            <= {
                MaterializeOutcome.CREATED,
                MaterializeOutcome.RECOVERED_IDEMPOTENTLY,
                MaterializeOutcome.ALREADY_MATERIALIZED,
                MaterializeOutcome.REDRIVEN,
                MaterializeOutcome.DISPATCH_DEFERRED,
            }
        )
        self._assert_one_row_at_most_one_enqueue(step.child_run_id)
        latest = self.workflow_registry.get_step(step.step_id)
        assert latest is not None
        self.assertEqual(
            latest.materialization_state, StepMaterializationState.MATERIALIZED
        )


class CrashInjectionTests(MaterializeTestCase):
    def test_crash_before_create_then_retry(self) -> None:
        wf, step = self._create_and_claim()

        def boom() -> None:
            raise RuntimeError("crash_before_create")

        with self.assertRaises(RuntimeError):
            self._materialize(
                wf.workflow_id,
                step.step_id,
                hooks=MaterializeCrashHooks(before_create=boom),
            )
        self.assertEqual(self.run_registry.count_runs(), 0)
        self.assertEqual(self.queue.calls, [])

        result = self._materialize(wf.workflow_id, step.step_id)
        self.assertEqual(result.outcome, MaterializeOutcome.CREATED)
        self.assertTrue(result.enqueued)
        self._assert_one_row_at_most_one_enqueue(step.child_run_id)

    def test_crash_after_create_before_mark_then_retry(self) -> None:
        wf, step = self._create_and_claim()

        def boom() -> None:
            raise RuntimeError("crash_after_create")

        with self.assertRaises(RuntimeError):
            self._materialize(
                wf.workflow_id,
                step.step_id,
                hooks=MaterializeCrashHooks(after_create=boom),
            )
        self.assertEqual(self.run_registry.count_runs(), 1)
        latest = self.workflow_registry.get_step(step.step_id)
        assert latest is not None
        self.assertEqual(
            latest.materialization_state, StepMaterializationState.CLAIMED
        )
        self.assertEqual(self.queue.calls, [])

        result = self._materialize(wf.workflow_id, step.step_id)
        self.assertEqual(
            result.outcome, MaterializeOutcome.RECOVERED_IDEMPOTENTLY
        )
        self.assertTrue(result.enqueued)
        self._assert_one_row_at_most_one_enqueue(step.child_run_id)

    def test_crash_before_mark_persistence_then_retry(self) -> None:
        wf, step = self._create_and_claim()

        def boom() -> None:
            raise RuntimeError("crash_before_mark")

        with self.assertRaises(RuntimeError):
            self._materialize(
                wf.workflow_id,
                step.step_id,
                hooks=MaterializeCrashHooks(before_mark=boom),
            )
        self.assertEqual(self.run_registry.count_runs(), 1)
        self.assertEqual(self.queue.calls, [])

        result = self._materialize(wf.workflow_id, step.step_id)
        self.assertIn(
            result.outcome,
            {
                MaterializeOutcome.CREATED,
                MaterializeOutcome.RECOVERED_IDEMPOTENTLY,
            },
        )
        self.assertTrue(result.enqueued)
        self._assert_one_row_at_most_one_enqueue(step.child_run_id)

    def test_crash_after_mark_persistence_then_retry(self) -> None:
        wf, step = self._create_and_claim()

        def boom() -> None:
            raise RuntimeError("crash_after_mark")

        with self.assertRaises(RuntimeError):
            self._materialize(
                wf.workflow_id,
                step.step_id,
                hooks=MaterializeCrashHooks(after_mark=boom),
            )
        # Mark + pending dispatch intent persisted; enqueue did not run.
        latest = self.workflow_registry.get_step(step.step_id)
        assert latest is not None
        self.assertEqual(
            latest.materialization_state, StepMaterializationState.MATERIALIZED
        )
        self.assertEqual(self.run_registry.count_runs(), 1)
        self.assertEqual(self.queue.calls, [])
        self._assert_intent_state(step.child_run_id, DispatchIntentState.PENDING)

        result = self._materialize(wf.workflow_id, step.step_id)
        self.assertEqual(result.outcome, MaterializeOutcome.REDRIVEN)
        self.assertTrue(result.enqueued)
        # At most one queued execution across the crash + retry.
        self._assert_one_row_at_most_one_enqueue(step.child_run_id)
        self._assert_intent_state(step.child_run_id, DispatchIntentState.ACKED)


class RegistryRelatedTests(MaterializeTestCase):
    def test_non_workflow_create_run_unchanged(self) -> None:
        record = self.run_registry.create_run()
        self.assertIsNotNone(record.run_id)
        self.assertEqual(self.run_registry.count_runs(), 1)
        # Materialize must not touch unrelated rows.
        wf, step = self._create_and_claim()
        result = self._materialize(wf.workflow_id, step.step_id)
        self.assertEqual(result.outcome, MaterializeOutcome.CREATED)
        self.assertEqual(self.run_registry.count_runs(), 2)
        untouched = self.run_registry.get_run(record.run_id)
        assert untouched is not None
        self.assertIsNone(untouched.mission_yaml)
        self.assertIsNone(untouched.retried_from)


class DurableDispatchTests(MaterializeTestCase):
    def test_crash_after_enqueue_before_ack_then_retry(self) -> None:
        wf, step = self._create_and_claim()

        def boom() -> None:
            raise RuntimeError("crash_before_ack")

        with self.assertRaises(RuntimeError):
            self._materialize(
                wf.workflow_id,
                step.step_id,
                lease_seconds=0.05,
                hooks=MaterializeCrashHooks(before_ack=boom),
            )
        self.assertEqual(len(self.queue.calls), 1)
        intent = self.workflow_registry.get_dispatch_intent(step.child_run_id)
        assert intent is not None
        self.assertEqual(intent.state, DispatchIntentState.LEASED)

        time.sleep(0.08)
        result = self._materialize(wf.workflow_id, step.step_id)
        self.assertIn(
            result.outcome,
            {
                MaterializeOutcome.REDRIVEN,
                MaterializeOutcome.ALREADY_MATERIALIZED,
            },
        )
        self.assertFalse(result.enqueued)
        self._assert_one_row_at_most_one_enqueue(step.child_run_id)
        self._assert_intent_state(step.child_run_id, DispatchIntentState.ACKED)

    def test_enqueue_exception_leaves_retryable_intent(self) -> None:
        wf, step = self._create_and_claim()
        self.queue.fail_next(1)
        result = self._materialize(wf.workflow_id, step.step_id)
        self.assertEqual(result.outcome, MaterializeOutcome.DISPATCH_DEFERRED)
        self.assertEqual(result.reason, "enqueue_exception")
        self.assertEqual(self.queue.calls, [])
        intent = self.workflow_registry.get_dispatch_intent(step.child_run_id)
        assert intent is not None
        self.assertEqual(intent.state, DispatchIntentState.PENDING)
        self.assertEqual(self.run_registry.count_runs(), 1)

        retried = self._materialize(wf.workflow_id, step.step_id)
        self.assertEqual(retried.outcome, MaterializeOutcome.REDRIVEN)
        self.assertTrue(retried.enqueued)
        self._assert_one_row_at_most_one_enqueue(step.child_run_id)
        self._assert_intent_state(step.child_run_id, DispatchIntentState.ACKED)

    def test_process_restart_with_pending_intent(self) -> None:
        wf, step = self._create_and_claim()
        with self.assertRaises(RuntimeError):
            self._materialize(
                wf.workflow_id,
                step.step_id,
                hooks=MaterializeCrashHooks(
                    after_mark=lambda: (_ for _ in ()).throw(
                        RuntimeError("restart")
                    )
                ),
            )
        # Simulate process restart: new registry connections, empty queue.
        self.workflow_registry.close()
        self.run_registry.close()
        self.workflow_registry = WorkflowRegistry(self._wf_path)
        self.run_registry = RunRegistry(self._run_path)
        self.queue = RecordingQueue()

        result = redrive_materialized_dispatch(
            workflow_registry=self.workflow_registry,
            run_registry=self.run_registry,
            run_queue=self.queue,
            workflow_id=wf.workflow_id,
            step_id=step.step_id,
            environ=_FEATURE_ON,
            backoff_base_seconds=0.0,
        )
        self.assertEqual(result.outcome, MaterializeOutcome.REDRIVEN)
        self.assertTrue(result.enqueued)
        self._assert_one_row_at_most_one_enqueue(step.child_run_id)
        self._assert_intent_state(step.child_run_id, DispatchIntentState.ACKED)

    def test_process_restart_with_leased_intent_after_expiry(self) -> None:
        wf, step = self._create_and_claim()
        with self.assertRaises(RuntimeError):
            self._materialize(
                wf.workflow_id,
                step.step_id,
                lease_seconds=0.05,
                hooks=MaterializeCrashHooks(
                    before_ack=lambda: (_ for _ in ()).throw(
                        RuntimeError("crash_leased")
                    )
                ),
            )
        intent = self.workflow_registry.get_dispatch_intent(step.child_run_id)
        assert intent is not None
        self.assertEqual(intent.state, DispatchIntentState.LEASED)
        self.assertEqual(len(self.queue.calls), 1)

        time.sleep(0.08)
        self.workflow_registry.close()
        self.run_registry.close()
        self.workflow_registry = WorkflowRegistry(self._wf_path)
        self.run_registry = RunRegistry(self._run_path)
        # Fresh process-local queue; registry still queued.
        self.queue = RecordingQueue()

        result = redrive_materialized_dispatch(
            workflow_registry=self.workflow_registry,
            run_registry=self.run_registry,
            run_queue=self.queue,
            workflow_id=wf.workflow_id,
            step_id=step.step_id,
            environ=_FEATURE_ON,
            backoff_base_seconds=0.0,
        )
        self.assertIn(
            result.outcome,
            {
                MaterializeOutcome.REDRIVEN,
                MaterializeOutcome.ALREADY_MATERIALIZED,
            },
        )
        self.assertTrue(result.enqueued)
        self._assert_one_row_at_most_one_enqueue(step.child_run_id)
        self._assert_intent_state(step.child_run_id, DispatchIntentState.ACKED)

    def test_two_concurrent_dispatchers_at_most_one_enqueue(self) -> None:
        wf, step = self._create_and_claim()
        with self.assertRaises(RuntimeError):
            self._materialize(
                wf.workflow_id,
                step.step_id,
                hooks=MaterializeCrashHooks(
                    after_mark=lambda: (_ for _ in ()).throw(
                        RuntimeError("stop_before_handoff")
                    )
                ),
            )
        self.assertEqual(self.queue.calls, [])
        barrier = threading.Barrier(2)
        results: list = []
        lock = threading.Lock()

        def worker() -> None:
            workflow_reg = WorkflowRegistry(self._wf_path)
            run_reg = RunRegistry(self._run_path)
            try:
                barrier.wait(timeout=5)
                result = redrive_materialized_dispatch(
                    workflow_registry=workflow_reg,
                    run_registry=run_reg,
                    run_queue=self.queue,
                    workflow_id=wf.workflow_id,
                    step_id=step.step_id,
                    environ=_FEATURE_ON,
                    backoff_base_seconds=0.0,
                )
                with lock:
                    results.append(result)
            finally:
                workflow_reg.close()
                run_reg.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())

        self.assertEqual(len(results), 2)
        enqueued = [r for r in results if r.enqueued]
        self.assertEqual(len(enqueued), 1)
        self._assert_one_row_at_most_one_enqueue(step.child_run_id)
        self._assert_intent_state(step.child_run_id, DispatchIntentState.ACKED)

    def test_retry_of_materialized_redrives_pending(self) -> None:
        wf, step = self._create_and_claim()
        with self.assertRaises(RuntimeError):
            self._materialize(
                wf.workflow_id,
                step.step_id,
                hooks=MaterializeCrashHooks(
                    after_mark=lambda: (_ for _ in ()).throw(
                        RuntimeError("gap")
                    )
                ),
            )
        result = self._materialize(wf.workflow_id, step.step_id)
        self.assertEqual(result.outcome, MaterializeOutcome.REDRIVEN)
        self.assertTrue(result.enqueued)
        self._assert_intent_state(step.child_run_id, DispatchIntentState.ACKED)

    def test_worker_consumes_before_ack(self) -> None:
        wf, step = self._create_and_claim()
        executed = threading.Event()
        real_queue = RunQueue()

        def execute(run_id: str, mission: dict, registry) -> None:
            registry.update_status(run_id, RunStatus.RUNNING)
            executed.set()
            # Hold the active slot briefly so registry stays running.
            time.sleep(0.2)

        real_queue.configure(execute)
        self.queue = real_queue  # type: ignore[assignment]

        def after_enqueue() -> None:
            self.assertTrue(executed.wait(timeout=2.0))

        def boom() -> None:
            raise RuntimeError("crash_after_worker_before_ack")

        with self.assertRaises(RuntimeError):
            materialize_claimed_child(
                workflow_registry=self.workflow_registry,
                run_registry=self.run_registry,
                run_queue=real_queue,
                workflow_id=wf.workflow_id,
                step_id=step.step_id,
                environ=_FEATURE_ON,
                lease_seconds=0.05,
                backoff_base_seconds=0.0,
                hooks=MaterializeCrashHooks(
                    after_enqueue=after_enqueue,
                    before_ack=boom,
                ),
            )
        record = self.run_registry.get_run(step.child_run_id)
        assert record is not None
        self.assertEqual(record.status, RunStatus.RUNNING)
        intent = self.workflow_registry.get_dispatch_intent(step.child_run_id)
        assert intent is not None
        self.assertEqual(intent.state, DispatchIntentState.LEASED)

        time.sleep(0.08)
        # Fresh queue simulates restart; running registry suppresses enqueue.
        fresh = RecordingQueue()
        result = redrive_materialized_dispatch(
            workflow_registry=self.workflow_registry,
            run_registry=self.run_registry,
            run_queue=fresh,
            workflow_id=wf.workflow_id,
            step_id=step.step_id,
            environ=_FEATURE_ON,
            backoff_base_seconds=0.0,
        )
        self.assertIn(
            result.outcome,
            {
                MaterializeOutcome.REDRIVEN,
                MaterializeOutcome.ALREADY_MATERIALIZED,
            },
        )
        self.assertFalse(result.enqueued)
        self.assertEqual(fresh.calls, [])
        self._assert_intent_state(step.child_run_id, DispatchIntentState.ACKED)
        self.assertEqual(self.run_registry.count_runs(), 1)
        real_queue.stop()

    def test_terminal_run_with_stale_intent(self) -> None:
        wf, step = self._create_and_claim()
        with self.assertRaises(RuntimeError):
            self._materialize(
                wf.workflow_id,
                step.step_id,
                hooks=MaterializeCrashHooks(
                    after_mark=lambda: (_ for _ in ()).throw(
                        RuntimeError("gap")
                    )
                ),
            )
        self.run_registry.update_status(
            step.child_run_id, RunStatus.COMPLETED
        )
        result = self._materialize(wf.workflow_id, step.step_id)
        self.assertEqual(
            result.outcome, MaterializeOutcome.ALREADY_MATERIALIZED
        )
        self.assertFalse(result.enqueued)
        self.assertEqual(self.queue.calls, [])
        self._assert_intent_state(step.child_run_id, DispatchIntentState.ACKED)
        self.assertEqual(self.run_registry.count_runs(), 1)

    def test_list_redrivable_dispatch_intents_primitive(self) -> None:
        wf, step = self._create_and_claim()
        with self.assertRaises(RuntimeError):
            self._materialize(
                wf.workflow_id,
                step.step_id,
                hooks=MaterializeCrashHooks(
                    after_mark=lambda: (_ for _ in ()).throw(
                        RuntimeError("gap")
                    )
                ),
            )
        pending = self.workflow_registry.list_redrivable_dispatch_intents()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].child_run_id, step.child_run_id)


class PoisonCasTests(MaterializeTestCase):
    def test_poison_cas_retries_on_stale_version(self) -> None:
        wf, step = self._create_and_claim()
        calls = {"n": 0}
        original = self.workflow_registry.apply_cas_transition

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                from mission_control.workflow_registry import CasResult

                current = self.workflow_registry.get_workflow(wf.workflow_id)
                return CasResult(
                    ok=False,
                    workflow=current,
                    conflict=True,
                    error="version_conflict",
                )
            return original(**kwargs)

        self.workflow_registry.apply_cas_transition = flaky  # type: ignore[method-assign]
        try:
            result = _poison(
                self.workflow_registry,
                workflow=wf,
                step=step,
                reason="stale_poison_probe",
            )
        finally:
            self.workflow_registry.apply_cas_transition = original  # type: ignore[method-assign]

        self.assertTrue(result.ok)
        self.assertGreaterEqual(result.attempts, 2)
        blocked = self.workflow_registry.get_workflow(wf.workflow_id)
        assert blocked is not None
        self.assertEqual(blocked.state, WorkflowState.BLOCKED)
        self.assertEqual(blocked.error, "stale_poison_probe")

    def test_poison_cas_fail_closed_when_exhausted(self) -> None:
        wf, step = self._create_and_claim()

        original = self.workflow_registry.apply_cas_transition

        def always_conflict(**kwargs):
            current = self.workflow_registry.get_workflow(wf.workflow_id)
            from mission_control.workflow_registry import CasResult

            return CasResult(
                ok=False,
                workflow=current,
                conflict=True,
                error="version_conflict",
            )

        self.workflow_registry.apply_cas_transition = always_conflict  # type: ignore[method-assign]
        try:
            result = _poison(
                self.workflow_registry,
                workflow=wf,
                step=step,
                reason="should_fail_closed",
                max_attempts=POISON_CAS_MAX_ATTEMPTS,
            )
        finally:
            self.workflow_registry.apply_cas_transition = original  # type: ignore[method-assign]

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "poison_cas_exhausted")
        self.assertEqual(result.attempts, POISON_CAS_MAX_ATTEMPTS)
        latest = self.workflow_registry.get_workflow(wf.workflow_id)
        assert latest is not None
        self.assertNotEqual(latest.state, WorkflowState.BLOCKED)


class RunQueueIdempotentTests(unittest.TestCase):
    def test_suppresses_duplicate_and_terminal(self) -> None:
        queue = RunQueue()
        executed: list[str] = []
        gate = threading.Event()

        def execute(run_id: str, mission: dict, registry) -> None:
            executed.append(run_id)
            gate.wait(timeout=2.0)

        queue.configure(execute)
        fd, path = tempfile.mkstemp(suffix="-run.db")
        os.close(fd)
        registry = RunRegistry(path)
        try:
            record = registry.create_run()
            self.assertTrue(queue.enqueue(record.run_id, {"a": 1}, registry))
            self.assertFalse(queue.enqueue(record.run_id, {"a": 1}, registry))
            gate.set()
            queue.stop()
            queue.configure(execute)
            registry.update_status(record.run_id, RunStatus.COMPLETED)
            self.assertFalse(queue.enqueue(record.run_id, {"a": 1}, registry))
        finally:
            gate.set()
            queue.stop()
            registry.close()
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
