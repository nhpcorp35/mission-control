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
from mission_control.workspace import PersistenceResult, WorkspacePrepResult
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
    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    @patch("app.api.preflight_for_execution", return_value=None)
    def test_post_runs_accepts_and_returns_queued(
        self,
        _mock_preflight,
        mock_prepare,
        mock_execute,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path="/tmp/workspace",
        )
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout="done\n",
        )
        mock_persist.return_value = PersistenceResult(ok=True, commit_sha=None)
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
    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    @patch("app.api.preflight_for_execution", return_value=None)
    def test_status_polling_after_successful_completion(
        self,
        _mock_preflight,
        mock_prepare,
        mock_execute,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path="/tmp/workspace",
        )
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout="agent response\n",
            return_code=0,
        )
        mock_persist.return_value = PersistenceResult(ok=True, commit_sha=None)
        submit = self.client.post(
            "/runs",
            json={"mission_yaml": _executable_mission_yaml()},
        )
        self.assertEqual(submit.status_code, 202)
        run_id = submit.json()["run_id"]
        body = self._wait_for_terminal(run_id)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["stdout"], "agent response\n")
        self.assertIsNone(body["error"])
        self.assertEqual(body["return_code"], 0)
        self.assertIsNotNone(body["started_at"])
        self.assertIsNotNone(body["completed_at"])
        self.assertIn(run_id, api_module.run_registry._runs)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    @patch("app.api.preflight_for_execution", return_value=None)
    def test_status_polling_after_subprocess_failure(
        self,
        _mock_preflight,
        mock_prepare,
        mock_execute,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path="/tmp/workspace",
        )
        mock_execute.return_value = ExecutionResult(
            ok=False,
            stdout="partial out",
            stderr="cursor agent crashed",
            error="cursor agent crashed",
            return_code=1,
        )
        submit = self.client.post(
            "/runs",
            json={"mission_yaml": _executable_mission_yaml()},
        )
        run_id = submit.json()["run_id"]
        body = self._wait_for_terminal(run_id)
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["stdout"], "partial out")
        self.assertEqual(body["stderr"], "cursor agent crashed")
        self.assertEqual(body["error"], "cursor agent crashed")
        self.assertEqual(body["return_code"], 1)
        self.assertIsNotNone(body["completed_at"])
        mock_persist.assert_not_called()
        self.assertIn(run_id, api_module.run_registry._runs)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    @patch("app.api.preflight_for_execution", return_value=None)
    def test_retained_error_details_after_failure(
        self,
        _mock_preflight,
        mock_prepare,
        mock_execute,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path="/tmp/workspace",
        )
        mock_execute.return_value = ExecutionResult(
            ok=False,
            stdout="out-before-fail",
            stderr="stderr-detail",
            error="subprocess failed",
            return_code=42,
        )
        submit = self.client.post(
            "/runs",
            json={"mission_yaml": _executable_mission_yaml()},
        )
        run_id = submit.json()["run_id"]
        body = self._wait_for_terminal(run_id)
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "subprocess failed")
        self.assertEqual(body["stderr"], "stderr-detail")
        self.assertEqual(body["stdout"], "out-before-fail")
        self.assertEqual(body["return_code"], 42)
        self.assertIsNotNone(body["created_at"])
        self.assertIsNotNone(body["started_at"])
        self.assertIsNotNone(body["completed_at"])
        self.assertIsNotNone(body["elapsed_seconds"])
        stored = api_module.run_registry.get_run(run_id)
        assert stored is not None
        self.assertEqual(stored.status, RunStatus.FAILED)
        self.assertEqual(stored.error, "subprocess failed")
        self.assertEqual(stored.return_code, 42)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    @patch("app.api.preflight_for_execution", return_value=None)
    def test_repeated_get_returns_same_terminal_run(
        self,
        _mock_preflight,
        mock_prepare,
        mock_execute,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path="/tmp/workspace",
        )
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout="stable\n",
            return_code=0,
        )
        mock_persist.return_value = PersistenceResult(
            ok=True,
            commit_sha="abc123",
        )
        submit = self.client.post(
            "/runs",
            json={"mission_yaml": _executable_mission_yaml()},
        )
        run_id = submit.json()["run_id"]
        first = self._wait_for_terminal(run_id)
        self.assertEqual(first["status"], "completed")

        for _ in range(5):
            response = self.client.get(f"/runs/{run_id}")
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["run_id"], run_id)
            self.assertEqual(body["status"], "completed")
            self.assertEqual(body["stdout"], "stable\n")
            self.assertEqual(body["return_code"], 0)
            self.assertEqual(body["commit_sha"], "abc123")
            self.assertEqual(body["completed_at"], first["completed_at"])

        self.assertEqual(len(api_module.run_registry._runs), 1)
        self.assertIn(run_id, api_module.run_registry._runs)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    @patch("app.api.preflight_for_execution", return_value=None)
    def test_get_run_reports_completed(
        self,
        _mock_preflight,
        mock_prepare,
        mock_execute,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path="/tmp/workspace",
        )
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout="agent response\n",
        )
        mock_persist.return_value = PersistenceResult(ok=True, commit_sha=None)
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
        self.assertIsNone(body["commit_sha"])
    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    @patch("app.api.preflight_for_execution", return_value=None)
    def test_get_run_reports_failed(
        self,
        _mock_preflight,
        mock_prepare,
        mock_execute,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path="/tmp/workspace",
        )
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
    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    @patch("app.api.preflight_for_execution", return_value=None)
    def test_get_run_reports_timed_out(
        self,
        _mock_preflight,
        mock_prepare,
        mock_execute,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path="/tmp/workspace",
        )
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
