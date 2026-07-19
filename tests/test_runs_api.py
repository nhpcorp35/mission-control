"""Focused tests for asynchronous POST /runs and GET /runs/{run_id}."""
from __future__ import annotations
import os
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import app.api as api_module
from app.api import app
from mission_control.executor import ExecutionResult
from mission_control.run_registry import RunRegistry, RunStatus
REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE = REPO_ROOT / "missions" / "reference"
TEST_API_KEY = "mc_test_authentication_key"
AUTH_HEADERS = {
    "Authorization": f"Bearer {TEST_API_KEY}",
}
os.environ["MISSION_CONTROL_API_KEY"] = TEST_API_KEY
TERMINAL_STATUSES = {
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.TIMED_OUT.value,
}
def _executable_mission_yaml() -> str:
    return textwrap.dedent(
        f"""
        version: 1.0
        mission_id: 2026-07-19-runs
        title: Async Run Test
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
class TestRunsApi(unittest.TestCase):
    def setUp(self) -> None:
        api_module.run_registry = RunRegistry()
        self.client = TestClient(
            app,
            headers=AUTH_HEADERS,
        )
    def _wait_for_terminal(self, run_id: str, timeout: float = 2.0) -> dict:
        deadline = time.time() + timeout
        body: dict | None = None
        while time.time() < deadline:
            response = self.client.get(f"/runs/{run_id}")
            self.assertEqual(response.status_code, 200)
            body = response.json()
            if body["status"] in TERMINAL_STATUSES:
                return body
            time.sleep(0.01)
        self.fail(
            f"run {run_id} did not reach a terminal status; last={body}"
        )
    @patch("app.api.preflight_for_execution", return_value=None)
    @patch("app.api.execute_cursor_agent")
    def test_post_runs_accepts_and_returns_queued(
        self,
        mock_execute,
        _mock_preflight,
    ) -> None:
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout="done\n",
        )
        response = self.client.post(
            "/runs",
            json={"mission_yaml": _executable_mission_yaml()},
        )
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertIn("run_id", body)
        self.assertEqual(body["status"], "queued")
        self._wait_for_terminal(body["run_id"])
        mock_execute.assert_called_once()
    @patch("app.api.preflight_for_execution", return_value=None)
    @patch("app.api.execute_cursor_agent")
    def test_get_run_reports_completed(
        self,
        mock_execute,
        _mock_preflight,
    ) -> None:
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout="agent response\n",
        )
        submit = self.client.post(
            "/runs",
            json={"mission_yaml": _executable_mission_yaml()},
        )
        run_id = submit.json()["run_id"]
        body = self._wait_for_terminal(run_id)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["stdout"], "agent response\n")
        self.assertEqual(body["stderr"], "")
        self.assertIsNone(body["error"])
        self.assertIsNotNone(body["started_at"])
        self.assertIsNotNone(body["completed_at"])
        self.assertIsNotNone(body["elapsed_seconds"])
    @patch("app.api.preflight_for_execution", return_value=None)
    @patch("app.api.execute_cursor_agent")
    def test_get_run_reports_failed(
        self,
        mock_execute,
        _mock_preflight,
    ) -> None:
        mock_execute.return_value = ExecutionResult(
            ok=False,
            stderr="agent failed",
            error="agent failed",
        )
        submit = self.client.post(
            "/runs",
            json={"mission_yaml": _executable_mission_yaml()},
        )
        body = self._wait_for_terminal(submit.json()["run_id"])
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["stderr"], "agent failed")
        self.assertEqual(body["error"], "agent failed")
    @patch("app.api.preflight_for_execution", return_value=None)
    @patch("app.api.execute_cursor_agent")
    def test_get_run_reports_timed_out(
        self,
        mock_execute,
        _mock_preflight,
    ) -> None:
        mock_execute.return_value = ExecutionResult(
            ok=False,
            error="cursor-agent timed out after 600 seconds",
        )
        submit = self.client.post(
            "/runs",
            json={"mission_yaml": _executable_mission_yaml()},
        )
        body = self._wait_for_terminal(submit.json()["run_id"])
        self.assertEqual(body["status"], "timed_out")
        self.assertIn("timed out", body["error"])
    def test_post_runs_rejects_invalid_mission(self) -> None:
        mission_yaml = (REFERENCE / "invalid-bad-version.yaml").read_text(
            encoding="utf-8"
        )
        response = self.client.post(
            "/runs",
            json={"mission_yaml": mission_yaml},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertIn("Unsupported version", body["error"])
        self.assertEqual(len(api_module.run_registry._runs), 0)
    @patch("app.api.preflight_for_execution")
    def test_post_runs_rejects_preflight_failure(
        self,
        mock_preflight,
    ) -> None:
        from app.cursor_cli import StructuredError
        mock_preflight.return_value = StructuredError(
            code="CURSOR_API_KEY_MISSING",
            message="CURSOR_API_KEY environment variable is not set",
            stage="preflight",
        )
        response = self.client.post(
            "/runs",
            json={"mission_yaml": _executable_mission_yaml()},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(
            body["error_detail"]["code"],
            "CURSOR_API_KEY_MISSING",
        )
        self.assertEqual(len(api_module.run_registry._runs), 0)
    def test_get_unknown_run_returns_404(self) -> None:
        response = self.client.get("/runs/missing-run-id")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Run not found")
    def test_post_runs_requires_auth(self) -> None:
        client = TestClient(app)
        response = client.post(
            "/runs",
            json={"mission_yaml": _executable_mission_yaml()},
        )
        self.assertEqual(response.status_code, 401)
    def test_get_run_requires_auth(self) -> None:
        client = TestClient(app)
        response = client.get("/runs/some-id")
        self.assertEqual(response.status_code, 401)
if __name__ == "__main__":
    unittest.main()
