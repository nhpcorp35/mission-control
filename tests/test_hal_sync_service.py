"""Focused tests for tools/hal-sync-service (local Git fixtures only)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICE_DIR = REPO_ROOT / "tools" / "hal-sync-service"
SYNC_SH = SERVICE_DIR / "sync.sh"
INSTALL_SH = SERVICE_DIR / "install.sh"
UNINSTALL_SH = SERVICE_DIR / "uninstall.sh"
CONFIG_EXAMPLE = SERVICE_DIR / "config.env.example"
PLIST_TEMPLATE = (
    SERVICE_DIR / "launchd" / "com.nhpcorp.hal-sync.plist.template"
)


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=merged,
        text=True,
        capture_output=True,
        check=check,
    )


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd=cwd, check=check)


def _init_identity(repo: Path) -> None:
    _git(repo, "config", "user.email", "hal-sync-test@example.com")
    _git(repo, "config", "user.name", "HAL Sync Test")


def _create_clone_pair(tmp: Path) -> tuple[Path, Path]:
    """Create a bare origin and a clean main-branch clone. No network."""
    bare = tmp / "origin.git"
    work = tmp / "work"
    _git(tmp, "init", "--bare", str(bare))
    _git(tmp, "clone", str(bare), str(work))
    _init_identity(work)
    # Ensure branch is main
    _git(work, "checkout", "-B", "main")
    (work / "README").write_text("seed\n", encoding="utf-8")
    _git(work, "add", "README")
    _git(work, "commit", "-m", "seed")
    _git(work, "push", "-u", "origin", "main")
    return bare, work


def _advance_origin(bare: Path, tmp: Path, message: str) -> None:
    """Push a new commit to origin/main via a second local clone."""
    pusher = tmp / f"pusher-{message.replace(' ', '-')}"
    _git(tmp, "clone", str(bare), str(pusher))
    _init_identity(pusher)
    _git(pusher, "checkout", "main")
    readme = pusher / "README"
    readme.write_text(readme.read_text(encoding="utf-8") + message + "\n", encoding="utf-8")
    _git(pusher, "add", "README")
    _git(pusher, "commit", "-m", message)
    _git(pusher, "push", "origin", "main")


def _write_config(path: Path, repos: list[Path], **extra: str) -> None:
    lines = [
        f'HAL_SYNC_REPOS="{" ".join(str(p) for p in repos)}"',
        "HAL_SYNC_INTERVAL_SECONDS=60",
        "HAL_SYNC_LOG_MAX_BYTES=1048576",
    ]
    for key, value in extra.items():
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _bash_lib_call(script_body: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Source sync.sh functions and run a small bash snippet."""
    wrapper = textwrap.dedent(
        f"""\
        set -euo pipefail
        source "{SYNC_SH}"
        {script_body}
        """
    )
    return _run(["bash", "-c", wrapper], env=env, check=False)


class TestHalSyncServiceArtifacts(unittest.TestCase):
    def test_required_files_exist(self) -> None:
        for path in (
            SYNC_SH,
            INSTALL_SH,
            UNINSTALL_SH,
            CONFIG_EXAMPLE,
            PLIST_TEMPLATE,
            SERVICE_DIR / "README.md",
        ):
            self.assertTrue(path.is_file(), f"missing {path}")

    def test_shell_syntax(self) -> None:
        for script in (SYNC_SH, INSTALL_SH, UNINSTALL_SH):
            result = _run(["bash", "-n", str(script)], check=False)
            self.assertEqual(
                result.returncode,
                0,
                f"syntax error in {script}:\n{result.stderr}",
            )

    def test_example_config_documents_mission_control_path(self) -> None:
        text = CONFIG_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("/Users/allenk/Desktop/Mission-Control", text)
        self.assertIn("HAL_SYNC_INTERVAL_SECONDS=60", text)

    def test_plist_template_uses_start_interval_placeholder(self) -> None:
        text = PLIST_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("<key>StartInterval</key>", text)
        self.assertIn("__HAL_SYNC_INTERVAL_SECONDS__", text)
        self.assertIn("__HAL_SYNC_SCRIPT__", text)
        self.assertNotIn("Listen", text)


class TestHalSyncConfigParsing(unittest.TestCase):
    def test_parse_space_and_newline_separated_repos(self) -> None:
        result = _bash_lib_call(
            r"""
            out="$(hal_sync_parse_repos $'/tmp/a\n/tmp/b /tmp/c')"
            printf '%s\n' "$out"
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        self.assertEqual(lines, ["/tmp/a", "/tmp/b", "/tmp/c"])

    def test_parse_empty_repos_fails(self) -> None:
        result = _bash_lib_call('hal_sync_parse_repos "   "')
        self.assertNotEqual(result.returncode, 0)


class TestHalSyncCleanTreeAndFfOnly(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hal-sync-test-")
        self.tmp = Path(self._tmp.name)
        self.log_dir = self.tmp / "logs"
        self.log_dir.mkdir()
        self.bare, self.work = _create_clone_pair(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_sync(self, repos: list[Path]) -> subprocess.CompletedProcess[str]:
        config = self.tmp / "config.env"
        _write_config(
            config,
            repos,
            HAL_SYNC_LOG_DIR=f'"{self.log_dir}"',
            HAL_SYNC_LOCK_DIR=f'"{self.log_dir / "hal-sync.lock"}"',
        )
        return _run(
            ["bash", str(SYNC_SH)],
            env={"HAL_SYNC_CONFIG": str(config)},
            check=False,
        )

    def test_skips_dirty_working_tree(self) -> None:
        (self.work / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        before = _git(self.work, "rev-parse", "HEAD").stdout.strip()
        _advance_origin(self.bare, self.tmp, "remote-only")
        result = self._run_sync([self.work])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        after = _git(self.work, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(before, after)
        combined = result.stdout + result.stderr
        log_text = ""
        log_file = self.log_dir / "hal-sync.log"
        if log_file.is_file():
            log_text = log_file.read_text(encoding="utf-8")
        self.assertIn("working tree is not clean", combined + log_text)

    def test_ff_only_updates_when_clean_and_behind(self) -> None:
        before = _git(self.work, "rev-parse", "HEAD").stdout.strip()
        _advance_origin(self.bare, self.tmp, "ahead-commit")
        origin_main = _git(self.work, "ls-remote", "origin", "refs/heads/main").stdout.split()[0]
        self.assertNotEqual(before, origin_main)
        result = self._run_sync([self.work])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        after = _git(self.work, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(after, origin_main)
        combined = result.stdout + (self.log_dir / "hal-sync.log").read_text(encoding="utf-8")
        self.assertIn("Fast-forward complete", combined)

    def test_diverged_history_does_not_force_or_merge(self) -> None:
        # Local unique commit
        (self.work / "local.txt").write_text("local\n", encoding="utf-8")
        _git(self.work, "add", "local.txt")
        _git(self.work, "commit", "-m", "local-only")
        local_head = _git(self.work, "rev-parse", "HEAD").stdout.strip()
        # Remote unique commit (diverged)
        _advance_origin(self.bare, self.tmp, "remote-divergent")
        result = self._run_sync([self.work])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        after = _git(self.work, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(after, local_head)
        status = _git(self.work, "status", "--porcelain").stdout
        self.assertEqual(status, "")
        combined = result.stdout
        log_file = self.log_dir / "hal-sync.log"
        if log_file.is_file():
            combined += log_file.read_text(encoding="utf-8")
        self.assertIn("Fast-forward pull failed", combined)
        # Ensure we never used destructive flags in the sync script
        sync_src = SYNC_SH.read_text(encoding="utf-8")
        self.assertNotIn("reset --hard", sync_src)
        self.assertNotIn("git stash", sync_src)
        self.assertNotIn("merge --no-ff", sync_src)
        self.assertIn("pull --ff-only", sync_src)

    def test_lock_prevents_overlapping_logic(self) -> None:
        lock = self.log_dir / "hal-sync.lock"
        lock.mkdir()
        (lock / "pid").write_text("1\n", encoding="utf-8")
        result = self._run_sync([self.work])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        combined = result.stdout + result.stderr
        log_file = self.log_dir / "hal-sync.log"
        if log_file.is_file():
            combined += log_file.read_text(encoding="utf-8")
        self.assertIn("lock held", combined)


class TestHalSyncSafetyStatic(unittest.TestCase):
    def test_scripts_forbid_sudo_and_tokens(self) -> None:
        for script in (SYNC_SH, INSTALL_SH, UNINSTALL_SH):
            text = script.read_text(encoding="utf-8")
            self.assertNotIn("sudo ", text)
            self.assertNotIn("GITHUB_TOKEN", text)
            self.assertNotIn("ghp_", text)


if __name__ == "__main__":
    unittest.main()
