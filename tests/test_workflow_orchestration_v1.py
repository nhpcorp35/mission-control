"""Deterministic tests for durable workflow orchestration v1."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import os
import tempfile
import threading
import unittest

from mission_control.workflow_orchestrator import (
    ChildRunView,
    DecisionAction,
    ReviewVerdictKind,
    WorkflowOrchestrator,
    assert_review_step_read_only,
    decide_reconcile,
    fingerprint_findings,
    parse_review_verdict,
    redact_secrets,
    should_emit_workflow_alert,
    should_suppress_child_terminal_alert,
    truncate_prior_output,
    validate_followup_against_policy,
)
from mission_control.workflow_registry import (
    StepStatus,
    StepType,
    WorkflowPolicySnapshot,
    WorkflowRegistry,
    WorkflowState,
    WorkflowStepSpec,
    is_workflow_orchestration_enabled,
    make_idempotency_key,
)


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


def _specs() -> dict[str, WorkflowStepSpec]:
    return {
        "implementation": WorkflowStepSpec(
            step_type=StepType.IMPLEMENTATION,
            mission_yaml="mission: implement\ninstructions: do work\n",
        ),
        "review": WorkflowStepSpec(
            step_type=StepType.REVIEW,
            mission_yaml=(
                "mission: review\n"
                "permissions:\n  create_files: false\n"
                "  modify_files: false\n"
                "persistence:\n  mode: none\n"
                "instructions: report MERGE-READY or BLOCKED\n"
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
                "instructions: report MERGE-READY or BLOCKED\n"
            ),
        ),
    }


class WorkflowRegistryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.registry = WorkflowRegistry(self._db_path)
        self.orch = WorkflowOrchestrator(self.registry)

    def tearDown(self) -> None:
        self.registry.close()
        os.unlink(self._db_path)

    def _create(self, **policy_overrides):
        specs = _specs()
        return self.registry.create_workflow(
            policy=_policy(**policy_overrides),
            implementation=specs["implementation"],
            review=specs["review"],
            fix=specs["fix"],
            re_review=specs["re_review"],
        )


class FeatureFlagTests(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        self.assertFalse(is_workflow_orchestration_enabled({}))

    def test_enabled_only_when_explicit(self) -> None:
        self.assertTrue(
            is_workflow_orchestration_enabled(
                {"MISSION_CONTROL_WORKFLOW_ORCHESTRATION": "true"}
            )
        )
        self.assertFalse(
            is_workflow_orchestration_enabled(
                {"MISSION_CONTROL_WORKFLOW_ORCHESTRATION": "false"}
            )
        )


class VerdictParsingTests(unittest.TestCase):
    def test_merge_ready(self) -> None:
        v = parse_review_verdict("Summary\nMERGE-READY\n")
        self.assertEqual(v.kind, ReviewVerdictKind.MERGE_READY)

    def test_blocked_with_findings(self) -> None:
        v = parse_review_verdict(
            "BLOCKED\nFindings:\n- missing tests\n- flaky assert\n"
        )
        self.assertEqual(v.kind, ReviewVerdictKind.BLOCKED)
        self.assertEqual(len(v.findings), 2)
        self.assertEqual(v.fingerprint, fingerprint_findings(v.findings))

    def test_malformed_without_marker(self) -> None:
        v = parse_review_verdict("looks fine to me")
        self.assertEqual(v.kind, ReviewVerdictKind.MALFORMED)

    def test_ambiguous_both_markers(self) -> None:
        v = parse_review_verdict("MERGE-READY\nBLOCKED\n- x\n")
        self.assertEqual(v.kind, ReviewVerdictKind.AMBIGUOUS)

    def test_blocked_without_findings_malformed(self) -> None:
        v = parse_review_verdict("BLOCKED\nno list")
        self.assertEqual(v.kind, ReviewVerdictKind.MALFORMED)


class PolicyGateTests(unittest.TestCase):
    def test_default_deny_merge_deploy(self) -> None:
        policy = _policy()
        self.assertEqual(
            validate_followup_against_policy(
                policy=policy,
                repository_name=policy.repository_name,
                target_branch=policy.target_branch,
                wants_merge=True,
            ),
            "merge_not_authorized",
        )
        self.assertEqual(
            validate_followup_against_policy(
                policy=policy,
                repository_name=policy.repository_name,
                target_branch=policy.target_branch,
                wants_deploy=True,
            ),
            "deploy_not_authorized",
        )

    def test_repository_and_branch_mismatch(self) -> None:
        policy = _policy()
        self.assertEqual(
            validate_followup_against_policy(
                policy=policy,
                repository_name="other-repo",
                target_branch=policy.target_branch,
            ),
            "repository_mismatch",
        )
        self.assertEqual(
            validate_followup_against_policy(
                policy=policy,
                repository_name=policy.repository_name,
                target_branch="evil/branch",
            ),
            "branch_lineage_mismatch",
        )

    def test_scope_expansion_denied(self) -> None:
        policy = _policy()
        self.assertEqual(
            validate_followup_against_policy(
                policy=policy,
                repository_name=policy.repository_name,
                target_branch=policy.target_branch,
                requested_scope=["app/secrets.py"],
            ),
            "scope_expansion",
        )

    def test_permission_migration_secret_denials(self) -> None:
        policy = _policy()
        for kwargs, reason in [
            ({"wants_destructive": True}, "destructive_not_authorized"),
            (
                {"wants_permission_expansion": True},
                "permission_expansion_not_authorized",
            ),
            ({"wants_migrations": True}, "migrations_not_authorized"),
            ({"wants_secret_changes": True}, "secret_changes_not_authorized"),
            (
                {"wants_scope_or_repo_change": True},
                "scope_or_repo_change_not_authorized",
            ),
        ]:
            self.assertEqual(
                validate_followup_against_policy(
                    policy=policy,
                    repository_name=policy.repository_name,
                    target_branch=policy.target_branch,
                    **kwargs,
                ),
                reason,
            )

    def test_review_must_be_read_only(self) -> None:
        self.assertIsNone(
            assert_review_step_read_only(
                "persistence:\n  mode: none\npermissions:\n  create_files: false\n"
            )
        )
        self.assertEqual(
            assert_review_step_read_only(
                "persistence:\n  mode: push\n"
            ),
            "review_persistence_not_none",
        )
        self.assertEqual(
            assert_review_step_read_only("create_files: true\n"),
            "review_must_be_read_only",
        )


class RedactionTests(unittest.TestCase):
    def test_redact_and_truncate(self) -> None:
        text = "Authorization: Bearer " + ("a" * 40) + "\n" + ("x" * 5000)
        redacted = redact_secrets(text)
        self.assertNotIn("Bearer aaaa", redacted)
        self.assertIn("[redacted]", redacted)
        truncated = truncate_prior_output(text, max_chars=100)
        self.assertLessEqual(len(truncated), 100)


class StateMachineTransitionTests(WorkflowRegistryTestCase):
    def test_submit_launches_implementation(self) -> None:
        wf = self._create()
        applied = self.orch.reconcile_workflow(wf.workflow_id, child_runs={})
        self.assertEqual(applied[0].action, DecisionAction.LAUNCH_CHILD)
        self.assertEqual(applied[0].step_type, StepType.IMPLEMENTATION)
        steps = self.registry.list_steps(wf.workflow_id)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].step_type, StepType.IMPLEMENTATION)
        self.assertIsNotNone(steps[0].child_run_id)
        self.assertEqual(
            self.registry.get_workflow(wf.workflow_id).state,
            WorkflowState.RUNNING,
        )

    def test_implementation_success_launches_review_without_external_check(
        self,
    ) -> None:
        """Integration: impl terminal success creates review (no HTTP/status)."""
        wf = self._create()
        self.orch.reconcile_workflow(wf.workflow_id, child_runs={})
        impl = self.registry.list_steps(wf.workflow_id)[0]
        child_runs = {
            impl.child_run_id: ChildRunView(
                run_id=impl.child_run_id,
                status="completed",
                stdout="done",
            )
        }
        applied = self.orch.reconcile_workflow(
            wf.workflow_id, child_runs=child_runs
        )
        launch = [
            d for d in applied if d.action is DecisionAction.LAUNCH_CHILD
        ]
        self.assertEqual(len(launch), 1)
        self.assertEqual(launch[0].step_type, StepType.REVIEW)
        steps = self.registry.list_steps(wf.workflow_id)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[1].step_type, StepType.REVIEW)
        # Review mission remains read-only / persistence none.
        self.assertIsNone(assert_review_step_read_only(steps[1].mission_yaml))

    def test_blocked_fix_rereview_needs_approval(self) -> None:
        wf = self._create()
        self.orch.reconcile_workflow(wf.workflow_id, child_runs={})
        impl = self.registry.list_steps(wf.workflow_id)[0]
        self.orch.reconcile_workflow(
            wf.workflow_id,
            child_runs={
                impl.child_run_id: ChildRunView(
                    run_id=impl.child_run_id, status="completed"
                )
            },
        )
        review = [
            s
            for s in self.registry.list_steps(wf.workflow_id)
            if s.step_type is StepType.REVIEW
        ][0]
        blocked_out = "BLOCKED\nFindings:\n- missing unit test for CAS\n"
        self.orch.reconcile_workflow(
            wf.workflow_id,
            child_runs={
                impl.child_run_id: ChildRunView(
                    run_id=impl.child_run_id, status="completed"
                ),
                review.child_run_id: ChildRunView(
                    run_id=review.child_run_id,
                    status="completed",
                    stdout=blocked_out,
                ),
            },
        )
        steps = self.registry.list_steps(wf.workflow_id)
        fix = [s for s in steps if s.step_type is StepType.FIX][0]
        self.assertIn("missing unit test for CAS", fix.mission_yaml)
        self.assertNotIn("password", fix.mission_yaml.lower() + "x")

        # Fix succeeds → re-review.
        all_children = {
            impl.child_run_id: ChildRunView(
                run_id=impl.child_run_id, status="completed"
            ),
            review.child_run_id: ChildRunView(
                run_id=review.child_run_id,
                status="completed",
                stdout=blocked_out,
            ),
            fix.child_run_id: ChildRunView(
                run_id=fix.child_run_id, status="completed", stdout="fixed"
            ),
        }
        self.orch.reconcile_workflow(wf.workflow_id, child_runs=all_children)
        rereview = [
            s
            for s in self.registry.list_steps(wf.workflow_id)
            if s.step_type is StepType.RE_REVIEW
        ][0]
        all_children[rereview.child_run_id] = ChildRunView(
            run_id=rereview.child_run_id,
            status="completed",
            stdout="MERGE-READY\n",
        )
        self.orch.reconcile_workflow(wf.workflow_id, child_runs=all_children)
        final = self.registry.get_workflow(wf.workflow_id)
        self.assertEqual(final.state, WorkflowState.NEEDS_APPROVAL)
        self.assertTrue(final.notification_emitted)

    def test_merge_ready_defaults_to_needs_approval(self) -> None:
        wf = self._create()
        self.orch.reconcile_workflow(wf.workflow_id, child_runs={})
        impl = self.registry.list_steps(wf.workflow_id)[0]
        self.orch.reconcile_workflow(
            wf.workflow_id,
            child_runs={
                impl.child_run_id: ChildRunView(
                    run_id=impl.child_run_id, status="completed"
                )
            },
        )
        review = self.registry.list_steps(wf.workflow_id)[1]
        self.orch.reconcile_workflow(
            wf.workflow_id,
            child_runs={
                impl.child_run_id: ChildRunView(
                    run_id=impl.child_run_id, status="completed"
                ),
                review.child_run_id: ChildRunView(
                    run_id=review.child_run_id,
                    status="completed",
                    stdout="verdict: MERGE-READY\n",
                ),
            },
        )
        self.assertEqual(
            self.registry.get_workflow(wf.workflow_id).state,
            WorkflowState.NEEDS_APPROVAL,
        )

    def test_repeated_blocker_fingerprint_stops(self) -> None:
        wf = self._create()
        # Seed: one completed review with fingerprint, fix_cycle already 1.
        self.orch.reconcile_workflow(wf.workflow_id, child_runs={})
        impl = self.registry.list_steps(wf.workflow_id)[0]
        self.orch.reconcile_workflow(
            wf.workflow_id,
            child_runs={
                impl.child_run_id: ChildRunView(
                    run_id=impl.child_run_id, status="completed"
                )
            },
        )
        findings = "BLOCKED\nFindings:\n- same bug again\n"
        fp = fingerprint_findings(("same bug again",))
        review = self.registry.list_steps(wf.workflow_id)[1]
        # First blocked → fix
        children = {
            impl.child_run_id: ChildRunView(
                run_id=impl.child_run_id, status="completed"
            ),
            review.child_run_id: ChildRunView(
                run_id=review.child_run_id,
                status="completed",
                stdout=findings,
            ),
        }
        self.orch.reconcile_workflow(wf.workflow_id, child_runs=children)
        fix = [
            s
            for s in self.registry.list_steps(wf.workflow_id)
            if s.step_type is StepType.FIX
        ][0]
        children[fix.child_run_id] = ChildRunView(
            run_id=fix.child_run_id, status="completed"
        )
        self.orch.reconcile_workflow(wf.workflow_id, child_runs=children)
        rereview = [
            s
            for s in self.registry.list_steps(wf.workflow_id)
            if s.step_type is StepType.RE_REVIEW
        ][0]
        children[rereview.child_run_id] = ChildRunView(
            run_id=rereview.child_run_id,
            status="completed",
            stdout=findings,
        )
        self.orch.reconcile_workflow(wf.workflow_id, child_runs=children)
        final = self.registry.get_workflow(wf.workflow_id)
        self.assertEqual(final.state, WorkflowState.BLOCKED)
        self.assertEqual(final.error, "repeated_blocker_fingerprint")
        self.assertEqual(final.last_blocker_fingerprint, fp)

    def test_malformed_verdict_blocked(self) -> None:
        wf = self._create()
        self.orch.reconcile_workflow(wf.workflow_id, child_runs={})
        impl = self.registry.list_steps(wf.workflow_id)[0]
        self.orch.reconcile_workflow(
            wf.workflow_id,
            child_runs={
                impl.child_run_id: ChildRunView(
                    run_id=impl.child_run_id, status="completed"
                )
            },
        )
        review = self.registry.list_steps(wf.workflow_id)[1]
        self.orch.reconcile_workflow(
            wf.workflow_id,
            child_runs={
                impl.child_run_id: ChildRunView(
                    run_id=impl.child_run_id, status="completed"
                ),
                review.child_run_id: ChildRunView(
                    run_id=review.child_run_id,
                    status="completed",
                    stdout="ship it maybe?",
                ),
            },
        )
        final = self.registry.get_workflow(wf.workflow_id)
        self.assertEqual(final.state, WorkflowState.BLOCKED)
        self.assertEqual(final.error, "malformed")

    def test_child_failure_and_timeout(self) -> None:
        wf = self._create()
        self.orch.reconcile_workflow(wf.workflow_id, child_runs={})
        impl = self.registry.list_steps(wf.workflow_id)[0]
        self.orch.reconcile_workflow(
            wf.workflow_id,
            child_runs={
                impl.child_run_id: ChildRunView(
                    run_id=impl.child_run_id,
                    status="failed",
                    error="boom",
                )
            },
        )
        self.assertEqual(
            self.registry.get_workflow(wf.workflow_id).state,
            WorkflowState.FAILED,
        )

        wf2 = self._create()
        self.orch.reconcile_workflow(wf2.workflow_id, child_runs={})
        impl2 = self.registry.list_steps(wf2.workflow_id)[0]
        self.orch.reconcile_workflow(
            wf2.workflow_id,
            child_runs={
                impl2.child_run_id: ChildRunView(
                    run_id=impl2.child_run_id, status="timed_out"
                )
            },
        )
        self.assertEqual(
            self.registry.get_workflow(wf2.workflow_id).state,
            WorkflowState.BLOCKED,
        )

    def test_cancellation(self) -> None:
        wf = self._create()
        result = self.registry.cancel_workflow(wf.workflow_id)
        self.assertTrue(result.ok)
        self.assertEqual(result.workflow.state, WorkflowState.CANCELLED)
        # Further reconcile is noop / no new launches.
        applied = self.orch.reconcile_workflow(
            wf.workflow_id, child_runs={}
        )
        self.assertTrue(
            all(d.action is not DecisionAction.LAUNCH_CHILD for d in applied)
        )

    def test_budget_ceilings(self) -> None:
        wf = self._create(max_child_runs=1, max_credit_units=1)
        self.orch.reconcile_workflow(wf.workflow_id, child_runs={})
        impl = self.registry.list_steps(wf.workflow_id)[0]
        self.orch.reconcile_workflow(
            wf.workflow_id,
            child_runs={
                impl.child_run_id: ChildRunView(
                    run_id=impl.child_run_id, status="completed"
                )
            },
        )
        final = self.registry.get_workflow(wf.workflow_id)
        self.assertEqual(final.state, WorkflowState.BUDGET_EXHAUSTED)

        wf2 = self._create(max_wall_clock_seconds=1)
        # Force started_at in the past via claim then CAS wall-clock check.
        self.orch.reconcile_workflow(wf2.workflow_id, child_runs={})
        past = datetime.now(timezone.utc) - timedelta(seconds=30)
        # Re-decide with injected now far beyond wall clock from created_at.
        # Use created_at-based budget by reconciling with now >> created.
        wf2_rec = self.registry.get_workflow(wf2.workflow_id)
        decisions = decide_reconcile(
            workflow=wf2_rec,
            steps=self.registry.list_steps(wf2.workflow_id),
            child_runs={
                self.registry.list_steps(wf2.workflow_id)[0].child_run_id: (
                    ChildRunView(
                        run_id=self.registry.list_steps(wf2.workflow_id)[
                            0
                        ].child_run_id,
                        status="running",
                    )
                )
            },
            now=past + timedelta(seconds=120),
        )
        self.assertTrue(
            any(
                d.to_state is WorkflowState.BUDGET_EXHAUSTED for d in decisions
            )
        )

    def test_max_fix_cycles(self) -> None:
        wf = self._create(max_fix_cycles=1)
        self.orch.reconcile_workflow(wf.workflow_id, child_runs={})
        impl = self.registry.list_steps(wf.workflow_id)[0]
        children = {
            impl.child_run_id: ChildRunView(
                run_id=impl.child_run_id, status="completed"
            )
        }
        self.orch.reconcile_workflow(wf.workflow_id, child_runs=children)
        review = [
            s
            for s in self.registry.list_steps(wf.workflow_id)
            if s.step_type is StepType.REVIEW
        ][0]
        children[review.child_run_id] = ChildRunView(
            run_id=review.child_run_id,
            status="completed",
            stdout="BLOCKED\nFindings:\n- bug one\n",
        )
        self.orch.reconcile_workflow(wf.workflow_id, child_runs=children)
        fix = [
            s
            for s in self.registry.list_steps(wf.workflow_id)
            if s.step_type is StepType.FIX
        ][0]
        children[fix.child_run_id] = ChildRunView(
            run_id=fix.child_run_id, status="completed"
        )
        self.orch.reconcile_workflow(wf.workflow_id, child_runs=children)
        rereview = [
            s
            for s in self.registry.list_steps(wf.workflow_id)
            if s.step_type is StepType.RE_REVIEW
        ][0]
        children[rereview.child_run_id] = ChildRunView(
            run_id=rereview.child_run_id,
            status="completed",
            stdout="BLOCKED\nFindings:\n- bug two different\n",
        )
        self.orch.reconcile_workflow(wf.workflow_id, child_runs=children)
        final = self.registry.get_workflow(wf.workflow_id)
        self.assertEqual(final.state, WorkflowState.BLOCKED)
        self.assertEqual(final.error, "max_fix_cycles")


class IdempotencyAndCasTests(WorkflowRegistryTestCase):
    def test_restart_safe_no_duplicate_child(self) -> None:
        wf = self._create()
        self.orch.reconcile_workflow(wf.workflow_id, child_runs={})
        steps_before = self.registry.list_steps(wf.workflow_id)
        child_id = steps_before[0].child_run_id
        key = steps_before[0].idempotency_key
        # Simulate crash + re-reconcile before materialize: still awaiting.
        applied = self.orch.reconcile_workflow(wf.workflow_id, child_runs={})
        self.assertTrue(
            all(d.action is not DecisionAction.LAUNCH_CHILD for d in applied)
        )
        # Explicit re-claim with same key returns same child_run_id.
        claim = self.registry.claim_child_launch(
            workflow_id=wf.workflow_id,
            expected_version=self.registry.get_workflow(wf.workflow_id).version,
            step_type=StepType.IMPLEMENTATION,
            mission_yaml="x",
            cycle=0,
            attempt=1,
            parent_run_id=None,
            idempotency_key=key,
        )
        self.assertTrue(claim.ok)
        self.assertTrue(claim.already_claimed)
        self.assertEqual(claim.child_run_id, child_id)
        self.assertEqual(len(self.registry.list_steps(wf.workflow_id)), 1)

    def test_concurrent_reconcilers_cas(self) -> None:
        wf = self._create()
        barrier = threading.Barrier(8)
        results = []

        def worker() -> str:
            barrier.wait()
            # Each worker tries to launch implementation via claim.
            key = make_idempotency_key(
                wf.workflow_id, StepType.IMPLEMENTATION, 0, 1
            )
            claim = self.registry.claim_child_launch(
                workflow_id=wf.workflow_id,
                expected_version=1,
                step_type=StepType.IMPLEMENTATION,
                mission_yaml="mission: implement\n",
                cycle=0,
                attempt=1,
                parent_run_id=None,
                idempotency_key=key,
            )
            if claim.ok and not claim.already_claimed:
                return "created"
            if claim.ok and claim.already_claimed:
                return "idempotent"
            if claim.conflict:
                return "conflict"
            return claim.error or "error"

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(worker) for _ in range(8)]
            for fut in as_completed(futures):
                results.append(fut.result())

        self.assertEqual(results.count("created"), 1)
        self.assertEqual(len(self.registry.list_steps(wf.workflow_id)), 1)
        child_ids = {
            self.registry.list_steps(wf.workflow_id)[0].child_run_id
        }
        self.assertEqual(len(child_ids), 1)
        # Survivors are idempotent hits or version conflicts — never a
        # second distinct child.
        self.assertTrue(
            all(r in {"created", "idempotent", "conflict"} for r in results)
        )


class NotificationDecisionTests(WorkflowRegistryTestCase):
    def test_one_workflow_alert_and_child_suppression(self) -> None:
        wf = self._create()
        self.orch.reconcile_workflow(wf.workflow_id, child_runs={})
        impl = self.registry.list_steps(wf.workflow_id)[0]
        self.assertTrue(
            should_suppress_child_terminal_alert(
                child_run_id=impl.child_run_id, step=impl
            )
        )
        self.orch.reconcile_workflow(
            wf.workflow_id,
            child_runs={
                impl.child_run_id: ChildRunView(
                    run_id=impl.child_run_id, status="completed"
                )
            },
        )
        review = self.registry.list_steps(wf.workflow_id)[1]
        applied = self.orch.reconcile_workflow(
            wf.workflow_id,
            child_runs={
                impl.child_run_id: ChildRunView(
                    run_id=impl.child_run_id, status="completed"
                ),
                review.child_run_id: ChildRunView(
                    run_id=review.child_run_id,
                    status="completed",
                    stdout="MERGE-READY\n",
                ),
            },
        )
        suppress = [
            d
            for d in applied
            if d.action is DecisionAction.SUPPRESS_CHILD_ALERT
        ]
        self.assertEqual(len(suppress), 1)
        final = self.registry.get_workflow(wf.workflow_id)
        self.assertEqual(final.state, WorkflowState.NEEDS_APPROVAL)
        self.assertTrue(final.notification_emitted)
        self.assertTrue(should_emit_workflow_alert(final) is False)
        # History retained
        history = self.registry.get_history(wf.workflow_id)
        self.assertGreaterEqual(len(history), 2)


class AuditHistoryTests(WorkflowRegistryTestCase):
    def test_history_records_transitions(self) -> None:
        wf = self._create()
        self.orch.reconcile_workflow(wf.workflow_id, child_runs={})
        history = self.registry.get_history(wf.workflow_id)
        reasons = [h.reason for h in history]
        self.assertIn("submitted", reasons)
        self.assertIn("child_launched", reasons)


if __name__ == "__main__":
    unittest.main()
