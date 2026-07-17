"""Tests for Phase 2 run-eligibility validation."""

import tempfile
import unittest
from pathlib import Path

from mission_control.validator import validate_mission_for_run

REPO_ROOT = Path(__file__).resolve().parent.parent


def _base_mission(repo_path: str) -> dict:
    return {
        "version": 1.0,
        "mission_id": "2026-07-17-001",
        "title": "Test Mission",
        "repository": {
            "name": "Mission-Control",
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

    def test_rejects_missing_repository_path(self) -> None:
        mission = _base_mission(self.repo_path)
        mission["repository"]["path"] = ""
        result = validate_mission_for_run(mission)
        self.assertFalse(result.ok)
        self.assertIn("repository.path", result.error or "")

    def test_rejects_nonexistent_repository_path(self) -> None:
        mission = _base_mission("/does/not/exist")
        result = validate_mission_for_run(mission)
        self.assertFalse(result.ok)
        self.assertIn("does not exist", result.error or "")

    def test_rejects_repository_path_that_is_not_directory(self) -> None:
        with tempfile.NamedTemporaryFile() as handle:
            mission = _base_mission(handle.name)
            result = validate_mission_for_run(mission)
            self.assertFalse(result.ok)
            self.assertIn("not a directory", result.error or "")


if __name__ == "__main__":
    unittest.main()
