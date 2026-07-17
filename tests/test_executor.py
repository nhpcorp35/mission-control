"""Tests for Cursor Agent execution helpers."""

import subprocess
import unittest
from unittest.mock import patch

from mission_control.executor import (
    CURSOR_AGENT,
    EXECUTION_TIMEOUT_SECONDS,
    build_cursor_agent_command,
    build_cursor_instruction,
    run_cursor_agent,
)


def _sample_mission() -> dict:
    return {
        "title": "Repository Verification",
        "instructions": "List the files in this directory.",
        "deliverables": ["file list", "summary"],
        "repository": {"path": "/Users/allenk/Desktop/Mission-Control"},
    }


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

    def test_excludes_forbidden_flags(self) -> None:
        command = build_cursor_agent_command("/tmp/repo", "test")
        forbidden = {"--force", "--yolo", "--auto-review", "--worktree", "-w"}
        self.assertTrue(forbidden.isdisjoint(set(command)))


class TestRunCursorAgent(unittest.TestCase):
    @patch("mission_control.executor.subprocess.run")
    def test_run_success_prints_stdout(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="PONG\n",
            stderr="",
        )
        result = run_cursor_agent(_sample_mission())
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "PONG\n")

    @patch("mission_control.executor.subprocess.run")
    def test_run_failure_returns_stderr(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="agent failed",
        )
        result = run_cursor_agent(_sample_mission())
        self.assertFalse(result.ok)
        self.assertEqual(result.stderr, "agent failed")
        self.assertIn("agent failed", result.error or "")

    @patch("mission_control.executor.subprocess.run")
    def test_run_timeout(self, mock_run) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=[CURSOR_AGENT],
            timeout=EXECUTION_TIMEOUT_SECONDS,
        )
        result = run_cursor_agent(_sample_mission())
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error or "")


if __name__ == "__main__":
    unittest.main()
