"""Regression tests for Phase 1B validate behavior."""

import io
import subprocess
import sys
import unittest
from pathlib import Path

from mission_control.validator import (
    validate_mission,
    validate_mission_file,
    validate_mission_for_execute,
)
from mission_control.workspace import PLATFORM_PUSH_APPROVAL_REQUIRED

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE = REPO_ROOT / "missions" / "reference"


def _executable_mission(
    *,
    persistence_mode: str | None = None,
    platform_push_approved: bool | None = None,
    allow_automatic_platform_push: bool | None = None,
    permissions_push: bool = False,
) -> dict:
    mission: dict = {
        "version": "1.0",
        "mission_id": "2026-07-22-platform-push",
        "title": "Platform Push Approval",
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
            "push": permissions_push,
        },
        "instructions": "Create a file.",
        "deliverables": ["summary"],
        "approval": {
            "execute_without_approval": True,
            "commit_requires_approval": True,
            "push_requires_approval": True,
        },
    }
    if persistence_mode is not None:
        mission["persistence"] = {"mode": persistence_mode}
    if platform_push_approved is not None:
        mission["approval"]["platform_push_approved"] = platform_push_approved
    if allow_automatic_platform_push is not None:
        mission["approval"]["allow_automatic_platform_push"] = (
            allow_automatic_platform_push
        )
    return mission


class TestValidateMission(unittest.TestCase):
    def test_accepts_valid_structure(self) -> None:
        result = validate_mission_file(str(REFERENCE / "valid-v1.0.yaml"))
        self.assertTrue(result.ok)
        self.assertIsNone(result.error)

    def test_rejects_bad_version(self) -> None:
        result = validate_mission_file(str(REFERENCE / "invalid-bad-version.yaml"))
        self.assertFalse(result.ok)
        self.assertIn("Unsupported version", result.error or "")

    def test_rejects_missing_permissions(self) -> None:
        result = validate_mission_file(
            str(REFERENCE / "invalid-missing-permissions.yaml")
        )
        self.assertFalse(result.ok)
        self.assertIn("Missing required keys: permissions", result.error or "")

    def test_accepts_float_version_1_0(self) -> None:
        mission = {
            "version": 1.0,
            "mission_id": "test",
            "title": "Test",
            "repository": {},
            "execution": {},
            "permissions": {},
            "instructions": "Do something.",
            "deliverables": [],
            "approval": {},
        }
        result = validate_mission(mission)
        self.assertTrue(result.ok)

    def test_omitted_persistence_defaults_to_valid(self) -> None:
        mission = {
            "version": "1.0",
            "mission_id": "test",
            "title": "Test",
            "repository": {},
            "execution": {},
            "permissions": {},
            "instructions": "Do something.",
            "deliverables": [],
            "approval": {},
        }
        self.assertNotIn("persistence", mission)
        result = validate_mission(mission)
        self.assertTrue(result.ok)

    def test_accepts_supported_persistence_modes(self) -> None:
        for mode in ("none", "commit", "push"):
            with self.subTest(mode=mode):
                mission = {
                    "version": "1.0",
                    "mission_id": "test",
                    "title": "Test",
                    "repository": {},
                    "execution": {},
                    "permissions": {},
                    "persistence": {"mode": mode},
                    "instructions": "Do something.",
                    "deliverables": [],
                    "approval": {},
                }
                result = validate_mission(mission)
                self.assertTrue(result.ok, result.error)

    def test_rejects_unsupported_persistence_mode(self) -> None:
        mission = {
            "version": "1.0",
            "mission_id": "test",
            "title": "Test",
            "repository": {},
            "execution": {},
            "permissions": {},
            "persistence": {"mode": "rebase"},
            "instructions": "Do something.",
            "deliverables": [],
            "approval": {},
        }
        result = validate_mission(mission)
        self.assertFalse(result.ok)
        self.assertIn("Unsupported persistence.mode", result.error or "")
        self.assertIn("rebase", result.error or "")

    def test_rejects_non_mapping_persistence(self) -> None:
        mission = {
            "version": "1.0",
            "mission_id": "test",
            "title": "Test",
            "repository": {},
            "execution": {},
            "permissions": {},
            "persistence": "push",
            "instructions": "Do something.",
            "deliverables": [],
            "approval": {},
        }
        result = validate_mission(mission)
        self.assertFalse(result.ok)
        self.assertIn("persistence must be a mapping", result.error or "")


class TestPlatformPushApprovalForExecute(unittest.TestCase):
    def test_execute_rejects_push_without_platform_push_approval(self) -> None:
        result = validate_mission_for_execute(
            _executable_mission(persistence_mode="push")
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, PLATFORM_PUSH_APPROVAL_REQUIRED)

    def test_execute_accepts_push_when_platform_push_approved(self) -> None:
        result = validate_mission_for_execute(
            _executable_mission(
                persistence_mode="push",
                platform_push_approved=True,
            )
        )
        self.assertTrue(result.ok, result.error)

    def test_execute_accepts_push_with_automatic_platform_push_policy(
        self,
    ) -> None:
        result = validate_mission_for_execute(
            _executable_mission(
                persistence_mode="push",
                allow_automatic_platform_push=True,
            )
        )
        self.assertTrue(result.ok, result.error)

    def test_execute_none_does_not_require_platform_push_approval(self) -> None:
        result = validate_mission_for_execute(
            _executable_mission(persistence_mode="none")
        )
        self.assertTrue(result.ok, result.error)

    def test_execute_commit_does_not_require_platform_push_approval(self) -> None:
        result = validate_mission_for_execute(
            _executable_mission(persistence_mode="commit")
        )
        self.assertTrue(result.ok, result.error)

    def test_execute_agent_push_requires_approval_does_not_authorize_platform_push(
        self,
    ) -> None:
        """Agent approval.push_requires_approval=false is not platform-push approval."""
        mission = _executable_mission(persistence_mode="push")
        mission["approval"]["push_requires_approval"] = False
        result = validate_mission_for_execute(mission)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, PLATFORM_PUSH_APPROVAL_REQUIRED)


class TestValidateCli(unittest.TestCase):
    def _run_mc(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "mc.py"), *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

    def test_validate_valid_reference_mission(self) -> None:
        result = self._run_mc("validate", "missions/reference/valid-v1.0.yaml")
        self.assertEqual(result.returncode, 0)
        self.assertIn("\u2713 Mission valid", result.stdout)

    def test_validate_invalid_bad_version(self) -> None:
        result = self._run_mc(
            "validate", "missions/reference/invalid-bad-version.yaml"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("\u2717 Mission invalid", result.stdout)
        self.assertIn("Unsupported version", result.stdout)

    def test_validate_invalid_missing_permissions(self) -> None:
        result = self._run_mc(
            "validate", "missions/reference/invalid-missing-permissions.yaml"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("\u2717 Mission invalid", result.stdout)
        self.assertIn("Missing required keys: permissions", result.stdout)

    def test_validate_cli_usage(self) -> None:
        result = self._run_mc()
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
