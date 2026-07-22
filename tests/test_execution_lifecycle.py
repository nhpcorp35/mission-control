"""Regression tests for FIFO single-active asynchronous run execution."""

from __future__ import annotations

import os
import threading
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.api as api_module
from app.api import app
from app.cursor_cli import (
    RECURSIVE_SUBMISSIONS_ENV,
    cursor_cli_env,
)
from mission_control.executor import ExecutionResult
from mission_control.recursion import (
    RECURSIVE_SUBMISSION_ERROR,
    RECURSIVE_SUBMISSION_HEADER,
    enter_execution,
    exit_execution,
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


def _executable_mission_yaml(mission_id: str = "2026-07-22-queue") -> str:
    return textwrap.dedent(
        f"""
        version: 1.0
        mission_id: {mission_id}
        title: Async Queue Test
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


class TestRunQueueUnit(unittest.TestCase):
    def test_fifo_order_and_single_active(self) -> None:
        queue = RunQueue()
        started: list[str] = []
        finished: list[str] = []
        release_first = threading.Event()
        second_started = threading.Event()

        def execute(run_id: str, _mission: dict, _registry: object) -> None:
            started.append(run_id)
            if run_id == "run-a":
                # Hold the active slot until the test releases it.
                release_first.wait(timeout=2.0)
            else:
                second_started.set()
            finished.append(run_id)

        queue.configure(execute)
        queue.enqueue("run-a", {"id": "a"}, registry=None)
        queue.enqueue("run-b", {"id": "b"}, registry=None)

        deadline = time.time() + 2.0
        while time.time() < deadline and started != ["run-a"]:
            time.sleep(0.01)
        self.assertEqual(started, ["run-a"])
        self.assertEqual(queue.active_run_id, "run-a")
        self.assertEqual(queue.pending_run_ids(), ["run-b"])

        release_first.set()
        self.assertTrue(second_started.wait(timeout=2.0))
        deadline = time.time() + 2.0
        while time.time() < deadline and finished != ["run-a", "run-b"]:
            time.sleep(0.01)
        self.assertEqual(finished, ["run-a", "run-b"])
        self.assertIsNone(queue.active_run_id)
        self.assertEqual(queue.pending_count(), 0)


class TestRunsLifecycleApi(unittest.TestCase):
    def setUp(self) -> None:
        api_module.run_registry = RunRegistry()
        api_module.run_queue = RunQueue()
        api_module.run_queue.configure(api_module._execute_queued_run)
        self.client = TestClient(app, headers=AUTH_HEADERS)

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
        self.fail(
            f"run {run_id} did not reach a terminal status; last={body}"
        )

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    @patch("app.api.preflight_for_execution", return_value=None)
    def test_queueing_keeps_second_run_queued_while_first_active(
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
        mock_persist.return_value = PersistenceResult(ok=True, commit_sha=None)

        release_first = threading.Event()
        second_may_run = threading.Event()
        call_order: list[str] = []
        lock = threading.Lock()

        def slow_execute(mission: dict) -> ExecutionResult:
            mission_id = str(mission.get("mission_id", ""))
            with lock:
                call_order.append(mission_id)
            if mission_id.endswith("first"):
                release_first.wait(timeout=2.0)
            else:
                second_may_run.set()
            return ExecutionResult(ok=True, stdout=f"{mission_id}\n", return_code=0)

        mock_execute.side_effect = slow_execute

        first = self.client.post(
            "/runs",
            json={"mission_yaml": _executable_mission_yaml("queue-first")},
        )
        second = self.client.post(
            "/runs",
            json={"mission_yaml": _executable_mission_yaml("queue-second")},
        )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        first_id = first.json()["run_id"]
        second_id = second.json()["run_id"]

        deadline = time.time() + 2.0
        while time.time() < deadline:
            first_body = self.client.get(f"/runs/{first_id}").json()
            second_body = self.client.get(f"/runs/{second_id}").json()
            if first_body["status"] == "running" and second_body["status"] == "queued":
                break
            time.sleep(0.01)
        else:
            self.fail("expected first running and second queued")

        release_first.set()
        self.assertTrue(second_may_run.wait(timeout=2.0))
        first_done = self._wait_for_terminal(first_id)
        second_done = self._wait_for_terminal(second_id)
        self.assertEqual(first_done["status"], "completed")
        self.assertEqual(second_done["status"], "completed")
        self.assertEqual(call_order, ["queue-first", "queue-second"])

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    @patch("app.api.preflight_for_execution", return_value=None)
    def test_sequential_execution_never_overlaps(
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
        mock_persist.return_value = PersistenceResult(ok=True, commit_sha=None)

        active = 0
        max_active = 0
        lock = threading.Lock()

        def tracked_execute(_mission: dict) -> ExecutionResult:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return ExecutionResult(ok=True, stdout="ok\n", return_code=0)

        mock_execute.side_effect = tracked_execute

        run_ids = []
        for index in range(3):
            response = self.client.post(
                "/runs",
                json={
                    "mission_yaml": _executable_mission_yaml(
                        f"seq-{index}"
                    )
                },
            )
            self.assertEqual(response.status_code, 202)
            run_ids.append(response.json()["run_id"])

        for run_id in run_ids:
            body = self._wait_for_terminal(run_id)
            self.assertEqual(body["status"], "completed")
            self.assertEqual(
                self.client.get(f"/runs/{run_id}").status_code,
                200,
            )

        self.assertEqual(max_active, 1)
        self.assertEqual(mock_execute.call_count, 3)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    @patch("app.api.preflight_for_execution", return_value=None)
    def test_retained_terminal_states_do_not_404(
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
        mock_execute.side_effect = [
            ExecutionResult(ok=True, stdout="done\n", return_code=0),
            ExecutionResult(
                ok=False,
                stdout="partial",
                stderr="boom",
                error="boom",
                return_code=9,
            ),
        ]
        mock_persist.return_value = PersistenceResult(
            ok=True,
            commit_sha="abc123",
        )

        completed = self.client.post(
            "/runs",
            json={"mission_yaml": _executable_mission_yaml("retain-ok")},
        ).json()["run_id"]
        failed = self.client.post(
            "/runs",
            json={"mission_yaml": _executable_mission_yaml("retain-fail")},
        ).json()["run_id"]

        completed_body = self._wait_for_terminal(completed)
        failed_body = self._wait_for_terminal(failed)
        self.assertEqual(completed_body["status"], "completed")
        self.assertEqual(failed_body["status"], "failed")

        for _ in range(3):
            ok_response = self.client.get(f"/runs/{completed}")
            fail_response = self.client.get(f"/runs/{failed}")
            self.assertEqual(ok_response.status_code, 200)
            self.assertEqual(fail_response.status_code, 200)
            self.assertEqual(ok_response.json()["status"], "completed")
            self.assertEqual(fail_response.json()["status"], "failed")
            self.assertEqual(fail_response.json()["error"], "boom")
            self.assertEqual(fail_response.json()["return_code"], 9)

        self.assertIn(completed, api_module.run_registry._runs)
        self.assertIn(failed, api_module.run_registry._runs)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    @patch("app.api.preflight_for_execution", return_value=None)
    def test_failure_details_preserved(
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
            stdout="out",
            stderr="err-detail",
            error="agent crashed",
            return_code=42,
        )

        run_id = self.client.post(
            "/runs",
            json={"mission_yaml": _executable_mission_yaml("fail-detail")},
        ).json()["run_id"]
        body = self._wait_for_terminal(run_id)
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["stdout"], "out")
        self.assertEqual(body["stderr"], "err-detail")
        self.assertEqual(body["error"], "agent crashed")
        self.assertEqual(body["return_code"], 42)
        mock_persist.assert_not_called()

        stored = api_module.run_registry.get_run(run_id)
        assert stored is not None
        self.assertEqual(stored.error, "agent crashed")
        self.assertEqual(stored.return_code, 42)

    def test_recursion_guard_blocks_nested_local_submission(self) -> None:
        from mission_control.recursion import is_recursive_submission
        from starlette.requests import Request

        enter_execution()
        try:
            self.assertTrue(is_recursive_submission())
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "path": "/runs",
                "raw_path": b"/runs",
                "root_path": "",
                "scheme": "http",
                "query_string": b"",
                "headers": [
                    (b"authorization", f"Bearer {TEST_API_KEY}".encode("utf-8")),
                    (b"content-type", b"application/json"),
                ],
                "client": ("127.0.0.1", 123),
                "server": ("test", 80),
            }

            async def receive() -> dict:
                return {"type": "http.request", "body": b"", "more_body": False}

            raw_request = Request(scope, receive)
            from app.api import MissionYamlRequest, submit_run_endpoint

            response = submit_run_endpoint(
                MissionYamlRequest(mission_yaml=_executable_mission_yaml("nested")),
                raw_request,
                _auth=None,
            )
        finally:
            exit_execution()

        self.assertEqual(response.status_code, 200)
        body = response.body
        import json

        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], RECURSIVE_SUBMISSION_ERROR)
        self.assertEqual(payload["error_detail"]["code"], "RECURSIVE_SUBMISSION")
        self.assertEqual(len(api_module.run_registry._runs), 0)

    def test_recursion_guard_blocks_explicit_header(self) -> None:
        response = self.client.post(
            "/runs",
            json={"mission_yaml": _executable_mission_yaml("header-nested")},
            headers={RECURSIVE_SUBMISSION_HEADER: "nested"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertIn("Recursive", body["error"])
        self.assertEqual(len(api_module.run_registry._runs), 0)

    def test_cursor_env_strips_mission_control_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MISSION_CONTROL_API_KEY": "secret-key",
                "MISSION_CONTROL_URL": "http://127.0.0.1:8000",
                "PATH": "/usr/bin",
            },
            clear=False,
        ):
            env = cursor_cli_env()
        self.assertNotIn("MISSION_CONTROL_API_KEY", env)
        self.assertNotIn("MISSION_CONTROL_URL", env)
        self.assertEqual(env[RECURSIVE_SUBMISSIONS_ENV], "blocked")


if __name__ == "__main__":
    unittest.main()
