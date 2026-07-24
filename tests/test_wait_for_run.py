"""Focused regression tests for wait_for_run and submit_and_wait REST APIs."""

from __future__ import annotations

import os
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.api as api_module
from app.api import (
    WAIT_MAX_POLL_INTERVAL_SECONDS,
    WAIT_MAX_TIMEOUT_SECONDS,
    WAIT_MIN_POLL_INTERVAL_SECONDS,
    WAIT_MIN_TIMEOUT_SECONDS,
    app,
)
from mission_control.executor import ExecutionResult
from mission_control.run_registry import RunRegistry, RunStatus, is_terminal_status
from mission_control.workspace import PersistenceResult, WorkspacePrepResult

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_API_KEY = "mc_test_authentication_key"
AUTH_HEADERS = {
    "Authorization": f"Bearer {TEST_API_KEY}",
}
os.environ["MISSION_CONTROL_API_KEY"] = TEST_API_KEY


def _executable_mission_yaml(mission_id: str = "2026-07-24-saw") -> str:
    return textwrap.dedent(
        f"""
        version: 1.0
        mission_id: {mission_id}
        title: Submit And Wait Test
        repository:
          name: Mission-Control
          path: {REPO_ROOT}
          base_branch: main
        execution:
          agent: cursor
          mode: execute
          sandbox: true
          worktree: false
        permissions:
          read: true
          create_files: true
          modify_files: false
          delete_files: false
          run_commands: true
          stage_changes: false
          commit: false
          push: false
        instructions: |
          Create a file.
        deliverables:
          - summary
        approval:
          execute_without_approval: true
          commit_requires_approval: true
          push_requires_approval: true
        """
    )


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
        self.assertEqual(body["run_id"], record.run_id)
        self.assertEqual(body["status"], "completed")
        self.assertTrue(body["reached_terminal"])
        self.assertFalse(body["wait_expired"])
        self.assertEqual(body["timeout_seconds"], 5.0)
        self.assertEqual(body["stdout"], "done")
        self.assertEqual(body["commit_sha"], "abc123")
        self.assertLess(elapsed, 1.0)

    def test_completes_during_wait(self) -> None:
        record = api_module.run_registry.create_run()
        api_module.run_registry.update_status(record.run_id, RunStatus.RUNNING)

        def complete_soon() -> None:
            time.sleep(0.15)
            api_module.run_registry.store_result(
                record.run_id,
                stdout="finished during wait",
            )
            api_module.run_registry.update_status(
                record.run_id, RunStatus.COMPLETED
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
        self.assertEqual(body["run_id"], record.run_id)
        self.assertEqual(body["status"], "completed")
        self.assertTrue(body["reached_terminal"])
        self.assertFalse(body["wait_expired"])
        self.assertEqual(body["timeout_seconds"], 2.0)
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
        self.assertEqual(body["run_id"], record.run_id)
        self.assertEqual(body["status"], "running")
        self.assertFalse(body["reached_terminal"])
        self.assertTrue(body["wait_expired"])
        self.assertEqual(body["timeout_seconds"], 0.2)

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

    def test_wait_requires_auth(self) -> None:
        client = TestClient(app)
        response = client.post(
            "/runs/some-id/wait",
            json={"timeout_seconds": 0.2},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Missing bearer token")
        self.assertNotIn(TEST_API_KEY, response.text)

    def test_wait_rejects_invalid_auth(self) -> None:
        client = TestClient(app)
        response = client.post(
            "/runs/some-id/wait",
            headers={"Authorization": "Bearer wrong-key"},
            json={"timeout_seconds": 0.2},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Invalid bearer token")
        self.assertNotIn(TEST_API_KEY, response.text)


class TestSubmitAndWaitApi(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        api_module.run_registry = RunRegistry(self._db_path)
        from mission_control.run_queue import RunQueue

        api_module.run_queue = RunQueue()
        api_module.run_queue.configure(api_module._execute_queued_run)
        self.client = TestClient(app, headers=AUTH_HEADERS)

    def tearDown(self) -> None:
        api_module.run_registry.close()
        os.unlink(self._db_path)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    @patch("app.api.preflight_for_execution", return_value=None)
    def test_successful_submit_and_wait(
        self,
        _mock_preflight,
        mock_prepare,
        mock_execute,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        workspace = Path(tempfile.mkdtemp())
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path=str(workspace),
        )
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout="agent done",
            return_code=0,
        )
        mock_persist.return_value = PersistenceResult(ok=True, commit_sha=None)

        response = self.client.post(
            "/runs/submit-and-wait",
            json={
                "mission_yaml": _executable_mission_yaml(),
                "timeout_seconds": 2.0,
                "poll_interval_seconds": 0.05,
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("run_id", body)
        self.assertEqual(body["status"], "completed")
        self.assertTrue(body["reached_terminal"])
        self.assertFalse(body["wait_expired"])
        self.assertEqual(body["timeout_seconds"], 2.0)
        self.assertEqual(body["stdout"], "agent done")

    def test_immediate_submission_validation_failure(self) -> None:
        with patch.object(
            api_module,
            "_wait_for_run",
            wraps=api_module._wait_for_run,
        ) as wait_mock:
            response = self.client.post(
                "/runs/submit-and-wait",
                json={
                    "mission_yaml": "version: 1.0",
                    "timeout_seconds": 2.0,
                    "poll_interval_seconds": 0.05,
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertIn("error", body)
        self.assertNotIn("reached_terminal", body)
        self.assertEqual(api_module.run_registry.count_runs(), 0)
        wait_mock.assert_not_called()

    def test_submit_and_wait_requires_auth(self) -> None:
        client = TestClient(app)
        response = client.post(
            "/runs/submit-and-wait",
            json={"mission_yaml": _executable_mission_yaml()},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Missing bearer token")
        self.assertNotIn(TEST_API_KEY, response.text)

    def test_submit_and_wait_rejects_invalid_auth(self) -> None:
        client = TestClient(app)
        response = client.post(
            "/runs/submit-and-wait",
            headers={"Authorization": "Bearer wrong-key"},
            json={"mission_yaml": _executable_mission_yaml()},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Invalid bearer token")
        self.assertNotIn(TEST_API_KEY, response.text)


class TestWaitOpenApiDiscovery(unittest.TestCase):
    def test_wait_operations_appear_in_openapi(self) -> None:
        schema = app.openapi()
        paths = schema["paths"]

        # Custom GPT Actions require an absolute HTTPS URL in servers.
        self.assertEqual(
            schema.get("servers"),
            [{"url": api_module.PRODUCTION_SERVER_URL}],
        )
        self.assertEqual(
            schema["servers"][0]["url"],
            "https://mission-control-production-76ff.up.railway.app",
        )

        self.assertIn("/runs/{run_id}/wait", paths)
        wait_op = paths["/runs/{run_id}/wait"]["post"]
        self.assertEqual(wait_op["operationId"], "wait_for_run")

        self.assertIn("/runs/submit-and-wait", paths)
        saw_op = paths["/runs/submit-and-wait"]["post"]
        self.assertEqual(saw_op["operationId"], "submit_and_wait")

        components = schema["components"]["schemas"]
        self.assertIn("WaitForRunRequest", components)
        self.assertIn("WaitForRunResponse", components)
        self.assertIn("SubmitAndWaitRequest", components)
        wait_response = components["WaitForRunResponse"]["properties"]
        for field in (
            "run_id",
            "timeout_seconds",
            "wait_expired",
            "reached_terminal",
            "status",
        ):
            self.assertIn(field, wait_response)
        saw_request = components["SubmitAndWaitRequest"]["properties"]
        self.assertIn("mission_yaml", saw_request)
        self.assertIn("timeout_seconds", saw_request)
        self.assertIn("poll_interval_seconds", saw_request)

    def test_openapi_json_endpoint_includes_servers(self) -> None:
        # Match other tests in this module: avoid lifespan context that can
        # collide with a closed shared SQLite connection after prior clients.
        client = TestClient(app)
        response = client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        schema = response.json()
        self.assertEqual(
            schema.get("servers"),
            [
                {
                    "url": (
                        "https://mission-control-production-76ff.up.railway.app"
                    )
                }
            ],
        )
        operation_ids = {
            method_obj.get("operationId")
            for path_item in schema["paths"].values()
            for method_obj in path_item.values()
            if isinstance(method_obj, dict)
        }
        self.assertIn("wait_for_run", operation_ids)
        self.assertIn("submit_and_wait", operation_ids)


if __name__ == "__main__":
    unittest.main()
