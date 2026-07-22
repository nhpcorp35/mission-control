"""Focused tests for asynchronous run executor lifecycle logging hooks."""

from __future__ import annotations

import logging
import os
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import app.api as api_module
from app.api import app
from mission_control.executor import (
    CURSOR_AGENT,
    execute_cursor_agent,
)
from mission_control.run_queue import RunQueue
from mission_control.run_registry import RunRegistry, RunStatus
from mission_control.workspace import PersistenceResult, WorkspacePrepResult

REPO_ROOT = Path(__file__).resolve().parent.parent
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

LIFECYCLE_LOGGERS = (
    "app.api",
    "mission_control.run_registry",
    "mission_control.run_queue",
    "mission_control.workspace",
    "mission_control.executor",
)


def _executable_mission_yaml(mission_id: str = "2026-07-22-lifecycle") -> str:
    return textwrap.dedent(
        f"""
        version: 1.0
        mission_id: {mission_id}
        title: Lifecycle Instrumentation Test
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


def _mock_proc(
    *,
    returncode: int = 0,
    stdout: str = "ok\n",
    stderr: str = "",
    pid: int = 7777,
) -> MagicMock:
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


def _log_text(records: list[logging.LogRecord]) -> str:
    return "\n".join(record.getMessage() for record in records)


class TestExecutorSubprocessLifecycleLogs(unittest.TestCase):
    @patch("mission_control.executor.find_cursor_agent_binary", return_value=CURSOR_AGENT)
    @patch("mission_control.executor.subprocess.Popen")
    def test_success_emits_subprocess_lifecycle_events(
        self,
        mock_popen,
        _mock_binary,
    ) -> None:
        mock_popen.return_value = _mock_proc(stdout="done\n", pid=5555)
        mission = {
            "mission_id": "life-ok",
            "title": "ok",
            "instructions": "go",
            "deliverables": [],
            "repository": {"path": str(REPO_ROOT)},
            "permissions": {"create_files": True, "modify_files": False},
        }

        with self.assertLogs("mission_control.executor", level="INFO") as captured:
            result = execute_cursor_agent(mission, run_id="run-success")

        self.assertTrue(result.ok)
        self.assertEqual(result.return_code, 0)
        text = _log_text(captured.records)
        for event in (
            "event=subprocess_create_start",
            "event=subprocess_created",
            "event=subprocess_wait_start",
            "event=subprocess_completed",
        ):
            self.assertIn(event, text)
        self.assertIn("run_id=run-success", text)
        self.assertIn("child_pid=5555", text)
        self.assertIn(f"api_pid={os.getpid()}", text)
        self.assertIn("returncode=0", text)
        # Never dump the full instruction / mission YAML into lifecycle logs.
        self.assertNotIn("Create a file.", text)

    @patch("mission_control.executor.find_cursor_agent_binary", return_value=CURSOR_AGENT)
    @patch("mission_control.executor.subprocess.Popen")
    def test_failure_emits_completion_and_bounds_error(
        self,
        mock_popen,
        _mock_binary,
    ) -> None:
        huge = "SECRET_TOKEN=" + ("x" * 800)
        mock_popen.return_value = _mock_proc(
            returncode=9,
            stdout="",
            stderr=huge,
            pid=6666,
        )
        mission = {
            "mission_id": "life-fail",
            "title": "fail",
            "instructions": "go",
            "deliverables": [],
            "repository": {"path": str(REPO_ROOT)},
            "permissions": {"create_files": True, "modify_files": False},
        }

        with self.assertLogs("mission_control.executor", level="INFO") as captured:
            result = execute_cursor_agent(mission, run_id="run-fail")

        self.assertFalse(result.ok)
        self.assertEqual(result.return_code, 9)
        text = _log_text(captured.records)
        self.assertIn("event=subprocess_completed", text)
        self.assertIn("returncode=9", text)
        self.assertIn("child_pid=6666", text)
        self.assertIn("...[truncated]", text)
        self.assertNotIn(huge, text)


class TestAsyncRunLifecycleInstrumentation(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        api_module.run_registry = RunRegistry(self._db_path, recover=False)
        api_module.run_queue = RunQueue()
        api_module.run_queue.configure(api_module._execute_queued_run)
        self.client = TestClient(app, headers=AUTH_HEADERS)

    def tearDown(self) -> None:
        api_module.run_registry.close()
        os.unlink(self._db_path)

    def _wait_for_terminal(self, run_id: str, timeout: float = 3.0) -> dict:
        deadline = time.time() + timeout
        body: dict | None = None
        while time.time() < deadline:
            response = self.client.get(f"/runs/{run_id}")
            self.assertEqual(response.status_code, 200)
            body = response.json()
            if body["status"] in TERMINAL_STATUSES:
                return body
            time.sleep(0.01)
        self.fail(f"run {run_id} did not reach a terminal status; last={body}")

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    @patch("app.api.preflight_for_execution", return_value=None)
    def test_success_path_emits_lifecycle_hooks(
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
        mock_execute.return_value = MagicMock(
            ok=True,
            stdout="done\n",
            stderr="",
            error=None,
            return_code=0,
        )
        mock_persist.return_value = PersistenceResult(
            ok=True,
            commit_sha="abc123",
        )

        with self.assertLogs(level="INFO") as captured:
            for name in LIFECYCLE_LOGGERS:
                logging.getLogger(name).setLevel(logging.INFO)
            response = self.client.post(
                "/runs",
                json={"mission_yaml": _executable_mission_yaml("life-api-ok")},
            )
            self.assertEqual(response.status_code, 202)
            run_id = response.json()["run_id"]
            body = self._wait_for_terminal(run_id)

        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["commit_sha"], "abc123")
        text = _log_text(captured.records)
        for event in (
            "event=run_record_created",
            "event=accepted",
            "event=worker_scheduled",
            "event=worker_entered",
            "event=started",
            "event=final_status_update",
            "event=finished",
        ):
            self.assertIn(event, text)
        self.assertIn(f"run_id={run_id}", text)
        self.assertIn(f"api_pid={os.getpid()}", text)
        self.assertIn("registry_id=", text)
        self.assertIn("registry_count=", text)
        mock_execute.assert_called_once()
        self.assertEqual(mock_execute.call_args.kwargs.get("run_id"), run_id)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    @patch("app.api.preflight_for_execution", return_value=None)
    def test_subprocess_failure_path_emits_hooks_without_api_change(
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
        mock_execute.return_value = MagicMock(
            ok=False,
            stdout="partial",
            stderr="agent boom",
            error="agent boom",
            return_code=11,
        )

        with self.assertLogs(level="INFO") as captured:
            for name in LIFECYCLE_LOGGERS:
                logging.getLogger(name).setLevel(logging.INFO)
            response = self.client.post(
                "/runs",
                json={"mission_yaml": _executable_mission_yaml("life-api-fail")},
            )
            self.assertEqual(response.status_code, 202)
            run_id = response.json()["run_id"]
            body = self._wait_for_terminal(run_id)

        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "agent boom")
        self.assertEqual(body["return_code"], 11)
        text = _log_text(captured.records)
        self.assertIn("event=run_record_created", text)
        self.assertIn("event=worker_entered", text)
        self.assertIn("event=final_status_update", text)
        self.assertIn("status=failed", text)
        self.assertIn("event=finished", text)
        self.assertIn("has_error=True", text)
        mock_persist.assert_not_called()
        # Polling still finds the retained failed record (no 404).
        follow_up = self.client.get(f"/runs/{run_id}")
        self.assertEqual(follow_up.status_code, 200)
        self.assertEqual(follow_up.json()["status"], "failed")


if __name__ == "__main__":
    unittest.main()
