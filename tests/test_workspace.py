"""Tests for isolated workspace preparation and Git persistence."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import os

from mission_control.executor import ExecutionResult
from mission_control.run_registry import RunRegistry, RunStatus
from mission_control.workspace import (
    PersistenceResult,
    WorkspacePrepResult,
    cleanup_workspace,
    configure_workspace_origin,
    execute_registered_run,
    get_origin_url,
    persist_workspace_changes,
    prepare_isolated_workspace,
)


def _run_git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=check,
        shell=False,
    )


class GitRepoFixture:
    """Create a source repo and bare remote for workspace tests."""

    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.bare_remote = root / "remote.git"
        self.source_repo = root / "source"
        self.base_branch = "main"

        _run_git(["init", "--bare", str(self.bare_remote)])
        self.source_repo.mkdir()
        _run_git(["init", str(self.source_repo)])
        _run_git(
            ["-C", str(self.source_repo), "config", "user.email", "test@example.com"]
        )
        _run_git(["-C", str(self.source_repo), "config", "user.name", "Test User"])
        (self.source_repo / "README.md").write_text("initial\n", encoding="utf-8")
        _run_git(["-C", str(self.source_repo), "add", "README.md"])
        _run_git(["-C", str(self.source_repo), "commit", "-m", "init"])
        _run_git(
            [
                "-C",
                str(self.source_repo),
                "branch",
                "-M",
                self.base_branch,
            ]
        )
        _run_git(
            [
                "-C",
                str(self.source_repo),
                "remote",
                "add",
                "origin",
                str(self.bare_remote),
            ]
        )
        _run_git(["-C", str(self.source_repo), "push", "-u", "origin", self.base_branch])

        self._previous_repo_url = os.environ.get("MISSION_CONTROL_REPOSITORY_URL")
        self._previous_git_name = os.environ.get("MISSION_CONTROL_GIT_NAME")
        self._previous_git_email = os.environ.get("MISSION_CONTROL_GIT_EMAIL")
        os.environ["MISSION_CONTROL_REPOSITORY_URL"] = str(self.bare_remote)
        os.environ["MISSION_CONTROL_GIT_NAME"] = "Test User"
        os.environ["MISSION_CONTROL_GIT_EMAIL"] = "test@example.com"

    def mission(self) -> dict:
        return {
            "mission_id": "2026-07-19-workspace",
            "repository": {
                "name": "test-repo",
                "path": str(self.source_repo),
                "base_branch": self.base_branch,
            },
        }

    def cleanup(self) -> None:
        if self._previous_repo_url is None:
            os.environ.pop("MISSION_CONTROL_REPOSITORY_URL", None)
        else:
            os.environ["MISSION_CONTROL_REPOSITORY_URL"] = self._previous_repo_url

        if self._previous_git_name is None:
            os.environ.pop("MISSION_CONTROL_GIT_NAME", None)
        else:
            os.environ["MISSION_CONTROL_GIT_NAME"] = self._previous_git_name

        if self._previous_git_email is None:
            os.environ.pop("MISSION_CONTROL_GIT_EMAIL", None)
        else:
            os.environ["MISSION_CONTROL_GIT_EMAIL"] = self._previous_git_email

        self.temp.cleanup()


class TestOriginDiscovery(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitRepoFixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def test_get_origin_url_returns_configured_remote(self) -> None:
        origin = get_origin_url(str(self.fixture.source_repo))
        self.assertEqual(origin, str(self.fixture.bare_remote))

    def test_get_origin_url_returns_none_when_missing(self) -> None:
        repo = Path(self.fixture.temp.name) / "no-remote"
        repo.mkdir()
        _run_git(["init", str(repo)])
        self.assertIsNone(get_origin_url(str(repo)))


class TestWorkspacePreparation(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitRepoFixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def test_prepare_isolated_workspace_clones_and_configures_origin(self) -> None:
        prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None

        try:
            self.assertEqual(
                get_origin_url(prep.workspace_path),
                str(self.fixture.bare_remote),
            )
            configure = configure_workspace_origin(
                prep.workspace_path,
                "https://example.com/org/repo.git",
            )
            self.assertEqual(configure.returncode, 0)
            self.assertEqual(
                get_origin_url(prep.workspace_path),
                "https://example.com/org/repo.git",
            )
        finally:
            cleanup_workspace(prep.workspace_path)

    def test_prepare_isolated_workspace_fails_when_origin_missing(self) -> None:
        with patch.dict(os.environ, {"MISSION_CONTROL_REPOSITORY_URL": ""}, clear=False):
            prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertFalse(prep.ok)
        self.assertIn(
            "mission_control_repository_url",
            (prep.error or "").lower(),
        )


class TestWorkspacePersistence(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitRepoFixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def _prepare_workspace(self) -> str:
        prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        return prep.workspace_path

    def test_persist_workspace_changes_with_no_changes(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            result = persist_workspace_changes(
                "run-no-change",
                self.fixture.mission(),
                workspace_path,
            )
            self.assertTrue(result.ok, result.error)
            self.assertIsNone(result.commit_sha)
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_workspace_changes_commits_and_pushes(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )

            with patch(
                "mission_control.workspace._github_push_environment",
                return_value=(os.environ.copy(), None),
            ):
                result = persist_workspace_changes(
                    "run-with-change",
                    self.fixture.mission(),
                    workspace_path,
                )
            self.assertTrue(result.ok, result.error)
            self.assertIsNotNone(result.commit_sha)

            remote_head = _run_git(
                [
                    "-C",
                    str(self.fixture.bare_remote),
                    "rev-parse",
                    self.fixture.base_branch,
                ]
            )
            self.assertEqual(remote_head.stdout.strip(), result.commit_sha)
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_workspace_changes_commit_failure(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            status = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=" M README.md\n",
                stderr="",
            )
            add = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr="",
            )
            commit = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="commit failed",
            )
            with patch(
                "mission_control.workspace._run_git",
                side_effect=[status, add, commit],
            ):
                result = persist_workspace_changes(
                    "run-commit-fail",
                    self.fixture.mission(),
                    workspace_path,
                )
            self.assertFalse(result.ok)
            self.assertIn("commit failed", result.error or "")
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_workspace_changes_push_failure(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            status = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=" M README.md\n",
                stderr="",
            )
            add = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr="",
            )
            commit = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="[main abc1234] Mission Control run run-push-fail\n",
                stderr="",
            )
            push = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="push rejected",
            )
            with patch(
                "mission_control.workspace._run_git",
                side_effect=[status, add, commit, push],
            ):
                result = persist_workspace_changes(
                    "run-push-fail",
                    self.fixture.mission(),
                    workspace_path,
                )
            self.assertFalse(result.ok)
            self.assertIn("push rejected", result.error or "")
        finally:
            cleanup_workspace(workspace_path)


class TestExecuteRegisteredRun(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitRepoFixture()
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)

    def tearDown(self) -> None:
        self.fixture.cleanup()
        self.registry.close()
        os.unlink(self._db_path)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    def test_execute_registered_run_stores_commit_sha(
        self,
        mock_prepare,
        mock_execute,
        mock_persist,
        mock_cleanup,
    ) -> None:
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path="/tmp/workspace",
        )
        mock_execute.return_value = ExecutionResult(ok=True, stdout="done\n")
        mock_persist.return_value = PersistenceResult(
            ok=True,
            commit_sha="abc123def456",
        )

        record = self.registry.create_run()
        execute_registered_run(record.run_id, self.fixture.mission(), self.registry)

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        self.assertEqual(updated.status, RunStatus.COMPLETED)
        self.assertEqual(updated.commit_sha, "abc123def456")
        mock_cleanup.assert_called_once_with("/tmp/workspace")

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    def test_execute_registered_run_marks_commit_failure_as_failed(
        self,
        mock_prepare,
        mock_execute,
        mock_persist,
        mock_cleanup,
    ) -> None:
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path="/tmp/workspace",
        )
        mock_execute.return_value = ExecutionResult(ok=True, stdout="done\n")
        mock_persist.return_value = PersistenceResult(
            ok=False,
            error="git commit failed",
        )

        record = self.registry.create_run()
        execute_registered_run(record.run_id, self.fixture.mission(), self.registry)

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        self.assertEqual(updated.status, RunStatus.FAILED)
        self.assertEqual(updated.error, "git commit failed")
        mock_cleanup.assert_called_once_with("/tmp/workspace")

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    def test_execute_registered_run_cleans_up_after_execution_failure(
        self,
        mock_prepare,
        mock_cleanup,
    ) -> None:
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path="/tmp/workspace",
        )

        with patch(
            "mission_control.workspace.execute_cursor_agent",
            return_value=ExecutionResult(ok=False, error="agent failed"),
        ):
            record = self.registry.create_run()
            execute_registered_run(
                record.run_id,
                self.fixture.mission(),
                self.registry,
            )

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        self.assertEqual(updated.status, RunStatus.FAILED)
        mock_cleanup.assert_called_once_with("/tmp/workspace")

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    def test_execute_registered_run_fails_when_origin_missing(
        self,
        mock_prepare,
        mock_cleanup,
    ) -> None:
        mock_prepare.return_value = WorkspacePrepResult(
            ok=False,
            error="MISSION_CONTROL_REPOSITORY_URL is not configured.",
        )

        record = self.registry.create_run()
        execute_registered_run(record.run_id, self.fixture.mission(), self.registry)

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        self.assertEqual(updated.status, RunStatus.FAILED)
        self.assertIn("repository_url", (updated.error or "").lower())
        mock_cleanup.assert_not_called()

    @patch("mission_control.workspace._safe_cleanup")
    @patch("mission_control.workspace._run_git")
    def test_cleanup_runs_after_prepare_failure_leaves_no_workspace(
        self,
        mock_run_git,
        mock_safe_cleanup,
    ) -> None:
        clone = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="clone failed",
        )
        mock_run_git.return_value = clone

        prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertFalse(prep.ok)
        mock_safe_cleanup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
