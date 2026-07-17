"""Tests for Mission Control cloud API endpoints."""

import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import app
from mission_control.executor import ExecutionResult

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE = REPO_ROOT / "missions" / "reference"


class TestHealthEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health_returns_ok(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class TestValidateEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_validate_valid_mission(self) -> None:
        mission_yaml = (REFERENCE / "valid-v1.0.yaml").read_text(encoding="utf-8")
        response = self.client.post("/validate", json={"mission_yaml": mission_yaml})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "error": None})

    def test_validate_invalid_mission(self) -> None:
        mission_yaml = (REFERENCE / "invalid-bad-version.yaml").read_text(
            encoding="utf-8"
        )
        response = self.client.post("/validate", json={"mission_yaml": mission_yaml})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertIn("Unsupported version", body["error"])

    def test_validate_rejects_empty_yaml(self) -> None:
        response = self.client.post("/validate", json={"mission_yaml": ""})
        self.assertEqual(response.status_code, 422)


class TestRunEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def _runnable_mission_yaml(self) -> str:
        return textwrap.dedent(
            f"""
            version: 1.0
            mission_id: 2026-07-17-999
            title: Runnable Test
            repository:
              name: Mission-Control
              path: {REPO_ROOT}
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
        )

    def test_run_invalid_mission(self) -> None:
        mission_yaml = (REFERENCE / "invalid-bad-version.yaml").read_text(
            encoding="utf-8"
        )
        response = self.client.post("/run", json={"mission_yaml": mission_yaml})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertIn("Unsupported version", body["error"])

    def test_run_ineligible_mission(self) -> None:
        mission_yaml = (REFERENCE / "valid-v1.0.yaml").read_text(encoding="utf-8")
        response = self.client.post("/run", json={"mission_yaml": mission_yaml})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertIn("Worktrees", body["error"])

    @patch("app.api.run_cursor_agent")
    def test_run_valid_mission_calls_executor(self, mock_run) -> None:
        mock_run.return_value = ExecutionResult(ok=True, stdout="agent response\n")

        response = self.client.post(
            "/run", json={"mission_yaml": self._runnable_mission_yaml()}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "ok": True,
                "stdout": "agent response\n",
                "stderr": "",
                "error": None,
            },
        )
        mock_run.assert_called_once()

    @patch("app.api.run_cursor_agent")
    def test_run_execution_failure(self, mock_run) -> None:
        mock_run.return_value = ExecutionResult(
            ok=False,
            stderr="agent failed",
            error="agent failed",
        )

        response = self.client.post(
            "/run", json={"mission_yaml": self._runnable_mission_yaml()}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["stderr"], "agent failed")
        self.assertEqual(body["error"], "agent failed")


if __name__ == "__main__":
    unittest.main()
