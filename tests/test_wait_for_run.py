"""Focused regression tests for POST /runs/{run_id}/wait (wait_for_run)."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient

import app.api as api_module
from app.api import (
    WAIT_MAX_POLL_INTERVAL_SECONDS,
    WAIT_MAX_TIMEOUT_SECONDS,
    WAIT_MIN_POLL_INTERVAL_SECONDS,
    WAIT_MIN_TIMEOUT_SECONDS,
    app,
)
from mission_control.run_registry import RunRegistry, RunStatus, is_terminal_status

TEST_API_KEY = "mc_test_authentication_key"
AUTH_HEADERS = {
    "Authorization": f"Bearer {TEST_API_KEY}",
}
os.environ["MISSION_CONTROL_API_KEY"] = TEST_API_KEY


class TestIsTerminalStatus(unittest.TestCase):
    def test_covers_defined_terminal_statuses(self) -> None:
        for status in (
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.TIMED_OUT,
            "completed",
            "failed",
            "timed_out",
        ):
            with self.subTest(status=status):
                self.assertTrue(is_terminal_status(status))

    def test_non_terminal_statuses(self) -> None:
        for status in (RunStatus.QUEUED, RunStatus.RUNNING, "queued", "running"):
            with self.subTest(status=status):
                self.assertFalse(is_terminal_status(status))


class TestWaitForRunApi(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        api_module.run_registry = RunRegistry(self._db_path)
        self.client = TestClient(app, headers=AUTH_HEADERS)

    def tearDown(self) -> None:
        api_module.run_registry.close()
        os.unlink(self._db_path)

    def test_already_terminal_returns_immediately(self) -> None:
        record = api_module.run_registry.create_run()
        api_module.run_registry.update_status(record.run_id, RunStatus.RUNNING)
        api_module.run_registry.update_status(
            record.run_id, RunStatus.COMPLETED
        )
        api_module.run_registry.store_result(
            record.run_id,
            stdout="done",
            commit_sha="abc123",
        )

        started = time.monotonic()
        response = self.client.post(
            f"/runs/{record.run_id}/wait",
            json={
                "timeout_seconds": 5.0,
                "poll_interval_seconds": 1.0,
            },
        )
        elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertTrue(body["reached_terminal"])
        self.assertFalse(body["wait_expired"])
        self.assertEqual(body["stdout"], "done")
        self.assertEqual(body["commit_sha"], "abc123")
        self.assertLess(elapsed, 1.0)

    def test_completes_during_wait(self) -> None:
        record = api_module.run_registry.create_run()
        api_module.run_registry.update_status(record.run_id, RunStatus.RUNNING)

        def complete_soon() -> None:
            time.sleep(0.15)
            api_module.run_registry.update_status(
                record.run_id, RunStatus.COMPLETED
            )
            api_module.run_registry.store_result(
                record.run_id,
                stdout="finished during wait",
            )

        thread = threading.Thread(target=complete_soon)
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
        self.assertEqual(body["status"], "completed")
        self.assertTrue(body["reached_terminal"])
        self.assertFalse(body["wait_expired"])
        self.assertEqual(body["stdout"], "finished during wait")

    def test_timeout_while_still_running(self) -> None:
        record = api_module.run_registry.create_run()
        api_module.run_registry.update_status(record.run_id, RunStatus.RUNNING)

        response = self.client.post(
            f"/runs/{record.run_id}/wait",
            json={
                "timeout_seconds": 0.2,
                "poll_interval_seconds": 0.05,
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "running")
        self.assertFalse(body["reached_terminal"])
        self.assertTrue(body["wait_expired"])

    def test_invalid_bounds_rejected(self) -> None:
        record = api_module.run_registry.create_run()
        cases = (
            {"timeout_seconds": WAIT_MIN_TIMEOUT_SECONDS - 0.01},
            {"timeout_seconds": WAIT_MAX_TIMEOUT_SECONDS + 1},
            {"poll_interval_seconds": WAIT_MIN_POLL_INTERVAL_SECONDS - 0.01},
            {"poll_interval_seconds": WAIT_MAX_POLL_INTERVAL_SECONDS + 1},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                response = self.client.post(
                    f"/runs/{record.run_id}/wait",
                    json=payload,
                )
                self.assertEqual(response.status_code, 422)

    def test_timeout_does_not_mutate_run_state(self) -> None:
        record = api_module.run_registry.create_run()
        api_module.run_registry.update_status(record.run_id, RunStatus.RUNNING)
        before = api_module.run_registry.get_run(record.run_id)
        assert before is not None

        response = self.client.post(
            f"/runs/{record.run_id}/wait",
            json={
                "timeout_seconds": 0.15,
                "poll_interval_seconds": 0.05,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["wait_expired"])

        after = api_module.run_registry.get_run(record.run_id)
        assert after is not None
        self.assertEqual(after.status, RunStatus.RUNNING)
        self.assertIsNone(after.completed_at)
        self.assertEqual(after.started_at, before.started_at)
        self.assertIsNone(after.error)
        self.assertNotEqual(after.status, RunStatus.TIMED_OUT)


if __name__ == "__main__":
    unittest.main()
