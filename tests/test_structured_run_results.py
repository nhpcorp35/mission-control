"""Focused regression tests for structured async run results."""

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
from mission_control.run_result import (
    CommandEvidence,
    DeliverableEvidence,
    PersistenceEvidence,
    StructuredRunResult,
    WARNING_NO_TEST_COUNTS,
    deserialize_structured_result,
    empty_structured_result,
    parse_git_status_porcelain_paths,
    serialize_structured_result,
)
from mission_control.workspace import (
    PersistenceResult,
    WorkspacePrepResult,
    collect_deliverable_evidence,
    execute_registered_run,
)
from tests.registry_test_utils import SqliteRegistryTestCase

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

LEGACY_RUN_STATUS_FIELDS = {
    "run_id",
    "status",
    "created_at",
    "started_at",
    "completed_at",
    "elapsed_seconds",
    "stdout",
    "stderr",
    "error",
    "return_code",
    "commit_sha",
}


def _executable_mission_yaml(*, deliverables: list[str] | None = None) -> str:
    deliverable_lines = "\n".join(
        f"          - {item}" for item in (deliverables or ["summary"])
    )
    return textwrap.dedent(
        f"""
        version: 1.0
        mission_id: 2026-07-23-structured-result
        title: Structured Result Test
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
        persistence:
          mode: commit
        instructions: |
          Create a file.
        deliverables:
{deliverable_lines}
        approval:
          execute_without_approval: true
          commit_requires_approval: true
          push_requires_approval: true
        """
    )


def _base_mission(*, deliverables: list[str] | None = None) -> dict:
    return {
        "version": "1.0",
        "mission_id": "structured-result",
        "title": "Structured Result",
        "repository": {
            "name": "Mission-Control",
            "path": str(REPO_ROOT),
            "base_branch": "main",
        },
        "execution": {
            "agent": "cursor",
            "mode": "execute",
            "sandbox": True,
            "worktree": False,
        },
        "permissions": {
            "read": True,
            "create_files": True,
            "modify_files": False,
            "delete_files": False,
            "run_commands": True,
            "stage_changes": False,
            "commit": False,
            "push": False,
        },
        "persistence": {"mode": "commit"},
        "instructions": "Create a file.",
        "deliverables": deliverables or ["summary"],
        "approval": {
            "execute_without_approval": True,
            "commit_requires_approval": True,
            "push_requires_approval": True,
        },
    }


class TestPorcelainParsing(unittest.TestCase):
    def test_parse_git_status_porcelain_paths(self) -> None:
        stdout = textwrap.dedent(
            """\
            M  mission_control/run_result.py
            ?? docs/HAL_OPERATOR_LOG.md
            R  old.txt -> new.txt
            """
        )
        self.assertEqual(
            parse_git_status_porcelain_paths(stdout),
            [
                "docs/HAL_OPERATOR_LOG.md",
                "mission_control/run_result.py",
                "new.txt",
            ],
        )


class TestStructuredResultSerialization(SqliteRegistryTestCase):
    def test_serialize_round_trip_and_registry_persistence(self) -> None:
        structured = StructuredRunResult(
            files_changed=["a.py", "b.md"],
            commands=[
                CommandEvidence(
                    argv=["cursor-agent", "--force", "<instruction>"],
                    exit_code=0,
                    passed=True,
                    kind="cursor_agent",
                )
            ],
            test_counts=None,
            deliverables=DeliverableEvidence(
                verified=True,
                passed=True,
                checked_paths=["b.md"],
                missing=[],
            ),
            persistence=PersistenceEvidence(
                mode="commit",
                attempted=True,
                ok=True,
                commit_sha="deadbeef",
            ),
            warnings=[WARNING_NO_TEST_COUNTS],
        )
        raw = serialize_structured_result(structured)
        self.assertIsInstance(raw, str)
        restored = deserialize_structured_result(raw)
        assert restored is not None
        self.assertEqual(restored.files_changed, ["a.py", "b.md"])
        self.assertEqual(restored.commands[0].exit_code, 0)
        self.assertTrue(restored.commands[0].passed)
        self.assertEqual(restored.persistence.commit_sha, "deadbeef")
        self.assertIsNone(restored.test_counts)

        record = self.registry.create_run()
        self.registry.store_result(
            record.run_id,
            stdout="agent prose",
            stderr="",
            return_code=0,
            commit_sha="deadbeef",
            result=structured,
        )
        self.registry.update_status(record.run_id, RunStatus.COMPLETED)
        self.registry.close()

        reloaded = RunRegistry(self._db_path)
        try:
            fetched = reloaded.get_run(record.run_id)
            assert fetched is not None
            self.assertEqual(fetched.stdout, "agent prose")
            self.assertEqual(fetched.commit_sha, "deadbeef")
            assert fetched.result is not None
            self.assertEqual(fetched.result.files_changed, ["a.py", "b.md"])
            self.assertEqual(fetched.result.persistence.commit_sha, "deadbeef")
            self.assertEqual(fetched.result.commands[0].kind, "cursor_agent")
        finally:
            reloaded.close()


class TestDeliverableEvidenceCollection(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmpdir.name)
        (self.workspace / "docs").mkdir()
        (self.workspace / "docs" / "out.txt").write_text("ok\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_declared_deliverable_verification_evidence(self) -> None:
        mission = {
            "deliverables": ["docs/out.txt", "missing.txt", "summary"],
        }
        evidence = collect_deliverable_evidence(mission, str(self.workspace))
        self.assertTrue(evidence.verified)
        self.assertFalse(evidence.passed)
        self.assertEqual(evidence.checked_paths, ["docs/out.txt", "missing.txt"])
        self.assertEqual(evidence.missing, ["missing.txt"])


class TestExecuteRegisteredRunStructuredResult(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)

    def tearDown(self) -> None:
        self.registry.close()
        os.unlink(self._db_path)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.collect_changed_files")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    def test_successful_run_includes_files_commit_and_command_evidence(
        self,
        mock_prepare,
        mock_execute,
        mock_changed,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        workspace = tempfile.mkdtemp(prefix="mc-structured-")
        (Path(workspace) / "docs").mkdir()
        (Path(workspace) / "docs" / "out.txt").write_text("x\n", encoding="utf-8")
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path=workspace,
        )
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout="agent prose claiming success\n",
            return_code=0,
            command=[
                "cursor-agent",
                "--print",
                "--force",
                "--workspace",
                workspace,
                "--trust",
                "<instruction>",
            ],
        )
        mock_changed.return_value = (
            ["docs/out.txt", "mission_control/run_result.py"],
            None,
        )
        mock_persist.return_value = PersistenceResult(
            ok=True,
            commit_sha="abc123def456",
        )

        record = self.registry.create_run()
        mission = _base_mission(deliverables=["docs/out.txt"])
        execute_registered_run(record.run_id, mission, self.registry)

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        self.assertEqual(updated.status, RunStatus.COMPLETED)
        self.assertEqual(updated.commit_sha, "abc123def456")
        self.assertEqual(updated.stdout, "agent prose claiming success\n")
        assert updated.result is not None
        self.assertEqual(
            updated.result.files_changed,
            ["docs/out.txt", "mission_control/run_result.py"],
        )
        self.assertEqual(len(updated.result.commands), 1)
        command = updated.result.commands[0]
        self.assertEqual(command.kind, "cursor_agent")
        self.assertEqual(command.exit_code, 0)
        self.assertTrue(command.passed)
        self.assertIn("cursor-agent", command.argv[0])
        self.assertIsNone(updated.result.test_counts)
        assert updated.result.deliverables is not None
        self.assertTrue(updated.result.deliverables.passed)
        self.assertEqual(
            updated.result.deliverables.checked_paths,
            ["docs/out.txt"],
        )
        self.assertEqual(updated.result.deliverables.missing, [])
        assert updated.result.persistence is not None
        self.assertTrue(updated.result.persistence.attempted)
        self.assertTrue(updated.result.persistence.ok)
        self.assertEqual(updated.result.persistence.commit_sha, "abc123def456")
        self.assertEqual(updated.result.persistence.mode, "commit")
        self.assertIn(WARNING_NO_TEST_COUNTS, updated.result.warnings)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.collect_changed_files")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    def test_failed_run_retains_partial_evidence(
        self,
        mock_prepare,
        mock_execute,
        mock_changed,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        workspace = tempfile.mkdtemp(prefix="mc-structured-fail-")
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path=workspace,
        )
        mock_execute.return_value = ExecutionResult(
            ok=False,
            stdout="partial out",
            stderr="boom",
            error="boom",
            return_code=1,
            command=["cursor-agent", "--force", "<instruction>"],
        )
        mock_changed.return_value = (["partial.txt"], None)

        record = self.registry.create_run()
        execute_registered_run(
            record.run_id,
            _base_mission(deliverables=["docs/out.txt"]),
            self.registry,
        )

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        self.assertEqual(updated.status, RunStatus.FAILED)
        self.assertEqual(updated.stdout, "partial out")
        self.assertEqual(updated.return_code, 1)
        assert updated.result is not None
        self.assertEqual(updated.result.files_changed, ["partial.txt"])
        self.assertEqual(updated.result.commands[0].exit_code, 1)
        self.assertFalse(updated.result.commands[0].passed)
        assert updated.result.deliverables is not None
        self.assertFalse(updated.result.deliverables.verified)
        assert updated.result.persistence is not None
        self.assertFalse(updated.result.persistence.attempted)
        mock_persist.assert_not_called()

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.collect_changed_files")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    def test_missing_deliverable_recorded_in_structured_result(
        self,
        mock_prepare,
        mock_execute,
        mock_changed,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        workspace = tempfile.mkdtemp(prefix="mc-structured-deliv-")
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path=workspace,
        )
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout="done\n",
            return_code=0,
            command=["cursor-agent", "<instruction>"],
        )
        mock_changed.return_value = (["other.txt"], None)

        record = self.registry.create_run()
        execute_registered_run(
            record.run_id,
            _base_mission(deliverables=["missing-output.txt"]),
            self.registry,
        )

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        self.assertEqual(updated.status, RunStatus.FAILED)
        self.assertIn("Missing declared file deliverable", updated.error or "")
        assert updated.result is not None
        assert updated.result.deliverables is not None
        self.assertTrue(updated.result.deliverables.verified)
        self.assertFalse(updated.result.deliverables.passed)
        self.assertEqual(
            updated.result.deliverables.missing,
            ["missing-output.txt"],
        )
        mock_persist.assert_not_called()


class TestStructuredResultApi(unittest.TestCase):
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
        self.fail(f"run {run_id} did not reach a terminal status; last={body}")

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.collect_changed_files")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    @patch("app.api.preflight_for_execution", return_value=None)
    def test_get_run_exposes_structured_result_and_keeps_legacy_fields(
        self,
        _mock_preflight,
        mock_prepare,
        mock_execute,
        mock_changed,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        workspace = tempfile.mkdtemp(prefix="mc-structured-api-")
        (Path(workspace) / "created.txt").write_text("hi\n", encoding="utf-8")
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path=workspace,
        )
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout="agent response\n",
            return_code=0,
            command=["cursor-agent", "--force", "<instruction>"],
        )
        mock_changed.return_value = (["created.txt"], None)
        mock_persist.return_value = PersistenceResult(
            ok=True,
            commit_sha="abc123",
        )

        submit = self.client.post(
            "/runs",
            json={
                "mission_yaml": _executable_mission_yaml(
                    deliverables=["created.txt"]
                )
            },
        )
        self.assertEqual(submit.status_code, 202)
        run_id = submit.json()["run_id"]
        body = self._wait_for_terminal(run_id)

        for field in LEGACY_RUN_STATUS_FIELDS:
            self.assertIn(field, body)

        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["stdout"], "agent response\n")
        self.assertEqual(body["stderr"], "")
        self.assertIsNone(body["error"])
        self.assertEqual(body["return_code"], 0)
        self.assertEqual(body["commit_sha"], "abc123")

        result = body["result"]
        self.assertIsInstance(result, dict)
        self.assertEqual(result["files_changed"], ["created.txt"])
        self.assertEqual(result["commands"][0]["exit_code"], 0)
        self.assertTrue(result["commands"][0]["passed"])
        self.assertEqual(result["commands"][0]["kind"], "cursor_agent")
        self.assertIsNone(result["test_counts"])
        self.assertTrue(result["deliverables"]["passed"])
        self.assertEqual(result["deliverables"]["checked_paths"], ["created.txt"])
        self.assertEqual(result["persistence"]["commit_sha"], "abc123")
        self.assertTrue(result["persistence"]["ok"])

    def test_queued_run_keeps_null_result_for_compatibility(self) -> None:
        record = api_module.run_registry.create_run()
        response = self.client.get(f"/runs/{record.run_id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        for field in LEGACY_RUN_STATUS_FIELDS:
            self.assertIn(field, body)
        self.assertIsNone(body["result"])

    def test_empty_structured_result_defaults(self) -> None:
        result = empty_structured_result()
        self.assertEqual(result.files_changed, [])
        self.assertEqual(result.commands, [])
        self.assertIsNone(result.test_counts)
        self.assertIn(WARNING_NO_TEST_COUNTS, result.warnings)


if __name__ == "__main__":
    unittest.main()
