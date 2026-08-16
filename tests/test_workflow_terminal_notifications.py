"""Slice C (first half): one durable workflow-level terminal notification."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

import httpx

from mission_control.notifications import (
    PUSHOVER_API_URL,
    DeliveryState,
    NotificationConfig,
    NotificationEventKind,
    NotificationOutbox,
    build_workflow_terminal_payload,
    format_pushover_message,
    format_pushover_title,
    sanitize_notification_payload,
    workflow_terminal_dedupe_key,
    workflow_terminal_run_id,
)
from mission_control.run_registry import RunRegistry, RunStatus
from mission_control.workflow_orchestrator import format_review_verdict_envelope
from mission_control.workflow_reconciler import WorkflowReconciler
from mission_control.workflow_registry import (
    TransitionReason,
    WorkflowPolicySnapshot,
    WorkflowRecord,
    WorkflowRegistry,
    WorkflowState,
    WorkflowStepSpec,
    StepType,
)

_FEATURE_ON = {"MISSION_CONTROL_WORKFLOW_ORCHESTRATION": "true"}
_FEATURE_OFF = {"MISSION_CONTROL_WORKFLOW_ORCHESTRATION": "false"}
_TEST_USER = "pushover-user-key-fixture-aaaa"
_TEST_TOKEN = "pushover-app-token-fixture-bbbb"


def _policy(**overrides) -> WorkflowPolicySnapshot:
    base = dict(
        repository_name="Mission-Control",
        base_branch="main",
        target_branch="wf/notify-v1",
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
        "mission_id: wf-notify\n"
        "title: Notification child\n"
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
    review_yaml = _mission_yaml(instructions="emit MC_REVIEW_VERDICT_V1 envelope")
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


def _disabled_outbox_config() -> NotificationConfig:
    return NotificationConfig(
        enabled=False,
        webhook_url=None,
        timeout_seconds=2.0,
        max_attempts=3,
        backoff_base_seconds=0.01,
        backoff_max_seconds=1.0,
        claim_lease_seconds=30.0,
        allow_http=False,
        worker_poll_seconds=0.05,
        _secret=None,
    )


def _pushover_config() -> NotificationConfig:
    return NotificationConfig(
        enabled=True,
        webhook_url=None,
        timeout_seconds=2.0,
        max_attempts=3,
        backoff_base_seconds=0.01,
        backoff_max_seconds=1.0,
        claim_lease_seconds=30.0,
        allow_http=False,
        worker_poll_seconds=0.05,
        _secret=None,
        _pushover_user_key=_TEST_USER,
        _pushover_app_token=_TEST_TOKEN,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _workflow_record(
    *,
    state: WorkflowState = WorkflowState.FAILED,
    workflow_id: str | None = None,
    child_run_count: int = 1,
    fix_cycle_count: int = 0,
    credit_units_used: int = 1,
    error: str | None = "should-not-leak",
) -> WorkflowRecord:
    clock = _now()
    return WorkflowRecord(
        workflow_id=workflow_id or str(uuid.uuid4()),
        state=state,
        version=3,
        policy_snapshot=_policy(),
        step_specs={
            "implementation": {"mission_yaml": "api_key: SUPER_SECRET_YAML"}
        },
        created_at=clock,
        updated_at=clock,
        started_at=clock,
        completed_at=clock,
        child_run_count=child_run_count,
        fix_cycle_count=fix_cycle_count,
        credit_units_used=credit_units_used,
        error=error,
        last_decision={"stdout": "LEAK_STDOUT", "token": "webhook-secret"},
    )


def _raw_outbox_rows(outbox: NotificationOutbox) -> list[sqlite3.Row]:
    with outbox._lock:
        return list(
            outbox._conn.execute(
                "SELECT * FROM notification_outbox ORDER BY created_at, event_id"
            ).fetchall()
        )


class RecordingQueue:
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


class TestSanitizedWorkflowPayload(unittest.TestCase):
    def test_allowlist_drops_yaml_errors_secrets_and_stdio(self) -> None:
        workflow = _workflow_record(
            state=WorkflowState.BLOCKED,
            child_run_count=2,
            fix_cycle_count=1,
            credit_units_used=3,
        )
        dirty = {
            **build_workflow_terminal_payload(workflow),
            "mission_yaml": "token: SUPER_SECRET_YAML",
            "stdout": "LEAK_STDOUT",
            "stderr": "LEAK_STDERR",
            "error": workflow.error,
            "last_decision": workflow.last_decision,
            "webhook_secret": "hook-secret",
            "credentials": "api_key=abcd",
            "instructions": workflow.step_specs,
        }
        clean = sanitize_notification_payload(dirty)
        self.assertEqual(clean["workflow_id"], workflow.workflow_id)
        self.assertEqual(clean["status"], WorkflowState.BLOCKED.value)
        self.assertEqual(clean["phase"], "workflow")
        self.assertEqual(clean["event_kind"], NotificationEventKind.TERMINAL.value)
        self.assertEqual(clean["child_run_count"], 2)
        self.assertEqual(clean["fix_cycle_count"], 1)
        self.assertEqual(clean["credit_units_used"], 3)
        self.assertEqual(
            clean["run_id"], workflow_terminal_run_id(workflow.workflow_id)
        )
        blob = json.dumps(clean)
        for leaked in (
            "SUPER_SECRET_YAML",
            "LEAK_STDOUT",
            "LEAK_STDERR",
            "should-not-leak",
            "hook-secret",
            "api_key=abcd",
            "webhook-secret",
        ):
            self.assertNotIn(leaked, blob)
        for banned in (
            "mission_yaml",
            "stdout",
            "stderr",
            "error",
            "last_decision",
            "webhook_secret",
            "credentials",
            "instructions",
        ):
            self.assertNotIn(banned, clean)

    def test_title_and_message_identify_workflow(self) -> None:
        workflow = _workflow_record(state=WorkflowState.NEEDS_APPROVAL)
        payload = build_workflow_terminal_payload(workflow)
        title = format_pushover_title("terminal", payload=payload)
        self.assertEqual(title, "Mission Control · workflow terminal")
        self.assertEqual(
            format_pushover_title("terminal"),
            "Mission Control · terminal",
        )
        body = format_pushover_message(payload)
        self.assertIn(f"workflow={workflow.workflow_id}", body)
        self.assertIn("status=needs_approval", body)
        self.assertIn("child_runs=1", body)
        self.assertNotIn("run=", body)
        self.assertNotIn("SUPER_SECRET_YAML", body)
        self.assertNotIn("should-not-leak", body)

    def test_counts_are_bounded_non_negative_ints(self) -> None:
        clean = sanitize_notification_payload(
            {
                "workflow_id": "wf-1",
                "child_run_count": -4,
                "fix_cycle_count": 10_000_000,
                "credit_units_used": True,
            }
        )
        self.assertEqual(clean["child_run_count"], 0)
        self.assertEqual(clean["fix_cycle_count"], 1_000_000)
        self.assertEqual(clean["credit_units_used"], 0)


class TestWorkflowTerminalEnqueueDedupe(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix="-notify.db")
        os.close(self._db_fd)
        self.outbox = NotificationOutbox(
            self._db_path, config=_disabled_outbox_config()
        )

    def tearDown(self) -> None:
        self.outbox.close()
        os.unlink(self._db_path)

    def test_non_terminal_is_skipped(self) -> None:
        pending = _workflow_record(state=WorkflowState.RUNNING)
        result = self.outbox.maybe_enqueue_workflow_terminal(pending)
        self.assertFalse(result.created)
        self.assertEqual(result.skipped_reason, "not_terminal")
        self.assertEqual(_raw_outbox_rows(self.outbox), [])

    def test_repeat_enqueue_is_exactly_one_row(self) -> None:
        workflow = _workflow_record(state=WorkflowState.COMPLETED)
        first = self.outbox.maybe_enqueue_workflow_terminal(workflow)
        second = self.outbox.maybe_enqueue_workflow_terminal(workflow)
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(second.skipped_reason, "duplicate")
        self.assertEqual(first.event_id, second.event_id)
        identity = workflow_terminal_run_id(workflow.workflow_id)
        events = self.outbox.list_for_run(identity)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_kind"], "terminal")
        self.assertEqual(events[0]["status"], "completed")
        rows = _raw_outbox_rows(self.outbox)
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload_json"])
        self.assertEqual(payload["workflow_id"], workflow.workflow_id)
        self.assertEqual(
            rows[0]["dedupe_key"],
            workflow_terminal_dedupe_key(WorkflowState.COMPLETED.value),
        )

    def test_does_not_collide_with_standalone_run_terminal(self) -> None:
        registry = RunRegistry(self._db_path)
        try:
            run = registry.create_run()
            run_result = self.outbox.enqueue_for_record(
                run,
                event_kind=NotificationEventKind.TERMINAL,
                dedupe_key="terminal:completed:t1",
            )
            workflow = _workflow_record(
                state=WorkflowState.FAILED, workflow_id=run.run_id
            )
            wf_result = self.outbox.maybe_enqueue_workflow_terminal(workflow)
            self.assertTrue(run_result.created)
            self.assertTrue(wf_result.created)
            self.assertNotEqual(run_result.event_id, wf_result.event_id)
            self.assertEqual(len(_raw_outbox_rows(self.outbox)), 2)
            self.assertEqual(len(self.outbox.list_for_run(run.run_id)), 1)
            self.assertEqual(
                len(
                    self.outbox.list_for_run(
                        workflow_terminal_run_id(run.run_id)
                    )
                ),
                1,
            )
        finally:
            registry.close()

    def test_concurrent_enqueue_is_idempotent(self) -> None:
        workflow = _workflow_record(state=WorkflowState.BUDGET_EXHAUSTED)
        results: list = []

        def _worker() -> None:
            box = NotificationOutbox(
                self._db_path, config=_disabled_outbox_config()
            )
            try:
                results.append(box.maybe_enqueue_workflow_terminal(workflow))
            finally:
                box.close()

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        created = [item for item in results if item.created]
        self.assertEqual(len(created), 1)
        event_ids = {item.event_id for item in results if item.event_id}
        self.assertEqual(len(event_ids), 1)
        self.assertEqual(len(_raw_outbox_rows(self.outbox)), 1)

    def test_restart_reuses_same_dedupe_identity(self) -> None:
        workflow = _workflow_record(state=WorkflowState.CANCELLED)
        first = self.outbox.maybe_enqueue_workflow_terminal(workflow)
        self.assertTrue(first.created)
        event_id = first.event_id
        self.outbox.close()
        self.outbox = NotificationOutbox(
            self._db_path, config=_disabled_outbox_config()
        )
        again = self.outbox.maybe_enqueue_workflow_terminal(workflow)
        self.assertFalse(again.created)
        self.assertEqual(again.event_id, event_id)
        self.assertEqual(len(_raw_outbox_rows(self.outbox)), 1)


class ReconcilerNotifyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._wf_fd, self._wf_path = tempfile.mkstemp(suffix="-wf.db")
        self._run_fd, self._run_path = tempfile.mkstemp(suffix="-run.db")
        os.close(self._wf_fd)
        os.close(self._run_fd)
        self.workflow_registry = WorkflowRegistry(self._wf_path)
        self.run_registry = RunRegistry(self._run_path)
        self.queue = RecordingQueue()
        self.outbox = NotificationOutbox(
            self._run_path, config=_disabled_outbox_config()
        )
        self.run_registry._notification_outbox = self.outbox
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

    def _force_terminal(self, workflow_id: str, state: WorkflowState):
        current = self.workflow_registry.get_workflow(workflow_id)
        assert current is not None
        result = self.workflow_registry.apply_cas_transition(
            workflow_id=workflow_id,
            expected_version=current.version,
            to_state=state,
            reason=TransitionReason.ERROR,
            detail={"test": "terminal"},
        )
        self.assertTrue(result.ok)
        return result.workflow

    def _terminal_rows(self, workflow_id: str) -> list[sqlite3.Row]:
        identity = workflow_terminal_run_id(workflow_id)
        return [
            row
            for row in _raw_outbox_rows(self.outbox)
            if row["run_id"] == identity and row["event_kind"] == "terminal"
        ]


class TestReconcilerDurableEnqueue(ReconcilerNotifyTestCase):
    def test_child_failure_enqueues_one_workflow_terminal(self) -> None:
        wf = self._create_workflow()
        self.reconciler.tick_once()
        impl = self.workflow_registry.list_steps(wf.workflow_id)[0]
        self._complete_child(
            impl.child_run_id,
            status=RunStatus.FAILED,
            stdout="",
            error="boom",
        )
        self.reconciler.tick_once()
        final = self.workflow_registry.get_workflow(wf.workflow_id)
        assert final is not None
        self.assertEqual(final.state, WorkflowState.FAILED)
        self.assertTrue(final.notification_emitted)
        rows = self._terminal_rows(wf.workflow_id)
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload_json"])
        self.assertEqual(payload["workflow_id"], wf.workflow_id)
        self.assertEqual(payload["status"], "failed")
        self.assertNotIn("boom", json.dumps(payload))
        self.reconciler.tick_once()
        self.assertEqual(len(self._terminal_rows(wf.workflow_id)), 1)

    def test_needs_approval_and_cancel_each_enqueue_once(self) -> None:
        wf = self._create_workflow()
        self.reconciler.tick_once()
        impl = self.workflow_registry.list_steps(wf.workflow_id)[0]
        self._complete_child(impl.child_run_id)
        self.reconciler.tick_once()
        review = [
            step
            for step in self.workflow_registry.list_steps(wf.workflow_id)
            if step.step_type is StepType.REVIEW
        ][0]
        self._complete_child(
            review.child_run_id,
            stdout=format_review_verdict_envelope("merge_ready"),
        )
        self.reconciler.tick_once()
        approved = self.workflow_registry.get_workflow(wf.workflow_id)
        assert approved is not None
        self.assertEqual(approved.state, WorkflowState.NEEDS_APPROVAL)
        self.assertTrue(approved.notification_emitted)
        self.assertEqual(len(self._terminal_rows(wf.workflow_id)), 1)

        cancelled = self._create_workflow()
        self.workflow_registry.cancel_workflow(cancelled.workflow_id)
        self.reconciler.tick_once()
        latched = self.workflow_registry.get_workflow(cancelled.workflow_id)
        assert latched is not None
        self.assertEqual(latched.state, WorkflowState.CANCELLED)
        self.assertTrue(latched.notification_emitted)
        self.assertEqual(len(self._terminal_rows(cancelled.workflow_id)), 1)

    def test_feature_off_does_not_enqueue(self) -> None:
        off = WorkflowReconciler(
            workflow_registry=self.workflow_registry,
            run_registry=self.run_registry,
            run_queue=self.queue,
            environ=_FEATURE_OFF,
        )
        wf = self._create_workflow()
        self._force_terminal(wf.workflow_id, WorkflowState.FAILED)
        stats = off.tick_once()
        self.assertTrue(stats.feature_disabled)
        latest = self.workflow_registry.get_workflow(wf.workflow_id)
        assert latest is not None
        self.assertFalse(latest.notification_emitted)
        self.assertEqual(self._terminal_rows(wf.workflow_id), [])

    def test_crash_between_enqueue_and_mark_retries_same_identity(self) -> None:
        wf = self._create_workflow()
        terminal = self._force_terminal(wf.workflow_id, WorkflowState.BLOCKED)
        assert terminal is not None
        first = self.outbox.maybe_enqueue_workflow_terminal(terminal)
        self.assertTrue(first.created)
        self.assertFalse(terminal.notification_emitted)
        self.reconciler.tick_once()
        latched = self.workflow_registry.get_workflow(wf.workflow_id)
        assert latched is not None
        self.assertTrue(latched.notification_emitted)
        self.assertEqual(len(self._terminal_rows(wf.workflow_id)), 1)
        self.assertEqual(
            self._terminal_rows(wf.workflow_id)[0]["event_id"], first.event_id
        )

    def test_crash_after_mark_keeps_durable_row(self) -> None:
        wf = self._create_workflow()
        terminal = self._force_terminal(wf.workflow_id, WorkflowState.FAILED)
        assert terminal is not None
        enqueued = self.outbox.maybe_enqueue_workflow_terminal(terminal)
        marked = self.workflow_registry.mark_notification_emitted(
            wf.workflow_id, expected_version=terminal.version
        )
        self.assertTrue(enqueued.created)
        self.assertTrue(marked.ok)
        self.reconciler.tick_once()
        self.reconciler.tick_once()
        self.assertEqual(len(self._terminal_rows(wf.workflow_id)), 1)

    def test_two_reconcilers_enqueue_one_row(self) -> None:
        wf = self._create_workflow()
        self._force_terminal(wf.workflow_id, WorkflowState.COMPLETED)
        other_wf = WorkflowRegistry(self._wf_path)
        other_runs = RunRegistry(self._run_path)
        other_runs._notification_outbox = self.outbox
        other = WorkflowReconciler(
            workflow_registry=other_wf,
            run_registry=other_runs,
            run_queue=self.queue,
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
            rows = self._terminal_rows(wf.workflow_id)
            self.assertEqual(len(rows), 1)
            latched = self.workflow_registry.get_workflow(wf.workflow_id)
            assert latched is not None
            self.assertTrue(latched.notification_emitted)
        finally:
            other.stop(timeout=1.0)
            other_wf.close()
            other_runs.close()


class TestWorkflowTerminalPushoverDelivery(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix="-po.db")
        os.close(self._db_fd)

    def tearDown(self) -> None:
        os.unlink(self._db_path)

    def _outbox(self, handler) -> NotificationOutbox:
        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, follow_redirects=False)
        return NotificationOutbox(
            self._db_path,
            config=_pushover_config(),
            http_client=client,
        )

    def test_one_pushover_delivery_across_retries_and_reenqueue(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.content.decode("utf-8"))
            return httpx.Response(200, json={"status": 1})

        outbox = self._outbox(handler)
        try:
            workflow = _workflow_record(state=WorkflowState.NEEDS_APPROVAL)
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=PUSHOVER_API_URL,
            ):
                first = outbox.maybe_enqueue_workflow_terminal(workflow)
                self.assertTrue(first.created)
                outbox.process_due_deliveries()
                outbox.process_due_deliveries()
                again = outbox.maybe_enqueue_workflow_terminal(workflow)
                self.assertFalse(again.created)
                outbox.process_due_deliveries()
            self.assertEqual(len(seen), 1)
            body = seen[0]
            self.assertIn("workflow", body)
            self.assertIn(workflow.workflow_id, body)
            self.assertIn("needs_approval", body)
            self.assertIn("Mission+Control", body)
            self.assertIn("workflow+terminal", body)
            self.assertNotIn("SUPER_SECRET_YAML", body)
            self.assertNotIn("should-not-leak", body)
            row = outbox.get_event(first.event_id or "")
            assert row is not None
            self.assertEqual(row["delivery_state"], DeliveryState.DELIVERED.value)
            self.assertEqual(
                len(
                    [
                        item
                        for item in _raw_outbox_rows(outbox)
                        if item["event_kind"] == "terminal"
                    ]
                ),
                1,
            )
        finally:
            outbox.close()


if __name__ == "__main__":
    unittest.main()
