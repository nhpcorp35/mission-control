"""Tests for Mission Control cloud API endpoints."""

import os
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import app
from mission_control.executor import ExecutionResult

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE = REPO_ROOT / "missions" / "reference"

TEST_API_KEY = "mc_test_authentication_key"
AUTH_HEADERS = {
    "Authorization": f"Bearer {TEST_API_KEY}",
}

os.environ["MISSION_CONTROL_API_KEY"] = TEST_API_KEY


class TestHealthEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health_returns_ok(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class TestRunPreflightEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(
            app,
            headers=AUTH_HEADERS,
        )

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

    @patch("app.api.preflight_for_execution")
    def test_run_cursor_agent_unavailable(self, mock_preflight) -> None:
        from app.cursor_cli import StructuredError

        mock_preflight.return_value = StructuredError(
            code="CURSOR_AGENT_UNAVAILABLE",
            message="cursor-agent is not installed",
            stage="preflight",
        )

        response = self.client.post(
            "/run", json={"mission_yaml": self._runnable_mission_yaml()}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "cursor-agent is not installed")
        self.assertEqual(
            body["error_detail"],
            {
                "code": "CURSOR_AGENT_UNAVAILABLE",
                "message": "cursor-agent is not installed",
                "stage": "preflight",
            },
        )

    @patch("app.api.preflight_for_execution")
    def test_run_cursor_api_key_missing(self, mock_preflight) -> None:
        from app.cursor_cli import StructuredError

        mock_preflight.return_value = StructuredError(
            code="CURSOR_API_KEY_MISSING",
            message="CURSOR_API_KEY environment variable is not set",
            stage="preflight",
        )

        response = self.client.post(
            "/run", json={"mission_yaml": self._runnable_mission_yaml()}
        )
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error_detail"]["code"], "CURSOR_API_KEY_MISSING")


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
        self.client = TestClient(
            app,
            headers=AUTH_HEADERS,
        )

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

    @patch("app.api.preflight_for_execution", return_value=None)
    @patch("app.api.run_cursor_agent")
    def test_run_valid_mission_calls_executor(self, mock_run, _mock_preflight) -> None:
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
                "error_detail": None,
            },
        )
        mock_run.assert_called_once()

    @patch("app.api.preflight_for_execution", return_value=None)
    @patch("app.api.run_cursor_agent")
    def test_run_execution_failure(self, mock_run, _mock_preflight) -> None:
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


class TestAuthentication(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health_remains_public(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_validate_remains_public(self) -> None:
        mission_yaml = (
            REFERENCE / "valid-v1.0.yaml"
        ).read_text(encoding="utf-8")

        response = self.client.post(
            "/validate",
            json={"mission_yaml": mission_yaml},
        )

        self.assertEqual(response.status_code, 200)

    def test_run_rejects_missing_token(self) -> None:
        response = self.client.post(
            "/run",
            json={"mission_yaml": "version: 1.0"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["detail"],
            "Missing bearer token",
        )
        self.assertEqual(
            response.headers.get("www-authenticate"),
            "Bearer",
        )

    def test_execute_rejects_missing_token(self) -> None:
        response = self.client.post(
            "/execute",
            json={"mission_yaml": "version: 1.0"},
        )

        self.assertEqual(response.status_code, 401)

    def test_run_rejects_invalid_token(self) -> None:
        response = self.client.post(
            "/run",
            headers={
                "Authorization": "Bearer wrong-key",
            },
            json={"mission_yaml": "version: 1.0"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["detail"],
            "Invalid bearer token",
        )

    def test_correct_token_reaches_run_endpoint(self) -> None:
        response = self.client.post(
            "/run",
            headers=AUTH_HEADERS,
            json={"mission_yaml": "version: 1.0"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])

    def test_correct_token_reaches_execute_endpoint(self) -> None:
        response = self.client.post(
            "/execute",
            headers=AUTH_HEADERS,
            json={"mission_yaml": "version: 1.0"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])

    def test_missing_server_configuration_returns_503(self) -> None:
        with patch.dict(
            os.environ,
            {"MISSION_CONTROL_API_KEY": ""},
        ):
            response = self.client.post(
                "/run",
                headers=AUTH_HEADERS,
                json={"mission_yaml": "version: 1.0"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn(
            "MISSION_CONTROL_API_KEY",
            response.json()["detail"],
        )


class TestExecuteEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(
            app,
            headers=AUTH_HEADERS,
        )

    def _executable_mission_yaml(self) -> str:
        return textwrap.dedent(
            f"""
            version: 1.0
            mission_id: 2026-07-19-execute
            title: Execute Test
            repository:
              name: Mission-Control
              path: {REPO_ROOT}
              base_branch: main
            execution:
              agent: cursor
              mode: execute
              sandbox: true
              worktree: false
            permissions:
              read: true
              create_files: true
              modify_files: false
              delete_files: false
              run_commands: true
              stage_changes: false
              commit: false
              push: false
            instructions: |
              Create a file.
            deliverables:
              - summary
            approval:
              execute_without_approval: true
              commit_requires_approval: true
              push_requires_approval: true
            """
        )

    @patch("app.api.preflight_for_execution", return_value=None)
    @patch("app.api.execute_cursor_agent")
    @patch("mission_control.workspace.execute_registered_run")
    def test_execute_does_not_use_async_workspace_flow(
        self,
        mock_registered_run,
        mock_execute,
        _mock_preflight,
    ) -> None:
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout="sync result\n",
        )

        response = self.client.post(
            "/execute",
            json={"mission_yaml": self._executable_mission_yaml()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        mock_execute.assert_called_once()
        mock_registered_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
