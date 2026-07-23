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
from mission_control.validator import validate_mission_for_execute
from mission_control.workspace import (
    PLATFORM_PUSH_APPROVAL_REQUIRED,
    PersistenceResult,
    WorkspacePrepResult,
    cleanup_workspace,
    configure_workspace_origin,
    execute_registered_run,
    get_origin_url,
    is_platform_push_authorized,
    looks_like_file_path_deliverable,
    persist_workspace_changes,
    prepare_isolated_workspace,
    require_platform_push_approval,
    resolve_safe_workspace_deliverable,
    verify_declared_file_deliverables,
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

    def mission(
        self,
        *,
        persistence_mode: str | None = "push",
        platform_push_approved: bool | None = None,
        allow_automatic_platform_push: bool | None = None,
        permissions_push: bool = False,
    ) -> dict:
        mission = {
            "mission_id": "2026-07-19-workspace",
            "repository": {
                "name": "test-repo",
                "path": str(self.source_repo),
                "base_branch": self.base_branch,
            },
            "permissions": {
                "push": permissions_push,
            },
        }
        if persistence_mode is not None:
            mission["persistence"] = {"mode": persistence_mode}
        approval: dict[str, bool] = {}
        if platform_push_approved is not None:
            approval["platform_push_approved"] = platform_push_approved
        if allow_automatic_platform_push is not None:
            approval["allow_automatic_platform_push"] = (
                allow_automatic_platform_push
            )
        if approval:
            mission["approval"] = approval
        return mission

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
                self.fixture.mission(
                    persistence_mode="push",
                    platform_push_approved=True,
                ),
                workspace_path,
            )
            self.assertTrue(result.ok, result.error)
            self.assertIsNone(result.commit_sha)
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_mode_none_invokes_no_git_add_commit_or_push(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            with patch("mission_control.workspace._run_git") as mock_git:
                result = persist_workspace_changes(
                    "run-none",
                    self.fixture.mission(persistence_mode="none"),
                    workspace_path,
                )
            self.assertTrue(result.ok, result.error)
            self.assertIsNone(result.commit_sha)
            mock_git.assert_not_called()
            status = _run_git(["-C", workspace_path, "status", "--porcelain"])
            self.assertIn("created.txt", status.stdout)
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_omitted_persistence_defaults_to_none(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            with patch("mission_control.workspace._run_git") as mock_git:
                result = persist_workspace_changes(
                    "run-default-none",
                    self.fixture.mission(persistence_mode=None),
                    workspace_path,
                )
            self.assertTrue(result.ok, result.error)
            self.assertIsNone(result.commit_sha)
            mock_git.assert_not_called()
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_mode_commit_never_pushes(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            remote_before = _run_git(
                [
                    "-C",
                    str(self.fixture.bare_remote),
                    "rev-parse",
                    self.fixture.base_branch,
                ]
            ).stdout.strip()

            recorded_args: list[list[str]] = []
            real_run_git = persist_workspace_changes.__globals__["_run_git"]

            def tracking_run_git(
                args: list[str],
                *,
                env: dict[str, str] | None = None,
            ) -> subprocess.CompletedProcess[str]:
                recorded_args.append(list(args))
                return real_run_git(args, env=env)

            with patch(
                "mission_control.workspace._run_git",
                side_effect=tracking_run_git,
            ):
                result = persist_workspace_changes(
                    "run-commit-only",
                    self.fixture.mission(persistence_mode="commit"),
                    workspace_path,
                )

            self.assertTrue(result.ok, result.error)
            self.assertIsNotNone(result.commit_sha)
            self.assertFalse(
                any("push" in args for args in recorded_args),
                recorded_args,
            )

            remote_after = _run_git(
                [
                    "-C",
                    str(self.fixture.bare_remote),
                    "rev-parse",
                    self.fixture.base_branch,
                ]
            ).stdout.strip()
            self.assertEqual(remote_before, remote_after)
            self.assertNotEqual(remote_after, result.commit_sha)
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
                    self.fixture.mission(
                        persistence_mode="push",
                        platform_push_approved=True,
                    ),
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

    def test_persist_push_rejected_without_platform_push_approval(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            with patch("mission_control.workspace._run_git") as mock_git:
                result = persist_workspace_changes(
                    "run-push-unapproved",
                    self.fixture.mission(persistence_mode="push"),
                    workspace_path,
                )
            self.assertFalse(result.ok)
            self.assertTrue(
                (result.error or "").startswith("PLATFORM_PUSH_APPROVAL_REQUIRED"),
                result.error,
            )
            mock_git.assert_not_called()
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_push_succeeds_when_platform_push_approved(self) -> None:
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
                    "run-push-approved",
                    self.fixture.mission(
                        persistence_mode="push",
                        platform_push_approved=True,
                    ),
                    workspace_path,
                )
            self.assertTrue(result.ok, result.error)
            self.assertIsNotNone(result.commit_sha)
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_push_succeeds_with_automatic_platform_push_policy(
        self,
    ) -> None:
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
                    "run-push-auto-policy",
                    self.fixture.mission(
                        persistence_mode="push",
                        allow_automatic_platform_push=True,
                    ),
                    workspace_path,
                )
            self.assertTrue(result.ok, result.error)
            self.assertIsNotNone(result.commit_sha)
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_mode_none_does_not_require_platform_push_approval(
        self,
    ) -> None:
        workspace_path = self._prepare_workspace()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            mission = self.fixture.mission(persistence_mode="none")
            self.assertIsNone(require_platform_push_approval(mission))
            with patch("mission_control.workspace._run_git") as mock_git:
                result = persist_workspace_changes(
                    "run-none-no-approval",
                    mission,
                    workspace_path,
                )
            self.assertTrue(result.ok, result.error)
            mock_git.assert_not_called()
        finally:
            cleanup_workspace(workspace_path)

    def test_agent_permissions_push_does_not_authorize_platform_push(
        self,
    ) -> None:
        workspace_path = self._prepare_workspace()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            mission = self.fixture.mission(
                persistence_mode="push",
                permissions_push=True,
            )
            self.assertFalse(is_platform_push_authorized(mission))
            with patch("mission_control.workspace._run_git") as mock_git:
                result = persist_workspace_changes(
                    "run-agent-push-not-enough",
                    mission,
                    workspace_path,
                )
            self.assertFalse(result.ok)
            self.assertEqual(result.error, PLATFORM_PUSH_APPROVAL_REQUIRED)
            mock_git.assert_not_called()
        finally:
            cleanup_workspace(workspace_path)

    def test_persistence_layer_enforces_approval_independently(self) -> None:
        """Boundary check rejects push even if a caller skipped queue validation."""
        workspace_path = self._prepare_workspace()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            mission = self.fixture.mission(persistence_mode="push")
            # Simulate a caller that did not run validate_mission_for_execute.
            self.assertFalse(is_platform_push_authorized(mission))
            with patch("mission_control.workspace._run_git") as mock_git:
                result = persist_workspace_changes(
                    "run-boundary-only",
                    mission,
                    workspace_path,
                )
            self.assertFalse(result.ok)
            self.assertTrue(
                (result.error or "").startswith("PLATFORM_PUSH_APPROVAL_REQUIRED"),
                result.error,
            )
            mock_git.assert_not_called()
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
                    self.fixture.mission(
                        persistence_mode="push",
                        platform_push_approved=True,
                    ),
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
                    self.fixture.mission(
                        persistence_mode="push",
                        platform_push_approved=True,
                    ),
                    workspace_path,
                )
            self.assertFalse(result.ok)
            self.assertIn("push rejected", result.error or "")
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_unsupported_mode_fails_inside_persist(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            with patch("mission_control.workspace._run_git") as mock_git:
                result = persist_workspace_changes(
                    "run-bad-mode",
                    self.fixture.mission(persistence_mode="rebase"),
                    workspace_path,
                )
            self.assertFalse(result.ok)
            self.assertIn("Unsupported persistence.mode", result.error or "")
            mock_git.assert_not_called()
        finally:
            cleanup_workspace(workspace_path)


class TestDeclaredFileDeliverables(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        (self.workspace / "README.md").write_text("ok\n", encoding="utf-8")
        (self.workspace / "docs").mkdir()
        (self.workspace / "docs" / "out.txt").write_text("out\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_looks_like_file_path_deliverable_detection(self) -> None:
        self.assertTrue(looks_like_file_path_deliverable("MISSION_SPEC.md"))
        self.assertTrue(looks_like_file_path_deliverable("docs/out.txt"))
        self.assertTrue(looks_like_file_path_deliverable("src/app.py"))
        self.assertTrue(looks_like_file_path_deliverable("/etc/passwd"))
        self.assertTrue(looks_like_file_path_deliverable("../outside.txt"))

        self.assertFalse(looks_like_file_path_deliverable("summary"))
        self.assertFalse(looks_like_file_path_deliverable("report"))
        self.assertFalse(looks_like_file_path_deliverable("confirmation"))
        self.assertFalse(looks_like_file_path_deliverable("repository status"))
        self.assertFalse(looks_like_file_path_deliverable(""))

    def test_existing_declared_file_deliverable_passes(self) -> None:
        mission = {"deliverables": ["README.md", "docs/out.txt"]}
        self.assertIsNone(
            verify_declared_file_deliverables(mission, str(self.workspace))
        )

    def test_missing_declared_file_deliverable_fails(self) -> None:
        mission = {"deliverables": ["missing-output.txt"]}
        error = verify_declared_file_deliverables(mission, str(self.workspace))
        self.assertEqual(
            error,
            "Missing declared file deliverable: missing-output.txt",
        )

    def test_multiple_file_deliverables_identify_missing_item(self) -> None:
        mission = {
            "deliverables": [
                "README.md",
                "docs/out.txt",
                "docs/missing.txt",
            ]
        }
        error = verify_declared_file_deliverables(mission, str(self.workspace))
        self.assertEqual(
            error,
            "Missing declared file deliverable: docs/missing.txt",
        )

    def test_descriptive_only_deliverables_preserve_current_behavior(self) -> None:
        mission = {
            "deliverables": [
                "summary",
                "report",
                "confirmation",
                "repository status",
            ]
        }
        self.assertIsNone(
            verify_declared_file_deliverables(mission, str(self.workspace))
        )

    def test_empty_deliverables_preserve_current_behavior(self) -> None:
        self.assertIsNone(
            verify_declared_file_deliverables(
                {"deliverables": []},
                str(self.workspace),
            )
        )
        self.assertIsNone(
            verify_declared_file_deliverables({}, str(self.workspace))
        )

    def test_unsafe_escaping_paths_are_not_read_outside_workspace(self) -> None:
        outside = Path(self.temp.name) / "outside_secret.txt"
        outside.write_text("secret\n", encoding="utf-8")
        workspace = self.workspace.resolve()
        real_is_file = Path.is_file

        def guarded_is_file(self: Path) -> bool:
            resolved = self if self.is_absolute() else (Path.cwd() / self)
            resolved = resolved.resolve()
            try:
                resolved.relative_to(workspace)
            except ValueError as exc:
                raise AssertionError(
                    f"attempted filesystem check outside workspace: {resolved}"
                ) from exc
            return real_is_file(self)

        mission = {
            "deliverables": [
                str(outside),
                f"../{outside.name}",
                "/etc/passwd",
                "~/secret.txt",
            ]
        }
        with patch.object(Path, "is_file", guarded_is_file):
            self.assertIsNone(
                verify_declared_file_deliverables(mission, str(self.workspace))
            )
            self.assertIsNone(
                resolve_safe_workspace_deliverable(
                    str(self.workspace),
                    f"../{outside.name}",
                )
            )
            self.assertIsNone(
                resolve_safe_workspace_deliverable(str(self.workspace), str(outside))
            )


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

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    def test_existing_file_deliverable_allows_persistence(
        self,
        mock_execute,
        mock_persist,
        mock_cleanup,
    ) -> None:
        prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            (Path(prep.workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            mock_execute.return_value = ExecutionResult(ok=True, stdout="done\n")
            mock_persist.return_value = PersistenceResult(ok=True, commit_sha=None)

            mission = self.fixture.mission(persistence_mode="none")
            mission["deliverables"] = ["created.txt"]
            with patch(
                "mission_control.workspace.prepare_isolated_workspace",
                return_value=prep,
            ):
                record = self.registry.create_run()
                execute_registered_run(record.run_id, mission, self.registry)

            updated = self.registry.get_run(record.run_id)
            assert updated is not None
            self.assertEqual(updated.status, RunStatus.COMPLETED)
            mock_persist.assert_called_once()
            mock_cleanup.assert_called_once_with(prep.workspace_path)
        finally:
            cleanup_workspace(prep.workspace_path)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    def test_missing_file_deliverable_fails_before_persistence(
        self,
        mock_execute,
        mock_persist,
        mock_cleanup,
    ) -> None:
        prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            mock_execute.return_value = ExecutionResult(ok=True, stdout="done\n")

            mission = self.fixture.mission(persistence_mode="none")
            mission["deliverables"] = ["missing-output.txt"]
            with patch(
                "mission_control.workspace.prepare_isolated_workspace",
                return_value=prep,
            ):
                record = self.registry.create_run()
                execute_registered_run(record.run_id, mission, self.registry)

            updated = self.registry.get_run(record.run_id)
            assert updated is not None
            self.assertEqual(updated.status, RunStatus.FAILED)
            self.assertEqual(
                updated.error,
                "Missing declared file deliverable: missing-output.txt",
            )
            mock_persist.assert_not_called()
            mock_cleanup.assert_called_once_with(prep.workspace_path)
        finally:
            cleanup_workspace(prep.workspace_path)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    def test_descriptive_and_empty_deliverables_still_persist(
        self,
        mock_execute,
        mock_persist,
        mock_cleanup,
    ) -> None:
        prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            mock_execute.return_value = ExecutionResult(ok=True, stdout="done\n")
            mock_persist.return_value = PersistenceResult(ok=True, commit_sha=None)

            for deliverables in (
                ["summary", "report", "confirmation"],
                [],
            ):
                mock_persist.reset_mock()
                mock_cleanup.reset_mock()
                mission = self.fixture.mission(persistence_mode="none")
                mission["deliverables"] = list(deliverables)
                with patch(
                    "mission_control.workspace.prepare_isolated_workspace",
                    return_value=WorkspacePrepResult(
                        ok=True,
                        workspace_path=prep.workspace_path,
                    ),
                ):
                    record = self.registry.create_run()
                    execute_registered_run(record.run_id, mission, self.registry)

                updated = self.registry.get_run(record.run_id)
                assert updated is not None
                self.assertEqual(updated.status, RunStatus.COMPLETED)
                mock_persist.assert_called_once()

            mock_cleanup.assert_called_with(prep.workspace_path)
        finally:
            cleanup_workspace(prep.workspace_path)
    def test_persistence_modes_none_commit_push_unchanged_with_file_gate(
        self,
    ) -> None:
        """Deliverable verification must not alter none/commit/push semantics."""
        for mode, expect_sha in (
            ("none", False),
            ("commit", True),
            ("push", True),
        ):
            workspace_path = prepare_isolated_workspace(
                self.fixture.mission()
            )
            self.assertTrue(workspace_path.ok, workspace_path.error)
            assert workspace_path.workspace_path is not None
            path = workspace_path.workspace_path
            try:
                (Path(path) / "created.txt").write_text(
                    "mission output\n",
                    encoding="utf-8",
                )
                mission = self.fixture.mission(
                    persistence_mode=mode,
                    platform_push_approved=(mode == "push"),
                )
                mission["deliverables"] = ["created.txt"]
                self.assertIsNone(
                    verify_declared_file_deliverables(mission, path)
                )
                with patch(
                    "mission_control.workspace._github_push_environment",
                    return_value=(os.environ.copy(), None),
                ):
                    result = persist_workspace_changes(
                        f"run-mode-{mode}",
                        mission,
                        path,
                    )
                self.assertTrue(result.ok, result.error)
                if expect_sha:
                    self.assertIsNotNone(result.commit_sha)
                else:
                    self.assertIsNone(result.commit_sha)
            finally:
                cleanup_workspace(path)


if __name__ == "__main__":
    unittest.main()
