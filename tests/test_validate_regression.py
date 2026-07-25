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
    stage_changes: bool = False,
    commit: bool = False,
    create_files: bool = True,
    modify_files: bool = False,
    delete_files: bool = False,
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
            "create_files": create_files,
            "modify_files": modify_files,
            "delete_files": delete_files,
            "run_commands": True,
            "stage_changes": stage_changes,
            "commit": commit,
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

    def test_execute_push_only_accepted_without_create_or_modify_files(
        self,
    ) -> None:
        result = validate_mission_for_execute(
            _executable_mission(
                persistence_mode="push",
                platform_push_approved=True,
                create_files=False,
                modify_files=False,
            )
        )
        self.assertTrue(result.ok, result.error)

    def test_execute_push_only_with_automatic_policy_without_file_perms(
        self,
    ) -> None:
        result = validate_mission_for_execute(
            _executable_mission(
                persistence_mode="push",
                allow_automatic_platform_push=True,
                create_files=False,
                modify_files=False,
            )
        )
        self.assertTrue(result.ok, result.error)

    def test_execute_push_only_still_requires_platform_push_approval(
        self,
    ) -> None:
        result = validate_mission_for_execute(
            _executable_mission(
                persistence_mode="push",
                create_files=False,
                modify_files=False,
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, PLATFORM_PUSH_APPROVAL_REQUIRED)

    def test_execute_read_only_accepted_without_create_or_modify_files(
        self,
    ) -> None:
        """Read-only inspection missions use execution.mode: execute."""
        for mode in ("none", "commit", None):
            with self.subTest(mode=mode):
                result = validate_mission_for_execute(
                    _executable_mission(
                        persistence_mode=mode,
                        create_files=False,
                        modify_files=False,
                    )
                )
                self.assertTrue(result.ok, result.error)

    def test_execute_implementation_mission_still_accepted(self) -> None:
        """Normal implementation missions (create and/or modify) still work."""
        cases = (
            (True, False),
            (False, True),
            (True, True),
        )
        for create_files, modify_files in cases:
            with self.subTest(
                create_files=create_files,
                modify_files=modify_files,
            ):
                result = validate_mission_for_execute(
                    _executable_mission(
                        persistence_mode="none",
                        create_files=create_files,
                        modify_files=modify_files,
                    )
                )
                self.assertTrue(result.ok, result.error)

    def test_execute_write_permissions_still_accepted_as_before(self) -> None:
        """Execute with write permissions keeps prior acceptance behavior."""
        result = validate_mission_for_execute(
            _executable_mission(
                persistence_mode="commit",
                create_files=True,
                modify_files=False,
            )
        )
        self.assertTrue(result.ok, result.error)
        result_modify = validate_mission_for_execute(
            _executable_mission(
                persistence_mode="commit",
                create_files=False,
                modify_files=True,
            )
        )
        self.assertTrue(result_modify.ok, result_modify.error)

    def test_execute_without_file_perms_rejected_when_not_read_only(
        self,
    ) -> None:
        """Non-push execute without create/modify must be exact read-only."""
        for mode in ("none", "commit", None):
            with self.subTest(mode=mode):
                result = validate_mission_for_execute(
                    _executable_mission(
                        persistence_mode=mode,
                        create_files=False,
                        modify_files=False,
                        stage_changes=True,
                    )
                )
                self.assertFalse(result.ok)
                self.assertIn(
                    "create_files or modify_files",
                    result.error or "",
                )

    def test_execute_unauthorized_writes_still_rejected(self) -> None:
        """Unauthorized write-related permissions remain rejected."""
        result = validate_mission_for_execute(
            _executable_mission(
                persistence_mode="none",
                create_files=False,
                modify_files=False,
                delete_files=True,
            )
        )
        self.assertFalse(result.ok)
        # delete_files fails either the read-only gate or EXECUTE_FALSE check
        self.assertTrue(
            "create_files or modify_files" in (result.error or "")
            or "delete_files" in (result.error or ""),
            result.error,
        )

    def test_execute_push_does_not_require_permissions_push(self) -> None:
        result = validate_mission_for_execute(
            _executable_mission(
                persistence_mode="push",
                platform_push_approved=True,
                permissions_push=False,
                create_files=False,
                modify_files=False,
            )
        )
        self.assertTrue(result.ok, result.error)
        # Legacy permissions.push=true is accepted but does not authorize
        # platform push by itself (approval still required when mode=push).
        with_legacy_push = validate_mission_for_execute(
            _executable_mission(
                persistence_mode="push",
                platform_push_approved=True,
                permissions_push=True,
            )
        )
        self.assertTrue(with_legacy_push.ok, with_legacy_push.error)

    def test_execute_accepts_legacy_stage_changes_with_persistence_modes(
        self,
    ) -> None:
        """Regression: stage_changes must not reject execute eligibility.

        Previously rejected with:
        Permission not allowed for execute: stage_changes
        Platform Git is selected via persistence.mode instead.
        """
        cases = (
            ("none", False, False),
            ("commit", False, False),
            ("push", True, False),
            ("push", False, True),
        )
        for mode, approved, automatic in cases:
            with self.subTest(mode=mode, approved=approved, automatic=automatic):
                result = validate_mission_for_execute(
                    _executable_mission(
                        persistence_mode=mode,
                        platform_push_approved=True if approved else None,
                        allow_automatic_platform_push=(
                            True if automatic else None
                        ),
                        stage_changes=True,
                        commit=True,
                        permissions_push=True,
                    )
                )
                self.assertTrue(result.ok, result.error)

    def test_execute_still_rejects_delete_files(self) -> None:
        result = validate_mission_for_execute(
            _executable_mission(
                persistence_mode="commit",
                delete_files=True,
            )
        )
        self.assertFalse(result.ok)
        self.assertIn("delete_files", result.error or "")
        self.assertIn("not allowed for execute", result.error or "")

    def test_execute_legacy_git_flags_do_not_bypass_platform_push_approval(
        self,
    ) -> None:
        result = validate_mission_for_execute(
            _executable_mission(
                persistence_mode="push",
                stage_changes=True,
                commit=True,
                permissions_push=True,
            )
        )
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
