"""Contract tests: Phase 2B monitoring through MCP / gateway / Unified wait."""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# Settings are read at import time by mcp_connector.server.
os.environ.setdefault("MISSION_CONTROL_URL", "http://mission-control.test")
os.environ.setdefault("MISSION_CONTROL_API_KEY", "mc_test_key")

from fastapi.testclient import TestClient

from app import api as api_module
from app.api import WaitForRunRequest, app
from hal_legalai_gateway.forwarding import ToolBinding, forward_mcp_tool
from hal_legalai_gateway.mcp_server import (
    DEFAULT_TOOL_BINDINGS,
    create_mcp_server,
)
from mcp_connector import server as mcp_server
from mcp_connector.client import MissionControlClient
from mcp_connector.config import Settings
from mission_control.monitoring import (
    MONITOR_CURSOR_MAX_CHARS,
    decode_monitor_cursor,
    encode_monitor_cursor,
)
from mission_control.openapi_actions import (
    MAX_OPERATION_DESCRIPTION_LENGTH,
    _ACTIONS_OPERATION_DESCRIPTIONS,
    build_actions_openapi,
)
from mission_control.run_registry import RunRegistry


AUTH_HEADERS = {"Authorization": "Bearer mc_test_authentication_key"}
PHASE2B_FIELDS = (
    "heartbeat_health",
    "stale_heartbeat",
    "monitoring_history",
    "cursor",
    "stale_threshold_seconds",
)

os.environ["MISSION_CONTROL_API_KEY"] = "mc_test_authentication_key"


def _settings() -> Settings:
    return Settings(
        mission_control_url="http://mission-control.test",
        mission_control_api_key="mc_test_key",
        request_timeout_seconds=5.0,
    )


def _wait_expired_payload(*, cursor: str = "cursor-out") -> dict[str, Any]:
    return {
        "run_id": "run-fwd-1",
        "status": "running",
        "created_at": "2026-08-13T00:00:00+00:00",
        "started_at": "2026-08-13T00:00:01+00:00",
        "completed_at": None,
        "elapsed_seconds": None,
        "stdout": "",
        "stderr": "",
        "error": None,
        "return_code": None,
        "commit_sha": None,
        "reached_terminal": False,
        "wait_expired": True,
        "timeout_seconds": 8.0,
        "heartbeat_health": "healthy",
        "stale_heartbeat": False,
        "monitoring_history": [
            {
                "at": "2026-08-13T00:00:01+00:00",
                "status": "running",
                "phase": "agent_execution",
                "progress": {"step": "agent_execution", "detail": "working"},
                "heartbeat_health": "healthy",
            }
        ],
        "cursor": cursor,
            "stale_threshold_seconds": 90.0,
    }


def _terminal_payload(*, cursor: str = "cursor-done") -> dict[str, Any]:
    payload = _wait_expired_payload(cursor=cursor)
    payload.update(
        {
            "status": "completed",
            "completed_at": "2026-08-13T00:00:10+00:00",
            "elapsed_seconds": 9.0,
            "stdout": "done",
            "return_code": 0,
            "commit_sha": "abc123",
            "reached_terminal": True,
            "wait_expired": False,
            "heartbeat_health": "terminal",
            "monitoring_history": [
                {
                    "at": "2026-08-13T00:00:10+00:00",
                    "status": "completed",
                    "phase": "completed",
                    "progress": None,
                    "heartbeat_health": "terminal",
                }
            ],
        }
    )
    return payload


class TestMonitorCursorSizeBound(unittest.TestCase):
    def test_decode_rejects_oversized_cursor(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            decode_monitor_cursor("x" * (MONITOR_CURSOR_MAX_CHARS + 1))
        self.assertIn("cursor", str(ctx.exception))

    def test_api_request_model_rejects_oversized_cursor(self) -> None:
        with self.assertRaises(Exception):
            WaitForRunRequest(
                timeout_seconds=1.0,
                cursor="x" * (MONITOR_CURSOR_MAX_CHARS + 1),
            )

    def test_api_wait_rejects_oversized_cursor_with_422(self) -> None:
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        try:
            api_module.run_registry = RunRegistry(db_path)
            client = TestClient(app, headers=AUTH_HEADERS)
            record = api_module.run_registry.create_run()
            response = client.post(
                f"/runs/{record.run_id}/wait",
                json={
                    "timeout_seconds": 0.1,
                    "cursor": "x" * (MONITOR_CURSOR_MAX_CHARS + 1),
                },
            )
            self.assertEqual(response.status_code, 422)
        finally:
            api_module.run_registry.close()
            os.unlink(db_path)


class TestMcpWaitContractBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_tool_schema_exposes_optional_cursor(self) -> None:
        tools = mcp_server.mcp._tool_manager.list_tools()
        wait_tool = next(tool for tool in tools if tool.name == "wait_for_run")
        props = wait_tool.parameters["properties"]
        self.assertIn("cursor", props)
        self.assertEqual(wait_tool.parameters["required"], ["run_id"])
        description = wait_tool.description or ""
        self.assertIn("cursor", description)
        self.assertIn("cancelled", description)
        self.assertIn("heartbeat_health", description)

    async def test_tool_forwards_phase2b_fields_unchanged(self) -> None:
        payload = _wait_expired_payload()
        with patch.object(
            mcp_server.client,
            "wait_for_run",
            new=AsyncMock(return_value=payload),
        ):
            result = await mcp_server.wait_for_run(
                "run-fwd-1",
                timeout_seconds=8.0,
                cursor="cursor-in",
            )
        self.assertTrue(result["ok"])
        for field in PHASE2B_FIELDS:
            self.assertEqual(result[field], payload[field])


class TestGatewayMissionWaitContractBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_mission_wait_schema_accepts_optional_cursor(self) -> None:
        with patch(
            "hal_legalai_gateway.mcp_server.bindings_from_registry",
            return_value=DEFAULT_TOOL_BINDINGS,
        ):
            mcp = create_mcp_server(MagicMock(registry=MagicMock()), auth=MagicMock())
        manager = mcp._tool_manager
        if hasattr(manager, "list_tools"):
            tools = manager.list_tools()
        else:
            maybe = manager.get_tools()
            if hasattr(maybe, "__await__"):
                tools_map = await maybe
            else:
                tools_map = maybe
            tools = list(tools_map.values())
        wait_tool = next(tool for tool in tools if tool.name == "mission.wait")
        params = getattr(wait_tool, "parameters", None)
        if params is None:
            import inspect

            fn = getattr(wait_tool, "fn", None) or getattr(wait_tool, "function", None)
            self.assertIsNotNone(fn)
            sig = inspect.signature(fn)
            self.assertIn("cursor", sig.parameters)
            description = wait_tool.description or ""
        else:
            props = params["properties"]
            self.assertIn("cursor", props)
            self.assertEqual(params.get("required") or ["run_id"], ["run_id"])
            description = wait_tool.description or ""
        self.assertIn("cursor", description)
        self.assertIn("heartbeat_health", description)
        self.assertIn("cancelled", description)
        wait_binding = next(
            b for b in DEFAULT_TOOL_BINDINGS if b.gateway_tool == "mission.wait"
        )
        self.assertIn("cursor", wait_binding.description)

    async def test_forwarding_passes_wait_result_unchanged(self) -> None:
        """Gateway must not fabricate monitoring fields; passthrough only."""
        downstream = {
            "ok": True,
            **_wait_expired_payload(cursor="from-mc"),
        }

        class FakeClient:
            def __init__(self) -> None:
                self.name: str | None = None
                self.arguments: dict[str, Any] | None = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def call_tool(self, name, arguments, raise_on_error=False):
                self.name = name
                self.arguments = arguments

                class Result:
                    is_error = False
                    data = downstream
                    structured_content = downstream
                    content = None

                return Result()

        binding = ToolBinding(
            gateway_tool="mission.wait",
            namespace="mission",
            downstream_service="mission_control",
            downstream_tool="wait_for_run",
            description="test",
        )
        client = FakeClient()
        envelope = await forward_mcp_tool(
            binding=binding,
            arguments={
                "run_id": "run-fwd-1",
                "timeout_seconds": 8.0,
                "cursor": "cursor-in",
            },
            base_url="https://mission.example",
            authorization=None,
            connect_timeout_seconds=1.0,
            read_timeout_seconds=10.0,
            require_authorization=False,
            client_factory=lambda: client,
        )
        self.assertTrue(envelope["ok"])
        result = envelope["result"]
        self.assertIsInstance(result, dict)
        for field in PHASE2B_FIELDS:
            self.assertEqual(result[field], downstream[field])
        self.assertEqual(client.name, "wait_for_run")
        assert client.arguments is not None
        self.assertEqual(client.arguments.get("cursor"), "cursor-in")
        # No fabricated monitoring keys beyond Mission Control payload.
        self.assertEqual(result["cursor"], "from-mc")
        self.assertNotIn("monitoring_history_truncated", result)


class TestOpenApiActionsWaitDescriptions(unittest.TestCase):
    def test_wait_descriptions_document_terminal_and_cursor(self) -> None:
        wait_desc = _ACTIONS_OPERATION_DESCRIPTIONS["wait_for_run"]
        saw_desc = _ACTIONS_OPERATION_DESCRIPTIONS["submit_and_wait"]
        for desc in (wait_desc, saw_desc):
            self.assertLess(len(desc), MAX_OPERATION_DESCRIPTION_LENGTH)
            self.assertIn("cursor", desc.lower())
            self.assertIn("cancelled", desc.lower())
        actions = build_actions_openapi(app.openapi())
        wait_path = actions.get("paths", {}).get("/runs/{run_id}/wait", {})
        description = (wait_path.get("post") or {}).get("description") or ""
        self.assertIn("cursor", description.lower())
        props = (
            actions.get("components", {})
            .get("schemas", {})
            .get("WaitForRunRequest", {})
            .get("properties", {})
        )
        self.assertIn("cursor", props)
        self.assertEqual(props["cursor"].get("maxLength"), MONITOR_CURSOR_MAX_CHARS)


class TestEndToEndMockedForwarding(unittest.IsolatedAsyncioTestCase):
    async def test_wait_expired_cursor_resume_bound_reject_and_terminal(
        self,
    ) -> None:
        client = MissionControlClient(_settings())
        expired = _wait_expired_payload(cursor="resume-cursor")
        terminal = _terminal_payload(cursor="final-cursor")
        calls: list[dict[str, Any]] = []

        async def fake_request(method, path, *, json=None, timeout=None):
            calls.append(
                {
                    "method": method,
                    "path": path,
                    "json": json,
                    "timeout": timeout,
                }
            )
            if json and json.get("cursor") == "resume-cursor":
                return terminal
            return expired

        with patch.object(client, "_request", side_effect=fake_request):
            first = await client.wait_for_run(
                "run-fwd-1",
                timeout_seconds=8.0,
                poll_interval_seconds=2.0,
            )
            self.assertTrue(first["wait_expired"])
            self.assertEqual(first["cursor"], "resume-cursor")
            for field in PHASE2B_FIELDS:
                self.assertIn(field, first)

            second = await client.wait_for_run(
                "run-fwd-1",
                timeout_seconds=8.0,
                poll_interval_seconds=2.0,
                cursor=first["cursor"],
            )
            self.assertFalse(second["wait_expired"])
            self.assertTrue(second["reached_terminal"])
            self.assertEqual(second["status"], "completed")
            self.assertEqual(second["cursor"], "final-cursor")
            self.assertEqual(second["heartbeat_health"], "terminal")

            with self.assertRaises(ValueError):
                await client.wait_for_run(
                    "run-fwd-1",
                    timeout_seconds=8.0,
                    cursor="x" * (MONITOR_CURSOR_MAX_CHARS + 1),
                )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["path"], "/runs/run-fwd-1/wait")
        self.assertNotIn("cursor", calls[0]["json"])
        self.assertEqual(calls[1]["json"]["cursor"], "resume-cursor")
        self.assertLessEqual(calls[0]["json"]["timeout_seconds"], 8.0)
        self.assertGreater(calls[0]["timeout"], 8.0)

        with patch.object(
            mcp_server.client,
            "wait_for_run",
            new=AsyncMock(side_effect=[expired, terminal]),
        ):
            tool_expired = await mcp_server.wait_for_run(
                "run-fwd-1",
                timeout_seconds=8.0,
            )
            tool_terminal = await mcp_server.wait_for_run(
                "run-fwd-1",
                timeout_seconds=8.0,
                cursor="resume-cursor",
            )
        self.assertTrue(tool_expired["ok"])
        self.assertTrue(tool_expired["wait_expired"])
        self.assertEqual(tool_expired["cursor"], "resume-cursor")
        self.assertTrue(tool_terminal["ok"])
        self.assertFalse(tool_terminal["wait_expired"])
        self.assertEqual(tool_terminal["status"], "completed")

        history = expired["monitoring_history"]
        encoded = encode_monitor_cursor(history)
        self.assertLessEqual(len(encoded), MONITOR_CURSOR_MAX_CHARS)
        restored = decode_monitor_cursor(encoded)
        self.assertEqual(restored[0]["heartbeat_health"], "healthy")


if __name__ == "__main__":
    unittest.main()
