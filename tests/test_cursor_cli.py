"""Tests for Cursor CLI cloud support helpers."""

import os
import unittest
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
