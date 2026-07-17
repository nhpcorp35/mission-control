"""CLI tests for mc.py run command."""

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestMcRunCli(unittest.TestCase):
    def _run_mc(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "mc.py"), *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

    def test_run_cli_usage_missing_file(self) -> None:
        result = self._run_mc("run")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage:", result.stderr)

    def test_run_invalid_mission_exits_1(self) -> None:
        result = self._run_mc(
            "run", "missions/reference/invalid-bad-version.yaml"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("\u2717 Mission invalid", result.stderr)

    def test_run_ineligible_mission_exits_1(self) -> None:
        result = self._run_mc("run", "missions/reference/valid-v1.0.yaml")
        self.assertEqual(result.returncode, 1)
        self.assertIn("\u2717 Mission not runnable", result.stderr)
        self.assertIn("Worktrees", result.stderr)

"""CLI tests for mc.py run command."""

import io
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import mc

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestMcRunCli(unittest.TestCase):
    def _run_mc(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "mc.py"), *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

    def test_run_cli_usage_missing_file(self) -> None:
        result = self._run_mc("run")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage:", result.stderr)

    def test_run_invalid_mission_exits_1(self) -> None:
        result = self._run_mc(
            "run", "missions/reference/invalid-bad-version.yaml"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("\u2717 Mission invalid", result.stderr)

    def test_run_ineligible_mission_exits_1(self) -> None:
        result = self._run_mc("run", "missions/reference/valid-v1.0.yaml")
        self.assertEqual(result.returncode, 1)
        self.assertIn("\u2717 Mission not runnable", result.stderr)
        self.assertIn("Worktrees", result.stderr)

    @patch("mc.run_cursor_agent")
    def test_run_valid_mission_calls_executor(self, mock_run) -> None:
        from mission_control.executor import ExecutionResult

        mock_run.return_value = ExecutionResult(ok=True, stdout="agent response\n")

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write(
                textwrap.dedent(
                    """
                    version: 1.0
                    mission_id: 2026-07-17-999
                    title: Runnable Test
                    repository:
                      name: Mission-Control
                      path: {repo_path}
                      base_branch: main
                    execution:
                      agent: cursor
                      mode: plan
                      sandbox: true
                      worktree: false
                    permissions:
                      read: true
                      create_files: false
                      modify_files: false
                      delete_files: false
                      run_commands: true
                      stage_changes: false
                      commit: false
                      push: false
                    instructions: |
                      List files.
                    deliverables:
                      - summary
                    approval:
                      execute_without_approval: true
                      commit_requires_approval: true
                      push_requires_approval: true
                    """
                ).format(repo_path=REPO_ROOT)
            )
            mission_path = handle.name

        try:
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                exit_code = mc.main(["run", mission_path])
        finally:
            Path(mission_path).unlink(missing_ok=True)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "agent response\n")
        mock_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
