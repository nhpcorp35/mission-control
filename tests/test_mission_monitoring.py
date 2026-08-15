"""Focused tests for Phase 2B phase-aware mission monitoring / wait."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.api as api_module
from app.api import (
    WAIT_DEFAULT_POLL_INTERVAL_SECONDS,
    app,
)
from mission_control.monitoring import (
    DEFAULT_MONITOR_POLL_INTERVAL_SECONDS,
    HEARTBEAT_STALE_THRESHOLD_SECONDS,
    MONITORING_HISTORY_MAX_EVENTS,
    HeartbeatHealth,
    append_monitoring_event,
    build_monitoring_event,
    classify_heartbeat_health,
    decode_monitor_cursor,
    encode_monitor_cursor,
    is_monitoring_terminal,
    observe_run,
)
from mission_control.run_registry import (
    RunPhase,
    RunRegistry,
    RunStatus,
    platform_progress,
)

TEST_API_KEY = "mc_test_authentication_key"
AUTH_HEADERS = {
    "Authorization": f"Bearer {TEST_API_KEY}",
}
os.environ["MISSION_CONTROL_API_KEY"] = TEST_API_KEY


class TestMonitoringHelpers(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)

    def tearDown(self) -> None:
        self.registry.close()
        os.unlink(self._db_path)

    def test_default_poll_interval_approximately_25s(self) -> None:
        self.assertEqual(DEFAULT_MONITOR_POLL_INTERVAL_SECONDS, 25.0)
        self.assertEqual(WAIT_DEFAULT_POLL_INTERVAL_SECONDS, 25.0)
        self.assertGreater(HEARTBEAT_STALE_THRESHOLD_SECONDS, 5.0)
        self.assertEqual(HEARTBEAT_STALE_THRESHOLD_SECONDS, 90.0)

    def test_terminal_includes_cancelled(self) -> None:
        for status in (
            "completed",
            "failed",
            "timed_out",
            "cancelled",
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.TIMED_OUT,
        ):
            with self.subTest(status=status):
                self.assertTrue(is_monitoring_terminal(status))
        self.assertFalse(is_monitoring_terminal(RunStatus.RUNNING))
        self.assertFalse(is_monitoring_terminal("queued"))

    def test_heartbeat_classification_matrix(self) -> None:
        record = self.registry.create_run()
        self.assertEqual(
            classify_heartbeat_health(record),
            HeartbeatHealth.NOT_APPLICABLE,
        )

        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        self.registry.set_phase(record.run_id, RunPhase.AGENT_EXECUTION)
        active = self.registry.get_run(record.run_id)
        assert active is not None
        self.assertEqual(
            classify_heartbeat_health(active),
            HeartbeatHealth.HEALTHY,
        )

        stale_at = datetime.now(timezone.utc) - timedelta(
            seconds=HEARTBEAT_STALE_THRESHOLD_SECONDS + 5
        )
        with self.registry._lock:
            self.registry._conn.execute(
                "UPDATE runs SET heartbeat_at = ? WHERE run_id = ?",
                (stale_at.isoformat(), record.run_id),
            )
            self.registry._conn.commit()
        stale = self.registry.get_run(record.run_id)
        assert stale is not None
        self.assertEqual(
            classify_heartbeat_health(stale),
            HeartbeatHealth.STALE,
        )

        with self.registry._lock:
            self.registry._conn.execute(
                "UPDATE runs SET heartbeat_at = NULL WHERE run_id = ?",
                (record.run_id,),
            )
            self.registry._conn.commit()
        absent = self.registry.get_run(record.run_id)
        assert absent is not None
        self.assertEqual(
            classify_heartbeat_health(absent),
            HeartbeatHealth.ABSENT,
        )

        self.registry.update_status(record.run_id, RunStatus.FAILED)
        failed = self.registry.get_run(record.run_id)
        assert failed is not None
        self.assertEqual(
            classify_heartbeat_health(failed),
            HeartbeatHealth.TERMINAL,
        )

    def test_repeated_heartbeat_does_not_duplicate_events(self) -> None:
        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        self.registry.set_phase(
            record.run_id,
            RunPhase.AGENT_EXECUTION,
            progress=platform_progress(step="agent_execution", detail="working"),
        )
        current = self.registry.get_run(record.run_id)
        assert current is not None
        history: list[dict] = []
        history, _, _ = observe_run(current, history)
        self.assertEqual(len(history), 1)

        for _ in range(5):
            self.registry.touch_heartbeat(record.run_id)
            touched = self.registry.get_run(record.run_id)
            assert touched is not None
            history, _, _ = observe_run(touched, history)
        self.assertEqual(len(history), 1)

    def test_history_bounded_and_redacted(self) -> None:
        history: list[dict] = []
        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        for index in range(MONITORING_HISTORY_MAX_EVENTS + 10):
            self.registry.set_phase(
                record.run_id,
                RunPhase.AGENT_EXECUTION,
                progress=platform_progress(
                    step="agent_execution",
                    detail=(
                        f"step-{index} token=supersecrettokenvalue1234567890"
                    ),
                ),
            )
            current = self.registry.get_run(record.run_id)
            assert current is not None
            history, _, event = observe_run(current, history)
            detail = (event.get("progress") or {}).get("detail", "")
            self.assertNotIn("supersecrettokenvalue1234567890", detail)
            self.assertNotIn("stdout", event)
            self.assertNotIn("stderr", event)
            for key in ("prompt", "command", "commands", "secret"):
                self.assertNotIn(key, event)
        self.assertLessEqual(len(history), MONITORING_HISTORY_MAX_EVENTS)

    def test_cursor_round_trip_strips_unsafe_keys(self) -> None:
        event = {
            "at": "2026-08-13T00:00:00Z",
            "status": "running",
            "phase": "agent_execution",
            "progress": {"step": "agent_execution", "detail": "ok"},
            "heartbeat_health": "healthy",
            "stdout": "LEAK",
            "stderr": "LEAK",
            "prompt": "LEAK",
        }
        history = append_monitoring_event([], event)
        cursor = encode_monitor_cursor(history)
        restored = decode_monitor_cursor(cursor)
        self.assertEqual(len(restored), 1)
        self.assertNotIn("stdout", restored[0])
        self.assertNotIn("stderr", restored[0])
        self.assertNotIn("prompt", restored[0])
        self.assertEqual(decode_monitor_cursor("!!!"), [])


class TestPhaseAwareWaitApi(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self._previous_registry = api_module.run_registry
        api_module.run_registry = RunRegistry(self._db_path)
        self.client = TestClient(app, headers=AUTH_HEADERS)

    def tearDown(self) -> None:
        try:
            test_registry = api_module.run_registry
            if test_registry is not self._previous_registry:
                test_registry.close()
        finally:
            api_module.run_registry = self._previous_registry
            os.unlink(self._db_path)

    def test_queued_to_running_phase_transition_in_history(self) -> None:
        record = api_module.run_registry.create_run()

        def advance() -> None:
            time.sleep(0.08)
            api_module.run_registry.update_status(
                record.run_id, RunStatus.RUNNING
            )
            api_module.run_registry.set_phase(
                record.run_id,
                RunPhase.AGENT_EXECUTION,
                progress=platform_progress(
                    step="agent_execution",
                    detail="Agent started",
                ),
            )
            time.sleep(0.08)
            api_module.run_registry.store_result(record.run_id, stdout="done")
            api_module.run_registry.update_status(
                record.run_id, RunStatus.COMPLETED
            )

        thread = threading.Thread(target=advance)
        thread.start()
        try:
            response = self.client.post(
                f"/runs/{record.run_id}/wait",
                json={
                    "timeout_seconds": 2.0,
                    "poll_interval_seconds": 0.05,
                },
            )
        finally:
            thread.join(timeout=2.0)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["reached_terminal"])
        self.assertEqual(body["status"], "completed")
        phases = [event["phase"] for event in body["monitoring_history"]]
        self.assertIn("queued", phases)
        self.assertIn("agent_execution", phases)
        self.assertIn("completed", phases)
        self.assertEqual(body["heartbeat_health"], "terminal")
        self.assertFalse(body["stale_heartbeat"])

    def test_stale_heartbeat_reported_without_mutation(self) -> None:
        record = api_module.run_registry.create_run()
        api_module.run_registry.update_status(record.run_id, RunStatus.RUNNING)
        api_module.run_registry.set_phase(
            record.run_id, RunPhase.AGENT_EXECUTION
        )
        stale_at = datetime.now(timezone.utc) - timedelta(
            seconds=HEARTBEAT_STALE_THRESHOLD_SECONDS + 10
        )
        with api_module.run_registry._lock:
            api_module.run_registry._conn.execute(
                "UPDATE runs SET heartbeat_at = ? WHERE run_id = ?",
                (stale_at.isoformat(), record.run_id),
            )
            api_module.run_registry._conn.commit()

        response = self.client.post(
            f"/runs/{record.run_id}/wait",
            json={
                "timeout_seconds": 0.15,
                "poll_interval_seconds": 0.05,
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["wait_expired"])
        self.assertFalse(body["reached_terminal"])
        self.assertEqual(body["status"], "running")
        self.assertEqual(body["heartbeat_health"], "stale")
        self.assertTrue(body["stale_heartbeat"])

        after = api_module.run_registry.get_run(record.run_id)
        assert after is not None
        self.assertEqual(after.status, RunStatus.RUNNING)
        self.assertIsNone(after.completed_at)
        self.assertNotEqual(after.status, RunStatus.TIMED_OUT)
        self.assertNotEqual(after.status.value, "cancelled")

    def test_edge_wait_timeout_returns_resumable_cursor(self) -> None:
        record = api_module.run_registry.create_run()
        api_module.run_registry.update_status(record.run_id, RunStatus.RUNNING)
        api_module.run_registry.set_phase(
            record.run_id, RunPhase.AGENT_EXECUTION
        )

        first = self.client.post(
            f"/runs/{record.run_id}/wait",
            json={
                "timeout_seconds": 0.15,
                "poll_interval_seconds": 0.05,
            },
        )
        self.assertEqual(first.status_code, 200)
        first_body = first.json()
        self.assertTrue(first_body["wait_expired"])
        self.assertFalse(first_body["reached_terminal"])
        self.assertEqual(first_body["status"], "running")
        self.assertIsInstance(first_body["cursor"], str)
        self.assertTrue(first_body["cursor"])
        before_status = api_module.run_registry.get_run(record.run_id)
        assert before_status is not None
        self.assertEqual(before_status.status, RunStatus.RUNNING)

        api_module.run_registry.store_result(record.run_id, stdout="later")
        api_module.run_registry.update_status(
            record.run_id, RunStatus.COMPLETED
        )

        resumed = self.client.post(
            f"/runs/{record.run_id}/wait",
            json={
                "timeout_seconds": 1.0,
                "poll_interval_seconds": 0.05,
                "cursor": first_body["cursor"],
            },
        )
        self.assertEqual(resumed.status_code, 200)
        body = resumed.json()
        self.assertTrue(body["reached_terminal"])
        self.assertFalse(body["wait_expired"])
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["stdout"], "later")
        phases = [event["phase"] for event in body["monitoring_history"]]
        self.assertIn("agent_execution", phases)
        self.assertIn("completed", phases)

    def test_failure_timed_out_outcomes(self) -> None:
        for status in (RunStatus.FAILED, RunStatus.TIMED_OUT):
            with self.subTest(status=status.value):
                record = api_module.run_registry.create_run()
                api_module.run_registry.update_status(
                    record.run_id, RunStatus.RUNNING
                )
                api_module.run_registry.update_status(record.run_id, status)
                response = self.client.post(
                    f"/runs/{record.run_id}/wait",
                    json={
                        "timeout_seconds": 1.0,
                        "poll_interval_seconds": 0.05,
                    },
                )
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertTrue(body["reached_terminal"])
                self.assertFalse(body["wait_expired"])
                self.assertEqual(body["status"], status.value)
                self.assertEqual(body["heartbeat_health"], "terminal")

    def test_cancelled_terminal_helper_and_history_event(self) -> None:
        record = api_module.run_registry.create_run()
        api_module.run_registry.update_status(record.run_id, RunStatus.RUNNING)
        # Registry has no cancelled enum yet; monitoring still classifies it.
        fake = api_module.run_registry.get_run(record.run_id)
        assert fake is not None
        with patch.object(fake, "status", "cancelled"):
            self.assertTrue(is_monitoring_terminal(fake.status))
            event = build_monitoring_event(fake)
            self.assertEqual(event["heartbeat_health"], "terminal")
            self.assertEqual(event["status"], "cancelled")

    def test_concurrent_waiters_no_mutation(self) -> None:
        record = api_module.run_registry.create_run()
        api_module.run_registry.update_status(record.run_id, RunStatus.RUNNING)
        api_module.run_registry.set_phase(
            record.run_id, RunPhase.AGENT_EXECUTION
        )
        results: list[dict] = []
        lock = threading.Lock()

        def waiter() -> None:
            response = self.client.post(
                f"/runs/{record.run_id}/wait",
                json={
                    "timeout_seconds": 0.2,
                    "poll_interval_seconds": 0.05,
                },
            )
            with lock:
                results.append(response.json())

        threads = [threading.Thread(target=waiter) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2.0)

        self.assertEqual(len(results), 3)
        for body in results:
            self.assertTrue(body["wait_expired"])
            self.assertEqual(body["status"], "running")
            self.assertFalse(body["reached_terminal"])

        after = api_module.run_registry.get_run(record.run_id)
        assert after is not None
        self.assertEqual(after.status, RunStatus.RUNNING)
        self.assertIsNone(after.completed_at)
        self.assertIsNone(after.error)

    def test_wait_timeout_does_not_cancel_or_fail_run(self) -> None:
        record = api_module.run_registry.create_run()
        api_module.run_registry.update_status(record.run_id, RunStatus.RUNNING)
        before = api_module.run_registry.get_run(record.run_id)
        assert before is not None

        response = self.client.post(
            f"/runs/{record.run_id}/wait",
            json={
                "timeout_seconds": 0.12,
                "poll_interval_seconds": 0.05,
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["wait_expired"])
        self.assertIn("cursor", body)
        self.assertEqual(body["run_id"], record.run_id)

        after = api_module.run_registry.get_run(record.run_id)
        assert after is not None
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.phase, before.phase)
        self.assertIsNone(after.completed_at)
        self.assertNotIn(after.status, (RunStatus.FAILED, RunStatus.TIMED_OUT))


if __name__ == "__main__":
    unittest.main()
