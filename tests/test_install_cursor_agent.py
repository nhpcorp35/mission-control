"""Regression tests for Cursor Agent install packaging persistence."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install-cursor-agent.sh"


def _make_fake_cursor_home(home: Path) -> Path:
    """Create a minimal official-installer layout under *home*."""
    version_dir = (
        home / ".local" / "share" / "cursor-agent" / "versions" / "test-version"
    )
    version_dir.mkdir(parents=True)
    agent = version_dir / "cursor-agent"
    agent.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ] || [ \"$1\" = \"--help\" ]; then\n"
        "  echo 'test-version'\n"
        "  exit 0\n"
        "fi\n"
        "echo \"unexpected args: $*\" >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    agent.chmod(agent.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Companion file so we can assert the whole tree was copied.
    (version_dir / "companion.txt").write_text("runtime-companion\n", encoding="utf-8")

    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    link = bin_dir / "cursor-agent"
    link.symlink_to(agent)
    return agent


class TestInstallCursorAgentPersistence(unittest.TestCase):
    def test_packages_executable_into_app_cursor_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            app_root = tmp_path / "app"
            app_root.mkdir()
            # Simulate Railway Python venv present during install.
            (app_root / ".venv" / "bin").mkdir(parents=True)

            _make_fake_cursor_home(home)

            env = os.environ.copy()
            env["HOME"] = str(home)
            env["APP_ROOT"] = str(app_root)
            env["CURSOR_AGENT_SKIP_DOWNLOAD"] = "1"

            result = subprocess.run(
                ["bash", str(INSTALL_SCRIPT)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn("test-version", result.stdout)

            dest = app_root / ".cursor-runtime" / "cursor-agent"
            self.assertTrue(dest.is_file(), f"missing expected launcher: {dest}")
            self.assertTrue(
                os.access(dest, os.X_OK),
                f"expected executable permissions on {dest}",
            )
            self.assertFalse(dest.is_symlink(), "launcher must be a real file")
            self.assertEqual(
                (app_root / ".cursor-runtime" / "companion.txt").read_text(
                    encoding="utf-8"
                ),
                "runtime-companion\n",
            )

            # Railpack-persisted mirror under .venv
            venv_agent = app_root / ".venv" / ".cursor-runtime" / "cursor-agent"
            self.assertTrue(venv_agent.is_file())
            self.assertTrue(os.access(venv_agent, os.X_OK))
            venv_bin = app_root / ".venv" / "bin" / "cursor-agent"
            self.assertTrue(venv_bin.exists())

            version = subprocess.run(
                [str(dest), "--version"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("test-version", version.stdout)

    def test_packages_without_venv_for_local_dev(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            app_root = tmp_path / "app"
            app_root.mkdir()
            _make_fake_cursor_home(home)

            env = os.environ.copy()
            env["HOME"] = str(home)
            env["APP_ROOT"] = str(app_root)
            env["CURSOR_AGENT_SKIP_DOWNLOAD"] = "1"

            result = subprocess.run(
                ["bash", str(INSTALL_SCRIPT)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            dest = app_root / ".cursor-runtime" / "cursor-agent"
            self.assertTrue(dest.is_file())
            self.assertTrue(os.access(dest, os.X_OK))
            self.assertFalse((app_root / ".venv").exists())


if __name__ == "__main__":
    unittest.main()
