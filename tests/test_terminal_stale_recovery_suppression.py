"""Regression: suppress heartbeat stale/recovery once the run is terminal."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx

from mission_control.notifications import (
    STALE_RECOVERY_RUN_STATUS_UNAVAILABLE,
    STALE_RECOVERY_TERMINAL_RUN_SUPPRESSED,
    DeliveryState,
    NotificationConfig,
    NotificationEventKind,
    NotificationOutbox,
    _format_dt,
    is_heartbeat_stale_or_paired_recovery,
)
from mission_control.run_registry import (
    RunPhase,
    RunRegistry,
    RunStatus,
    is_terminal_status,
)

_PUBLIC_WEBHOOK = "https://example.com/hooks/mission-control"


def _config(
    *,
    enabled: bool = True,
    url: str | None = _PUBLIC_WEBHOOK,
    secret: str | None = "test-webhook-secret-value",
) -> NotificationConfig:
    return NotificationConfig(
        enabled=enabled,
        webhook_url=url,
        timeout_seconds=2.0,
        max_attempts=3,
        backoff_base_seconds=0.01,
        backoff_max_seconds=1.0,
        claim_lease_seconds=30.0,
        allow_http=True,
        worker_poll_seconds=0.05,
        _secret=secret,
    )


def _insert_row(
    outbox: NotificationOutbox,
    *,
    event_id: str,
    run_id: str,
    event_kind: str,
    delivery_state: str,
    dedupe_key: str,
    claim_owner: str | None = None,
    claim_expires_at: str | None = None,
    next_attempt_at: str | None = None,
    last_error: str | None = None,
    attempt_count: int = 0,
) -> None:
    now_s = _format_dt(datetime.now(timezone.utc))
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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                run_id,
                event_kind,
                dedupe_key,
                json.dumps(body, separators=(",", ":"), sort_keys=True),
                delivery_state,
                attempt_count,
                next_attempt_at if next_attempt_at is not None else now_s,
                last_error,
                None,
                now_s,
                now_s,
                claim_owner,
                claim_expires_at,
            ),
        )
        outbox._conn.commit()


def _raw(outbox: NotificationOutbox, event_id: str):
    with outbox._lock:
        row = outbox._conn.execute(
            "SELECT * FROM notification_outbox WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    assert row is not None
    return row


class TestHeartbeatStaleRecoveryClassifier(unittest.TestCase):
    def test_classifier_distinguishes_startup_recovery(self) -> None:
        self.assertTrue(is_heartbeat_stale_or_paired_recovery("stale", "stale:x"))
        self.assertTrue(
            is_heartbeat_stale_or_paired_recovery(
                "recovery", "recovery:stale:episode-1"
            )
        )
        self.assertFalse(
            is_heartbeat_stale_or_paired_recovery(
                "recovery", "recovery:2026-08-14T16:00:00+00:00"
            )
        )
        self.assertFalse(is_heartbeat_stale_or_paired_recovery("terminal", "t"))
        self.assertTrue(is_terminal_status(RunStatus.FAILED))


class TestTerminalStaleRecoverySuppression(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)
        self.outbox = NotificationOutbox(self._db_path, config=_config())

    def tearDown(self) -> None:
        self.outbox.close()
        self.registry.close()
        os.unlink(self._db_path)

    def _running_run(self, *, with_phase: bool = False):
        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        if with_phase:
            self.registry.set_phase(record.run_id, RunPhase.AGENT_EXECUTION)
        live = self.registry.get_run(record.run_id)
        assert live is not None
        return live

    def _mark_run_terminal_without_notifications(
        self, run_id: str, status: RunStatus = RunStatus.COMPLETED
    ) -> None:
        """Flip run status without enqueue/suppress side effects."""
        now_s = _format_dt(datetime.now(timezone.utc))
        assert now_s is not None
        with self.registry._lock:
            self.registry._conn.execute(
                """
                UPDATE runs
                SET status = ?, completed_at = ?, phase = ?
                WHERE run_id = ?
                """,
                (status.value, now_s, status.value, run_id),
            )
            self.registry._conn.commit()

    def test_suppresses_pending_stale_and_paired_recovery(self) -> None:
        live = self._running_run()
        future = _format_dt(datetime.now(timezone.utc) + timedelta(minutes=5))
        _insert_row(
            self.outbox,
            event_id="stale-pending",
            run_id=live.run_id,
            event_kind=NotificationEventKind.STALE.value,
            delivery_state=DeliveryState.PENDING.value,
            dedupe_key="stale:hb-1",
            next_attempt_at=future,
        )
        _insert_row(
            self.outbox,
            event_id="recovery-pending",
            run_id=live.run_id,
            event_kind=NotificationEventKind.RECOVERY.value,
            delivery_state=DeliveryState.PENDING.value,
            dedupe_key="recovery:stale:ep-1",
            next_attempt_at=future,
        )
        self._mark_run_terminal_without_notifications(live.run_id)
        affected = self.outbox.suppress_stale_recovery_for_terminal_run(
            live.run_id
        )
        self.assertEqual(affected, 2)
        for event_id in ("stale-pending", "recovery-pending"):
            row = _raw(self.outbox, event_id)
            self.assertEqual(row["delivery_state"], DeliveryState.SKIPPED.value)
            self.assertEqual(
                row["last_error"], STALE_RECOVERY_TERMINAL_RUN_SUPPRESSED
            )
            self.assertIsNone(row["claim_owner"])
            self.assertIsNone(row["next_attempt_at"])
            self.assertIsNotNone(row["payload_json"])

    def test_suppresses_in_flight_rows(self) -> None:
        live = self._running_run()
        future = _format_dt(datetime.now(timezone.utc) + timedelta(minutes=5))
        _insert_row(
            self.outbox,
            event_id="stale-inflight",
            run_id=live.run_id,
            event_kind=NotificationEventKind.STALE.value,
            delivery_state=DeliveryState.IN_FLIGHT.value,
            dedupe_key="stale:hb-2",
            claim_owner="worker-a",
            claim_expires_at=future,
            next_attempt_at=future,
        )
        self._mark_run_terminal_without_notifications(
            live.run_id, RunStatus.FAILED
        )
        affected = self.outbox.suppress_stale_recovery_for_terminal_run(
            live.run_id
        )
        self.assertEqual(affected, 1)
        row = _raw(self.outbox, "stale-inflight")
        self.assertEqual(row["delivery_state"], DeliveryState.SKIPPED.value)
        self.assertIsNone(row["claim_owner"])
        self.assertIsNone(row["claim_expires_at"])
        self.assertIsNone(row["next_attempt_at"])

    def test_ordering_proactive_suppress_wins_before_finalize(self) -> None:
        """Ordering 1: terminal suppress commits before delivery finalization."""
        live = self._running_run()
        posts = {"n": 0}
        entered = threading.Event()
        release = threading.Event()

        def handler(request: httpx.Request) -> httpx.Response:
            posts["n"] += 1
            entered.set()
            self.assertTrue(release.wait(timeout=5), "release timed out")
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, follow_redirects=False)
        outbox = NotificationOutbox(
            self._db_path, config=_config(), http_client=client
        )
        try:
            outbox.enqueue_for_record(
                live,
                event_kind=NotificationEventKind.STALE,
                dedupe_key="stale:race-suppress-first",
            )
            errors: list[BaseException] = []

            def _drain() -> None:
                try:
                    with patch(
                        "mission_control.notifications.validate_webhook_url",
                        return_value=_PUBLIC_WEBHOOK,
                    ):
                        outbox.process_due_deliveries(limit=8)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            worker = threading.Thread(target=_drain)
            worker.start()
            self.assertTrue(entered.wait(timeout=5))
            self._mark_run_terminal_without_notifications(live.run_id)
            # Proactive suppress while HTTP is in flight.
            outbox.suppress_stale_recovery_for_terminal_run(live.run_id)
            done = self.registry.get_run(live.run_id)
            assert done is not None
            outbox.enqueue_for_record(
                done,
                event_kind=NotificationEventKind.TERMINAL,
                dedupe_key="terminal:race-suppress-first",
            )
            release.set()
            worker.join(timeout=10)
            self.assertFalse(errors)
            self.assertEqual(posts["n"], 1)
            events = {
                e["event_kind"]: e for e in outbox.list_for_run(live.run_id)
            }
            self.assertEqual(
                events["stale"]["delivery_state"], DeliveryState.SKIPPED.value
            )
            self.assertEqual(
                events["stale"]["last_error"],
                STALE_RECOVERY_TERMINAL_RUN_SUPPRESSED,
            )
            self.assertEqual(
                events["terminal"]["delivery_state"],
                DeliveryState.PENDING.value,
            )
        finally:
            release.set()
            outbox.close()
            client.close()

    def test_ordering_terminal_after_check_before_finalize(self) -> None:
        """Ordering 2: terminal commits after claim check, before finalize.

        HTTP may already have left the wire (unrecallable); durable state must
        still be skipped, never delivered.
        """
        live = self._running_run()
        posts = {"n": 0}
        entered = threading.Event()
        release = threading.Event()

        def handler(request: httpx.Request) -> httpx.Response:
            posts["n"] += 1
            entered.set()
            self.assertTrue(release.wait(timeout=5), "release timed out")
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, follow_redirects=False)
        outbox = NotificationOutbox(
            self._db_path, config=_config(), http_client=client
        )
        try:
            outbox.enqueue_for_record(
                live,
                event_kind=NotificationEventKind.STALE,
                dedupe_key="stale:race-finalize",
            )
            errors: list[BaseException] = []

            def _drain() -> None:
                try:
                    with patch(
                        "mission_control.notifications.validate_webhook_url",
                        return_value=_PUBLIC_WEBHOOK,
                    ):
                        outbox.process_due_deliveries(limit=8)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            worker = threading.Thread(target=_drain)
            worker.start()
            self.assertTrue(entered.wait(timeout=5))
            # Terminal after pre-send check / during HTTP; no proactive suppress.
            self._mark_run_terminal_without_notifications(live.run_id)
            release.set()
            worker.join(timeout=10)
            self.assertFalse(errors)
            self.assertEqual(posts["n"], 1)
            stale = next(
                e
                for e in outbox.list_for_run(live.run_id)
                if e["event_kind"] == "stale"
            )
            self.assertEqual(stale["delivery_state"], DeliveryState.SKIPPED.value)
            self.assertEqual(
                stale["last_error"], STALE_RECOVERY_TERMINAL_RUN_SUPPRESSED
            )
            self.assertNotEqual(
                stale["delivery_state"], DeliveryState.DELIVERED.value
            )
        finally:
            release.set()
            outbox.close()
            client.close()

    def test_retry_and_dead_letter_respect_terminal_suppression(self) -> None:
        live = self._running_run()
        future = _format_dt(datetime.now(timezone.utc) + timedelta(minutes=5))
        _insert_row(
            self.outbox,
            event_id="stale-retry",
            run_id=live.run_id,
            event_kind=NotificationEventKind.STALE.value,
            delivery_state=DeliveryState.IN_FLIGHT.value,
            dedupe_key="stale:retry",
            claim_owner="worker-a",
            claim_expires_at=future,
            attempt_count=1,
        )
        _insert_row(
            self.outbox,
            event_id="stale-dead",
            run_id=live.run_id,
            event_kind=NotificationEventKind.STALE.value,
            delivery_state=DeliveryState.IN_FLIGHT.value,
            dedupe_key="stale:dead",
            claim_owner="worker-a",
            claim_expires_at=future,
            attempt_count=2,
        )
        self._mark_run_terminal_without_notifications(
            live.run_id, RunStatus.TIMED_OUT
        )
        retry_row = _raw(self.outbox, "stale-retry")
        dead_row = _raw(self.outbox, "stale-dead")
        retry_outcome = self.outbox._finalize_failed_delivery(
            retry_row,
            attempt_count=2,
            error="http_status_500",
            max_attempts=3,
            backoff_base_seconds=0.01,
            backoff_max_seconds=1.0,
        )
        dead_outcome = self.outbox._finalize_failed_delivery(
            dead_row,
            attempt_count=3,
            error="http_status_500",
            max_attempts=3,
            backoff_base_seconds=0.01,
            backoff_max_seconds=1.0,
        )
        self.assertEqual(retry_outcome, "skipped")
        self.assertEqual(dead_outcome, "skipped")
        for event_id in ("stale-retry", "stale-dead"):
            row = _raw(self.outbox, event_id)
            self.assertEqual(row["delivery_state"], DeliveryState.SKIPPED.value)
            self.assertEqual(
                row["last_error"], STALE_RECOVERY_TERMINAL_RUN_SUPPRESSED
            )
            self.assertIsNone(row["next_attempt_at"])
            self.assertNotEqual(row["delivery_state"], DeliveryState.PENDING.value)
            self.assertNotEqual(row["delivery_state"], DeliveryState.DEAD.value)

    def test_skipped_not_overwritten_by_delivered_or_retry(self) -> None:
        live = self._running_run()
        _insert_row(
            self.outbox,
            event_id="stale-skipped",
            run_id=live.run_id,
            event_kind=NotificationEventKind.STALE.value,
            delivery_state=DeliveryState.SKIPPED.value,
            dedupe_key="stale:already-skipped",
            last_error=STALE_RECOVERY_TERMINAL_RUN_SUPPRESSED,
            next_attempt_at=None,
        )
        self._mark_run_terminal_without_notifications(live.run_id)
        row = _raw(self.outbox, "stale-skipped")
        now_s = _format_dt(datetime.now(timezone.utc))
        assert now_s is not None
        deliver_outcome = self.outbox._finalize_terminal_dependent_outbox_row(
            row,
            active_delivery_state=DeliveryState.DELIVERED.value,
            next_attempt_at=None,
            delivered_at=now_s,
            clear_error=True,
        )
        retry_outcome = self.outbox._finalize_failed_delivery(
            row,
            attempt_count=1,
            error="http_status_500",
            max_attempts=3,
            backoff_base_seconds=0.01,
            backoff_max_seconds=1.0,
        )
        self.assertEqual(deliver_outcome, "cas_missed")
        self.assertEqual(retry_outcome, "cas_missed")
        final = _raw(self.outbox, "stale-skipped")
        self.assertEqual(final["delivery_state"], DeliveryState.SKIPPED.value)
        self.assertEqual(
            final["last_error"], STALE_RECOVERY_TERMINAL_RUN_SUPPRESSED
        )

    def test_missing_runs_table_fail_closed(self) -> None:
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        outbox = NotificationOutbox(db_path, config=_config())
        try:
            future = _format_dt(
                datetime.now(timezone.utc) + timedelta(minutes=5)
            )
            _insert_row(
                outbox,
                event_id="stale-no-runs",
                run_id="missing-run",
                event_kind=NotificationEventKind.STALE.value,
                delivery_state=DeliveryState.IN_FLIGHT.value,
                dedupe_key="stale:no-runs",
                claim_owner="worker-a",
                claim_expires_at=future,
            )
            row = _raw(outbox, "stale-no-runs")
            now_s = _format_dt(datetime.now(timezone.utc))
            assert now_s is not None
            outcome = outbox._finalize_terminal_dependent_outbox_row(
                row,
                active_delivery_state=DeliveryState.DELIVERED.value,
                next_attempt_at=None,
                delivered_at=now_s,
                clear_error=True,
            )
            self.assertEqual(outcome, "skipped")
            final = _raw(outbox, "stale-no-runs")
            self.assertEqual(final["delivery_state"], DeliveryState.SKIPPED.value)
            self.assertEqual(
                final["last_error"], STALE_RECOVERY_RUN_STATUS_UNAVAILABLE
            )
            self.assertIsNone(final["next_attempt_at"])
        finally:
            outbox.close()
            os.unlink(db_path)

    def test_missing_run_row_fail_closed(self) -> None:
        future = _format_dt(datetime.now(timezone.utc) + timedelta(minutes=5))
        _insert_row(
            self.outbox,
            event_id="stale-orphan",
            run_id="no-such-run-id",
            event_kind=NotificationEventKind.RECOVERY.value,
            delivery_state=DeliveryState.IN_FLIGHT.value,
            dedupe_key="recovery:stale:orphan",
            claim_owner="worker-a",
            claim_expires_at=future,
        )
        row = _raw(self.outbox, "stale-orphan")
        outcome = self.outbox._finalize_failed_delivery(
            row,
            attempt_count=3,
            error="boom",
            max_attempts=3,
            backoff_base_seconds=0.01,
            backoff_max_seconds=1.0,
        )
        self.assertEqual(outcome, "skipped")
        final = _raw(self.outbox, "stale-orphan")
        self.assertEqual(final["delivery_state"], DeliveryState.SKIPPED.value)
        self.assertEqual(
            final["last_error"], STALE_RECOVERY_RUN_STATUS_UNAVAILABLE
        )
        self.assertNotEqual(final["delivery_state"], DeliveryState.DEAD.value)

    def test_active_run_stale_and_recovery_still_deliver(self) -> None:
        live = self._running_run()
        posts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            posts.append(request.url.path)
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, follow_redirects=False)
        outbox = NotificationOutbox(
            self._db_path, config=_config(), http_client=client
        )
        try:
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=_PUBLIC_WEBHOOK,
            ):
                outbox.enqueue_for_record(
                    live,
                    event_kind=NotificationEventKind.STALE,
                    dedupe_key="stale:active",
                )
                outbox.enqueue_for_record(
                    live,
                    event_kind=NotificationEventKind.RECOVERY,
                    dedupe_key="recovery:stale:active-ep",
                )
                outbox.process_due_deliveries(limit=8)
            self.assertEqual(len(posts), 2)
            by_kind = {
                e["event_kind"]: e for e in outbox.list_for_run(live.run_id)
            }
            self.assertEqual(
                by_kind["stale"]["delivery_state"], DeliveryState.DELIVERED.value
            )
            self.assertEqual(
                by_kind["recovery"]["delivery_state"],
                DeliveryState.DELIVERED.value,
            )
        finally:
            outbox.close()
            client.close()

    def test_unrelated_types_and_startup_recovery_preserved(self) -> None:
        live = self._running_run()
        self._mark_run_terminal_without_notifications(
            live.run_id, RunStatus.TIMED_OUT
        )
        done = self.registry.get_run(live.run_id)
        assert done is not None
        _insert_row(
            self.outbox,
            event_id="phase-pending",
            run_id=done.run_id,
            event_kind=NotificationEventKind.PHASE_CHANGE.value,
            delivery_state=DeliveryState.PENDING.value,
            dedupe_key="phase:x",
        )
        _insert_row(
            self.outbox,
            event_id="terminal-pending",
            run_id=done.run_id,
            event_kind=NotificationEventKind.TERMINAL.value,
            delivery_state=DeliveryState.PENDING.value,
            dedupe_key="terminal:x",
        )
        _insert_row(
            self.outbox,
            event_id="startup-recovery",
            run_id=done.run_id,
            event_kind=NotificationEventKind.RECOVERY.value,
            delivery_state=DeliveryState.PENDING.value,
            dedupe_key="recovery:2026-08-14T18:00:00+00:00",
        )
        _insert_row(
            self.outbox,
            event_id="stale-obsolete",
            run_id=done.run_id,
            event_kind=NotificationEventKind.STALE.value,
            delivery_state=DeliveryState.PENDING.value,
            dedupe_key="stale:old",
        )
        affected = self.outbox.suppress_stale_recovery_for_terminal_run(
            done.run_id
        )
        self.assertEqual(affected, 1)
        self.assertEqual(
            _raw(self.outbox, "stale-obsolete")["delivery_state"],
            DeliveryState.SKIPPED.value,
        )
        for event_id in ("phase-pending", "terminal-pending", "startup-recovery"):
            self.assertEqual(
                _raw(self.outbox, event_id)["delivery_state"],
                DeliveryState.PENDING.value,
            )

    def test_terminal_enqueue_suppresses_without_second_cutoff(self) -> None:
        live = self._running_run(with_phase=True)
        stale_at = datetime.now(timezone.utc) - timedelta(seconds=120)
        with self.registry._lock:
            self.registry._conn.execute(
                "UPDATE runs SET heartbeat_at = ? WHERE run_id = ?",
                (stale_at.isoformat(), live.run_id),
            )
            self.registry._conn.commit()
        live = self.registry.get_run(live.run_id)
        assert live is not None
        self.assertTrue(self.outbox.maybe_enqueue_stale(live).created)
        self.registry.update_status(live.run_id, RunStatus.COMPLETED)
        events = {
            e["event_kind"]: e for e in self.outbox.list_for_run(live.run_id)
        }
        self.assertEqual(
            events["stale"]["delivery_state"], DeliveryState.SKIPPED.value
        )
        self.assertEqual(
            events["stale"]["last_error"],
            STALE_RECOVERY_TERMINAL_RUN_SUPPRESSED,
        )
        self.assertIn("terminal", events)
        # Exactly one terminal row; no date-cutoff reason strings.
        self.assertEqual(
            sum(
                1
                for e in self.outbox.list_for_run(live.run_id)
                if e["event_kind"] == "terminal"
            ),
            1,
        )
        blob = json.dumps(list(events.values()))
        self.assertNotIn("legacy_predeploy_backlog", blob)
        self.assertNotIn("2026-08-14T16:38:00", blob)


if __name__ == "__main__":
    unittest.main()
