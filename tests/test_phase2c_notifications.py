"""Focused tests for Phase 2C durable mission notifications."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import app.api as api_module
from app.api import app
from mission_control.notifications import (
    WEBHOOK_SECRET_ENV,
    WEBHOOK_URL_ENV,
    DeliveryState,
    NotificationConfig,
    NotificationEventKind,
    NotificationOutbox,
    compute_backoff_seconds,
    is_notifications_configured,
    load_notification_config,
    redact_notification_error,
    redact_outbox_row,
    sign_webhook_body,
    validate_webhook_url,
    verify_webhook_signature,
)
from mission_control.run_registry import (
    RunPhase,
    RunRegistry,
    RunStatus,
    platform_progress,
)

TEST_API_KEY = "mc_test_authentication_key"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_API_KEY}"}
os.environ["MISSION_CONTROL_API_KEY"] = TEST_API_KEY

# Public host that resolves publicly; SSRF tests patch getaddrinfo.
_PUBLIC_WEBHOOK = "https://example.com/hooks/mission-control"


def _config(
    *,
    enabled: bool = True,
    url: str | None = _PUBLIC_WEBHOOK,
    secret: str | None = "test-webhook-secret-value",
    max_attempts: int = 3,
    timeout_seconds: float = 2.0,
    backoff_base_seconds: float = 0.01,
    backoff_max_seconds: float = 1.0,
) -> NotificationConfig:
    return NotificationConfig(
        enabled=enabled,
        webhook_url=url,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        backoff_base_seconds=backoff_base_seconds,
        backoff_max_seconds=backoff_max_seconds,
        _secret=secret,
    )


class TestNotificationConfig(unittest.TestCase):
    def test_no_config_is_safe_disabled(self) -> None:
        cfg = load_notification_config(environ={})
        self.assertFalse(cfg.enabled)
        self.assertFalse(is_notifications_configured(cfg))
        rendered = repr(cfg)
        self.assertIn("has_secret=False", rendered)
        self.assertNotIn("webhook_secret", rendered.lower())
        self.assertNotRegex(rendered, r"secret=['\"]")

    def test_requires_url_and_secret(self) -> None:
        cfg = load_notification_config(
            environ={WEBHOOK_URL_ENV: _PUBLIC_WEBHOOK}
        )
        self.assertFalse(is_notifications_configured(cfg))
        cfg2 = load_notification_config(
            environ={
                WEBHOOK_URL_ENV: _PUBLIC_WEBHOOK,
                WEBHOOK_SECRET_ENV: "s3cret-value-for-tests",
            }
        )
        self.assertTrue(is_notifications_configured(cfg2))
        self.assertNotIn("s3cret-value-for-tests", repr(cfg2))


class TestIdempotencyAndFiltering(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)
        self.outbox = NotificationOutbox(
            self._db_path, config=_config(enabled=False)
        )

    def tearDown(self) -> None:
        self.outbox.close()
        self.registry.close()
        os.unlink(self._db_path)

    def test_idempotent_enqueue(self) -> None:
        record = self.registry.create_run()
        first = self.outbox.enqueue_for_record(
            record,
            event_kind=NotificationEventKind.PHASE_CHANGE,
            dedupe_key="phase:agent_execution:t1",
        )
        second = self.outbox.enqueue_for_record(
            record,
            event_kind=NotificationEventKind.PHASE_CHANGE,
            dedupe_key="phase:agent_execution:t1",
        )
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(second.skipped_reason, "duplicate")
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(len(self.outbox.list_for_run(record.run_id)), 1)

    def test_concurrent_enqueue_is_idempotent(self) -> None:
        record = self.registry.create_run()
        results: list = []

        def _worker() -> None:
            box = NotificationOutbox(
                self._db_path, config=_config(enabled=False)
            )
            try:
                results.append(
                    box.enqueue_for_record(
                        record,
                        event_kind=NotificationEventKind.TERMINAL,
                        dedupe_key="terminal:completed:t1",
                    )
                )
            finally:
                box.close()

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        created = [r for r in results if r.created]
        self.assertEqual(len(created), 1)
        self.assertEqual(len(self.outbox.list_for_run(record.run_id)), 1)

    def test_filters_non_eligible_and_never_heartbeats(self) -> None:
        record = self.registry.create_run()
        skipped = self.outbox.enqueue(
            run_id=record.run_id,
            event_kind="heartbeat",
            dedupe_key="hb-1",
            payload={"run_id": record.run_id, "event_kind": "heartbeat"},
        )
        self.assertFalse(skipped.created)
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        for _ in range(5):
            self.registry.touch_heartbeat(record.run_id)
        events = self.outbox.list_for_run(record.run_id)
        self.assertEqual(events, [])
        # Phase change via set_phase should emit exactly once per phase entry.
        self.registry.set_phase(
            record.run_id,
            RunPhase.AGENT_EXECUTION,
            progress=platform_progress(step="agent_execution", detail="go"),
        )
        self.registry.set_phase(
            record.run_id,
            RunPhase.AGENT_EXECUTION,
            progress=platform_progress(step="agent_execution", detail="go"),
        )
        kinds = [e["event_kind"] for e in self.outbox.list_for_run(record.run_id)]
        self.assertEqual(kinds.count("phase_change"), 1)
        self.assertNotIn("heartbeat", kinds)


class TestStaleRecoveryTerminal(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)
        self.outbox = self.registry._get_notification_outbox()
        self.outbox._config = _config(enabled=False)

    def tearDown(self) -> None:
        self.registry.close()
        os.unlink(self._db_path)

    def test_stale_and_terminal_and_recovery(self) -> None:
        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        self.registry.set_phase(record.run_id, RunPhase.AGENT_EXECUTION)
        stale_at = datetime.now(timezone.utc) - timedelta(seconds=120)
        with self.registry._lock:
            self.registry._conn.execute(
                "UPDATE runs SET heartbeat_at = ? WHERE run_id = ?",
                (stale_at.isoformat(), record.run_id),
            )
            self.registry._conn.commit()
        live = self.registry.get_run(record.run_id)
        assert live is not None
        first = self.outbox.maybe_enqueue_stale(live)
        second = self.outbox.maybe_enqueue_stale(live)
        self.assertTrue(first.created)
        self.assertFalse(second.created)

        self.registry.update_status(record.run_id, RunStatus.COMPLETED)
        kinds = {
            e["event_kind"] for e in self.outbox.list_for_run(record.run_id)
        }
        self.assertIn("stale", kinds)
        self.assertIn("terminal", kinds)
        self.assertIn("phase_change", kinds)

        # Recovery path on a fresh interrupted run.
        interrupted = self.registry.create_run()
        self.registry.update_status(interrupted.run_id, RunStatus.RUNNING)
        recovered = self.registry.recover_interrupted_runs()
        self.assertGreaterEqual(recovered, 1)
        rec_kinds = {
            e["event_kind"]
            for e in self.outbox.list_for_run(interrupted.run_id)
        }
        self.assertIn("recovery", rec_kinds)
        self.assertIn("terminal", rec_kinds)
        final = self.registry.get_run(interrupted.run_id)
        assert final is not None
        self.assertEqual(final.status, RunStatus.FAILED)


class TestDeliveryRetriesAndHmac(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)
        self.secret = "test-webhook-secret-value"
        self.calls: list[httpx.Request] = []

    def tearDown(self) -> None:
        self.registry.close()
        os.unlink(self._db_path)

    def _outbox_with_transport(self, handler) -> NotificationOutbox:
        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, timeout=2.0)
        return NotificationOutbox(
            self._db_path,
            config=_config(secret=self.secret, max_attempts=3),
            http_client=client,
        )

    def test_hmac_signed_delivery_and_retry_then_dead(self) -> None:
        statuses = [500, 500, 500]

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            return httpx.Response(statuses.pop(0) if statuses else 500)

        with patch(
            "mission_control.notifications.validate_webhook_url",
            return_value=_PUBLIC_WEBHOOK,
        ):
            outbox = self._outbox_with_transport(handler)
            try:
                record = self.registry.create_run()
                self.registry.update_status(record.run_id, RunStatus.RUNNING)
                self.registry.update_status(record.run_id, RunStatus.COMPLETED)
                # Force due immediately.
                with outbox._lock:
                    outbox._conn.execute(
                        "UPDATE notification_outbox SET next_attempt_at = NULL"
                    )
                    outbox._conn.commit()

                for _ in range(3):
                    with outbox._lock:
                        outbox._conn.execute(
                            "UPDATE notification_outbox SET next_attempt_at = NULL "
                            "WHERE delivery_state = 'pending'"
                        )
                        outbox._conn.commit()
                    outbox.process_due_deliveries()

                events = outbox.list_for_run(record.run_id)
                terminal = [e for e in events if e["event_kind"] == "terminal"]
                self.assertTrue(terminal)
                self.assertEqual(terminal[0]["delivery_state"], "dead")
                self.assertGreaterEqual(len(self.calls), 3)

                body = self.calls[0].content
                signature = self.calls[0].headers["X-Mission-Control-Signature"]
                self.assertTrue(
                    verify_webhook_signature(
                        body, secret=self.secret, signature_header=signature
                    )
                )
                self.assertNotIn(self.secret, body.decode("utf-8"))
                self.assertNotIn(
                    self.secret, json.dumps(terminal[0])
                )
            finally:
                outbox.close()

    def test_successful_delivery(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            return httpx.Response(204)

        with patch(
            "mission_control.notifications.validate_webhook_url",
            return_value=_PUBLIC_WEBHOOK,
        ):
            outbox = self._outbox_with_transport(handler)
            try:
                record = self.registry.create_run()
                outbox.enqueue_for_record(
                    record,
                    event_kind=NotificationEventKind.PHASE_CHANGE,
                    dedupe_key="phase:queued:t0",
                )
                outbox.process_due_deliveries()
                events = outbox.list_for_run(record.run_id)
                self.assertEqual(events[0]["delivery_state"], "delivered")
                self.assertEqual(len(self.calls), 1)
            finally:
                outbox.close()

    def test_permanent_invalid_url_marks_dead_without_http(self) -> None:
        outbox = NotificationOutbox(
            self._db_path,
            config=_config(url="https://127.0.0.1/hook", secret=self.secret),
        )
        try:
            record = self.registry.create_run()
            outbox.enqueue_for_record(
                record,
                event_kind=NotificationEventKind.STALE,
                dedupe_key="stale:t0",
            )
            outbox.process_due_deliveries()
            events = outbox.list_for_run(record.run_id)
            self.assertEqual(events[0]["delivery_state"], "dead")
            self.assertIsNotNone(events[0]["last_error"])
            self.assertNotIn(self.secret, str(events[0]["last_error"]))
        finally:
            outbox.close()

    def test_delivery_failure_does_not_alter_run_status(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        with patch(
            "mission_control.notifications.validate_webhook_url",
            return_value=_PUBLIC_WEBHOOK,
        ):
            outbox = self._outbox_with_transport(handler)
            try:
                record = self.registry.create_run()
                self.registry.update_status(record.run_id, RunStatus.RUNNING)
                self.registry.update_status(record.run_id, RunStatus.COMPLETED)
                before = self.registry.get_run(record.run_id)
                assert before is not None
                outbox.process_due_deliveries()
                after = self.registry.get_run(record.run_id)
                assert after is not None
                self.assertEqual(before.status, after.status)
                self.assertEqual(before.phase, after.phase)
                self.assertEqual(before.completed_at, after.completed_at)
            finally:
                outbox.close()

    def test_restart_preserves_pending_outbox(self) -> None:
        record = self.registry.create_run()
        box1 = NotificationOutbox(
            self._db_path, config=_config(enabled=False)
        )
        result = box1.enqueue_for_record(
            record,
            event_kind=NotificationEventKind.PHASE_CHANGE,
            dedupe_key="phase:workspace_preparation:t1",
        )
        self.assertTrue(result.created)
        box1.close()

        box2 = NotificationOutbox(
            self._db_path, config=_config(enabled=False)
        )
        try:
            events = box2.list_for_run(record.run_id)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["delivery_state"], "pending")
            # Re-enqueue after restart is idempotent.
            again = box2.enqueue_for_record(
                record,
                event_kind=NotificationEventKind.PHASE_CHANGE,
                dedupe_key="phase:workspace_preparation:t1",
            )
            self.assertFalse(again.created)
        finally:
            box2.close()


class TestRedactionAndHelpers(unittest.TestCase):
    def test_hmac_helpers(self) -> None:
        body = b'{"ok":true}'
        secret = "unit-test-secret"
        ts, header = sign_webhook_body(body, secret=secret, timestamp="1000")
        self.assertEqual(ts, "1000")
        self.assertTrue(
            verify_webhook_signature(
                body, secret=secret, signature_header=header, now=1000.0
            )
        )
        expected = hmac.new(
            secret.encode(), b"1000." + body, hashlib.sha256
        ).hexdigest()
        self.assertIn(expected, header)

    def test_backoff_exponential(self) -> None:
        self.assertEqual(compute_backoff_seconds(1, base_seconds=1.0), 1.0)
        self.assertEqual(compute_backoff_seconds(2, base_seconds=1.0), 2.0)
        self.assertEqual(compute_backoff_seconds(3, base_seconds=1.0), 4.0)
        self.assertEqual(
            compute_backoff_seconds(20, base_seconds=1.0, max_seconds=10.0),
            10.0,
        )

    def test_redaction_strips_secretish_errors(self) -> None:
        self.assertEqual(
            redact_notification_error("Authorization bearer abc"),
            "[redacted]",
        )
        row = redact_outbox_row(
            {
                "event_id": "e1",
                "run_id": "r1",
                "event_kind": "terminal",
                "delivery_state": "pending",
                "attempt_count": 0,
                "payload_json": json.dumps(
                    {
                        "run_id": "r1",
                        "event_kind": "terminal",
                        "status": "completed",
                        "phase": "completed",
                        "stdout": "SECRET SHOULD DROP",
                    }
                ),
            }
        )
        blob = json.dumps(row)
        self.assertNotIn("SECRET SHOULD DROP", blob)
        self.assertNotIn("stdout", blob)

    def test_validate_webhook_blocks_localhost(self) -> None:
        with self.assertRaises(ValueError):
            validate_webhook_url("http://localhost/hook")
        with self.assertRaises(ValueError):
            validate_webhook_url("https://127.0.0.1/hook")


class TestApiInspectionAndNoConfig(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)
        self.outbox = self.registry._get_notification_outbox()
        self.outbox._config = _config(enabled=False)
        self._prev_registry = api_module.run_registry
        self._prev_outbox = api_module.notification_outbox
        api_module.run_registry = self.registry
        api_module.notification_outbox = self.outbox
        # Clear webhook env for no-config behavior.
        self._env_backup = {
            WEBHOOK_URL_ENV: os.environ.pop(WEBHOOK_URL_ENV, None),
            WEBHOOK_SECRET_ENV: os.environ.pop(WEBHOOK_SECRET_ENV, None),
        }
        self.client = TestClient(app, raise_server_exceptions=True)

    def tearDown(self) -> None:
        self.client.close()
        api_module.run_registry = self._prev_registry
        api_module.notification_outbox = self._prev_outbox
        self.registry.close()
        os.unlink(self._db_path)
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_api_lists_redacted_notifications_auth_required(self) -> None:
        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        self.registry.set_phase(record.run_id, RunPhase.VERIFICATION)
        self.registry.update_status(record.run_id, RunStatus.COMPLETED)

        unauth = self.client.get(f"/runs/{record.run_id}/notifications")
        self.assertEqual(unauth.status_code, 401)

        response = self.client.get(
            f"/runs/{record.run_id}/notifications",
            headers=AUTH_HEADERS,
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["run_id"], record.run_id)
        self.assertFalse(body["notifications_enabled"])
        kinds = [e["event_kind"] for e in body["events"]]
        self.assertIn("phase_change", kinds)
        self.assertIn("terminal", kinds)
        blob = json.dumps(body)
        self.assertNotIn("test-webhook-secret-value", blob)
        self.assertNotIn(WEBHOOK_SECRET_ENV, blob)
        for event in body["events"]:
            self.assertIn(event["delivery_state"], {"pending", "delivered", "dead", "in_flight"})

    def test_wait_still_works_and_emits_stale_without_mutation(self) -> None:
        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        self.registry.set_phase(record.run_id, RunPhase.AGENT_EXECUTION)
        stale_at = datetime.now(timezone.utc) - timedelta(seconds=120)
        with self.registry._lock:
            self.registry._conn.execute(
                "UPDATE runs SET heartbeat_at = ? WHERE run_id = ?",
                (stale_at.isoformat(), record.run_id),
            )
            self.registry._conn.commit()

        response = self.client.post(
            f"/runs/{record.run_id}/wait",
            headers=AUTH_HEADERS,
            json={"timeout_seconds": 0.1, "poll_interval_seconds": 0.05},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["wait_expired"])
        self.assertEqual(body["heartbeat_health"], "stale")
        self.assertTrue(body["stale_heartbeat"])
        after = self.registry.get_run(record.run_id)
        assert after is not None
        self.assertEqual(after.status, RunStatus.RUNNING)
        kinds = {
            e["event_kind"] for e in self.outbox.list_for_run(record.run_id)
        }
        self.assertIn("stale", kinds)


class TestNoConfigDeliverySkipped(unittest.TestCase):
    def test_process_due_without_config_leaves_pending(self) -> None:
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        try:
            registry = RunRegistry(db_path)
            outbox = NotificationOutbox(
                db_path, config=_config(enabled=False, url=None, secret=None)
            )
            record = registry.create_run()
            outbox.enqueue_for_record(
                record,
                event_kind=NotificationEventKind.PHASE_CHANGE,
                dedupe_key="phase:queued:t0",
            )
            attempted = outbox.process_due_deliveries()
            self.assertEqual(attempted, 1)
            events = outbox.list_for_run(record.run_id)
            self.assertEqual(events[0]["delivery_state"], "pending")
            outbox.close()
            registry.close()
        finally:
            os.unlink(db_path)


if __name__ == "__main__":
    unittest.main()
