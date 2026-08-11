"""Tests for Cursor CLI cloud support helpers."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.cursor_cli import (
    ERROR_CURSOR_AGENT_UNAVAILABLE,
    ERROR_CURSOR_API_KEY_MISSING,
    ERROR_PYTHON_UNAVAILABLE,
    CURSOR_API_KEY_ENV,
    CURSOR_LOCAL_BIN,
    augment_path,
    check_cursor_cli_status,
    cursor_cli_env,
    find_cursor_agent_binary,
    find_python_interpreter,
    is_api_key_configured,
    preflight_for_execution,
)
from mission_control.executor import (
    PLATFORM_PERSISTENCE_OWNERSHIP_INSTRUCTIONS,
    build_cursor_instruction,
)
from mission_control.workspace import (
    AGENT_GIT_PUSH_DISABLED_URL,
    cleanup_workspace,
    disable_agent_git_push,
    get_agent_push_url,
    get_origin_url,
    persist_workspace_changes,
    prepare_isolated_workspace,
)


class TestAugmentPath(unittest.TestCase):
    def test_prepends_local_bin(self) -> None:
        local_bin = str(CURSOR_LOCAL_BIN)
        self.assertEqual(augment_path("/usr/bin"), f"{local_bin}{os.pathsep}/usr/bin")

    def test_does_not_duplicate_local_bin(self) -> None:
        local_bin = str(CURSOR_LOCAL_BIN)
        current = f"{local_bin}{os.pathsep}/usr/bin"
        self.assertEqual(augment_path(current), current)

    def test_empty_path_returns_local_bin(self) -> None:
        self.assertEqual(augment_path(""), str(CURSOR_LOCAL_BIN))


class TestCursorCliEnv(unittest.TestCase):
    def test_env_includes_local_bin_on_path(self) -> None:
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=False):
            env = cursor_cli_env()
            self.assertTrue(env["PATH"].startswith(str(CURSOR_LOCAL_BIN)))

    def test_env_strips_mission_control_submission_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "MISSION_CONTROL_API_KEY": "secret",
                "MISSION_CONTROL_URL": "http://127.0.0.1:8000",
            },
            clear=False,
        ):
            env = cursor_cli_env()
        self.assertNotIn("MISSION_CONTROL_API_KEY", env)
        self.assertNotIn("MISSION_CONTROL_URL", env)
        self.assertEqual(env["MISSION_CONTROL_RECURSIVE_SUBMISSIONS"], "blocked")

    def test_env_strips_github_write_credentials(self) -> None:
        """Agent subprocess must not inherit GitHub write credentials."""
        with patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "CURSOR_API_KEY": "crsr_keep_for_agent",
                "GITHUB_TOKEN": "ghp_write_secret",
                "GH_TOKEN": "gh_write_secret",
                "GH_ENTERPRISE_TOKEN": "ghe_write_secret",
                "GITHUB_PAT": "pat_write_secret",
                "GIT_ASKPASS": "/bin/askpass",
                "GH_ASKPASS": "/bin/gh-askpass",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic dG9rZW4=",
            },
            clear=False,
        ):
            env = cursor_cli_env()
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GH_ENTERPRISE_TOKEN", env)
        self.assertNotIn("GITHUB_PAT", env)
        self.assertNotIn("GIT_ASKPASS", env)
        self.assertNotIn("GH_ASKPASS", env)
        self.assertNotIn("GIT_CONFIG_COUNT", env)
        self.assertNotIn("GIT_CONFIG_KEY_0", env)
        self.assertNotIn("GIT_CONFIG_VALUE_0", env)
        # Local coding/auth for Cursor itself remains available.
        self.assertEqual(env.get("CURSOR_API_KEY"), "crsr_keep_for_agent")
        self.assertTrue(env["PATH"].startswith(str(CURSOR_LOCAL_BIN)))


class TestPersistenceOwnershipPhase1(unittest.TestCase):
    """Mission Control is the sole Git publisher; agents edit/test only."""

    def test_prompt_contains_platform_persistence_ownership_rule(self) -> None:
        mission = {
            "title": "Edit something",
            "instructions": "Change a file.",
            "deliverables": ["summary"],
            "repository": {"path": "/tmp/mission-control-run-example"},
        }
        instruction = build_cursor_instruction(mission)
        for line in PLATFORM_PERSISTENCE_OWNERSHIP_INSTRUCTIONS:
            self.assertIn(line, instruction)
        self.assertIn("Do not commit, push, create pull requests", instruction)
        self.assertIn("Mission Control persistence", instruction)

    def test_agent_side_push_disabled_after_workspace_prep(self) -> None:
        temp = tempfile.TemporaryDirectory()
        try:
            root = Path(temp.name)
            bare = root / "remote.git"
            source = root / "source"
            subprocess.run(
                ["git", "init", "--bare", str(bare)],
                check=True,
                capture_output=True,
                text=True,
            )
            source.mkdir()
            subprocess.run(
                ["git", "init", str(source)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(source), "config", "user.email", "t@example.com"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(source), "config", "user.name", "Test"],
                check=True,
                capture_output=True,
                text=True,
            )
            (source / "README.md").write_text("initial\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(source), "add", "README.md"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(source), "commit", "-m", "init"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(source), "branch", "-M", "main"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(source), "remote", "add", "origin", str(bare)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(source), "push", "-u", "origin", "main"],
                check=True,
                capture_output=True,
                text=True,
            )

            mission = {
                "repository": {
                    "name": "test-repo",
                    "path": str(source),
                    "base_branch": "main",
                }
            }
            with patch.dict(
                os.environ,
                {"MISSION_CONTROL_REPOSITORY_URL": str(bare)},
                clear=False,
            ):
                prep = prepare_isolated_workspace(mission)
            self.assertTrue(prep.ok, prep.error)
            assert prep.workspace_path is not None
            try:
                self.assertEqual(
                    get_agent_push_url(prep.workspace_path),
                    AGENT_GIT_PUSH_DISABLED_URL,
                )
                # Agent-style push by remote name must fail closed.
                denied = subprocess.run(
                    [
                        "git",
                        "-C",
                        prep.workspace_path,
                        "push",
                        "origin",
                        "HEAD:main",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(denied.returncode, 0)
                combined = (denied.stderr + denied.stdout).lower()
                self.assertTrue(
                    "disabled" in combined
                    or "does not appear to be a git repository" in combined
                    or "unable to access" in combined
                    or "failed" in combined,
                    msg=f"unexpected push denial output: {denied.stderr!r}",
                )
            finally:
                cleanup_workspace(prep.workspace_path)
        finally:
            temp.cleanup()

    def test_platform_persistence_available_after_agent_env_strip(self) -> None:
        """Platform keeps GITHUB_TOKEN / push path after agent env is built."""
        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "ghp_platform_only_token",
                "CURSOR_API_KEY": "crsr_test",
                "PATH": "/usr/bin",
            },
            clear=False,
        ):
            agent_env = cursor_cli_env()
            self.assertNotIn("GITHUB_TOKEN", agent_env)
            # Parent / platform environment retains write credentials.
            self.assertEqual(
                os.environ.get("GITHUB_TOKEN"),
                "ghp_platform_only_token",
            )

        temp = tempfile.TemporaryDirectory()
        try:
            root = Path(temp.name)
            bare = root / "remote.git"
            source = root / "source"
            subprocess.run(
                ["git", "init", "--bare", str(bare)],
                check=True,
                capture_output=True,
                text=True,
            )
            source.mkdir()
            for args in (
                ["git", "init", str(source)],
                ["git", "-C", str(source), "config", "user.email", "t@example.com"],
                ["git", "-C", str(source), "config", "user.name", "Test"],
            ):
                subprocess.run(args, check=True, capture_output=True, text=True)
            (source / "README.md").write_text("initial\n", encoding="utf-8")
            for args in (
                ["git", "-C", str(source), "add", "README.md"],
                ["git", "-C", str(source), "commit", "-m", "init"],
                ["git", "-C", str(source), "branch", "-M", "main"],
                ["git", "-C", str(source), "remote", "add", "origin", str(bare)],
                ["git", "-C", str(source), "push", "-u", "origin", "main"],
            ):
                subprocess.run(args, check=True, capture_output=True, text=True)

            mission = {
                "repository": {
                    "name": "test-repo",
                    "path": str(source),
                    "base_branch": "main",
                },
                "persistence": {"mode": "push"},
                "approval": {
                    "platform_push_approved": True,
                    "allow_automatic_platform_push": True,
                },
            }
            with patch.dict(
                os.environ,
                {"MISSION_CONTROL_REPOSITORY_URL": str(bare)},
                clear=False,
            ):
                prep = prepare_isolated_workspace(mission)
                self.assertTrue(prep.ok, prep.error)
                assert prep.workspace_path is not None
                try:
                    self.assertEqual(
                        get_agent_push_url(prep.workspace_path),
                        AGENT_GIT_PUSH_DISABLED_URL,
                    )
                    origin = get_origin_url(prep.workspace_path)
                    self.assertEqual(origin, str(bare))

                    (Path(prep.workspace_path) / "owned.txt").write_text(
                        "platform\n",
                        encoding="utf-8",
                    )
                    # Simulate agent env construction before platform persistence.
                    _ = cursor_cli_env()
                    with patch(
                        "mission_control.workspace._github_push_environment",
                        return_value=(os.environ.copy(), None),
                    ):
                        result = persist_workspace_changes(
                            "ownership-phase1",
                            mission,
                            prep.workspace_path,
                        )
                    self.assertTrue(result.ok, result.error)
                    self.assertTrue(result.pushed)
                    self.assertIsNotNone(result.commit_sha)
                    # Denial remains in place for any later agent-side push.
                    self.assertEqual(
                        get_agent_push_url(prep.workspace_path),
                        AGENT_GIT_PUSH_DISABLED_URL,
                    )
                    remote_sha = subprocess.run(
                        ["git", "--git-dir", str(bare), "rev-parse", "main"],
                        capture_output=True,
                        text=True,
                        check=True,
                    ).stdout.strip()
                    self.assertEqual(remote_sha, result.commit_sha)
                finally:
                    cleanup_workspace(prep.workspace_path)
        finally:
            temp.cleanup()

    def test_disable_agent_git_push_helper(self) -> None:
        temp = tempfile.TemporaryDirectory()
        try:
            repo = Path(temp.name) / "repo"
            repo.mkdir()
            subprocess.run(
                ["git", "init", str(repo)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "remote",
                    "add",
                    "origin",
                    "https://example.com/r.git",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            err = disable_agent_git_push(str(repo))
            self.assertIsNone(err)
            self.assertEqual(get_agent_push_url(str(repo)), AGENT_GIT_PUSH_DISABLED_URL)
        finally:
            temp.cleanup()


class TestApiKeyConfigured(unittest.TestCase):
    def test_missing_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(is_api_key_configured())

    def test_empty_key(self) -> None:
        with patch.dict(os.environ, {CURSOR_API_KEY_ENV: "   "}, clear=True):
            self.assertFalse(is_api_key_configured())

    def test_configured_key(self) -> None:
        with patch.dict(os.environ, {CURSOR_API_KEY_ENV: "crsr_test"}, clear=True):
            self.assertTrue(is_api_key_configured())


class TestFindCursorAgentBinary(unittest.TestCase):
    @patch("app.cursor_cli.shutil.which")
    def test_returns_resolved_binary(self, mock_which) -> None:
        mock_which.return_value = "/home/user/.local/bin/cursor-agent"
        self.assertEqual(find_cursor_agent_binary(), "/home/user/.local/bin/cursor-agent")
        mock_which.assert_called_once()

    @patch("app.cursor_cli.shutil.which")
    def test_returns_none_when_missing(self, mock_which) -> None:
        mock_which.return_value = None
        self.assertIsNone(find_cursor_agent_binary())


class TestFindPythonInterpreter(unittest.TestCase):
    @patch("app.cursor_cli.shutil.which")
    def test_returns_python3_when_found(self, mock_which) -> None:
        mock_which.side_effect = lambda cmd, path=None: (
            "/app/.venv/bin/python3" if cmd == "python3" else None
        )
        self.assertEqual(find_python_interpreter(), "/app/.venv/bin/python3")

    @patch("app.cursor_cli.shutil.which")
    def test_falls_back_to_python(self, mock_which) -> None:
        mock_which.side_effect = lambda cmd, path=None: (
            "/usr/bin/python" if cmd == "python" else None
        )
        self.assertEqual(find_python_interpreter(), "/usr/bin/python")

    @patch("app.cursor_cli.shutil.which")
    def test_returns_none_when_missing(self, mock_which) -> None:
        mock_which.return_value = None
        self.assertIsNone(find_python_interpreter())


class TestCheckCursorCliStatus(unittest.TestCase):
    @patch("app.cursor_cli.find_cursor_agent_binary")
    @patch("app.cursor_cli.is_api_key_configured")
    def test_reports_ready_state(self, mock_key, mock_binary) -> None:
        mock_binary.return_value = "/tmp/cursor-agent"
        mock_key.return_value = True
        status = check_cursor_cli_status()
        self.assertTrue(status.installed)
        self.assertTrue(status.authenticated)
        self.assertEqual(status.binary_path, "/tmp/cursor-agent")

    @patch("app.cursor_cli.find_cursor_agent_binary")
    @patch("app.cursor_cli.is_api_key_configured")
    def test_reports_missing_install(self, mock_key, mock_binary) -> None:
        mock_binary.return_value = None
        mock_key.return_value = True
        status = check_cursor_cli_status()
        self.assertFalse(status.installed)
        self.assertTrue(status.authenticated)


class TestPreflightForExecution(unittest.TestCase):
    @patch("app.cursor_cli.find_cursor_agent_binary")
    def test_agent_unavailable(self, mock_binary) -> None:
        mock_binary.return_value = None
        error = preflight_for_execution()
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.code, ERROR_CURSOR_AGENT_UNAVAILABLE)
        self.assertEqual(error.stage, "preflight")

    @patch("app.cursor_cli.is_api_key_configured")
    @patch("app.cursor_cli.find_cursor_agent_binary")
    def test_api_key_missing(self, mock_binary, mock_key) -> None:
        mock_binary.return_value = "/tmp/cursor-agent"
        mock_key.return_value = False
        error = preflight_for_execution()
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.code, ERROR_CURSOR_API_KEY_MISSING)
        self.assertIn(CURSOR_API_KEY_ENV, error.message)

    @patch("app.cursor_cli.find_python_interpreter")
    @patch("app.cursor_cli.is_api_key_configured")
    @patch("app.cursor_cli.find_cursor_agent_binary")
    def test_python_unavailable(self, mock_binary, mock_key, mock_python) -> None:
        mock_binary.return_value = "/tmp/cursor-agent"
        mock_key.return_value = True
        mock_python.return_value = None
        error = preflight_for_execution()
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.code, ERROR_PYTHON_UNAVAILABLE)
        self.assertEqual(error.stage, "preflight")
        self.assertIn("Python 3", error.message)

    @patch("app.cursor_cli.find_python_interpreter")
    @patch("app.cursor_cli.is_api_key_configured")
    @patch("app.cursor_cli.find_cursor_agent_binary")
    def test_passes_when_ready(self, mock_binary, mock_key, mock_python) -> None:
        mock_binary.return_value = "/tmp/cursor-agent"
        mock_key.return_value = True
        mock_python.return_value = "/app/.venv/bin/python3"
        self.assertIsNone(preflight_for_execution())


if __name__ == "__main__":
    unittest.main()
