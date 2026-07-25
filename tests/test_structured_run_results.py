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
    WARNING_STDOUT_PREDATES_PERSISTENCE,
    build_run_summary,
    deserialize_structured_result,
    empty_structured_result,
    finalize_structured_summary,
    parse_git_status_porcelain_paths,
    serialize_structured_result,
)
from mission_control.workspace import (
    PersistenceResult,
    WorkspacePrepResult,
    build_persistence_evidence,
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


class TestAuthoritativePersistenceSummary(unittest.TestCase):
    def test_build_run_summary_matches_successful_platform_persistence(self) -> None:
        persistence = PersistenceEvidence(
            mode="commit",
            attempted=True,
            ok=True,
            commit_sha="abc123def456",
        )
        summary = build_run_summary(persistence=persistence)
        self.assertIn("Platform persistence succeeded", summary)
        self.assertIn("mode=commit", summary)
        self.assertIn("commit_sha=abc123def456", summary)
        self.assertIn("prefer this summary", summary)

    def test_build_run_summary_reports_failed_persistence(self) -> None:
        persistence = PersistenceEvidence(
            mode="push",
            attempted=True,
            ok=False,
            commit_sha=None,
        )
        summary = build_run_summary(
            persistence=persistence,
            error="push rejected",
        )
        self.assertIn("Platform persistence failed (mode=push)", summary)
        self.assertIn("push rejected", summary)

    def test_finalize_warns_when_stdout_predates_persistence(self) -> None:
        structured = empty_structured_result()
        structured.persistence = PersistenceEvidence(
            mode="push",
            attempted=True,
            ok=True,
            commit_sha="deadbeef",
        )
        finalize_structured_summary(structured)
        self.assertIsNotNone(structured.summary)
        assert structured.summary is not None
        self.assertIn("commit_sha=deadbeef", structured.summary)
        self.assertIn(WARNING_STDOUT_PREDATES_PERSISTENCE, structured.warnings)


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
            summary=(
                "Platform persistence succeeded "
                "(mode=commit, commit_sha=deadbeef)."
            ),
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
        self.assertEqual(
            restored.summary,
            "Platform persistence succeeded (mode=commit, commit_sha=deadbeef).",
        )

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
            self.assertEqual(
                fetched.result.summary,
                "Platform persistence succeeded "
                "(mode=commit, commit_sha=deadbeef).",
            )
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
            stdout=(
                "Not committed in this mission "
                "(constraints forbid git staging/commits/pushes).\n"
            ),
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
            mode="commit",
            pushed=False,
        )

        record = self.registry.create_run()
        mission = _base_mission(deliverables=["docs/out.txt"])
        execute_registered_run(record.run_id, mission, self.registry)

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        self.assertEqual(updated.status, RunStatus.COMPLETED)
        self.assertEqual(updated.commit_sha, "abc123def456")
        # Agent stdout may deny commit/push; platform persistence still wins.
        self.assertIn("forbid git", updated.stdout)
        self.assertIn("Not committed", updated.stdout)
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
        self.assertFalse(updated.result.persistence.pushed)
        self.assertIn(WARNING_NO_TEST_COUNTS, updated.result.warnings)
        self.assertIn(
            WARNING_STDOUT_PREDATES_PERSISTENCE,
            updated.result.warnings,
        )
        assert updated.result.summary is not None
        self.assertIn("Platform persistence succeeded", updated.result.summary)
        self.assertIn("commit_sha=abc123def456", updated.result.summary)
        self.assertNotIn("forbid git", updated.result.summary)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.collect_changed_files")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    def test_summary_reconciles_agent_no_commit_claim_with_platform_push(
        self,
        mock_prepare,
        mock_execute,
        mock_changed,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        """Stdout can deny commit/push while platform persistence succeeded."""
        workspace = tempfile.mkdtemp(prefix="mc-structured-recon-")
        (Path(workspace) / "docs").mkdir()
        (Path(workspace) / "docs" / "out.txt").write_text("x\n", encoding="utf-8")
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path=workspace,
        )
        agent_stdout = (
            "implementation done\n"
            "commit hash: n/a (no commit or push occurred)\n"
            "push confirmation: not pushed\n"
        )
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout=agent_stdout,
            return_code=0,
            command=["cursor-agent", "--force", "<instruction>"],
        )
        mock_changed.return_value = (["docs/out.txt"], None)
        mock_persist.return_value = PersistenceResult(
            ok=True,
            commit_sha="feedface99",
            mode="push",
            pushed=True,
        )

        record = self.registry.create_run()
        mission = _base_mission(deliverables=["docs/out.txt"])
        mission["persistence"] = {"mode": "push"}
        execute_registered_run(record.run_id, mission, self.registry)

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        self.assertEqual(updated.status, RunStatus.COMPLETED)
        self.assertEqual(updated.stdout, agent_stdout)
        self.assertIn("no commit or push occurred", updated.stdout)
        self.assertEqual(updated.commit_sha, "feedface99")
        assert updated.result is not None
        assert updated.result.persistence is not None
        self.assertTrue(updated.result.persistence.ok)
        self.assertEqual(updated.result.persistence.commit_sha, "feedface99")
        self.assertEqual(updated.result.persistence.mode, "push")
        self.assertTrue(updated.result.persistence.pushed)
        assert updated.result.summary is not None
        self.assertIn("Platform persistence succeeded", updated.result.summary)
        self.assertIn("mode=push", updated.result.summary)
        self.assertIn("pushed=true", updated.result.summary)
        self.assertIn("commit_sha=feedface99", updated.result.summary)
        self.assertNotIn("no commit or push occurred", updated.result.summary)
        mock_persist.assert_called_once()
        mock_execute.assert_called_once()

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
        assert updated.result.summary is not None
        self.assertIn(
            "Platform persistence was not attempted",
            updated.result.summary,
        )
        mock_persist.assert_not_called()

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.collect_changed_files")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    def test_persistence_mode_none_reporting(
        self,
        mock_prepare,
        mock_execute,
        mock_changed,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        """persistence.mode none reports none (not commit/push)."""
        workspace = tempfile.mkdtemp(prefix="mc-persist-none-")
        (Path(workspace) / "docs").mkdir()
        (Path(workspace) / "docs" / "out.txt").write_text("x\n", encoding="utf-8")
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path=workspace,
        )
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout="done\n",
            return_code=0,
            command=["cursor-agent", "--force", "<instruction>"],
        )
        mock_changed.return_value = (["docs/out.txt"], None)
        mock_persist.return_value = PersistenceResult(
            ok=True,
            commit_sha=None,
            mode="none",
            pushed=False,
        )

        record = self.registry.create_run()
        mission = _base_mission(deliverables=["docs/out.txt"])
        mission["persistence"] = {"mode": "none"}
        execute_registered_run(record.run_id, mission, self.registry)

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        assert updated.result is not None
        assert updated.result.persistence is not None
        self.assertEqual(updated.result.persistence.mode, "none")
        self.assertTrue(updated.result.persistence.attempted)
        self.assertTrue(updated.result.persistence.ok)
        self.assertFalse(updated.result.persistence.pushed)
        self.assertIsNone(updated.result.persistence.commit_sha)
        self.assertIsNone(updated.commit_sha)
        assert updated.result.summary is not None
        self.assertIn("mode=none", updated.result.summary)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.collect_changed_files")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    def test_persistence_mode_commit_reporting(
        self,
        mock_prepare,
        mock_execute,
        mock_changed,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        """commit reports commit only when a commit is successfully created."""
        workspace = tempfile.mkdtemp(prefix="mc-persist-commit-")
        (Path(workspace) / "docs").mkdir()
        (Path(workspace) / "docs" / "out.txt").write_text("x\n", encoding="utf-8")
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path=workspace,
        )
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout="done\n",
            return_code=0,
            command=["cursor-agent", "--force", "<instruction>"],
        )
        mock_changed.return_value = (["docs/out.txt"], None)
        mock_persist.return_value = PersistenceResult(
            ok=True,
            commit_sha="commitonly01",
            mode="commit",
            pushed=False,
        )

        record = self.registry.create_run()
        mission = _base_mission(deliverables=["docs/out.txt"])
        mission["persistence"] = {"mode": "commit"}
        execute_registered_run(record.run_id, mission, self.registry)

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        assert updated.result is not None
        assert updated.result.persistence is not None
        self.assertEqual(updated.result.persistence.mode, "commit")
        self.assertTrue(updated.result.persistence.ok)
        self.assertFalse(updated.result.persistence.pushed)
        self.assertEqual(updated.result.persistence.commit_sha, "commitonly01")
        self.assertEqual(updated.commit_sha, "commitonly01")

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.collect_changed_files")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    def test_persistence_mode_push_reporting(
        self,
        mock_prepare,
        mock_execute,
        mock_changed,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        """push reports push only when the commit is successfully pushed."""
        workspace = tempfile.mkdtemp(prefix="mc-persist-push-")
        (Path(workspace) / "docs").mkdir()
        (Path(workspace) / "docs" / "out.txt").write_text("x\n", encoding="utf-8")
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path=workspace,
        )
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout="done\n",
            return_code=0,
            command=["cursor-agent", "--force", "<instruction>"],
        )
        mock_changed.return_value = (["docs/out.txt"], None)
        # Execution result carries mode=push; must not fall back to none.
        mock_persist.return_value = PersistenceResult(
            ok=True,
            commit_sha="pushsha0001",
            mode="push",
            pushed=True,
        )

        record = self.registry.create_run()
        mission = _base_mission(deliverables=["docs/out.txt"])
        mission["persistence"] = {"mode": "push"}
        mission["approval"] = {
            **mission["approval"],
            "platform_push_approved": True,
        }
        execute_registered_run(record.run_id, mission, self.registry)

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        assert updated.result is not None
        assert updated.result.persistence is not None
        self.assertEqual(updated.result.persistence.mode, "push")
        self.assertNotEqual(updated.result.persistence.mode, "none")
        self.assertTrue(updated.result.persistence.ok)
        self.assertTrue(updated.result.persistence.pushed)
        self.assertEqual(updated.result.persistence.commit_sha, "pushsha0001")
        self.assertEqual(updated.commit_sha, "pushsha0001")
        assert updated.result.summary is not None
        self.assertIn("mode=push", updated.result.summary)
        self.assertIn("pushed=true", updated.result.summary)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.collect_changed_files")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    def test_unsuccessful_push_not_reported_as_successful(
        self,
        mock_prepare,
        mock_execute,
        mock_changed,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        """Failed push keeps mode=push with ok=false and pushed=false."""
        workspace = tempfile.mkdtemp(prefix="mc-persist-push-fail-")
        (Path(workspace) / "docs").mkdir()
        (Path(workspace) / "docs" / "out.txt").write_text("x\n", encoding="utf-8")
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path=workspace,
        )
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout="done\n",
            return_code=0,
            command=["cursor-agent", "--force", "<instruction>"],
        )
        mock_changed.return_value = (["docs/out.txt"], None)
        mock_persist.return_value = PersistenceResult(
            ok=False,
            error="git push failed with code 1",
            commit_sha="partialc0mm1t",
            mode="push",
            pushed=False,
        )

        record = self.registry.create_run()
        mission = _base_mission(deliverables=["docs/out.txt"])
        mission["persistence"] = {"mode": "push"}
        mission["approval"] = {
            **mission["approval"],
            "platform_push_approved": True,
        }
        execute_registered_run(record.run_id, mission, self.registry)

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        self.assertEqual(updated.status, RunStatus.FAILED)
        assert updated.result is not None
        assert updated.result.persistence is not None
        self.assertEqual(updated.result.persistence.mode, "push")
        self.assertTrue(updated.result.persistence.attempted)
        self.assertFalse(updated.result.persistence.ok)
        self.assertFalse(updated.result.persistence.pushed)
        self.assertEqual(
            updated.result.persistence.commit_sha,
            "partialc0mm1t",
        )
        # Top-level commit_sha is only stored on successful persistence.
        self.assertIsNone(updated.commit_sha)
        assert updated.result.summary is not None
        self.assertIn(
            "Platform persistence failed (mode=push)",
            updated.result.summary,
        )
        self.assertNotIn(
            "Platform persistence succeeded",
            updated.result.summary,
        )

    def test_execution_result_mode_not_shadowed_by_none_default(self) -> None:
        """Missing PersistenceResult.mode falls back to mission, not bare none.

        A truthy default of ``mode="none"`` on PersistenceResult would wrongly
        shadow mission ``persistence.mode: push`` when combined with
        ``result.mode or mission_mode``.
        """
        mission = _base_mission()
        mission["persistence"] = {"mode": "push"}
        evidence = build_persistence_evidence(
            mission,
            attempted=True,
            ok=True,
            commit_sha="abc",
            mode=None,
            pushed=None,
        )
        self.assertEqual(evidence.mode, "push")

        from_result = build_persistence_evidence(
            mission,
            attempted=True,
            ok=True,
            commit_sha="abc",
            mode="push",
            pushed=True,
        )
        self.assertEqual(from_result.mode, "push")
        self.assertTrue(from_result.pushed)

        none_mission = _base_mission()
        none_mission["persistence"] = {"mode": "none"}
        none_evidence = build_persistence_evidence(
            none_mission,
            attempted=True,
            ok=True,
            commit_sha=None,
            mode="none",
            pushed=False,
        )
        self.assertEqual(none_evidence.mode, "none")
        self.assertFalse(none_evidence.pushed)

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
            stdout=(
                "Agent response claiming no commit or push occurred\n"
            ),
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
        self.assertIn("no commit or push occurred", body["stdout"])
        self.assertEqual(body["stderr"], "")
        self.assertIsNone(body["error"])
        self.assertEqual(body["return_code"], 0)
        self.assertEqual(body["commit_sha"], "abc123")
        self.assertIn("Platform persistence succeeded", body["summary"])
        self.assertIn("commit_sha=abc123", body["summary"])
        self.assertNotIn("no commit or push occurred", body["summary"])

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
        self.assertEqual(result["summary"], body["summary"])
        self.assertIn(
            WARNING_STDOUT_PREDATES_PERSISTENCE,
            result["warnings"],
        )

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.collect_changed_files")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    @patch("app.api.preflight_for_execution", return_value=None)
    def test_wait_for_run_summary_matches_platform_persistence(
        self,
        _mock_preflight,
        mock_prepare,
        mock_execute,
        mock_changed,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        """Wait payload summary must match platform persistence, not stdout."""
        workspace = tempfile.mkdtemp(prefix="mc-structured-wait-")
        (Path(workspace) / "created.txt").write_text("hi\n", encoding="utf-8")
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path=workspace,
        )
        agent_stdout = (
            "commit hash: n/a (no commit or push occurred)\n"
            "push confirmation: not pushed\n"
        )
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout=agent_stdout,
            return_code=0,
            command=["cursor-agent", "--force", "<instruction>"],
        )
        mock_changed.return_value = (["created.txt"], None)
        mock_persist.return_value = PersistenceResult(
            ok=True,
            commit_sha="cafef00d",
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
        self._wait_for_terminal(run_id)

        wait_response = self.client.post(
            f"/runs/{run_id}/wait",
            json={"timeout_seconds": 1.0, "poll_interval_seconds": 0.05},
        )
        self.assertEqual(wait_response.status_code, 200)
        body = wait_response.json()
        self.assertEqual(body["status"], "completed")
        self.assertTrue(body["reached_terminal"])
        self.assertFalse(body["wait_expired"])
        self.assertEqual(body["stdout"], agent_stdout)
        self.assertIn("no commit or push occurred", body["stdout"])
        self.assertEqual(body["commit_sha"], "cafef00d")
        self.assertIn("Platform persistence succeeded", body["summary"])
        self.assertIn("commit_sha=cafef00d", body["summary"])
        self.assertNotIn("no commit or push occurred", body["summary"])
        self.assertEqual(body["result"]["summary"], body["summary"])
        self.assertEqual(body["result"]["persistence"]["commit_sha"], "cafef00d")
        self.assertTrue(body["result"]["persistence"]["ok"])
        mock_persist.assert_called_once()

    def test_queued_run_keeps_null_result_for_compatibility(self) -> None:
        record = api_module.run_registry.create_run()
        response = self.client.get(f"/runs/{record.run_id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        for field in LEGACY_RUN_STATUS_FIELDS:
            self.assertIn(field, body)
        self.assertIsNone(body["result"])
        self.assertIsNone(body["summary"])

    def test_empty_structured_result_defaults(self) -> None:
        result = empty_structured_result()
        self.assertEqual(result.files_changed, [])
        self.assertEqual(result.commands, [])
        self.assertIsNone(result.test_counts)
        self.assertIn(WARNING_NO_TEST_COUNTS, result.warnings)


if __name__ == "__main__":
    unittest.main()
