"""Tests for authorized run cancellation and heartbeat watchdog."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import app.api as api_module
from app.api import app
from mission_control.run_cancellation import (
    CANCELLED_BY_OPERATOR_ERROR,
    HeartbeatWatchdog,
    STALE_HEARTBEAT_WATCHDOG_TIMEOUT_SECONDS,
    active_execution_registry,
    cancel_run,
    recover_stale_run,
    stale_heartbeat_failure_reason,
)
from mission_control.run_registry import RunRegistry, RunStatus
from mission_control.run_queue import RunQueue

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_API_KEY = "mc_test_cancel_key"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_API_KEY}"}
os.environ["MISSION_CONTROL_API_KEY"] = TEST_API_KEY


class RunCancellationRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)

    def tearDown(self) -> None:
        self.registry.close()
        os.unlink(self._db_path)

    def _insert_running(
        self,
        *,
        heartbeat_age_seconds: float = 0.0,
    ) -> str:
        record = self.registry.create_run(mission_yaml="# test")
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        if heartbeat_age_seconds > 0.0:
            stale = datetime.now(timezone.utc) - timedelta(
                seconds=heartbeat_age_seconds
            )
            with self.registry._lock:
                self.registry._conn.execute(
                    "UPDATE runs SET heartbeat_at = ? WHERE run_id = ?",
                    (stale.isoformat(), record.run_id),
                )
                self.registry._conn.commit()
        refreshed = self.registry.get_run(record.run_id)
        assert refreshed is not None
        return refreshed.run_id

    def test_cancel_success_marks_cancelled(self) -> None:
        run_id = self._insert_running()
        result = cancel_run(self.registry, run_id, source="test")
        self.assertTrue(result.ok)
        self.assertEqual(result.status, RunStatus.CANCELLED.value)
        record = self.registry.get_run(run_id)
        assert record is not None
        self.assertEqual(record.status, RunStatus.CANCELLED)
        self.assertEqual(record.error, CANCELLED_BY_OPERATOR_ERROR)

    def test_idempotent_repeat_cancel(self) -> None:
        run_id = self._insert_running()
        first = cancel_run(self.registry, run_id, source="test")
        second = cancel_run(self.registry, run_id, source="test")
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertTrue(second.already_terminal)

    def test_watchdog_stale_recovery(self) -> None:
        run_id = self._insert_running(
            heartbeat_age_seconds=STALE_HEARTBEAT_WATCHDOG_TIMEOUT_SECONDS + 30.0,
        )
        watchdog = HeartbeatWatchdog(self.registry)
        count = watchdog.tick()
        self.assertEqual(count, 1)
        record = self.registry.get_run(run_id)
        assert record is not None
        self.assertEqual(record.status, RunStatus.FAILED)
        self.assertIn("stale heartbeat", record.error or "")

    def test_healthy_heartbeat_not_cancelled_by_watchdog(self) -> None:
        run_id = self._insert_running(heartbeat_age_seconds=5.0)
        watchdog = HeartbeatWatchdog(self.registry)
        count = watchdog.tick()
        self.assertEqual(count, 0)
        record = self.registry.get_run(run_id)
        assert record is not None
        self.assertEqual(record.status, RunStatus.RUNNING)

    def test_terminal_state_race_is_idempotent(self) -> None:
        run_id = self._insert_running()
        self.registry.update_status(run_id, RunStatus.COMPLETED)
        result = cancel_run(self.registry, run_id, source="test")
        self.assertTrue(result.ok)
        self.assertTrue(result.already_terminal)
        record = self.registry.get_run(run_id)
        assert record is not None
        self.assertEqual(record.status, RunStatus.COMPLETED)

    def test_recover_stale_run_operator_path(self) -> None:
        run_id = self._insert_running(
            heartbeat_age_seconds=STALE_HEARTBEAT_WATCHDOG_TIMEOUT_SECONDS + 10.0,
        )
        recovery_at = datetime.now(timezone.utc)
        result = recover_stale_run(
            self.registry,
            run_id,
            source="operator_recovery",
            recovery_at=recovery_at,
        )
        self.assertTrue(result.ok)
        record = self.registry.get_run(run_id)
        assert record is not None
        self.assertEqual(record.status, RunStatus.FAILED)
        expected = stale_heartbeat_failure_reason(
            last_heartbeat_at=record.heartbeat_at,
            recovery_at=recovery_at,
        )
        self.assertIn("stale heartbeat", record.error or "")
        self.assertIn("recovered_at=", record.error or "")


class RunCancellationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        api_module.run_registry = RunRegistry(self._db_path)
        api_module.run_queue = RunQueue()
        api_module.run_queue.configure(api_module._execute_queued_run)
        api_module.heartbeat_watchdog = HeartbeatWatchdog(
            api_module.run_registry,
            run_queue=api_module.run_queue,
        )
        self.client = TestClient(app, headers=AUTH_HEADERS)

    def tearDown(self) -> None:
        api_module.run_registry.close()
        os.unlink(self._db_path)

    def test_unauthorized_cancel_rejected(self) -> None:
        record = api_module.run_registry.create_run(mission_yaml="# x")
        response = TestClient(app).post(f"/runs/{record.run_id}/cancel")
        self.assertEqual(response.status_code, 401)

    def test_cancel_running_run_via_http(self) -> None:
        record = api_module.run_registry.create_run(mission_yaml="# cancel-http")
        api_module.run_registry.update_status(record.run_id, RunStatus.RUNNING)
        response = self.client.post(f"/runs/{record.run_id}/cancel")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], RunStatus.CANCELLED.value)
        self.assertIn("action", body["diagnostics"])


if __name__ == "__main__":
    unittest.main()
