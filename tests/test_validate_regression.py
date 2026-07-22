"""Regression tests for Phase 1B validate behavior."""

import io
import subprocess
import sys
import unittest
from pathlib import Path

from mission_control.validator import validate_mission, validate_mission_file

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE = REPO_ROOT / "missions" / "reference"


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
