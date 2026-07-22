"""Tests for Cursor Agent execution helpers."""

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from mission_control.executor import (
    CURSOR_AGENT,
    EXECUTION_TIMEOUT_SECONDS,
    build_cursor_agent_command,
    build_cursor_instruction,
    execute_cursor_agent,
    run_cursor_agent,
)


def _sample_mission() -> dict:
    return {
        "title": "Repository Verification",
        "instructions": "List the files in this directory.",
        "deliverables": ["file list", "summary"],
        "repository": {"path": "/Users/allenk/Desktop/Mission-Control"},
    }


def _mock_completed_process(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    pid: int = 4242,
) -> MagicMock:
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


class TestBuildCursorInstruction(unittest.TestCase):
    def test_includes_title_and_instructions(self) -> None:
        instruction = build_cursor_instruction(_sample_mission())
        self.assertIn("Repository Verification", instruction)
        self.assertIn("List the files in this directory.", instruction)

    def test_includes_deliverables(self) -> None:
        instruction = build_cursor_instruction(_sample_mission())
        self.assertIn("- file list", instruction)
        self.assertIn("- summary", instruction)

    def test_includes_safety_constraints(self) -> None:
        instruction = build_cursor_instruction(_sample_mission())
        self.assertIn("read-only", instruction.lower())
        self.assertIn("Do not modify files.", instruction)
        self.assertIn("Do not run Git commands.", instruction)
        self.assertIn("Do not create commits.", instruction)
        self.assertIn("Do not use worktrees.", instruction)
        self.assertIn(
            "Do not submit recursive Mission Control missions.",
            instruction,
        )

    def test_execute_constraints_forbid_recursive_missions(self) -> None:
        mission = _sample_mission()
        mission["permissions"] = {
            "create_files": True,
            "modify_files": True,
        }
        with patch(
            "mission_control.executor.find_cursor_agent_binary",
            return_value=CURSOR_AGENT,
        ), patch(
            "mission_control.executor.subprocess.Popen",
        ) as mock_popen:
            mock_popen.return_value = _mock_completed_process(stdout="ok\n")
            execute_cursor_agent(mission)
            instruction = mock_popen.call_args.args[0][-1]
            self.assertIn(
                "Do not submit recursive Mission Control missions.",
                instruction,
            )


class TestBuildCursorAgentCommand(unittest.TestCase):
    def test_build_argv_shape(self) -> None:
        command = build_cursor_agent_command(
            "/Users/allenk/Desktop/Mission-Control",
            "Reply only with PONG.",
        )
        self.assertEqual(
            command,
            [
                CURSOR_AGENT,
                "--print",
                "--mode",
                "plan",
                "--output-format",
                "text",
                "--workspace",
                "/Users/allenk/Desktop/Mission-Control",
                "--trust",
                "Reply only with PONG.",
            ],
        )

    def test_execute_mode_omits_cursor_mode_flag(self) -> None:
        command = build_cursor_agent_command(
            "/Users/allenk/Desktop/Mission-Control",
            "Create a new file.",
            mode="execute",
        )

        self.assertEqual(
            command,
            [
                CURSOR_AGENT,
                "--print",
                "--force",
                "--output-format",
                "text",
                "--workspace",
                "/Users/allenk/Desktop/Mission-Control",
                "--trust",
                "Create a new file.",
            ],
        )
        self.assertNotIn("--mode", command)

    def test_rejects_unknown_cursor_mode(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported Cursor Agent mode",
        ):
            build_cursor_agent_command(
                "/tmp/repo",
                "test",
                mode="invalid",
            )

    def test_excludes_forbidden_flags(self) -> None:
        command = build_cursor_agent_command("/tmp/repo", "test")
        forbidden = {"--force", "--yolo", "--auto-review", "--worktree", "-w"}
        self.assertTrue(forbidden.isdisjoint(set(command)))


class TestRunCursorAgent(unittest.TestCase):
    @patch(
        "mission_control.executor.find_cursor_agent_binary",
        return_value=CURSOR_AGENT,
    )
    @patch("mission_control.executor.subprocess.Popen")
    def test_run_success_prints_stdout(self, mock_popen, _mock_binary) -> None:
        mock_popen.return_value = _mock_completed_process(stdout="PONG\n")
        result = run_cursor_agent(_sample_mission())
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "PONG\n")

    @patch(
        "mission_control.executor.find_cursor_agent_binary",
        return_value=CURSOR_AGENT,
    )
    @patch("mission_control.executor.subprocess.Popen")
    def test_execute_uses_write_capable_default_mode(
        self,
        mock_popen,
        _mock_binary,
    ) -> None:
        mock_popen.return_value = _mock_completed_process(stdout="created file\n")

        result = execute_cursor_agent(_sample_mission())

        self.assertTrue(result.ok)

        command = mock_popen.call_args.args[0]
        self.assertIn("--print", command)
        self.assertIn("--trust", command)
        self.assertNotIn("--mode", command)
        self.assertIn("--force", command)
        self.assertNotIn("--yolo", command)

    @patch(
        "mission_control.executor.find_cursor_agent_binary",
        return_value=CURSOR_AGENT,
    )
    @patch("mission_control.executor.subprocess.Popen")
    def test_run_failure_returns_stderr(self, mock_popen, _mock_binary) -> None:
        mock_popen.return_value = _mock_completed_process(
            returncode=1,
            stderr="agent failed",
        )
        result = run_cursor_agent(_sample_mission())
        self.assertFalse(result.ok)
        self.assertEqual(result.stderr, "agent failed")
        self.assertIn("agent failed", result.error or "")
        self.assertEqual(result.return_code, 1)

    @patch(
        "mission_control.executor.find_cursor_agent_binary",
        return_value=CURSOR_AGENT,
    )
    @patch("mission_control.executor.subprocess.Popen")
    def test_run_success_preserves_return_code(
        self,
        mock_popen,
        _mock_binary,
    ) -> None:
        mock_popen.return_value = _mock_completed_process(
            returncode=0,
            stdout="PONG\n",
        )
        result = run_cursor_agent(_sample_mission())
        self.assertTrue(result.ok)
        self.assertEqual(result.return_code, 0)

    @patch(
        "mission_control.executor.find_cursor_agent_binary",
        return_value=CURSOR_AGENT,
    )
    @patch("mission_control.executor.subprocess.Popen")
    def test_run_timeout(self, mock_popen, _mock_binary) -> None:
        proc = MagicMock()
        proc.pid = 99
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(
                cmd=[CURSOR_AGENT],
                timeout=EXECUTION_TIMEOUT_SECONDS,
            ),
            ("", ""),
        ]
        mock_popen.return_value = proc
        result = run_cursor_agent(_sample_mission())
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error or "")
        proc.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
