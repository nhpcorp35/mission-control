"""Focused tests for Phase 2C durable mission notifications."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import urlparse

import httpx
from fastapi.testclient import TestClient

import app.api as api_module
from app.api import app
from mission_control.notifications import (
    WEBHOOK_SECRET_ENV,
    WEBHOOK_URL_ENV,
    DeliveryState,
    NotificationConfig,
    NotificationDeliveryWorker,
    NotificationEventKind,
    NotificationOutbox,
    compute_backoff_seconds,
    is_notifications_configured,
    load_notification_config,
    post_webhook_ssrf_safe,
    redact_notification_error,
    redact_outbox_row,
    sign_webhook_body,
    validate_webhook_url,
    verify_webhook_signature,
)
from mission_control.run_registry import (
    EXECUTION_LEASE_GRACE_SECONDS,
    INTERRUPTED_RUN_ERROR,
    OWNER_LOST_RUN_ERROR,
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
    claim_lease_seconds: float = 30.0,
    allow_http: bool = True,
    worker_poll_seconds: float = 0.05,
) -> NotificationConfig:
    return NotificationConfig(
        enabled=enabled,
        webhook_url=url,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        backoff_base_seconds=backoff_base_seconds,
        backoff_max_seconds=backoff_max_seconds,
        claim_lease_seconds=claim_lease_seconds,
        allow_http=allow_http,
        worker_poll_seconds=worker_poll_seconds,
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

    def _running_with_stale_heartbeat(
        self, *, age_seconds: float = 120.0, require_default_stale: bool = True
    ):
        from mission_control.monitoring import HEARTBEAT_STALE_THRESHOLD_SECONDS

        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        self.registry.set_phase(record.run_id, RunPhase.AGENT_EXECUTION)
        stale_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        with self.registry._lock:
            self.registry._conn.execute(
                "UPDATE runs SET heartbeat_at = ? WHERE run_id = ?",
                (stale_at.isoformat(), record.run_id),
            )
            self.registry._conn.commit()
        live = self.registry.get_run(record.run_id)
        assert live is not None
        if require_default_stale:
            self.assertGreater(age_seconds, HEARTBEAT_STALE_THRESHOLD_SECONDS)
        return live

    def test_stale_and_terminal_and_recovery(self) -> None:
        live = self._running_with_stale_heartbeat()
        first = self.outbox.maybe_enqueue_stale(live)
        second = self.outbox.maybe_enqueue_stale(live)
        self.assertTrue(first.created)
        self.assertFalse(second.created)

        self.registry.update_status(live.run_id, RunStatus.COMPLETED)
        kinds = {
            e["event_kind"] for e in self.outbox.list_for_run(live.run_id)
        }
        self.assertIn("stale", kinds)
        self.assertIn("terminal", kinds)
        self.assertIn("phase_change", kinds)
        # Terminal-while-stale must not invent a paired recovery.
        self.assertNotIn("recovery", kinds)

        # Lease-aware startup recovery (distinct from heartbeat pair).
        # Freshly owned running leases are preserved; no recovery events.
        healthy = self.registry.create_run()
        self.registry.update_status(healthy.run_id, RunStatus.RUNNING)
        recovered_healthy = self.registry.recover_interrupted_runs()
        self.assertEqual(recovered_healthy, 0)
        healthy_kinds = {
            e["event_kind"]
            for e in self.outbox.list_for_run(healthy.run_id)
        }
        self.assertNotIn("recovery", healthy_kinds)
        self.assertNotIn("terminal", healthy_kinds)
        healthy_final = self.registry.get_run(healthy.run_id)
        assert healthy_final is not None
        self.assertEqual(healthy_final.status, RunStatus.RUNNING)
        self.assertIsNone(healthy_final.error)
        self.assertNotEqual(healthy_final.error, INTERRUPTED_RUN_ERROR)

        # Recovery/terminal enqueue only after a real owner-loss CAS.
        interrupted = self.registry.create_run()
        self.registry.update_status(interrupted.run_id, RunStatus.RUNNING)
        stale_at = datetime.now(timezone.utc) - timedelta(
            seconds=EXECUTION_LEASE_GRACE_SECONDS + 5
        )
        with self.registry._lock:
            self.registry._conn.execute(
                "UPDATE runs SET heartbeat_at = ? WHERE run_id = ?",
                (stale_at.isoformat(), interrupted.run_id),
            )
            self.registry._conn.commit()
        recovered = self.registry.recover_interrupted_runs()
        self.assertEqual(recovered, 1)
        rec_kinds = {
            e["event_kind"]
            for e in self.outbox.list_for_run(interrupted.run_id)
        }
        self.assertIn("recovery", rec_kinds)
        self.assertIn("terminal", rec_kinds)
        final = self.registry.get_run(interrupted.run_id)
        assert final is not None
        self.assertEqual(final.status, RunStatus.FAILED)
        self.assertEqual(final.error, OWNER_LOST_RUN_ERROR)
        self.assertNotEqual(final.error, INTERRUPTED_RUN_ERROR)

    def test_production_stale_healthy_recovery_terminal_sequence(self) -> None:
        """Exact production call sequence: observe stale → healthy → terminal."""
        live = self._running_with_stale_heartbeat()
        stale = self.outbox.maybe_enqueue_stale(live)
        self.assertTrue(stale.created)

        self.registry.touch_heartbeat(live.run_id)
        healthy = self.registry.get_run(live.run_id)
        assert healthy is not None
        recovery = self.outbox.maybe_enqueue_stale(healthy)
        self.assertTrue(recovery.created)
        again = self.outbox.maybe_enqueue_stale(healthy)
        self.assertFalse(again.created)
        self.assertEqual(again.skipped_reason, "no_open_stale_episode")

        self.registry.update_status(live.run_id, RunStatus.COMPLETED)
        events = self.outbox.list_for_run(live.run_id)
        kinds = [e["event_kind"] for e in events]
        self.assertEqual(kinds.count("stale"), 1)
        self.assertEqual(kinds.count("recovery"), 1)
        self.assertEqual(kinds.count("terminal"), 1)
        paired = [k for k in kinds if k in {"stale", "recovery", "terminal"}]
        self.assertEqual(paired, ["stale", "recovery", "terminal"])

    def test_restart_between_stale_and_recovery(self) -> None:
        live = self._running_with_stale_heartbeat()
        self.assertTrue(self.outbox.maybe_enqueue_stale(live).created)
        self.outbox.close()

        restarted = NotificationOutbox(
            self._db_path, config=_config(enabled=False)
        )
        try:
            self.registry.touch_heartbeat(live.run_id)
            healthy = self.registry.get_run(live.run_id)
            assert healthy is not None
            recovery = restarted.maybe_enqueue_stale(healthy)
            self.assertTrue(recovery.created)
            events = restarted.list_for_run(live.run_id)
            kinds = [e["event_kind"] for e in events]
            self.assertEqual(kinds.count("stale"), 1)
            self.assertEqual(kinds.count("recovery"), 1)
        finally:
            restarted.close()

    def test_concurrent_waiters_single_recovery(self) -> None:
        live = self._running_with_stale_heartbeat()
        self.assertTrue(self.outbox.maybe_enqueue_stale(live).created)
        self.registry.touch_heartbeat(live.run_id)
        healthy = self.registry.get_run(live.run_id)
        assert healthy is not None

        results: list = []
        errors: list[BaseException] = []

        def _observe() -> None:
            try:
                results.append(self.outbox.maybe_enqueue_stale(healthy))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_observe) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        created = [r for r in results if r.created]
        self.assertEqual(len(created), 1)
        events = self.outbox.list_for_run(live.run_id)
        self.assertEqual(
            sum(1 for e in events if e["event_kind"] == "recovery"), 1
        )

    def test_repeated_stale_checks_single_stale_event(self) -> None:
        live = self._running_with_stale_heartbeat()
        outcomes = [self.outbox.maybe_enqueue_stale(live) for _ in range(5)]
        self.assertTrue(outcomes[0].created)
        self.assertTrue(all(not o.created for o in outcomes[1:]))
        events = self.outbox.list_for_run(live.run_id)
        self.assertEqual(
            sum(1 for e in events if e["event_kind"] == "stale"), 1
        )

    def test_terminal_while_stale_no_false_recovery(self) -> None:
        live = self._running_with_stale_heartbeat()
        self.assertTrue(self.outbox.maybe_enqueue_stale(live).created)
        self.registry.update_status(live.run_id, RunStatus.FAILED)
        terminal = self.registry.get_run(live.run_id)
        assert terminal is not None
        # Observation after terminal must not emit recovery.
        observed = self.outbox.maybe_enqueue_stale(terminal)
        self.assertFalse(observed.created)
        kinds = {
            e["event_kind"] for e in self.outbox.list_for_run(live.run_id)
        }
        self.assertIn("stale", kinds)
        self.assertIn("terminal", kinds)
        self.assertNotIn("recovery", kinds)

    def test_default_threshold_90_and_explicit_override(self) -> None:
        from mission_control.monitoring import HEARTBEAT_STALE_THRESHOLD_SECONDS

        self.assertEqual(HEARTBEAT_STALE_THRESHOLD_SECONDS, 90.0)
        # 60s old: healthy under default 90s (no stale, no open episode).
        live = self._running_with_stale_heartbeat(
            age_seconds=60.0, require_default_stale=False
        )
        skipped = self.outbox.maybe_enqueue_stale(live)
        self.assertFalse(skipped.created)
        self.assertEqual(skipped.skipped_reason, "no_open_stale_episode")
        # Explicit operator override still respected.
        forced = self.outbox.maybe_enqueue_stale(
            live, stale_threshold_seconds=30.0
        )
        self.assertTrue(forced.created)

    def test_invalid_stale_threshold_fails_closed_no_queue_mutation(self) -> None:
        from mission_control.monitoring import validate_stale_threshold_seconds

        live = self._running_with_stale_heartbeat(
            age_seconds=60.0, require_default_stale=False
        )
        before = self.outbox.list_for_run(live.run_id)
        for bad in (0.0, -1.0, float("nan"), float("inf"), float("-inf")):
            with self.subTest(threshold=bad):
                with self.assertRaises(ValueError):
                    validate_stale_threshold_seconds(bad)
                result = self.outbox.maybe_enqueue_stale(
                    live, stale_threshold_seconds=bad
                )
                self.assertFalse(result.created)
                self.assertEqual(
                    result.skipped_reason, "invalid_stale_threshold"
                )
        after = self.outbox.list_for_run(live.run_id)
        self.assertEqual(after, before)

    def test_cross_connection_terminal_wins_no_false_recovery(self) -> None:
        """Separate outbox connections: terminal CAS beats healthy recovery."""
        live = self._running_with_stale_heartbeat()
        self.assertTrue(self.outbox.maybe_enqueue_stale(live).created)

        healthy_box = NotificationOutbox(
            self._db_path, config=_config(enabled=False)
        )
        terminal_box = NotificationOutbox(
            self._db_path, config=_config(enabled=False)
        )
        try:
            self.registry.touch_heartbeat(live.run_id)
            healthy = self.registry.get_run(live.run_id)
            assert healthy is not None
            self.registry.update_status(live.run_id, RunStatus.FAILED)
            terminal = self.registry.get_run(live.run_id)
            assert terminal is not None

            barrier = threading.Barrier(2)
            outcomes: dict[str, object] = {}
            errors: list[BaseException] = []

            def _healthy() -> None:
                try:
                    barrier.wait(timeout=5)
                    outcomes["healthy"] = healthy_box.maybe_enqueue_stale(
                        healthy
                    )
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            def _terminal() -> None:
                try:
                    barrier.wait(timeout=5)
                    outcomes["terminal"] = terminal_box.maybe_enqueue_stale(
                        terminal
                    )
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [
                threading.Thread(target=_healthy),
                threading.Thread(target=_terminal),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(errors, [])

            events = self.outbox.list_for_run(live.run_id)
            kinds = [e["event_kind"] for e in events]
            recovery_count = kinds.count("recovery")
            episode = self.outbox._conn.execute(
                "SELECT state FROM notification_stale_episodes WHERE run_id = ?",
                (live.run_id,),
            ).fetchone()
            assert episode is not None
            state = str(episode["state"])
            if state == "closed_terminal":
                self.assertEqual(recovery_count, 0)
                self.assertFalse(
                    getattr(outcomes["healthy"], "created", True)
                )
            else:
                self.assertEqual(state, "recovered")
                self.assertEqual(recovery_count, 1)
                self.assertTrue(getattr(outcomes["healthy"], "created", False))
            # Never both terminal-closed and a recovery row.
            self.assertFalse(
                state == "closed_terminal" and recovery_count > 0
            )
        finally:
            healthy_box.close()
            terminal_box.close()

    def test_cross_connection_terminal_healthy_stress_no_false_pairing(
        self,
    ) -> None:
        """Adversarial multi-connection terminal×healthy races stay exclusive."""
        iterations = 40
        false_pairings = 0
        duplicate_recoveries = 0
        for i in range(iterations):
            live = self._running_with_stale_heartbeat()
            self.assertTrue(self.outbox.maybe_enqueue_stale(live).created)
            self.registry.touch_heartbeat(live.run_id)
            healthy = self.registry.get_run(live.run_id)
            assert healthy is not None
            self.registry.update_status(live.run_id, RunStatus.COMPLETED)
            terminal = self.registry.get_run(live.run_id)
            assert terminal is not None

            boxes = [
                NotificationOutbox(
                    self._db_path, config=_config(enabled=False)
                )
                for _ in range(4)
            ]
            barrier = threading.Barrier(4)
            errors: list[BaseException] = []

            def _race(box: NotificationOutbox, record, label: str) -> None:
                try:
                    barrier.wait(timeout=5)
                    if label == "healthy":
                        box.maybe_enqueue_stale(record)
                    else:
                        box.maybe_enqueue_terminal(record)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [
                threading.Thread(
                    target=_race, args=(boxes[0], healthy, "healthy")
                ),
                threading.Thread(
                    target=_race, args=(boxes[1], healthy, "healthy")
                ),
                threading.Thread(
                    target=_race, args=(boxes[2], terminal, "terminal")
                ),
                threading.Thread(
                    target=_race, args=(boxes[3], terminal, "terminal")
                ),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(errors, [])

            events = self.outbox.list_for_run(live.run_id)
            kinds = [e["event_kind"] for e in events]
            recovery_count = kinds.count("recovery")
            if recovery_count > 1:
                duplicate_recoveries += 1
            episode = self.outbox._conn.execute(
                "SELECT state FROM notification_stale_episodes WHERE run_id = ?",
                (live.run_id,),
            ).fetchone()
            assert episode is not None
            state = str(episode["state"])
            if state == "closed_terminal" and recovery_count > 0:
                false_pairings += 1
            elif state == "recovered":
                self.assertEqual(recovery_count, 1)
            else:
                self.assertEqual(state, "closed_terminal")
                self.assertEqual(recovery_count, 0)
            for box in boxes:
                box.close()

        self.assertEqual(false_pairings, 0)
        self.assertEqual(duplicate_recoveries, 0)

    def test_cross_connection_repeated_healthy_single_recovery(self) -> None:
        live = self._running_with_stale_heartbeat()
        self.assertTrue(self.outbox.maybe_enqueue_stale(live).created)
        self.registry.touch_heartbeat(live.run_id)
        healthy = self.registry.get_run(live.run_id)
        assert healthy is not None

        boxes = [
            NotificationOutbox(self._db_path, config=_config(enabled=False))
            for _ in range(6)
        ]
        barrier = threading.Barrier(6)
        created = 0
        lock = threading.Lock()
        errors: list[BaseException] = []

        def _observe(box: NotificationOutbox) -> None:
            nonlocal created
            try:
                barrier.wait(timeout=5)
                result = box.maybe_enqueue_stale(healthy)
                if result.created:
                    with lock:
                        created += 1
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=_observe, args=(box,)) for box in boxes
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(created, 1)
        events = self.outbox.list_for_run(live.run_id)
        self.assertEqual(
            sum(1 for e in events if e["event_kind"] == "recovery"), 1
        )
        for box in boxes:
            box.close()

    def test_schema_migration_adds_stale_episode_table(self) -> None:
        """Existing production DBs gain the episode table without failure."""
        legacy_fd, legacy_path = tempfile.mkstemp(suffix=".db")
        os.close(legacy_fd)
        try:
            conn = __import__("sqlite3").connect(legacy_path)
            conn.execute(
                """
                CREATE TABLE notification_outbox (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    delivery_state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    delivered_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (run_id, event_kind, dedupe_key)
                )
                """
            )
            conn.commit()
            conn.close()
            outbox = NotificationOutbox(
                legacy_path, config=_config(enabled=False)
            )
            try:
                rows = outbox._conn.execute(
                    "SELECT name FROM sqlite_master WHERE name = ?",
                    ("notification_stale_episodes",),
                ).fetchall()
                self.assertEqual(len(rows), 1)
            finally:
                outbox.close()
        finally:
            os.unlink(legacy_path)


class TestStaleRecoveryPushoverCounts(unittest.TestCase):
    """Pushover: stale+recovery+terminal == 3; normal mission == 1."""

    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)

    def tearDown(self) -> None:
        self.registry.close()
        os.unlink(self._db_path)

    def test_pushover_stale_recovery_terminal_exactly_three(self) -> None:
        from mission_control.notifications import (
            PUSHOVER_API_URL,
            PUSHOVER_APP_TOKEN_ENV,
            PUSHOVER_USER_KEY_ENV,
            load_notification_config,
        )

        posts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            posts.append(request.url.path)
            return httpx.Response(200, json={"status": 1})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, follow_redirects=False)
        environ = {
            PUSHOVER_USER_KEY_ENV: "pushover-user-key-fixture-aaaa",
            PUSHOVER_APP_TOKEN_ENV: "pushover-app-token-fixture-bbbb",
        }
        cfg = load_notification_config(environ=environ)
        outbox = NotificationOutbox(
            self._db_path, config=cfg, http_client=client
        )
        try:
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
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=PUSHOVER_API_URL,
            ):
                self.assertTrue(outbox.maybe_enqueue_stale(live).created)
                outbox.process_due_deliveries(limit=32)
                self.registry.touch_heartbeat(record.run_id)
                healthy = self.registry.get_run(record.run_id)
                assert healthy is not None
                self.assertTrue(outbox.maybe_enqueue_stale(healthy).created)
                outbox.process_due_deliveries(limit=32)
                self.registry.update_status(record.run_id, RunStatus.COMPLETED)
                outbox.process_due_deliveries(limit=32)
            self.assertEqual(len(posts), 3)
            kinds = [
                e["event_kind"]
                for e in outbox.list_for_run(record.run_id)
                if e["event_kind"] in {"stale", "recovery", "terminal"}
            ]
            self.assertEqual(sorted(kinds), ["recovery", "stale", "terminal"])
            for event in outbox.list_for_run(record.run_id):
                if event["event_kind"] in {"stale", "recovery", "terminal"}:
                    self.assertEqual(event["delivery_state"], "delivered")
                if event["event_kind"] == "phase_change":
                    self.assertEqual(event["delivery_state"], "skipped")
        finally:
            outbox.close()
            client.close()

    def test_pushover_normal_mission_exactly_one_terminal(self) -> None:
        from mission_control.notifications import (
            PUSHOVER_API_URL,
            PUSHOVER_APP_TOKEN_ENV,
            PUSHOVER_USER_KEY_ENV,
            load_notification_config,
        )

        posts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            posts.append("http")
            return httpx.Response(200, json={"status": 1})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, follow_redirects=False)
        environ = {
            PUSHOVER_USER_KEY_ENV: "pushover-user-key-fixture-aaaa",
            PUSHOVER_APP_TOKEN_ENV: "pushover-app-token-fixture-bbbb",
        }
        cfg = load_notification_config(environ=environ)
        outbox = NotificationOutbox(
            self._db_path, config=cfg, http_client=client
        )
        try:
            record = self.registry.create_run()
            self.registry.update_status(record.run_id, RunStatus.RUNNING)
            self.registry.set_phase(record.run_id, RunPhase.AGENT_EXECUTION)
            self.registry.update_status(record.run_id, RunStatus.COMPLETED)
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=PUSHOVER_API_URL,
            ):
                outbox.process_due_deliveries(limit=32)
            self.assertEqual(len(posts), 1)
            events = outbox.list_for_run(record.run_id)
            terminals = [e for e in events if e["event_kind"] == "terminal"]
            self.assertEqual(len(terminals), 1)
            self.assertEqual(terminals[0]["delivery_state"], "delivered")
            for event in events:
                if event["event_kind"] == "phase_change":
                    self.assertEqual(event["delivery_state"], "skipped")
        finally:
            outbox.close()
            client.close()


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
            validate_webhook_url("http://localhost/hook", allow_http=True)
        with self.assertRaises(ValueError):
            validate_webhook_url("https://127.0.0.1/hook")
        with self.assertRaises(ValueError):
            validate_webhook_url("http://example.com/hook", allow_http=False)


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


class TestClaimLeaseRecovery(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)
        self.secret = "test-webhook-secret-value"
        self.calls: list[httpx.Request] = []

    def tearDown(self) -> None:
        self.registry.close()
        os.unlink(self._db_path)

    def test_crash_after_claim_reclaims_stale_lease(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            return httpx.Response(204)

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, timeout=2.0)
        outbox = NotificationOutbox(
            self._db_path,
            config=_config(secret=self.secret, claim_lease_seconds=0.05),
            http_client=client,
        )
        try:
            record = self.registry.create_run()
            result = outbox.enqueue_for_record(
                record,
                event_kind=NotificationEventKind.PHASE_CHANGE,
                dedupe_key="phase:queued:crash",
            )
            self.assertTrue(result.created)
            event_id = result.event_id
            assert event_id is not None
            # Simulate crash after claim: in_flight with expired lease.
            past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
            with outbox._lock:
                outbox._conn.execute(
                    "UPDATE notification_outbox SET delivery_state = ?, "
                    "claim_owner = ?, claim_expires_at = ? WHERE event_id = ?",
                    ("in_flight", "dead-worker", past, event_id),
                )
                outbox._conn.commit()
            reclaimed = outbox.reclaim_stale_claims()
            self.assertEqual(reclaimed, 1)
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=_PUBLIC_WEBHOOK,
            ):
                outbox.process_due_deliveries()
            events = outbox.list_for_run(record.run_id)
            self.assertEqual(events[0]["delivery_state"], "delivered")
            self.assertEqual(len(self.calls), 1)
        finally:
            outbox.close()
            client.close()

    def test_active_lease_not_prematurely_reclaimed(self) -> None:
        outbox = NotificationOutbox(
            self._db_path,
            config=_config(enabled=False, claim_lease_seconds=60.0),
        )
        try:
            record = self.registry.create_run()
            result = outbox.enqueue_for_record(
                record,
                event_kind=NotificationEventKind.PHASE_CHANGE,
                dedupe_key="phase:queued:active-lease",
            )
            event_id = result.event_id
            assert event_id is not None
            future = (
                datetime.now(timezone.utc) + timedelta(seconds=120)
            ).isoformat()
            with outbox._lock:
                outbox._conn.execute(
                    "UPDATE notification_outbox SET delivery_state = ?, "
                    "claim_owner = ?, claim_expires_at = ? WHERE event_id = ?",
                    ("in_flight", "live-worker", future, event_id),
                )
                outbox._conn.commit()
            self.assertEqual(outbox.reclaim_stale_claims(), 0)
            row = outbox.get_event(event_id)
            assert row is not None
            self.assertEqual(row["delivery_state"], "in_flight")
            # Concurrent claim must not steal the active lease.
            claimed = outbox._claim_due_events(limit=8)
            self.assertEqual(claimed, [])
            row = outbox.get_event(event_id)
            assert row is not None
            self.assertEqual(row["delivery_state"], "in_flight")
        finally:
            outbox.close()


class TestAsyncDeliveryAndLatency(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)
        self.outbox = self.registry._get_notification_outbox()
        self.calls: list[httpx.Request] = []
        self.secret = "test-webhook-secret-value"

        def handler(request: httpx.Request) -> httpx.Response:
            # Simulate slow webhook; request paths must not await this.
            time.sleep(0.35)
            self.calls.append(request)
            return httpx.Response(204)

        self.client_http = httpx.Client(
            transport=httpx.MockTransport(handler), timeout=2.0
        )
        self.outbox._config = _config(secret=self.secret, worker_poll_seconds=0.05)
        self.outbox._http_client = self.client_http
        self.worker = NotificationDeliveryWorker(
            self.outbox, poll_seconds=0.05
        )
        self._prev_registry = api_module.run_registry
        self._prev_outbox = api_module.notification_outbox
        self._prev_worker = api_module.notification_delivery_worker
        api_module.run_registry = self.registry
        api_module.notification_outbox = self.outbox
        api_module.notification_delivery_worker = self.worker
        self.worker.start()
        self.api_client = TestClient(app, raise_server_exceptions=True)

    def tearDown(self) -> None:
        self.api_client.close()
        self.worker.stop()
        api_module.run_registry = self._prev_registry
        api_module.notification_outbox = self._prev_outbox
        api_module.notification_delivery_worker = self._prev_worker
        self.client_http.close()
        self.registry.close()
        os.unlink(self._db_path)

    def test_webhook_latency_never_delays_wait_status_terminal(self) -> None:
        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        # Enqueue a due notification that the worker will deliver slowly.
        with patch(
            "mission_control.notifications.validate_webhook_url",
            return_value=_PUBLIC_WEBHOOK,
        ):
            self.outbox.enqueue_for_record(
                record,
                event_kind=NotificationEventKind.PHASE_CHANGE,
                dedupe_key="phase:agent_execution:latency",
            )
            started = time.monotonic()
            status_resp = self.api_client.get(
                f"/runs/{record.run_id}",
                headers=AUTH_HEADERS,
            )
            wait_resp = self.api_client.post(
                f"/runs/{record.run_id}/wait",
                headers=AUTH_HEADERS,
                json={"timeout_seconds": 0.15, "poll_interval_seconds": 0.05},
            )
            self.registry.update_status(record.run_id, RunStatus.COMPLETED)
            terminal_resp = self.api_client.get(
                f"/runs/{record.run_id}",
                headers=AUTH_HEADERS,
            )
            elapsed = time.monotonic() - started
        self.assertEqual(status_resp.status_code, 200)
        self.assertEqual(wait_resp.status_code, 200)
        self.assertEqual(terminal_resp.status_code, 200)
        self.assertEqual(terminal_resp.json()["status"], "completed")
        # Synchronous HTTP sleep is 0.35s; critical paths must stay well under.
        self.assertLess(elapsed, 0.3)

    def test_enqueue_wakes_delivery_without_waiter(self) -> None:
        record = self.registry.create_run()
        with patch(
            "mission_control.notifications.validate_webhook_url",
            return_value=_PUBLIC_WEBHOOK,
        ):
            self.outbox.enqueue_for_record(
                record,
                event_kind=NotificationEventKind.PHASE_CHANGE,
                dedupe_key="phase:queued:wake",
            )
            deadline = time.time() + 2.0
            while time.time() < deadline and not self.calls:
                time.sleep(0.02)
        self.assertEqual(len(self.calls), 1)
        events = self.outbox.list_for_run(record.run_id)
        self.assertEqual(events[0]["delivery_state"], "delivered")

    def test_worker_restart_continues_retries(self) -> None:
        statuses = [500, 204]

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            code = statuses.pop(0) if statuses else 204
            return httpx.Response(code)

        self.outbox._http_client = httpx.Client(
            transport=httpx.MockTransport(handler), timeout=2.0
        )
        self.outbox._config = _config(
            secret=self.secret,
            max_attempts=3,
            backoff_base_seconds=0.01,
            backoff_max_seconds=0.05,
            worker_poll_seconds=0.05,
        )
        record = self.registry.create_run()
        with patch(
            "mission_control.notifications.validate_webhook_url",
            return_value=_PUBLIC_WEBHOOK,
        ):
            self.outbox.enqueue_for_record(
                record,
                event_kind=NotificationEventKind.PHASE_CHANGE,
                dedupe_key="phase:queued:restart",
            )
            deadline = time.time() + 2.0
            while time.time() < deadline and len(self.calls) < 1:
                time.sleep(0.02)
            self.assertGreaterEqual(len(self.calls), 1)
            self.worker.stop()
            # Force retry due immediately, then restart worker.
            with self.outbox._lock:
                self.outbox._conn.execute(
                    "UPDATE notification_outbox SET next_attempt_at = NULL "
                    "WHERE delivery_state = 'pending'"
                )
                self.outbox._conn.commit()
            self.worker = NotificationDeliveryWorker(
                self.outbox, poll_seconds=0.05
            )
            api_module.notification_delivery_worker = self.worker
            self.worker.start()
            deadline = time.time() + 2.0
            while time.time() < deadline:
                events = self.outbox.list_for_run(record.run_id)
                if events and events[0]["delivery_state"] == "delivered":
                    break
                time.sleep(0.02)
        events = self.outbox.list_for_run(record.run_id)
        self.assertEqual(events[0]["delivery_state"], "delivered")
        self.assertGreaterEqual(len(self.calls), 2)

    def test_no_duplicate_delivery_under_concurrent_kicks(self) -> None:
        gate = threading.Event()
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            gate.wait(timeout=1.0)
            return httpx.Response(204)

        self.outbox._http_client = httpx.Client(
            transport=httpx.MockTransport(handler), timeout=2.0
        )
        record = self.registry.create_run()
        with patch(
            "mission_control.notifications.validate_webhook_url",
            return_value=_PUBLIC_WEBHOOK,
        ):
            self.outbox.enqueue_for_record(
                record,
                event_kind=NotificationEventKind.PHASE_CHANGE,
                dedupe_key="phase:queued:dup",
            )
            # Concurrent kicks / drain attempts while first delivery holds claim.
            threads = [
                threading.Thread(target=self.worker.kick) for _ in range(12)
            ]
            for thread in threads:
                thread.start()
            deadline = time.time() + 2.0
            while time.time() < deadline and not seen:
                time.sleep(0.01)
            # Extra direct drains must not double-deliver under active lease.
            extras = [
                threading.Thread(target=self.outbox.process_due_deliveries)
                for _ in range(4)
            ]
            for thread in extras:
                thread.start()
            time.sleep(0.05)
            gate.set()
            for thread in threads + extras:
                thread.join(timeout=2.0)
            deadline = time.time() + 2.0
            while time.time() < deadline:
                events = self.outbox.list_for_run(record.run_id)
                if events and events[0]["delivery_state"] == "delivered":
                    break
                time.sleep(0.02)
        self.assertEqual(len(seen), 1)
        events = self.outbox.list_for_run(record.run_id)
        self.assertEqual(events[0]["delivery_state"], "delivered")


class TestLifecycleUnderDelayedEnqueue(unittest.TestCase):
    def test_lifecycle_logs_before_delayed_notification_enqueue(self) -> None:
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        registry = RunRegistry(db_path)
        try:
            record = registry.create_run()
            release = threading.Event()
            started = threading.Event()

            def slow_enqueue(*_args, **_kwargs):
                started.set()
                release.wait(timeout=2.0)
                return type("R", (), {"created": False, "event_id": None})()

            with patch.object(
                registry._get_notification_outbox(),
                "maybe_enqueue_terminal",
                side_effect=slow_enqueue,
            ):
                with self.assertLogs(
                    "mission_control.run_registry", level="INFO"
                ) as captured:
                    thread = threading.Thread(
                        target=registry.update_status,
                        args=(record.run_id, RunStatus.COMPLETED),
                    )
                    thread.start()
                    self.assertTrue(started.wait(timeout=2.0))
                    # While enqueue is delayed, lifecycle must already be logged.
                    text = "\n".join(r.getMessage() for r in captured.records)
                    self.assertIn("event=final_status_update", text)
                    self.assertIn("event=finished", text)
                    release.set()
                    thread.join(timeout=2.0)
            live = registry.get_run(record.run_id)
            assert live is not None
            self.assertEqual(live.status, RunStatus.COMPLETED)
        finally:
            registry.close()
            os.unlink(db_path)


class TestSsrfTransport(unittest.TestCase):
    def test_https_only_without_override(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_webhook_url(
                "http://example.com/hook", allow_http=False
            )
        self.assertIn("https", str(ctx.exception).lower())

    def test_rejects_private_link_local_and_loopback(self) -> None:
        cases = [
            "https://127.0.0.1/hook",
            "https://10.0.0.5/hook",
            "https://169.254.169.254/hook",
            "https://[::1]/hook",
        ]
        for url in cases:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_webhook_url(url, allow_http=False)

    def test_dns_rebinding_uses_pinned_ip_not_second_lookup(self) -> None:
        resolutions = {
            "webhook.test": [
                (socket.AF_INET, ("1.2.3.4", 443)),
            ]
        }
        lookup_count = {"n": 0}

        def fake_getaddrinfo(host, port, *args, **kwargs):
            lookup_count["n"] += 1
            family_port = resolutions.get(str(host))
            if not family_port:
                raise socket.gaierror("not found")
            out = []
            for family, sockaddr in family_port:
                out.append((family, socket.SOCK_STREAM, 0, "", sockaddr))
            return out

        seen_urls: list[str] = []

        class Recorder:
            def __init__(self):
                self.timeout = 2.0
                self.follow_redirects = False

            def build_request(self, method, url, **kwargs):
                kwargs.pop("timeout", None)
                seen_urls.append(str(url))
                return httpx.Request(method, url, **kwargs)

            def send(self, request, follow_redirects=False):
                self.last_follow = follow_redirects
                return httpx.Response(204, request=request)

            def close(self):
                return None

        recorder = Recorder()
        with patch(
            "mission_control.notifications.socket.getaddrinfo",
            side_effect=fake_getaddrinfo,
        ):
            # First validate+pin resolves once; post_webhook must not reconnect
            # via a hostname that could rebind on a second lookup.
            response = post_webhook_ssrf_safe(
                "https://webhook.test/hooks",
                content=b"{}",
                headers={"Content-Type": "application/json"},
                timeout_seconds=2.0,
                allow_http=False,
                client=recorder,  # type: ignore[arg-type]
            )
        self.assertEqual(response.status_code, 204)
        self.assertTrue(seen_urls)
        self.assertIn("1.2.3.4", seen_urls[0])
        self.assertNotIn("webhook.test", urlparse(seen_urls[0]).netloc)
        self.assertFalse(getattr(recorder, "last_follow", True))
        # validate + pin share resolve_webhook_ip_targets (2 calls max).
        self.assertLessEqual(lookup_count["n"], 2)

    def test_redirects_disabled_on_injected_client(self) -> None:
        redirected = {"followed": False}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/redirect"):
                return httpx.Response(
                    302, headers={"Location": "https://127.0.0.1/private"}
                )
            redirected["followed"] = True
            return httpx.Response(204)

        client = httpx.Client(
            transport=httpx.MockTransport(handler),
            timeout=2.0,
            follow_redirects=False,
        )
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        try:
            outbox = NotificationOutbox(
                db_path,
                config=_config(
                    url="https://example.com/redirect",
                    secret="test-webhook-secret-value",
                ),
                http_client=client,
            )
            registry = RunRegistry(db_path)
            record = registry.create_run()
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value="https://example.com/redirect",
            ):
                outbox.enqueue_for_record(
                    record,
                    event_kind=NotificationEventKind.PHASE_CHANGE,
                    dedupe_key="phase:queued:redir",
                )
                outbox.process_due_deliveries()
            events = outbox.list_for_run(record.run_id)
            # 302 is a failed attempt, not a followed redirect to loopback.
            self.assertFalse(redirected["followed"])
            self.assertIn(events[0]["delivery_state"], {"pending", "dead"})
            outbox.close()
            registry.close()
        finally:
            client.close()
            os.unlink(db_path)


class TestWorkerLifecycle(unittest.TestCase):
    def test_worker_start_stop_and_no_duplicate_threads(self) -> None:
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        outbox = NotificationOutbox(db_path, config=_config(enabled=False))
        worker = NotificationDeliveryWorker(outbox, poll_seconds=0.05)
        try:
            self.assertTrue(worker.start())
            self.assertTrue(worker.is_running)
            self.assertFalse(worker.start())  # idempotent; no second thread
            worker.stop()
            self.assertFalse(worker.is_running)
        finally:
            worker.stop()
            outbox.close()
            os.unlink(db_path)


if __name__ == "__main__":
    unittest.main()
