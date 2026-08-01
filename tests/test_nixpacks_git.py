"""Regression: Railway/Railpack image must install git on PATH."""

from __future__ import annotations

import shutil
import subprocess
import tomllib
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
NIXPACKS_TOML = REPO_ROOT / "nixpacks.toml"


class TestNixpacksGitAvailability(unittest.TestCase):
    def test_nixpacks_apt_pkgs_include_git(self) -> None:
        raw = NIXPACKS_TOML.read_text(encoding="utf-8")
        config = tomllib.loads(raw)
        apt_pkgs = config["phases"]["setup"]["aptPkgs"]
        self.assertIn(
            "git",
            apt_pkgs,
            msg=(
                "nixpacks.toml [phases.setup] aptPkgs must include git so the "
                "Railway executor runtime can clone repositories"
            ),
        )
        # Preserve Cursor Agent install + existing packages.
        self.assertIn("curl", apt_pkgs)
        self.assertIn("python3", apt_pkgs)
        self.assertEqual(
            config["phases"]["build"]["cmds"],
            ["bash scripts/install-cursor-agent.sh"],
        )

    def test_git_executable_resolves_on_path(self) -> None:
        git_path = shutil.which("git")
        self.assertIsNotNone(
            git_path,
            msg="git must be available on PATH in the executor runtime",
        )
        assert git_path is not None
        version = subprocess.run(
            [git_path, "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("git version", version.stdout.lower())
        self.assertTrue(Path(git_path).is_file())


if __name__ == "__main__":
    unittest.main()
