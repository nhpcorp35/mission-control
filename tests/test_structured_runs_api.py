"""Focused tests for POST /runs/structured."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.api as api_module
from app.api import app
from mission_control.run_registry import RunRegistry
from mission_control.workspace import PLATFORM_PUSH_APPROVAL_REQUIRED

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_API_KEY = "mc_test_authentication_key"
AUTH_HEADERS = {
    "Authorization": f"Bearer {TEST_API_KEY}",
}
os.environ["MISSION_CONTROL_API_KEY"] = TEST_API_KEY


def _structured_payload(**overrides: object) -> dict:
    payload: dict = {
        "mission_id": "2026-07-24-structured",
        "title": "Structured Run Test",
        "instructions": "Create a file.",
        "deliverables": ["summary"],
        "create_files": True,
        "modify_files": False,
        "repository_path": str(REPO_ROOT),
    }
    payload.update(overrides)
    return payload


class TestStructuredRunsApi(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        api_module.run_registry = RunRegistry(self._db_path)
        from mission_control.run_queue import RunQueue

        api_module.run_queue = RunQueue()
        api_module.run_queue.configure(api_module._execute_queued_run)
        self.client = TestClient(app, headers=AUTH_HEADERS)

    def tearDown(self) -> None:
        api_module.run_registry.close()
        os.unlink(self._db_path)

    @patch("app.api.preflight_for_execution", return_value=None)
    def test_structured_happy_path_reaches_async_acceptance(
        self,
        _mock_preflight,
    ) -> None:
        with patch.object(
            api_module,
            "_accept_async_run",
            wraps=api_module._accept_async_run,
        ) as accept_mock:
            response = self.client.post(
                "/runs/structured",
                json=_structured_payload(),
            )
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertIn("run_id", body)
        self.assertEqual(body["status"], "queued")
        accept_mock.assert_called_once()
        mission_yaml = accept_mock.call_args.args[0]
        self.assertIsInstance(mission_yaml, str)
        self.assertIn("mission_id: 2026-07-24-structured", mission_yaml)
        self.assertIn("mode: execute", mission_yaml)
        record = api_module.run_registry.get_run(body["run_id"])
        assert record is not None
        self.assertEqual(record.mission_yaml, mission_yaml)

    @patch("app.api.preflight_for_execution", return_value=None)
    def test_valid_read_only_structured_mission_accepted(
        self,
        _mock_preflight,
    ) -> None:
        response = self.client.post(
            "/runs/structured",
            json=_structured_payload(
                create_files=False,
                modify_files=False,
                persistence_mode="none",
                run_commands=True,
            ),
        )
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertIn("run_id", body)
        self.assertEqual(body["status"], "queued")

    @patch("app.api.preflight_for_execution", return_value=None)
    def test_invalid_structured_mission_rejected_by_validation(
        self,
        _mock_preflight,
    ) -> None:
        # Non-push execute without create/modify must be exact read-only;
        # run_commands=false fails that gate.
        response = self.client.post(
            "/runs/structured",
            json=_structured_payload(
                create_files=False,
                modify_files=False,
                persistence_mode="none",
                run_commands=False,
            ),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertIn("create_files", body["error"] or "")
        self.assertEqual(api_module.run_registry.count_runs(), 0)

    @patch("app.api.preflight_for_execution", return_value=None)
    def test_push_without_platform_approval_rejected(
        self,
        _mock_preflight,
    ) -> None:
        response = self.client.post(
            "/runs/structured",
            json=_structured_payload(
                persistence_mode="push",
                platform_push_approved=False,
                allow_automatic_platform_push=False,
            ),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], PLATFORM_PUSH_APPROVAL_REQUIRED)
        self.assertEqual(api_module.run_registry.count_runs(), 0)

    @patch("app.api.preflight_for_execution", return_value=None)
    def test_flat_platform_push_approved_accepted(
        self,
        _mock_preflight,
    ) -> None:
        with patch.object(
            api_module,
            "_accept_async_run",
            wraps=api_module._accept_async_run,
        ) as accept_mock:
            response = self.client.post(
                "/runs/structured",
                json=_structured_payload(
                    persistence_mode="push",
                    platform_push_approved=True,
                ),
            )
        self.assertEqual(response.status_code, 202)
        mission_yaml = accept_mock.call_args.args[0]
        self.assertIn("platform_push_approved: true", mission_yaml)

    @patch("app.api.preflight_for_execution", return_value=None)
    def test_nested_approval_platform_push_approved_accepted(
        self,
        _mock_preflight,
    ) -> None:
        with patch.object(
            api_module,
            "_accept_async_run",
            wraps=api_module._accept_async_run,
        ) as accept_mock:
            response = self.client.post(
                "/runs/structured",
                json=_structured_payload(
                    persistence_mode="push",
                    approval={"platform_push_approved": True},
                ),
            )
        self.assertEqual(response.status_code, 202, response.text)
        mission_yaml = accept_mock.call_args.args[0]
        self.assertIn("platform_push_approved: true", mission_yaml)

    @patch("app.api.preflight_for_execution", return_value=None)
    def test_matching_flat_and_nested_platform_push_approved_accepted(
        self,
        _mock_preflight,
    ) -> None:
        with patch.object(
            api_module,
            "_accept_async_run",
            wraps=api_module._accept_async_run,
        ) as accept_mock:
            response = self.client.post(
                "/runs/structured",
                json=_structured_payload(
                    persistence_mode="push",
                    platform_push_approved=True,
                    approval={"platform_push_approved": True},
                ),
            )
        self.assertEqual(response.status_code, 202)
        mission_yaml = accept_mock.call_args.args[0]
        self.assertIn("platform_push_approved: true", mission_yaml)

    def test_conflicting_flat_and_nested_platform_push_approved_rejected(
        self,
    ) -> None:
        response = self.client.post(
            "/runs/structured",
            json=_structured_payload(
                persistence_mode="push",
                platform_push_approved=False,
                approval={"platform_push_approved": True},
            ),
        )
        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        detail_text = str(detail).lower()
        self.assertIn("conflict", detail_text)
        self.assertIn("platform_push_approved", detail_text)
        self.assertEqual(api_module.run_registry.count_runs(), 0)

    @patch("app.api.preflight_for_execution", return_value=None)
    def test_nested_approval_not_silently_dropped(
        self,
        _mock_preflight,
    ) -> None:
        with patch.object(
            api_module,
            "_accept_async_run",
            wraps=api_module._accept_async_run,
        ) as accept_mock:
            response = self.client.post(
                "/runs/structured",
                json=_structured_payload(
                    persistence_mode="push",
                    approval={"platform_push_approved": True},
                ),
            )
        self.assertEqual(response.status_code, 202)
        mission_yaml = accept_mock.call_args.args[0]
        # Nested-only input must become canonical Mission Spec approval.
        self.assertRegex(
            mission_yaml,
            r"approval:[\s\S]*platform_push_approved:\s*true",
        )
        self.assertNotIn("platform_push_approved: false", mission_yaml)

    @patch("app.api.preflight_for_execution", return_value=None)
    def test_nested_false_still_rejects_push_without_authorization(
        self,
        _mock_preflight,
    ) -> None:
        response = self.client.post(
            "/runs/structured",
            json=_structured_payload(
                persistence_mode="push",
                approval={"platform_push_approved": False},
                allow_automatic_platform_push=False,
            ),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], PLATFORM_PUSH_APPROVAL_REQUIRED)
        self.assertEqual(api_module.run_registry.count_runs(), 0)

    def test_structured_requires_auth(self) -> None:
        client = TestClient(app)
        response = client.post(
            "/runs/structured",
            json=_structured_payload(),
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["detail"],
            "Missing bearer token",
        )


if __name__ == "__main__":
    unittest.main()
