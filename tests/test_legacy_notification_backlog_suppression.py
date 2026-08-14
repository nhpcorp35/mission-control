"""Regression tests for legacy pre-deploy notification backlog suppression."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.api as api_module
from app.api import app
from mission_control.notifications import (
    LEGACY_PREDEPLOY_BACKLOG_CUTOFF_UTC,
    LEGACY_PREDEPLOY_BACKLOG_SUPPRESSED,
    DeliveryState,
    NotificationConfig,
    NotificationDeliveryWorker,
    NotificationEventKind,
    NotificationOutbox,
    _format_dt,
)
from mission_control.run_registry import RunRegistry

TEST_API_KEY = "mc_test_authentication_key"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_API_KEY}"}
os.environ["MISSION_CONTROL_API_KEY"] = TEST_API_KEY

_CUTOFF = LEGACY_PREDEPLOY_BACKLOG_CUTOFF_UTC
_CUTOFF_S = _format_dt(_CUTOFF)
assert _CUTOFF_S is not None


def _config() -> NotificationConfig:
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


def _insert_row(
    outbox: NotificationOutbox,
    *,
    event_id: str,
    run_id: str,
    event_kind: str,
    delivery_state: str,
    created_at: str,
    claim_owner: str | None = None,
    claim_expires_at: str | None = None,
    attempt_count: int = 2,
    next_attempt_at: str | None = "2026-08-14T15:00:00+00:00",
    last_error: str | None = "prior_error",
    delivered_at: str | None = None,
    payload: dict | None = None,
) -> dict:
    """Seed one outbox row with explicit timestamps (bypasses enqueue clock)."""
    now_s = created_at
    body = payload or {
        "run_id": run_id,
        "event_kind": event_kind,
        "occurred_at": created_at,
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
                f"dedupe:{event_id}",
                json.dumps(body, separators=(",", ":"), sort_keys=True),
                delivery_state,
                attempt_count,
                next_attempt_at,
                last_error,
                delivered_at,
                created_at,
                now_s,
                claim_owner,
                claim_expires_at,
            ),
        )
        outbox._conn.commit()
    return {
        "event_id": event_id,
        "run_id": run_id,
        "event_kind": event_kind,
        "delivery_state": delivery_state,
        "created_at": created_at,
        "attempt_count": attempt_count,
        "next_attempt_at": next_attempt_at,
        "last_error": last_error,
        "delivered_at": delivered_at,
        "payload_json": json.dumps(body, separators=(",", ":"), sort_keys=True),
        "claim_owner": claim_owner,
        "claim_expires_at": claim_expires_at,
    }


def _raw_row(outbox: NotificationOutbox, event_id: str) -> sqlite3.Row:
    with outbox._lock:
        row = outbox._conn.execute(
            "SELECT * FROM notification_outbox WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    assert row is not None
    return row


class TestSuppressLegacyPredeployBacklog(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.outbox = NotificationOutbox(self._db_path, config=_config())

    def tearDown(self) -> None:
        self.outbox.close()
        os.unlink(self._db_path)

    def test_suppresses_matching_pending_and_in_flight_only(self) -> None:
        before = _format_dt(_CUTOFF - timedelta(seconds=1))
        assert before is not None
        after = _format_dt(_CUTOFF + timedelta(seconds=1))
        assert after is not None
        exact = _CUTOFF_S

        matching = [
            _insert_row(
                self.outbox,
                event_id="stale-pending",
                run_id="run-a",
                event_kind=NotificationEventKind.STALE.value,
                delivery_state=DeliveryState.PENDING.value,
                created_at=before,
                claim_owner=None,
            ),
            _insert_row(
                self.outbox,
                event_id="recovery-in-flight",
                run_id="run-b",
                event_kind=NotificationEventKind.RECOVERY.value,
                delivery_state=DeliveryState.IN_FLIGHT.value,
                created_at=before,
                claim_owner="worker-1",
                claim_expires_at=_format_dt(_CUTOFF + timedelta(minutes=1)),
            ),
        ]
        nonmatching = [
            _insert_row(
                self.outbox,
                event_id="stale-at-cutoff",
                run_id="run-c",
                event_kind=NotificationEventKind.STALE.value,
                delivery_state=DeliveryState.PENDING.value,
                created_at=exact,
            ),
            _insert_row(
                self.outbox,
                event_id="stale-after",
                run_id="run-d",
                event_kind=NotificationEventKind.STALE.value,
                delivery_state=DeliveryState.PENDING.value,
                created_at=after,
            ),
            _insert_row(
                self.outbox,
                event_id="terminal-pending",
                run_id="run-e",
                event_kind=NotificationEventKind.TERMINAL.value,
                delivery_state=DeliveryState.PENDING.value,
                created_at=before,
            ),
            _insert_row(
                self.outbox,
                event_id="phase-pending",
                run_id="run-f",
                event_kind=NotificationEventKind.PHASE_CHANGE.value,
                delivery_state=DeliveryState.PENDING.value,
                created_at=before,
            ),
            _insert_row(
                self.outbox,
                event_id="stale-delivered",
                run_id="run-g",
                event_kind=NotificationEventKind.STALE.value,
                delivery_state=DeliveryState.DELIVERED.value,
                created_at=before,
                delivered_at=before,
                last_error=None,
            ),
            _insert_row(
                self.outbox,
                event_id="stale-dead",
                run_id="run-h",
                event_kind=NotificationEventKind.STALE.value,
                delivery_state=DeliveryState.DEAD.value,
                created_at=before,
            ),
            _insert_row(
                self.outbox,
                event_id="stale-skipped",
                run_id="run-i",
                event_kind=NotificationEventKind.STALE.value,
                delivery_state=DeliveryState.SKIPPED.value,
                created_at=before,
                last_error="already_skipped",
            ),
            _insert_row(
                self.outbox,
                event_id="recovery-pending-after",
                run_id="run-j",
                event_kind=NotificationEventKind.RECOVERY.value,
                delivery_state=DeliveryState.PENDING.value,
                created_at=after,
            ),
        ]

        affected = self.outbox.suppress_legacy_predeploy_backlog()
        self.assertEqual(affected, 2)

        for seed in matching:
            row = _raw_row(self.outbox, seed["event_id"])
            self.assertEqual(row["delivery_state"], DeliveryState.SKIPPED.value)
            self.assertEqual(
                row["last_error"], LEGACY_PREDEPLOY_BACKLOG_SUPPRESSED
            )
            self.assertIsNone(row["claim_owner"])
            self.assertIsNone(row["claim_expires_at"])
            self.assertEqual(row["attempt_count"], seed["attempt_count"])
            self.assertEqual(row["next_attempt_at"], seed["next_attempt_at"])
            self.assertEqual(row["created_at"], seed["created_at"])
            self.assertEqual(row["delivered_at"], seed["delivered_at"])
            self.assertEqual(row["payload_json"], seed["payload_json"])
            self.assertEqual(row["run_id"], seed["run_id"])
            self.assertEqual(row["event_id"], seed["event_id"])
            self.assertGreaterEqual(row["updated_at"], seed["created_at"])

        for seed in nonmatching:
            row = _raw_row(self.outbox, seed["event_id"])
            self.assertEqual(row["delivery_state"], seed["delivery_state"])
            self.assertEqual(row["last_error"], seed["last_error"])
            self.assertEqual(row["claim_owner"], seed["claim_owner"])
            self.assertEqual(row["claim_expires_at"], seed["claim_expires_at"])
            self.assertEqual(row["created_at"], seed["created_at"])
            self.assertEqual(row["payload_json"], seed["payload_json"])

    def test_every_delivery_state_boundary_matrix(self) -> None:
        before = _format_dt(_CUTOFF - timedelta(minutes=5))
        assert before is not None
        for state in DeliveryState:
            event_id = f"state-{state.value}"
            _insert_row(
                self.outbox,
                event_id=event_id,
                run_id=f"run-{state.value}",
                event_kind=NotificationEventKind.STALE.value,
                delivery_state=state.value,
                created_at=before,
                claim_owner="owner" if state == DeliveryState.IN_FLIGHT else None,
                claim_expires_at=(
                    _format_dt(_CUTOFF) if state == DeliveryState.IN_FLIGHT else None
                ),
            )

        affected = self.outbox.suppress_legacy_predeploy_backlog()
        self.assertEqual(affected, 2)  # pending + in_flight only

        for state in DeliveryState:
            row = _raw_row(self.outbox, f"state-{state.value}")
            if state in (DeliveryState.PENDING, DeliveryState.IN_FLIGHT):
                self.assertEqual(row["delivery_state"], DeliveryState.SKIPPED.value)
                self.assertEqual(
                    row["last_error"], LEGACY_PREDEPLOY_BACKLOG_SUPPRESSED
                )
            else:
                self.assertEqual(row["delivery_state"], state.value)

    def test_exact_cutoff_not_suppressed(self) -> None:
        _insert_row(
            self.outbox,
            event_id="exact-cutoff",
            run_id="run-exact",
            event_kind=NotificationEventKind.RECOVERY.value,
            delivery_state=DeliveryState.PENDING.value,
            created_at=_CUTOFF_S,
        )
        self.assertEqual(self.outbox.suppress_legacy_predeploy_backlog(), 0)
        row = _raw_row(self.outbox, "exact-cutoff")
        self.assertEqual(row["delivery_state"], DeliveryState.PENDING.value)

    def test_idempotent_second_run_changes_zero_rows(self) -> None:
        before = _format_dt(_CUTOFF - timedelta(hours=1))
        assert before is not None
        _insert_row(
            self.outbox,
            event_id="once",
            run_id="run-once",
            event_kind=NotificationEventKind.STALE.value,
            delivery_state=DeliveryState.PENDING.value,
            created_at=before,
        )
        first = self.outbox.suppress_legacy_predeploy_backlog()
        row_after_first = dict(_raw_row(self.outbox, "once"))
        second = self.outbox.suppress_legacy_predeploy_backlog()
        row_after_second = dict(_raw_row(self.outbox, "once"))
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(row_after_first, row_after_second)

    def test_transaction_failure_rolls_back(self) -> None:
        before = _format_dt(_CUTOFF - timedelta(seconds=30))
        assert before is not None
        _insert_row(
            self.outbox,
            event_id="rollback-me",
            run_id="run-rb",
            event_kind=NotificationEventKind.STALE.value,
            delivery_state=DeliveryState.PENDING.value,
            created_at=before,
            claim_owner=None,
        )
        before_row = dict(_raw_row(self.outbox, "rollback-me"))

        class _FailCommitProxy:
            """Force commit failure after UPDATE; sqlite3 methods are read-only."""

            def __init__(self, real: sqlite3.Connection) -> None:
                self._real = real
                self._fail_next_commit = False

            def execute(self, *args, **kwargs):
                result = self._real.execute(*args, **kwargs)
                sql = args[0] if args else ""
                if isinstance(sql, str) and "UPDATE" in sql.upper():
                    self._fail_next_commit = True
                return result

            def commit(self) -> None:
                if self._fail_next_commit:
                    self._fail_next_commit = False
                    raise sqlite3.Error("forced_commit_failure")
                self._real.commit()

            def rollback(self) -> None:
                self._real.rollback()

            def __getattr__(self, name: str):
                return getattr(self._real, name)

        self.outbox._conn = _FailCommitProxy(self.outbox._conn)  # type: ignore[assignment]
        with self.assertRaises(sqlite3.Error):
            self.outbox.suppress_legacy_predeploy_backlog()

        after_row = dict(_raw_row(self.outbox, "rollback-me"))
        self.assertEqual(after_row, before_row)
        self.assertEqual(after_row["delivery_state"], DeliveryState.PENDING.value)


class TestStartupOrderingBeforeWorker(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)
        self.outbox = self.registry._get_notification_outbox()
        self.outbox._config = _config()
        self.worker = NotificationDeliveryWorker(self.outbox, poll_seconds=0.05)
        self._prev_registry = api_module.run_registry
        self._prev_outbox = api_module.notification_outbox
        self._prev_worker = api_module.notification_delivery_worker
        api_module.run_registry = self.registry
        api_module.notification_outbox = self.outbox
        api_module.notification_delivery_worker = self.worker

    def tearDown(self) -> None:
        try:
            self.worker.stop()
        except Exception:  # noqa: BLE001
            pass
        api_module.run_registry = self._prev_registry
        api_module.notification_outbox = self._prev_outbox
        api_module.notification_delivery_worker = self._prev_worker
        self.registry.close()
        os.unlink(self._db_path)

    def test_lifespan_suppresses_before_worker_start(self) -> None:
        order: list[str] = []

        def suppress_probe() -> int:
            order.append("suppress")
            return 0

        def start_probe() -> bool:
            order.append("start")
            return True

        with (
            patch.object(
                api_module.notification_outbox,
                "suppress_legacy_predeploy_backlog",
                side_effect=suppress_probe,
            ),
            patch.object(
                api_module.notification_delivery_worker,
                "start",
                side_effect=start_probe,
            ),
            patch.object(
                api_module.notification_delivery_worker,
                "stop",
                return_value=None,
            ),
            patch.object(
                api_module.run_registry,
                "recover_interrupted_runs",
                return_value=0,
            ),
        ):
            with TestClient(app, headers=AUTH_HEADERS):
                self.assertIn("suppress", order)
                self.assertIn("start", order)
                self.assertLess(order.index("suppress"), order.index("start"))

    def test_lifespan_failure_does_not_start_worker(self) -> None:
        start_calls: list[str] = []

        def start_probe() -> bool:
            start_calls.append("start")
            return True

        with (
            patch.object(
                api_module.notification_outbox,
                "suppress_legacy_predeploy_backlog",
                side_effect=sqlite3.Error("suppress_failed"),
            ),
            patch.object(
                api_module.notification_delivery_worker,
                "start",
                side_effect=start_probe,
            ),
            patch.object(
                api_module.run_registry,
                "recover_interrupted_runs",
                return_value=0,
            ),
        ):
            with self.assertRaises(sqlite3.Error):
                with TestClient(app, headers=AUTH_HEADERS):
                    pass
        self.assertEqual(start_calls, [])

    def test_lifespan_suppresses_seeded_backlog_before_drain(self) -> None:
        before = _format_dt(_CUTOFF - timedelta(minutes=2))
        assert before is not None
        after = _format_dt(_CUTOFF + timedelta(minutes=2))
        assert after is not None
        _insert_row(
            self.outbox,
            event_id="legacy-stale",
            run_id="run-legacy",
            event_kind=NotificationEventKind.STALE.value,
            delivery_state=DeliveryState.PENDING.value,
            created_at=before,
        )
        _insert_row(
            self.outbox,
            event_id="fresh-stale",
            run_id="run-fresh",
            event_kind=NotificationEventKind.STALE.value,
            delivery_state=DeliveryState.PENDING.value,
            created_at=after,
        )

        drain_seen: list[str] = []
        real_drain = self.worker.drain_once

        def drain_probe(*, limit: int = 16) -> int:
            legacy = _raw_row(self.outbox, "legacy-stale")
            drain_seen.append(legacy["delivery_state"])
            return real_drain(limit=limit)

        with (
            patch.object(self.worker, "drain_once", side_effect=drain_probe),
            patch.object(
                api_module.run_registry,
                "recover_interrupted_runs",
                return_value=0,
            ),
        ):
            with TestClient(app, headers=AUTH_HEADERS):
                pass

        self.assertTrue(drain_seen)
        self.assertTrue(
            all(state == DeliveryState.SKIPPED.value for state in drain_seen)
        )
        legacy = _raw_row(self.outbox, "legacy-stale")
        fresh = _raw_row(self.outbox, "fresh-stale")
        self.assertEqual(legacy["delivery_state"], DeliveryState.SKIPPED.value)
        self.assertEqual(
            legacy["last_error"], LEGACY_PREDEPLOY_BACKLOG_SUPPRESSED
        )
        self.assertEqual(fresh["delivery_state"], DeliveryState.PENDING.value)


if __name__ == "__main__":
    unittest.main()
