"""Slice C (second half): suppress Pushover for durable workflow child runs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    WORKFLOW_CHILD_SUPPRESSED,
    DeliveryState,
    NotificationConfig,
    NotificationEventKind,
    NotificationOutbox,
    _format_dt,
    workflow_terminal_dedupe_key,
    workflow_terminal_run_id,
)
from mission_control.run_registry import RunPhase, RunRegistry, RunStatus
from mission_control.workflow_registry import (
    WorkflowPolicySnapshot,
    WorkflowRecord,
    WorkflowRegistry,
    WorkflowState,
)

_TEST_USER = "pushover-user-key-fixture-aaaa"
_TEST_TOKEN = "pushover-app-token-fixture-bbbb"


def _pushover_config(**overrides) -> NotificationConfig:
    base = dict(
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
    base.update(overrides)
    return NotificationConfig(**base)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _raw(outbox: NotificationOutbox, event_id: str) -> sqlite3.Row:
    with outbox._lock:
        row = outbox._conn.execute(
            "SELECT * FROM notification_outbox WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    assert row is not None
    return row


def _insert_pending_row(
    outbox: NotificationOutbox,
    *,
    event_id: str,
    run_id: str,
    event_kind: str,
    dedupe_key: str,
    delivery_state: str = DeliveryState.PENDING.value,
    claim_owner: str | None = None,
    claim_expires_at: str | None = None,
    attempt_count: int = 0,
) -> None:
    now_s = _format_dt(_now())
    assert now_s is not None
    body = {
        "run_id": run_id,
        "event_kind": event_kind,
        "occurred_at": now_s,
        "dedupe_key": dedupe_key,
    }
    with outbox._lock:
        outbox._conn.execute(
            """
            INSERT INTO notification_outbox (
                event_id,
                run_id,
                event_kind,
                dedupe_key,
                payload_json,
                delivery_state,
                attempt_count,
                next_attempt_at,
                last_error,
                delivered_at,
                created_at,
                updated_at,
                claim_owner,
                claim_expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
            """,
            (
                event_id,
                run_id,
                event_kind,
                dedupe_key,
                json.dumps(body, separators=(",", ":"), sort_keys=True),
                delivery_state,
                attempt_count,
                now_s,
                now_s,
                now_s,
                claim_owner,
                claim_expires_at,
            ),
        )
        outbox._conn.commit()


def _bind_child_run(wf_registry: WorkflowRegistry, child_run_id: str) -> None:
    """Record durable workflow_steps.child_run_id membership only."""
    now_s = _format_dt(_now())
    assert now_s is not None
    with wf_registry._lock:
        wf_registry._conn.execute(
            """
            INSERT INTO workflow_steps (
                step_id,
                workflow_id,
                step_type,
                status,
                attempt,
                cycle,
                idempotency_key,
                child_run_id,
                parent_run_id,
                mission_yaml,
                policy_json,
                created_at,
                updated_at,
                materialization_state
            ) VALUES (?, ?, 'implementation', 'running', 1, 0, ?, ?, NULL,
                      'mission: child\n', '{}', ?, ?, 'acked')
            """,
            (
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                child_run_id,
                now_s,
                now_s,
            ),
        )
        wf_registry._conn.commit()


def _workflow_record(*, workflow_id: str, state: WorkflowState) -> WorkflowRecord:
    clock = _now()
    return WorkflowRecord(
        workflow_id=workflow_id,
        state=state,
        version=1,
        policy_snapshot=WorkflowPolicySnapshot(
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
        ),
        step_specs={},
        created_at=clock,
        updated_at=clock,
        started_at=clock,
        completed_at=clock,
        child_run_count=1,
        fix_cycle_count=0,
        credit_units_used=1,
    )


class WorkflowChildSuppressionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix="-child-notify.db")
        os.close(self._db_fd)
        self.runs = RunRegistry(self._db_path)
        self.workflows = WorkflowRegistry(self._db_path)
        self.posts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.posts.append(request.content.decode("utf-8"))
            return httpx.Response(200, json={"status": 1})

        transport = httpx.MockTransport(handler)
        self._client = httpx.Client(transport=transport, follow_redirects=False)
        self.outbox = NotificationOutbox(
            self._db_path,
            config=_pushover_config(),
            http_client=self._client,
        )

    def tearDown(self) -> None:
        self.outbox.close()
        self.runs.close()
        self.workflows.close()
        self._client.close()
        os.unlink(self._db_path)

    def _drain(self, outbox: NotificationOutbox | None = None) -> int:
        box = outbox if outbox is not None else self.outbox
        with patch(
            "mission_control.notifications.validate_webhook_url",
            return_value=PUSHOVER_API_URL,
        ):
            return box.process_due_deliveries(limit=32)

    def _assert_skipped(self, event_id: str) -> sqlite3.Row:
        row = _raw(self.outbox, event_id)
        self.assertEqual(row["delivery_state"], DeliveryState.SKIPPED.value)
        self.assertEqual(row["last_error"], WORKFLOW_CHILD_SUPPRESSED)
        self.assertIsNone(row["delivered_at"])
        self.assertIsNone(row["claim_owner"])
        return row


class TestDurableChildMembership(WorkflowChildSuppressionTestCase):
    def test_membership_uses_workflow_steps_not_names(self) -> None:
        child = self.runs.create_run()
        standalone = self.runs.create_run()
        self.assertFalse(self.outbox.is_durable_workflow_child_run(child.run_id))
        self.assertFalse(
            self.outbox.is_durable_workflow_child_run(standalone.run_id)
        )
        _bind_child_run(self.workflows, child.run_id)
        self.assertTrue(self.outbox.is_durable_workflow_child_run(child.run_id))
        self.assertFalse(
            self.outbox.is_durable_workflow_child_run(standalone.run_id)
        )
        synthetic = workflow_terminal_run_id(str(uuid.uuid4()))
        _bind_child_run(self.workflows, synthetic)
        self.assertFalse(self.outbox.is_durable_workflow_child_run(synthetic))
        self.assertFalse(self.outbox.is_durable_workflow_child_run("workflow:x"))

    def test_missing_workflow_steps_table_is_not_a_child(self) -> None:
        fd, path = tempfile.mkstemp(suffix="-no-wf.db")
        os.close(fd)
        runs = RunRegistry(path)
        outbox = NotificationOutbox(path, config=_pushover_config())
        try:
            record = runs.create_run()
            self.assertFalse(outbox.is_durable_workflow_child_run(record.run_id))
        finally:
            outbox.close()
            runs.close()
            os.unlink(path)


class TestStandaloneAndWorkflowTerminalUnchanged(WorkflowChildSuppressionTestCase):
    def test_standalone_terminal_still_pages(self) -> None:
        record = self.runs.create_run()
        result = self.outbox.enqueue_for_record(
            record,
            event_kind=NotificationEventKind.TERMINAL,
            dedupe_key="terminal:standalone",
        )
        self.assertTrue(result.created)
        self.assertIsNone(result.skipped_reason)
        self._drain()
        self.assertEqual(len(self.posts), 1)
        events = self.outbox.list_for_run(record.run_id)
        self.assertEqual(events[0]["delivery_state"], DeliveryState.DELIVERED.value)
        self.assertNotEqual(events[0]["last_error"], WORKFLOW_CHILD_SUPPRESSED)

    def test_workflow_terminal_synthetic_id_is_not_suppressed(self) -> None:
        workflow_id = str(uuid.uuid4())
        identity = workflow_terminal_run_id(workflow_id)
        _bind_child_run(self.workflows, identity)
        workflow = _workflow_record(
            workflow_id=workflow_id, state=WorkflowState.FAILED
        )
        result = self.outbox.maybe_enqueue_workflow_terminal(workflow)
        self.assertTrue(result.created)
        self._drain()
        self.assertEqual(len(self.posts), 1)
        events = self.outbox.list_for_run(identity)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["delivery_state"], DeliveryState.DELIVERED.value)
        self.assertEqual(
            events[0]["event_kind"], NotificationEventKind.TERMINAL.value
        )
        self.assertEqual(
            json.loads(_raw(self.outbox, result.event_id)["payload_json"])[
                "dedupe_key"
            ],
            workflow_terminal_dedupe_key(WorkflowState.FAILED.value),
        )


class TestChildEventSuppression(WorkflowChildSuppressionTestCase):
    def test_enqueue_skips_all_child_event_kinds(self) -> None:
        child = self.runs.create_run()
        _bind_child_run(self.workflows, child.run_id)
        kinds = (
            (NotificationEventKind.STALE, "stale:child"),
            (
                NotificationEventKind.RECOVERY,
                "recovery:stale:episode-child",
            ),
            (NotificationEventKind.PHASE_CHANGE, "phase:agent_execution:t"),
            (NotificationEventKind.TERMINAL, "terminal:completed:t"),
        )
        for kind, dedupe in kinds:
            with self.subTest(kind=kind.value):
                result = self.outbox.enqueue_for_record(
                    child, event_kind=kind, dedupe_key=dedupe
                )
                self.assertTrue(result.created)
                assert result.event_id is not None
                self._assert_skipped(result.event_id)
        self._drain()
        self._drain()
        self.assertEqual(self.posts, [])

    def test_pending_backlog_skips_after_membership_observed(self) -> None:
        child = self.runs.create_run()
        event_id = "child-backlog-terminal"
        _insert_pending_row(
            self.outbox,
            event_id=event_id,
            run_id=child.run_id,
            event_kind=NotificationEventKind.TERMINAL.value,
            dedupe_key="terminal:legacy-backlog",
        )
        self.assertEqual(
            _raw(self.outbox, event_id)["delivery_state"],
            DeliveryState.PENDING.value,
        )
        _bind_child_run(self.workflows, child.run_id)
        attempted = self._drain()
        self.assertEqual(attempted, 0)
        self._assert_skipped(event_id)
        self.assertEqual(self.posts, [])

    def test_in_flight_claimed_row_cannot_deliver(self) -> None:
        child = self.runs.create_run()
        future = _format_dt(_now() + timedelta(minutes=5))
        event_id = "child-claimed-stale"
        _insert_pending_row(
            self.outbox,
            event_id=event_id,
            run_id=child.run_id,
            event_kind=NotificationEventKind.STALE.value,
            dedupe_key="stale:claimed",
            delivery_state=DeliveryState.IN_FLIGHT.value,
            claim_owner="replica-a",
            claim_expires_at=future,
            attempt_count=1,
        )
        _bind_child_run(self.workflows, child.run_id)
        row = _raw(self.outbox, event_id)
        outcome = self.outbox._finalize_claimed_outbox_row(
            row,
            active_delivery_state=DeliveryState.DELIVERED.value,
            delivered_at=_format_dt(_now()),
            clear_error=True,
        )
        self.assertEqual(outcome, "skipped")
        self._assert_skipped(event_id)
        again = self.outbox._finalize_claimed_outbox_row(
            _raw(self.outbox, event_id),
            active_delivery_state=DeliveryState.DELIVERED.value,
            clear_error=True,
        )
        self.assertEqual(again, "cas_missed")
        self.assertEqual(self.posts, [])

    def test_restart_recovers_and_skips_legacy_pending(self) -> None:
        child = self.runs.create_run()
        event_id = "child-restart-recovery"
        _insert_pending_row(
            self.outbox,
            event_id=event_id,
            run_id=child.run_id,
            event_kind=NotificationEventKind.RECOVERY.value,
            dedupe_key="recovery:stale:restart",
        )
        _bind_child_run(self.workflows, child.run_id)
        self.outbox.close()
        restarted = NotificationOutbox(
            self._db_path,
            config=_pushover_config(),
            http_client=self._client,
        )
        try:
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=PUSHOVER_API_URL,
            ):
                restarted.process_due_deliveries(limit=16)
            row = restarted.get_event(event_id)
            assert row is not None
            self.assertEqual(row["delivery_state"], DeliveryState.SKIPPED.value)
            self.assertEqual(row["last_error"], WORKFLOW_CHILD_SUPPRESSED)
        finally:
            restarted.close()
        self.assertEqual(self.posts, [])

    def test_skipped_state_is_stable_across_restart(self) -> None:
        child = self.runs.create_run()
        _bind_child_run(self.workflows, child.run_id)
        result = self.outbox.enqueue_for_record(
            child,
            event_kind=NotificationEventKind.PHASE_CHANGE,
            dedupe_key="phase:queued:t",
        )
        assert result.event_id is not None
        self.outbox.close()
        restarted = NotificationOutbox(
            self._db_path,
            config=_pushover_config(),
            http_client=self._client,
        )
        try:
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=PUSHOVER_API_URL,
            ):
                restarted.process_due_deliveries(limit=16)
                restarted.process_due_deliveries(limit=16)
            row = restarted.get_event(result.event_id)
            assert row is not None
            self.assertEqual(row["delivery_state"], DeliveryState.SKIPPED.value)
            self.assertEqual(row["last_error"], WORKFLOW_CHILD_SUPPRESSED)
        finally:
            restarted.close()
        self.assertEqual(self.posts, [])


class TestChildSuppressionRaces(WorkflowChildSuppressionTestCase):
    def test_concurrent_replicas_send_zero_child_pushover(self) -> None:
        child = self.runs.create_run()
        _bind_child_run(self.workflows, child.run_id)
        event_id = "child-race-terminal"
        _insert_pending_row(
            self.outbox,
            event_id=event_id,
            run_id=child.run_id,
            event_kind=NotificationEventKind.TERMINAL.value,
            dedupe_key="terminal:race",
        )
        errors: list[BaseException] = []

        def _worker() -> None:
            replica = NotificationOutbox(
                self._db_path,
                config=_pushover_config(),
                http_client=self._client,
            )
            try:
                with patch(
                    "mission_control.notifications.validate_webhook_url",
                    return_value=PUSHOVER_API_URL,
                ):
                    replica.process_due_deliveries(limit=8)
                    replica.process_due_deliveries(limit=8)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                replica.close()

        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(self.posts, [])
        self._assert_skipped(event_id)

    def test_membership_during_http_skips_instead_of_delivered(self) -> None:
        child = self.runs.create_run()
        self.runs.update_status(child.run_id, RunStatus.RUNNING)
        live = self.runs.get_run(child.run_id)
        assert live is not None
        entered = threading.Event()
        release = threading.Event()
        posts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            posts["n"] += 1
            entered.set()
            self.assertTrue(release.wait(timeout=5), "release timed out")
            return httpx.Response(200, json={"status": 1})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, follow_redirects=False)
        racing = NotificationOutbox(
            self._db_path,
            config=_pushover_config(),
            http_client=client,
        )
        try:
            racing.enqueue_for_record(
                live,
                event_kind=NotificationEventKind.STALE,
                dedupe_key="stale:race-bind",
            )
            errors: list[BaseException] = []

            def _drain() -> None:
                try:
                    with patch(
                        "mission_control.notifications.validate_webhook_url",
                        return_value=PUSHOVER_API_URL,
                    ):
                        racing.process_due_deliveries(limit=8)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            worker = threading.Thread(target=_drain)
            worker.start()
            self.assertTrue(entered.wait(timeout=5))
            _bind_child_run(self.workflows, child.run_id)
            release.set()
            worker.join(timeout=10)
            self.assertFalse(errors)
            events = racing.list_for_run(child.run_id)
            stale = next(e for e in events if e["event_kind"] == "stale")
            self.assertEqual(stale["delivery_state"], DeliveryState.SKIPPED.value)
            self.assertEqual(stale["last_error"], WORKFLOW_CHILD_SUPPRESSED)
            self.assertNotEqual(
                stale["delivery_state"], DeliveryState.DELIVERED.value
            )
        finally:
            release.set()
            racing.close()
            client.close()

    def test_standalone_stale_terminal_race_reason_unchanged(self) -> None:
        """Child logic must not steal heartbeat terminal skip reasons."""
        from mission_control.notifications import (
            STALE_RECOVERY_TERMINAL_RUN_SUPPRESSED,
        )

        live = self.runs.create_run()
        self.runs.update_status(live.run_id, RunStatus.RUNNING)
        future = _format_dt(_now() + timedelta(minutes=5))
        event_id = "standalone-stale-terminal"
        _insert_pending_row(
            self.outbox,
            event_id=event_id,
            run_id=live.run_id,
            event_kind=NotificationEventKind.STALE.value,
            dedupe_key="stale:standalone-terminal",
            delivery_state=DeliveryState.IN_FLIGHT.value,
            claim_owner="worker-a",
            claim_expires_at=future,
        )
        now_s = _format_dt(_now())
        assert now_s is not None
        with self.runs._lock:
            self.runs._conn.execute(
                """
                UPDATE runs
                SET status = ?, completed_at = ?, phase = ?
                WHERE run_id = ?
                """,
                (RunStatus.COMPLETED.value, now_s, RunPhase.COMPLETED.value, live.run_id),
            )
            self.runs._conn.commit()
        outcome = self.outbox._finalize_terminal_dependent_outbox_row(
            _raw(self.outbox, event_id),
            active_delivery_state=DeliveryState.DELIVERED.value,
            delivered_at=now_s,
            clear_error=True,
        )
        self.assertEqual(outcome, "skipped")
        row = _raw(self.outbox, event_id)
        self.assertEqual(row["delivery_state"], DeliveryState.SKIPPED.value)
        self.assertEqual(row["last_error"], STALE_RECOVERY_TERMINAL_RUN_SUPPRESSED)
        self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main()
