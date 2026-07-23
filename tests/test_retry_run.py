"""Focused regression tests for POST /runs/{run_id}/retry."""

from __future__ import annotations

import os
import tempfile
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


def _executable_mission_yaml(mission_id: str = "2026-07-23-retry") -> str:
    return textwrap.dedent(
        f"""
        version: 1.0
        mission_id: {mission_id}
        title: Async Retry Test
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


class TestRetryRunApi(unittest.TestCase):
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

    def _submit_failed_run(self, mission_yaml: str) -> tuple[str, dict]:
        with patch("mission_control.workspace.cleanup_workspace"), patch(
            "mission_control.workspace.persist_workspace_changes"
        ) as mock_persist, patch(
            "mission_control.workspace.execute_cursor_agent"
        ) as mock_execute, patch(
            "mission_control.workspace.prepare_isolated_workspace"
        ) as mock_prepare, patch(
            "app.api.preflight_for_execution", return_value=None
        ):
            mock_prepare.return_value = WorkspacePrepResult(
                ok=True,
                workspace_path="/tmp/workspace",
            )
            mock_execute.return_value = ExecutionResult(
                ok=False,
                stdout="partial",
                stderr="agent failed",
                error="agent failed",
                return_code=1,
            )
            mock_persist.return_value = PersistenceResult(
                ok=True, commit_sha=None
            )
            submit = self.client.post(
                "/runs",
                json={"mission_yaml": mission_yaml},
            )
            self.assertEqual(submit.status_code, 202)
            run_id = submit.json()["run_id"]
            body = self._wait_for_terminal(run_id)
            self.assertEqual(body["status"], "failed")
            return run_id, body

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    @patch("app.api.preflight_for_execution", return_value=None)
    def test_retry_failed_run_creates_new_run_with_exact_yaml(
        self,
        _mock_preflight,
        mock_prepare,
        mock_execute,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        mission_yaml = _executable_mission_yaml("retry-success")
        source_id, source_body = self._submit_failed_run(mission_yaml)
        source_error = source_body["error"]
        source_stdout = source_body["stdout"]

        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path="/tmp/workspace-retry",
        )
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout="retry ok\n",
            return_code=0,
        )
        mock_persist.return_value = PersistenceResult(
            ok=True, commit_sha=None
        )

        retry = self.client.post(f"/runs/{source_id}/retry")
        self.assertEqual(retry.status_code, 202)
        retry_body = retry.json()
        self.assertEqual(retry_body["status"], "queued")
        self.assertIn("run_id", retry_body)
        new_id = retry_body["run_id"]
        self.assertNotEqual(new_id, source_id)

        completed = self._wait_for_terminal(new_id)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["retried_from"], source_id)
        self.assertEqual(completed["stdout"], "retry ok\n")

        source_after = self.client.get(f"/runs/{source_id}")
        self.assertEqual(source_after.status_code, 200)
        source_after_body = source_after.json()
        self.assertEqual(source_after_body["status"], "failed")
        self.assertEqual(source_after_body["error"], source_error)
        self.assertEqual(source_after_body["stdout"], source_stdout)
        self.assertIsNone(source_after_body["retried_from"])

        source_record = api_module.run_registry.get_run(source_id)
        new_record = api_module.run_registry.get_run(new_id)
        assert source_record is not None
        assert new_record is not None
        self.assertEqual(source_record.mission_yaml, mission_yaml)
        self.assertEqual(new_record.mission_yaml, mission_yaml)
        self.assertEqual(new_record.mission_yaml, source_record.mission_yaml)
        self.assertEqual(new_record.retried_from, source_id)
        self.assertIsNone(source_record.retried_from)
        mock_execute.assert_called()

    def test_retried_from_persists_across_registry_reopen(self) -> None:
        mission_yaml = _executable_mission_yaml("retry-durable")
        source_id, _ = self._submit_failed_run(mission_yaml)

        with patch("app.api.preflight_for_execution", return_value=None), patch(
            "mission_control.workspace.prepare_isolated_workspace"
        ) as mock_prepare, patch(
            "mission_control.workspace.execute_cursor_agent"
        ) as mock_execute, patch(
            "mission_control.workspace.persist_workspace_changes"
        ) as mock_persist, patch(
            "mission_control.workspace.cleanup_workspace"
        ):
            mock_prepare.return_value = WorkspacePrepResult(
                ok=True,
                workspace_path="/tmp/workspace-durable",
            )
            mock_execute.return_value = ExecutionResult(
                ok=True, stdout="ok\n", return_code=0
            )
            mock_persist.return_value = PersistenceResult(
                ok=True, commit_sha=None
            )
            retry = self.client.post(f"/runs/{source_id}/retry")
            self.assertEqual(retry.status_code, 202)
            new_id = retry.json()["run_id"]
            self._wait_for_terminal(new_id)

        api_module.run_registry.close()
        reopened = RunRegistry(self._db_path)
        try:
            fetched = reopened.get_run(new_id)
            assert fetched is not None
            self.assertEqual(fetched.retried_from, source_id)
            self.assertEqual(fetched.mission_yaml, mission_yaml)
            source = reopened.get_run(source_id)
            assert source is not None
            self.assertEqual(source.status, RunStatus.FAILED)
            self.assertEqual(source.mission_yaml, mission_yaml)
            self.assertIsNone(source.retried_from)
        finally:
            reopened.close()
            api_module.run_registry = RunRegistry(self._db_path)

    def test_retry_unknown_run_returns_404(self) -> None:
        response = self.client.post("/runs/missing-run-id/retry")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Run not found")

    def test_retry_rejects_non_failed_statuses(self) -> None:
        mission_yaml = _executable_mission_yaml("retry-reject")

        # queued
        queued = api_module.run_registry.create_run(
            mission_yaml=mission_yaml
        )
        response = self.client.post(f"/runs/{queued.run_id}/retry")
        self.assertEqual(response.status_code, 409)
        self.assertIn("queued", response.json()["detail"])

        # running
        running = api_module.run_registry.create_run(
            mission_yaml=mission_yaml
        )
        api_module.run_registry.update_status(
            running.run_id, RunStatus.RUNNING
        )
        response = self.client.post(f"/runs/{running.run_id}/retry")
        self.assertEqual(response.status_code, 409)
        self.assertIn("running", response.json()["detail"])

        # completed
        completed = api_module.run_registry.create_run(
            mission_yaml=mission_yaml
        )
        api_module.run_registry.update_status(
            completed.run_id, RunStatus.RUNNING
        )
        api_module.run_registry.update_status(
            completed.run_id, RunStatus.COMPLETED
        )
        response = self.client.post(f"/runs/{completed.run_id}/retry")
        self.assertEqual(response.status_code, 409)
        self.assertIn("completed", response.json()["detail"])

        # timed_out
        timed_out = api_module.run_registry.create_run(
            mission_yaml=mission_yaml
        )
        api_module.run_registry.update_status(
            timed_out.run_id, RunStatus.RUNNING
        )
        api_module.run_registry.update_status(
            timed_out.run_id, RunStatus.TIMED_OUT
        )
        response = self.client.post(f"/runs/{timed_out.run_id}/retry")
        self.assertEqual(response.status_code, 409)
        self.assertIn("timed_out", response.json()["detail"])

    def test_retry_rejects_failed_run_without_stored_yaml(self) -> None:
        legacy = api_module.run_registry.create_run()
        api_module.run_registry.update_status(
            legacy.run_id, RunStatus.RUNNING
        )
        api_module.run_registry.update_status(
            legacy.run_id, RunStatus.FAILED
        )
        response = self.client.post(f"/runs/{legacy.run_id}/retry")
        self.assertEqual(response.status_code, 409)
        self.assertIn("mission YAML", response.json()["detail"])

    def test_get_run_exposes_retried_from_in_openapi(self) -> None:
        schema = app.openapi()
        run_status = schema["components"]["schemas"]["RunStatusResponse"]
        self.assertIn("retried_from", run_status["properties"])
        paths = schema["paths"]
        self.assertIn("/runs/{run_id}/retry", paths)
        self.assertIn("post", paths["/runs/{run_id}/retry"])


class TestRetryRegistryPersistence(unittest.TestCase):
    def test_create_run_stores_mission_yaml_and_retried_from(self) -> None:
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        registry = RunRegistry(db_path)
        try:
            record = registry.create_run(
                mission_yaml="version: 1.0\n",
                retried_from="source-run-id",
            )
            fetched = registry.get_run(record.run_id)
            assert fetched is not None
            self.assertEqual(fetched.mission_yaml, "version: 1.0\n")
            self.assertEqual(fetched.retried_from, "source-run-id")
        finally:
            registry.close()
            os.unlink(db_path)

    def test_schema_migration_adds_retry_columns(self) -> None:
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        import sqlite3

        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    elapsed_seconds REAL,
                    stdout TEXT NOT NULL DEFAULT '',
                    stderr TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    return_code INTEGER,
                    commit_sha TEXT,
                    result_json TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

        registry = RunRegistry(db_path)
        try:
            record = registry.create_run(
                mission_yaml="mission: yaml\n",
                retried_from="legacy-source",
            )
            fetched = registry.get_run(record.run_id)
            assert fetched is not None
            self.assertEqual(fetched.mission_yaml, "mission: yaml\n")
            self.assertEqual(fetched.retried_from, "legacy-source")
        finally:
            registry.close()
            os.unlink(db_path)


if __name__ == "__main__":
    unittest.main()
