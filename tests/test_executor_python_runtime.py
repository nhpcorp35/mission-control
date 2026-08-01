"""Regression: agent shells must see venv Python/pip and pypdf on Railway."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO_ROOT / "requirements.txt"
START_SCRIPT = REPO_ROOT / "scripts" / "railway-start.sh"

# SERVICE_MODE case body must stay the production dispatch (unchanged).
EXPECTED_SERVICE_MODE_CASE = """
case "${SERVICE_MODE:-api}" in
  mcp)
    echo "Starting MCP server"
    exec python -m mcp_connector.server
    ;;
  api)
    echo "Starting API server"
    exec uvicorn app.api:app \\
      --host 0.0.0.0 \\
      --port "${PORT:-8080}"
    ;;
  *)
    echo "Unknown SERVICE_MODE: ${SERVICE_MODE}" >&2
    exit 1
    ;;
esac
""".strip()


def _make_executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_start_script(*, venv_bin: Path, local_bin: Path) -> subprocess.CompletedProcess[str]:
    """Run start script through symlink setup; unknown SERVICE_MODE exits after."""
    env = os.environ.copy()
    env["MC_VENV_BIN"] = str(venv_bin)
    env["MC_LOCAL_BIN"] = str(local_bin)
    # Unknown mode: exercise expose logic, then exit without starting servers.
    env["SERVICE_MODE"] = "__python_runtime_test__"
    return subprocess.run(
        ["bash", str(START_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )


class TestExecutorPythonRuntime(unittest.TestCase):
    def test_requirements_declares_pypdf(self) -> None:
        text = REQUIREMENTS.read_text(encoding="utf-8")
        pins = re.findall(r"(?m)^pypdf(?:==\S+)?$", text)
        self.assertEqual(
            pins,
            ["pypdf"],
            msg="requirements.txt must declare unpinned pypdf for agent PDF work",
        )

    def test_service_mode_dispatch_unchanged(self) -> None:
        text = START_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            EXPECTED_SERVICE_MODE_CASE,
            text,
            msg="SERVICE_MODE dispatch in railway-start.sh must remain unchanged",
        )

    def test_exposes_venv_python_and_pip_into_usr_local_bin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            venv_bin = tmp_path / "venv" / "bin"
            local_bin = tmp_path / "usr-local-bin"
            venv_bin.mkdir(parents=True)
            local_bin.mkdir(parents=True)

            for name in ("python3", "python", "pip3", "pip"):
                _make_executable(venv_bin / name)

            result = _run_start_script(venv_bin=venv_bin, local_bin=local_bin)
            self.assertEqual(
                result.returncode,
                1,
                msg=f"expected unknown SERVICE_MODE exit; stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}",
            )
            self.assertIn("__python_runtime_test__", result.stdout)

            for name in ("python3", "python", "pip3", "pip"):
                link = local_bin / name
                self.assertTrue(link.is_symlink(), msg=f"{name} should be a symlink")
                self.assertEqual(
                    link.resolve(),
                    (venv_bin / name).resolve(),
                    msg=f"{name} symlink target mismatch",
                )
                self.assertTrue(
                    os.access(link, os.X_OK),
                    msg=f"{name} symlink must resolve to an executable",
                )

    def test_expose_logic_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            venv_bin = tmp_path / "venv" / "bin"
            local_bin = tmp_path / "usr-local-bin"
            venv_bin.mkdir(parents=True)
            local_bin.mkdir(parents=True)
            for name in ("python3", "python", "pip3", "pip"):
                _make_executable(venv_bin / name)

            first = _run_start_script(venv_bin=venv_bin, local_bin=local_bin)
            second = _run_start_script(venv_bin=venv_bin, local_bin=local_bin)
            self.assertEqual(first.returncode, 1)
            self.assertEqual(second.returncode, 1)

            for name in ("python3", "python", "pip3", "pip"):
                link = local_bin / name
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.resolve(), (venv_bin / name).resolve())

    def test_missing_optional_targets_do_not_create_broken_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            venv_bin = tmp_path / "venv" / "bin"
            local_bin = tmp_path / "usr-local-bin"
            venv_bin.mkdir(parents=True)
            local_bin.mkdir(parents=True)

            # Only python/python3 exist; pip/pip3 absent.
            _make_executable(venv_bin / "python3")
            _make_executable(venv_bin / "python")
            # Non-executable decoy must not be linked.
            (venv_bin / "pip3").write_text("not executable\n", encoding="utf-8")

            result = _run_start_script(venv_bin=venv_bin, local_bin=local_bin)
            self.assertEqual(result.returncode, 1)

            self.assertTrue((local_bin / "python3").is_symlink())
            self.assertTrue((local_bin / "python").is_symlink())
            self.assertFalse(
                (local_bin / "pip3").exists(),
                msg="non-executable pip3 must not create a symlink",
            )
            self.assertFalse(
                (local_bin / "pip").exists(),
                msg="missing pip must not create a broken symlink",
            )

            for name in ("python3", "python"):
                link = local_bin / name
                self.assertTrue(link.exists(), msg=f"{name} symlink must not be broken")
                self.assertTrue(os.access(link, os.X_OK))


if __name__ == "__main__":
    unittest.main()
