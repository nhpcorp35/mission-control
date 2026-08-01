"""Regression: agent shells must see venv Python/pip and pypdf on Railway."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
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
    """Run start script through wrapper setup; unknown SERVICE_MODE exits after."""
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


def _assert_wrapper(test: unittest.TestCase, wrapper: Path, target: Path) -> None:
    """Assert wrapper is an executable regular file that execs the venv target."""
    test.assertTrue(wrapper.is_file(), msg=f"{wrapper.name} must be a regular file")
    test.assertFalse(wrapper.is_symlink(), msg=f"{wrapper.name} must not be a symlink")
    test.assertTrue(
        os.access(wrapper, os.X_OK),
        msg=f"{wrapper.name} must be executable",
    )
    body = wrapper.read_text(encoding="utf-8")
    test.assertTrue(body.startswith("#!/bin/sh\n"), msg=f"{wrapper.name} shebang")
    # railway-start.sh always single-quotes the absolute target path.
    expected_exec = f"exec '{target}' \"$@\"\n"
    test.assertIn(
        expected_exec,
        body,
        msg=f"{wrapper.name} must exec absolute venv target with argument forwarding",
    )
    # Target path in wrapper must stay under the venv bin, never local_bin.
    test.assertIn(str(target), body)
    test.assertNotIn(str(wrapper.parent), body.split("\n")[1])


def _create_temp_venv(venv_dir: Path) -> Path:
    """Create a working temp venv (resolve through broken /usr/local/bin symlinks)."""
    creator = Path(sys.executable).resolve()
    subprocess.run(
        [str(creator), "-m", "venv", "--without-pip", str(venv_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    venv_bin = venv_dir / "bin"
    for name in ("python3", "python"):
        candidate = venv_bin / name
        if candidate.exists():
            return candidate
    raise AssertionError(f"temp venv missing python executable under {venv_bin}")


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

    def test_exposes_venv_python_and_pip_as_wrappers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            venv_bin = tmp_path / "venv" / "bin"
            local_bin = tmp_path / "usr-local-bin"
            venv_bin.mkdir(parents=True)
            local_bin.mkdir(parents=True)

            for name in ("python3", "python", "pip3", "pip"):
                # Echo markers prove wrappers forward argv and invoke the target.
                _make_executable(
                    venv_bin / name,
                    body=(
                        "#!/bin/sh\n"
                        f'echo "TARGET={name}"\n'
                        'printf "ARG:%s\\n" "$@"\n'
                        "exit 0\n"
                    ),
                )

            result = _run_start_script(venv_bin=venv_bin, local_bin=local_bin)
            self.assertEqual(
                result.returncode,
                1,
                msg=f"expected unknown SERVICE_MODE exit; stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}",
            )
            self.assertIn("__python_runtime_test__", result.stdout)

            for name in ("python3", "python", "pip3", "pip"):
                wrapper = local_bin / name
                target = venv_bin / name
                _assert_wrapper(self, wrapper, target)

                probe = subprocess.run(
                    [str(wrapper), "alpha", "beta gamma"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(probe.returncode, 0, msg=probe.stderr)
                self.assertIn(f"TARGET={name}", probe.stdout)
                self.assertIn("ARG:alpha", probe.stdout)
                self.assertIn("ARG:beta gamma", probe.stdout)

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
            bodies_first = {
                name: (local_bin / name).read_text(encoding="utf-8")
                for name in ("python3", "python", "pip3", "pip")
            }
            second = _run_start_script(venv_bin=venv_bin, local_bin=local_bin)
            self.assertEqual(first.returncode, 1)
            self.assertEqual(second.returncode, 1)

            for name in ("python3", "python", "pip3", "pip"):
                wrapper = local_bin / name
                _assert_wrapper(self, wrapper, venv_bin / name)
                self.assertEqual(
                    wrapper.read_text(encoding="utf-8"),
                    bodies_first[name],
                    msg=f"{name} wrapper must be stable across re-runs",
                )

    def test_missing_optional_targets_do_not_create_wrappers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            venv_bin = tmp_path / "venv" / "bin"
            local_bin = tmp_path / "usr-local-bin"
            venv_bin.mkdir(parents=True)
            local_bin.mkdir(parents=True)

            # Only python/python3 exist; pip/pip3 absent or non-executable.
            _make_executable(venv_bin / "python3")
            _make_executable(venv_bin / "python")
            (venv_bin / "pip3").write_text("not executable\n", encoding="utf-8")

            result = _run_start_script(venv_bin=venv_bin, local_bin=local_bin)
            self.assertEqual(result.returncode, 1)

            _assert_wrapper(self, local_bin / "python3", venv_bin / "python3")
            _assert_wrapper(self, local_bin / "python", venv_bin / "python")
            self.assertFalse(
                (local_bin / "pip3").exists(),
                msg="non-executable pip3 must not create a wrapper",
            )
            self.assertFalse(
                (local_bin / "pip").exists(),
                msg="missing pip must not create a wrapper",
            )

    def test_wrapper_preserves_venv_sys_prefix_and_imports(self) -> None:
        """Regression: wrappers must not lose venv prefix the way symlinks do."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            venv_dir = tmp_path / "venv"
            local_bin = tmp_path / "usr-local-bin"
            local_bin.mkdir(parents=True)

            venv_python = _create_temp_venv(venv_dir)
            venv_bin = venv_python.parent

            # Install a sentinel package without network access.
            purelib = subprocess.check_output(
                [
                    str(venv_python),
                    "-c",
                    "import sysconfig; print(sysconfig.get_path('purelib'))",
                ],
                text=True,
            ).strip()
            sentinel_dir = Path(purelib) / "mc_venv_sentinel"
            sentinel_dir.mkdir(parents=True)
            (sentinel_dir / "__init__.py").write_text(
                'MARKER = "from-temp-venv"\n',
                encoding="utf-8",
            )

            # Confirm direct venv interpreter sees the sentinel.
            direct = subprocess.run(
                [
                    str(venv_python),
                    "-c",
                    "import sys, mc_venv_sentinel; "
                    "print(sys.prefix); print(mc_venv_sentinel.MARKER)",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(direct.returncode, 0, msg=direct.stderr)
            self.assertEqual(direct.stdout.splitlines()[0], str(venv_dir))
            self.assertEqual(direct.stdout.splitlines()[1], "from-temp-venv")

            # Symlink contrast: linking to the venv binary from outside the venv
            # loses sys.prefix (the production /usr/local/bin failure mode).
            symlink_probe = local_bin / "python3-symlink-probe"
            symlink_probe.symlink_to(venv_python)
            via_symlink = subprocess.run(
                [
                    str(symlink_probe),
                    "-c",
                    "import sys; print(sys.prefix)",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            # On platforms that exhibit the bug, symlink prefix != venv_dir.
            # We still require the wrapper path to preserve the venv either way.

            result = _run_start_script(venv_bin=venv_bin, local_bin=local_bin)
            self.assertEqual(result.returncode, 1)

            wrapper = local_bin / "python3"
            if not wrapper.exists():
                wrapper = local_bin / "python"
            _assert_wrapper(self, wrapper, venv_bin / wrapper.name)

            via_wrapper = subprocess.run(
                [
                    str(wrapper),
                    "-c",
                    "import sys, mc_venv_sentinel; "
                    "print(sys.prefix); print(mc_venv_sentinel.MARKER)",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(via_wrapper.returncode, 0, msg=via_wrapper.stderr)
            self.assertEqual(via_wrapper.stdout.splitlines()[0], str(venv_dir))
            self.assertEqual(via_wrapper.stdout.splitlines()[1], "from-temp-venv")
            # Must not fall back to the base interpreter prefix.
            self.assertNotEqual(via_wrapper.stdout.splitlines()[0], sys.base_prefix)
            if via_symlink.returncode == 0:
                symlink_prefix = via_symlink.stdout.strip()
                if symlink_prefix != str(venv_dir):
                    self.assertNotEqual(
                        via_wrapper.stdout.splitlines()[0],
                        symlink_prefix,
                        msg="wrapper must preserve venv prefix that symlink loses",
                    )

if __name__ == "__main__":
    unittest.main()
