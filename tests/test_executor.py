"""Tests for Cursor Agent execution helpers."""

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from mission_control.executor import (
    CURSOR_AGENT,
    EXECUTION_TIMEOUT_SECONDS,
    SPLIT_RUN_POLICY_INSTRUCTIONS,
    build_cursor_agent_command,
    build_cursor_instruction,
    build_timeout_split_guidance,
    execute_cursor_agent,
    run_cursor_agent,
)
from mission_control.run_result import (
    PersistenceEvidence,
    StructuredRunResult,
    WARNING_PERSISTENCE_NOT_ATTEMPTED,
    command_evidence_from_execution,
    finalize_structured_summary,
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

    def test_split_run_policy_injected_and_timeout_split_guidance(self) -> None:
        """Every agent prompt carries split-run policy; timeouts guide splits."""
        instruction = build_cursor_instruction(_sample_mission())
        self.assertIn("Split-run scope policy:", instruction)
        for line in SPLIT_RUN_POLICY_INSTRUCTIONS:
            self.assertIn(line, instruction)

        with patch(
            "mission_control.executor.find_cursor_agent_binary",
            return_value=CURSOR_AGENT,
        ), patch(
            "mission_control.executor.subprocess.Popen",
        ) as mock_popen:
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
            result = execute_cursor_agent(_sample_mission())

        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error or "")
        self.assertIsNotNone(result.timeout_split_guidance)
        assert result.timeout_split_guidance is not None
        self.assertEqual(
            result.timeout_split_guidance["timeout_stage"],
            "agent_execution",
        )
        self.assertTrue(
            result.timeout_split_guidance["persistence_not_attempted"],
        )
        self.assertIn(
            "implementation (one objective, four files or fewer)",
            result.timeout_split_guidance["recommended_phases"],
        )

        structured = StructuredRunResult(
            files_changed=["mission_control/executor.py"],
            commands=[command_evidence_from_execution(result)],
            persistence=PersistenceEvidence(
                mode="push",
                attempted=False,
                ok=None,
            ),
            warnings=[WARNING_PERSISTENCE_NOT_ATTEMPTED],
        )
        finalize_structured_summary(structured, error=result.error)
        self.assertIsNotNone(structured.timeout_split_guidance)
        assert structured.timeout_split_guidance is not None
        self.assertEqual(
            structured.timeout_split_guidance["timeout_stage"],
            "agent_execution",
        )
        self.assertEqual(
            structured.timeout_split_guidance["observed_changed_paths"],
            ["mission_control/executor.py"],
        )
        self.assertTrue(
            structured.timeout_split_guidance["persistence_not_attempted"],
        )
        self.assertEqual(
            structured.timeout_split_guidance["recommended_phases"],
            build_timeout_split_guidance()["recommended_phases"],
        )
        assert structured.summary is not None
        self.assertIn("Timeout split guidance", structured.summary)
        self.assertIn("not persisted", structured.summary)
        self.assertIn("Do not blindly retry", structured.summary)
        self.assertIn(
            "Platform persistence was not attempted",
            structured.summary,
        )

    def test_binds_writes_to_concrete_repository_path(self) -> None:
        mission = _sample_mission()
        workspace = mission["repository"]["path"]
        instruction = build_cursor_instruction(mission)
        self.assertIn(workspace, instruction)
        self.assertIn(
            f"All file writes must stay inside this Mission Control workspace: "
            f"{workspace}",
            instruction,
        )
        self.assertIn("repository.name is clone identity only", instruction)
        self.assertIn("do NOT infer a filesystem path from it", instruction)
        self.assertIn("never absolute paths", instruction)
        self.assertIn("mktemp -d", instruction)
        self.assertIn("absolute system `/tmp`", instruction)
        self.assertIn("__pycache__", instruction)

    def test_workspace_binding_omitted_without_repository_path(self) -> None:
        mission = {
            "title": "No path",
            "instructions": "Inspect only.",
            "deliverables": ["summary"],
            "repository": {"name": "nhpcorp35/mission-control"},
        }
        instruction = build_cursor_instruction(mission)
        self.assertNotIn(
            "All file writes must stay inside this Mission Control workspace:",
            instruction,
        )
        self.assertNotIn("clone identity only", instruction)

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

    def test_execute_read_only_permissions_use_read_only_constraints(
        self,
    ) -> None:
        mission = _sample_mission()
        mission["permissions"] = {
            "read": True,
            "create_files": False,
            "modify_files": False,
            "delete_files": False,
            "run_commands": True,
            "stage_changes": False,
            "commit": False,
            "push": False,
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
            self.assertIn("read-only", instruction.lower())
            self.assertIn("Do not modify files.", instruction)
            self.assertNotIn("may create new files", instruction.lower())


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
        mock_popen.assert_called_once()
        self.assertTrue(
            mock_popen.call_args.kwargs.get("start_new_session"),
        )

    @patch(
        "mission_control.executor.find_cursor_agent_binary",
        return_value=CURSOR_AGENT,
    )
    @patch("mission_control.executor.subprocess.Popen")
    def test_run_timeout_cleanup_communicate_also_times_out(
        self,
        mock_popen,
        _mock_binary,
    ) -> None:
        """Post-kill communicate must not hang the worker forever."""
        from mission_control.executor import CLEANUP_TIMEOUT_SECONDS

        timed_out = subprocess.TimeoutExpired(
            cmd=[CURSOR_AGENT],
            timeout=EXECUTION_TIMEOUT_SECONDS,
            output="partial-out",
            stderr="partial-err",
        )
        proc = MagicMock()
        proc.pid = 99
        proc.stdout = MagicMock()
        proc.stderr = MagicMock()
        proc.stdin = None
        proc.communicate.side_effect = [
            timed_out,
            subprocess.TimeoutExpired(
                cmd=[CURSOR_AGENT],
                timeout=CLEANUP_TIMEOUT_SECONDS,
            ),
        ]
        mock_popen.return_value = proc

        with patch(
            "mission_control.executor._terminate_process_tree",
        ) as mock_terminate:
            result = run_cursor_agent(_sample_mission())

        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error or "")
        self.assertEqual(result.stdout, "partial-out")
        self.assertEqual(result.stderr, "partial-err")
        mock_terminate.assert_called_once_with(proc)
        proc.stdout.close.assert_called_once()
        proc.stderr.close.assert_called_once()
        self.assertEqual(proc.communicate.call_count, 2)
        second_kwargs = proc.communicate.call_args_list[1].kwargs
        self.assertEqual(
            second_kwargs.get("timeout"),
            CLEANUP_TIMEOUT_SECONDS,
        )

    def test_timeout_with_orphaned_child_pipe_holder_returns(self) -> None:
        """Real subprocess: grandchild holds stdout after parent kill.

        Legacy unbounded ``communicate()`` after ``kill()`` hangs forever in
        this situation; the fix must return a timed-out result promptly.
        """
        import sys
        import time

        from mission_control import executor as executor_module

        orphan_script = """
import os, signal, sys, time
if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    time.sleep(3600)
    os._exit(0)
sys.stdout.write("agent-started\\n")
sys.stdout.flush()
time.sleep(3600)
"""

        original_timeout = executor_module.EXECUTION_TIMEOUT_SECONDS
        original_cleanup = executor_module.CLEANUP_TIMEOUT_SECONDS
        executor_module.EXECUTION_TIMEOUT_SECONDS = 1
        executor_module.CLEANUP_TIMEOUT_SECONDS = 1
        mission = _sample_mission()
        mission["repository"] = {"path": "/tmp"}
        started = time.monotonic()
        try:
            with patch(
                "mission_control.executor.find_cursor_agent_binary",
                return_value=sys.executable,
            ), patch(
                "mission_control.executor.build_cursor_agent_command",
                return_value=[sys.executable, "-c", orphan_script],
            ):
                result = run_cursor_agent(mission)
        finally:
            executor_module.EXECUTION_TIMEOUT_SECONDS = original_timeout
            executor_module.CLEANUP_TIMEOUT_SECONDS = original_cleanup

        elapsed = time.monotonic() - started
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error or "")
        # Must finish well under the legacy infinite hang; allow generous CI.
        self.assertLess(elapsed, 15.0)


if __name__ == "__main__":
    unittest.main()
