"""Tests for Phase 2 run-eligibility validation."""

import os
import tempfile
import unittest
from pathlib import Path

from mission_control.validator import (
    validate_mission_for_execute,
    validate_mission_for_run,
)
from mission_control.workspace import REPOSITORY_PATH_IDENTITY_MISMATCH_PREFIX

REPO_ROOT = Path(__file__).resolve().parent.parent


def _base_mission(repo_path: str, *, name: str = "Mission-Control") -> dict:
    return {
        "version": 1.0,
        "mission_id": "2026-07-17-001",
        "title": "Test Mission",
        "repository": {
            "name": name,
            "path": repo_path,
            "base_branch": "main",
        },
        "execution": {
            "agent": "cursor",
            "mode": "plan",
            "sandbox": True,
            "worktree": False,
        },
        "permissions": {
            "read": True,
            "create_files": False,
            "modify_files": False,
            "delete_files": False,
            "run_commands": True,
            "stage_changes": False,
            "commit": False,
            "push": False,
        },
        "instructions": "Inspect the repository.",
        "deliverables": ["summary"],
        "approval": {
            "execute_without_approval": True,
            "commit_requires_approval": True,
            "push_requires_approval": True,
        },
    }


def _execute_mission(
    repo_path: str,
    *,
    name: str = "legal-ai",
) -> dict:
    mission = _base_mission(repo_path, name=name)
    mission["execution"]["mode"] = "execute"
    mission["permissions"]["create_files"] = True
    mission["persistence"] = {"mode": "none"}
    return mission


def _without_managed_url_env() -> dict[str, str | None]:
    """Snapshot and clear env so unknown names are not managed via legacy URL."""
    keys = (
        "MISSION_CONTROL_REPOSITORY_URL",
        "MISSION_CONTROL_REPOSITORY_URL_MAP",
        "MISSION_CONTROL_SELF_REPOSITORY_URL",
        "MISSION_CONTROL_LEGAL_AI_REPOSITORY_URL",
    )
    previous = {key: os.environ.get(key) for key in keys}
    for key in keys:
        os.environ.pop(key, None)
    return previous


def _restore_env(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class TestRunEligibility(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_path = str(REPO_ROOT)

    def test_accepts_eligible_mission(self) -> None:
        result = validate_mission_for_run(_base_mission(self.repo_path))
        self.assertTrue(result.ok)

    def test_rejects_non_cursor_agent(self) -> None:
        mission = _base_mission(self.repo_path)
        mission["execution"]["agent"] = "codex"
        result = validate_mission_for_run(mission)
        self.assertFalse(result.ok)
        self.assertIn("Unsupported agent", result.error or "")

    def test_rejects_execute_mode(self) -> None:
        mission = _base_mission(self.repo_path)
        mission["execution"]["mode"] = "execute"
        result = validate_mission_for_run(mission)
        self.assertFalse(result.ok)
        self.assertIn("Unsupported mode", result.error or "")

    def test_rejects_ask_mode(self) -> None:
        mission = _base_mission(self.repo_path)
        mission["execution"]["mode"] = "ask"
        result = validate_mission_for_run(mission)
        self.assertFalse(result.ok)
        self.assertIn("Unsupported mode", result.error or "")

    def test_rejects_create_files_true(self) -> None:
        mission = _base_mission(self.repo_path)
        mission["permissions"]["create_files"] = True
        result = validate_mission_for_run(mission)
        self.assertFalse(result.ok)
        self.assertIn("create_files", result.error or "")

    def test_rejects_modify_files_true(self) -> None:
        mission = _base_mission(self.repo_path)
        mission["permissions"]["modify_files"] = True
        result = validate_mission_for_run(mission)
        self.assertFalse(result.ok)
        self.assertIn("modify_files", result.error or "")

    def test_rejects_delete_files_true(self) -> None:
        mission = _base_mission(self.repo_path)
        mission["permissions"]["delete_files"] = True
        result = validate_mission_for_run(mission)
        self.assertFalse(result.ok)
        self.assertIn("delete_files", result.error or "")

    def test_rejects_stage_changes_true(self) -> None:
        mission = _base_mission(self.repo_path)
        mission["permissions"]["stage_changes"] = True
        result = validate_mission_for_run(mission)
        self.assertFalse(result.ok)
        self.assertIn("stage_changes", result.error or "")

    def test_rejects_commit_true(self) -> None:
        mission = _base_mission(self.repo_path)
        mission["permissions"]["commit"] = True
        result = validate_mission_for_run(mission)
        self.assertFalse(result.ok)
        self.assertIn("commit", result.error or "")

    def test_rejects_push_true(self) -> None:
        mission = _base_mission(self.repo_path)
        mission["permissions"]["push"] = True
        result = validate_mission_for_run(mission)
        self.assertFalse(result.ok)
        self.assertIn("push", result.error or "")

    def test_rejects_worktree_requested(self) -> None:
        mission = _base_mission(self.repo_path)
        mission["execution"]["worktree"] = True
        result = validate_mission_for_run(mission)
        self.assertFalse(result.ok)
        self.assertIn("Worktrees", result.error or "")

    def test_rejects_missing_repository_path_without_managed_identity(self) -> None:
        mission = _base_mission(self.repo_path, name="unknown-local-repo")
        mission["repository"]["path"] = ""
        previous = _without_managed_url_env()
        try:
            result = validate_mission_for_run(mission)
        finally:
            _restore_env(previous)
        self.assertFalse(result.ok)
        self.assertIn("repository.path", result.error or "")

    def test_rejects_nonexistent_repository_path_without_managed_identity(
        self,
    ) -> None:
        mission = _base_mission("/does/not/exist", name="unknown-local-repo")
        previous = _without_managed_url_env()
        try:
            result = validate_mission_for_run(mission)
        finally:
            _restore_env(previous)
        self.assertFalse(result.ok)
        self.assertIn("does not exist", result.error or "")

    def test_rejects_repository_path_that_is_not_directory(self) -> None:
        with tempfile.NamedTemporaryFile() as handle:
            mission = _base_mission(handle.name, name="unknown-local-repo")
            previous = _without_managed_url_env()
            try:
                result = validate_mission_for_run(mission)
            finally:
                _restore_env(previous)
            self.assertFalse(result.ok)
            self.assertIn("not a directory", result.error or "")


class TestManagedRepositoryPathResolution(unittest.TestCase):
    """Regression: canonical repository.name owns path resolution."""

    def test_absent_path_with_legal_ai_identity_normalizes(self) -> None:
        mission = _execute_mission("", name="legal-ai")
        result = validate_mission_for_execute(mission)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(mission["repository"]["path"], ".")

    def test_stale_workspace_legal_ai_path_is_ignored(self) -> None:
        mission = _execute_mission("/workspace/legal-ai", name="legal-ai")
        result = validate_mission_for_execute(mission)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(mission["repository"]["path"], ".")
        self.assertNotIn("does not exist", result.error or "")

    def test_valid_managed_path_is_accepted(self) -> None:
        mission = _execute_mission(
            str(REPO_ROOT),
            name="nhpcorp35/mission-control",
        )
        result = validate_mission_for_execute(mission)
        self.assertTrue(result.ok, result.error)
        normalized = mission["repository"]["path"]
        self.assertTrue(
            normalized == "." or Path(normalized).is_dir(),
            normalized,
        )

    def test_dot_path_with_legal_ai_identity_succeeds(self) -> None:
        mission = _execute_mission(".", name="legal-ai")
        result = validate_mission_for_execute(mission)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(mission["repository"]["path"], ".")

    def test_rejects_path_traversal_relative_segments(self) -> None:
        mission = _execute_mission("../other-repo", name="legal-ai")
        result = validate_mission_for_execute(mission)
        self.assertFalse(result.ok)
        self.assertIn("..", result.error or "")

    def test_identity_mismatch_for_existing_foreign_checkout(self) -> None:
        from mission_control.workspace import (
            get_origin_url,
            normalize_remote_url_identity,
            resolve_mission_clone_url,
        )

        mission = _execute_mission(str(REPO_ROOT), name="legal-ai")
        expected, _ = resolve_mission_clone_url(mission)
        actual = get_origin_url(str(REPO_ROOT))
        if not actual or not expected:
            self.skipTest("workspace origin unavailable for mismatch assertion")
        if normalize_remote_url_identity(expected) == normalize_remote_url_identity(
            actual
        ):
            self.skipTest("checkout origin already matches legal-ai")
        result = validate_mission_for_execute(mission)
        self.assertFalse(result.ok)
        self.assertTrue(
            (result.error or "").startswith(
                REPOSITORY_PATH_IDENTITY_MISMATCH_PREFIX
            ),
            result.error,
        )


if __name__ == "__main__":
    unittest.main()
