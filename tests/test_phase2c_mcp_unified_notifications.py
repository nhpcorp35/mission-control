"""Phase 2C MCP + Unified/Unified1 notification inspection forwarding."""

from __future__ import annotations

import os
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

# Settings are read at import time by mcp_connector.server.
os.environ.setdefault("MISSION_CONTROL_URL", "http://mission-control.test")
os.environ.setdefault("MISSION_CONTROL_API_KEY", "mc_test_key")

from fastapi.testclient import TestClient

from app import api as api_module
from app.api import app
from hal_legalai_gateway.forwarding import ToolBinding, forward_mcp_tool
from hal_legalai_gateway.mcp_server import (
    DEFAULT_TOOL_BINDINGS,
)
from hal_legalai_gateway.registry import load_registry
from mcp_connector import server as mcp_server
from mcp_connector.client import (
    MCP_NOTIFICATION_DEFAULT_LIMIT,
    MCP_NOTIFICATION_MAX_LIMIT,
    MCP_NOTIFICATION_MAX_RUN_ID_CHARS,
    MissionControlClient,
    normalize_mcp_notification_limit,
    normalize_mcp_notification_run_id,
    project_notification_inspection,
)
from mcp_connector.config import Settings
from mission_control.monitoring import MONITOR_CURSOR_MAX_CHARS
from mission_control.notifications import NOTIFICATION_INSPECT_MAX_EVENTS
from mission_control.run_registry import RunPhase, RunRegistry, RunStatus

AUTH_HEADERS = {"Authorization": "Bearer mc_test_authentication_key"}
os.environ["MISSION_CONTROL_API_KEY"] = "mc_test_authentication_key"

PHASE2B_WAIT_FIELDS = (
    "heartbeat_health",
    "stale_heartbeat",
    "monitoring_history",
    "cursor",
    "stale_threshold_seconds",
)
FORBIDDEN_KEYS = (
    "webhook_url",
    "webhook_secret",
    "secret",
    "claim_owner",
    "payload_json",
    "raw_headers",
    "raw_body",
)


def _settings() -> Settings:
    return Settings(
        mission_control_url="http://mission-control.test",
        mission_control_api_key="mc_test_key",
        request_timeout_seconds=5.0,
    )


def _clean_event(event_id: str = "evt-1") -> dict[str, Any]:
    return {
        "event_id": event_id,
        "run_id": "run-n1",
        "event_kind": "terminal",
        "status": "completed",
        "phase": "completed",
        "progress": {"step": "done", "detail": "ok"},
        "heartbeat_health": "terminal",
        "occurred_at": "2026-08-13T00:00:00+00:00",
        "delivery_state": "delivered",
        "attempt_count": 1,
        "next_attempt_at": None,
        "last_error": None,
        "delivered_at": "2026-08-13T00:00:01+00:00",
        "created_at": "2026-08-13T00:00:00+00:00",
    }


class TestNotificationLimitAndRunIdBounds(unittest.TestCase):
    def test_limit_default_and_clamp(self) -> None:
        self.assertEqual(
            normalize_mcp_notification_limit(None),
            MCP_NOTIFICATION_DEFAULT_LIMIT,
        )
        self.assertEqual(normalize_mcp_notification_limit(3), 3)
        self.assertEqual(
            normalize_mcp_notification_limit(10_000),
            MCP_NOTIFICATION_MAX_LIMIT,
        )
        self.assertEqual(
            MCP_NOTIFICATION_MAX_LIMIT,
            NOTIFICATION_INSPECT_MAX_EVENTS,
        )
        with self.assertRaises(ValueError):
            normalize_mcp_notification_limit(0)
        with self.assertRaises(ValueError):
            normalize_mcp_notification_limit(-1)
        with self.assertRaises(ValueError):
            normalize_mcp_notification_limit("nope")  # type: ignore[arg-type]

    def test_run_id_rejects_malicious_and_oversized(self) -> None:
        self.assertEqual(normalize_mcp_notification_run_id(" run-ok "), "run-ok")
        with self.assertRaises(ValueError):
            normalize_mcp_notification_run_id("")
        with self.assertRaises(ValueError):
            normalize_mcp_notification_run_id("x" * (MCP_NOTIFICATION_MAX_RUN_ID_CHARS + 1))
        with self.assertRaises(ValueError):
            normalize_mcp_notification_run_id("../secret")
        with self.assertRaises(ValueError):
            normalize_mcp_notification_run_id("run?x=1")


class TestNotificationProjectionRedaction(unittest.TestCase):
    def test_strips_forbidden_fields_and_redacts_errors(self) -> None:
        body = {
            "run_id": "run-n1",
            "notifications_enabled": True,
            "webhook_secret": "should-not-leak",
            "events": [
                {
                    **_clean_event(),
                    "webhook_url": "https://hooks.example/notify",
                    "webhook_secret": "hook-secret",
                    "claim_owner": "worker-1",
                    "payload_json": '{"stdout":"secret"}',
                    "raw_headers": {"Authorization": "Bearer abc"},
                    "raw_body": "leak",
                    "last_error": "Authorization bearer abc",
                },
                _clean_event("evt-2"),
            ],
            "truncated": False,
            "max_events": 64,
        }
        out = project_notification_inspection(body, limit=1)
        self.assertEqual(len(out["events"]), 1)
        self.assertTrue(out["truncated"])
        blob = str(out)
        for needle in (
            "should-not-leak",
            "hooks.example",
            "hook-secret",
            "worker-1",
            "stdout",
            "Bearer abc",
        ):
            self.assertNotIn(needle, blob)
        for key in FORBIDDEN_KEYS:
            self.assertNotIn(key, out)
            self.assertNotIn(key, out["events"][0])
        self.assertEqual(out["events"][0]["last_error"], "[redacted]")


class TestMcpNotificationClientAndTool(unittest.IsolatedAsyncioTestCase):
    async def test_client_sends_bounded_limit_and_projects(self) -> None:
        client = MissionControlClient(_settings())
        downstream = {
            "run_id": "run-n1",
            "notifications_enabled": False,
            "events": [_clean_event("a"), _clean_event("b"), _clean_event("c")],
            "truncated": False,
            "max_events": 64,
            "claim_owner": "should-strip",
        }
        with patch.object(
            client,
            "_request",
            new=AsyncMock(return_value=downstream),
        ) as request:
            result = await client.list_run_notifications("run-n1", limit=2)
        request.assert_awaited_once()
        self.assertEqual(request.await_args.args[0], "GET")
        self.assertEqual(
            request.await_args.args[1],
            "/runs/run-n1/notifications",
        )
        self.assertEqual(request.await_args.kwargs.get("params"), {"limit": 2})
        self.assertEqual(len(result["events"]), 2)
        self.assertTrue(result["truncated"])
        self.assertNotIn("claim_owner", result)

    async def test_client_auth_error_propagates(self) -> None:
        from mcp_connector.errors import MissionControlError

        client = MissionControlClient(_settings())
        with patch.object(
            client,
            "_request",
            new=AsyncMock(
                side_effect=MissionControlError(
                    "Mission Control request failed",
                    status_code=401,
                    details={"detail": "Unauthorized"},
                )
            ),
        ):
            with self.assertRaises(MissionControlError) as ctx:
                await client.list_run_notifications("run-n1", limit=5)
        self.assertEqual(ctx.exception.status_code, 401)

        with patch.object(
            mcp_server.client,
            "list_run_notifications",
            new=AsyncMock(side_effect=ctx.exception),
        ):
            shaped = await mcp_server.list_run_notifications("run-n1", limit=5)
        self.assertFalse(shaped["ok"])
        self.assertEqual(shaped["error"]["status_code"], 401)
        self.assertNotIn("webhook_secret", str(shaped))
        self.assertNotIn("claim_owner", str(shaped))

    async def test_mcp_tool_path_and_validation_errors(self) -> None:
        payload = {
            "run_id": "run-n1",
            "notifications_enabled": True,
            "events": [_clean_event()],
            "truncated": False,
            "max_events": 1,
        }
        with patch.object(
            mcp_server.client,
            "list_run_notifications",
            new=AsyncMock(return_value=payload),
        ):
            result = await mcp_server.list_run_notifications("run-n1", limit=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["run_id"], "run-n1")
        self.assertEqual(len(result["events"]), 1)

        bad = await mcp_server.list_run_notifications("../x", limit=1)
        self.assertFalse(bad["ok"])
        self.assertIn("run_id", bad["error"]["message"])

        oversized = await mcp_server.list_run_notifications(
            "run-n1",
            limit=0,
        )
        self.assertFalse(oversized["ok"])
        self.assertIn("limit", oversized["error"]["message"])

    def test_tool_registered(self) -> None:
        tools = mcp_server.mcp._tool_manager.list_tools()
        names = [tool.name for tool in tools]
        self.assertIn("list_run_notifications", names)
        self.assertEqual(names, list(mcp_server.EXPECTED_TOOL_NAMES))
        notif = next(t for t in tools if t.name == "list_run_notifications")
        props = notif.parameters["properties"]
        self.assertIn("run_id", props)
        self.assertIn("limit", props)


class TestApiNotificationLimit(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)
        self.outbox = self.registry._get_notification_outbox()
        self._prev_registry = api_module.run_registry
        self._prev_outbox = api_module.notification_outbox
        api_module.run_registry = self.registry
        api_module.notification_outbox = self.outbox
        self.client = TestClient(app, raise_server_exceptions=True)

    def tearDown(self) -> None:
        self.client.close()
        api_module.run_registry = self._prev_registry
        api_module.notification_outbox = self._prev_outbox
        self.registry.close()
        os.unlink(self._db_path)

    def test_api_honors_bounded_limit_and_auth(self) -> None:
        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        self.registry.set_phase(record.run_id, RunPhase.VERIFICATION)
        self.registry.update_status(record.run_id, RunStatus.COMPLETED)

        unauth = self.client.get(f"/runs/{record.run_id}/notifications?limit=1")
        self.assertEqual(unauth.status_code, 401)

        response = self.client.get(
            f"/runs/{record.run_id}/notifications",
            headers=AUTH_HEADERS,
            params={"limit": 1},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertLessEqual(len(body["events"]), 1)
        self.assertEqual(body["max_events"], 1)
        blob = str(body)
        self.assertNotIn("claim_owner", blob)
        self.assertNotIn("webhook_secret", blob)

        rejected = self.client.get(
            f"/runs/{record.run_id}/notifications",
            headers=AUTH_HEADERS,
            params={"limit": 0},
        )
        self.assertEqual(rejected.status_code, 422)


class TestUnifiedForwardingParity(unittest.IsolatedAsyncioTestCase):
    def test_registry_and_defaults_share_notification_binding(self) -> None:
        registry = load_registry()
        reg_binding = next(
            b
            for b in registry.tool_bindings
            if b.gateway_tool == "mission.list_notifications"
        )
        default_binding = next(
            b
            for b in DEFAULT_TOOL_BINDINGS
            if b.gateway_tool == "mission.list_notifications"
        )
        self.assertEqual(
            reg_binding.downstream_tool,
            "list_run_notifications",
        )
        self.assertEqual(
            default_binding.downstream_tool,
            "list_run_notifications",
        )
        self.assertEqual(
            reg_binding.downstream_service,
            default_binding.downstream_service,
        )
        self.assertIn(
            "mission.list_notifications",
            registry.namespaces["mission"].tools,
        )
        for text in (reg_binding.description, default_binding.description):
            self.assertIn("redacted", text.lower())
            self.assertIn("limit", text.lower())
            self.assertIn("webhook", text.lower())

        # Wait/cursor schema remains present (no regression).
        wait = next(
            b for b in registry.tool_bindings if b.gateway_tool == "mission.wait"
        )
        self.assertIn("cursor", wait.description)
        for field in ("heartbeat_health", "monitoring_history", "cursor"):
            self.assertIn(field, wait.description)

    async def test_unified_forward_passes_limit_and_strips_nothing_extra(self) -> None:
        """Unified / Unified1 share this gateway forwarder."""
        downstream = {
            "ok": True,
            "run_id": "run-n1",
            "notifications_enabled": True,
            "events": [_clean_event()],
            "truncated": False,
            "max_events": 3,
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
            gateway_tool="mission.list_notifications",
            namespace="mission",
            downstream_service="mission_control",
            downstream_tool="list_run_notifications",
            description="test",
        )
        client = FakeClient()
        envelope = await forward_mcp_tool(
            binding=binding,
            arguments={"run_id": "run-n1", "limit": 3},
            base_url="https://mission.example",
            authorization=None,
            connect_timeout_seconds=1.0,
            read_timeout_seconds=10.0,
            require_authorization=False,
            client_factory=lambda: client,
        )
        self.assertTrue(envelope["ok"])
        self.assertEqual(client.name, "list_run_notifications")
        assert client.arguments is not None
        self.assertEqual(client.arguments.get("run_id"), "run-n1")
        self.assertEqual(client.arguments.get("limit"), 3)
        result = envelope["result"]
        self.assertEqual(result["run_id"], "run-n1")

    async def test_forward_redacts_when_downstream_returns_forbidden(self) -> None:
        """Gateway returns downstream payload; MCP client projection is the gate.

        Simulate connector projection on a malicious downstream body to prove
        forbidden webhook/secret/claim fields never survive inspection.
        """
        malicious = {
            "run_id": "run-n1",
            "notifications_enabled": True,
            "events": [
                {
                    **_clean_event(),
                    "webhook_url": "https://evil.example/h",
                    "webhook_secret": "s3cret",
                    "claim_owner": "claimed",
                }
            ],
            "truncated": False,
            "max_events": 64,
            "webhook_url": "https://evil.example/h",
        }
        projected = project_notification_inspection(malicious, limit=10)
        blob = str(projected)
        self.assertNotIn("evil.example", blob)
        self.assertNotIn("s3cret", blob)
        self.assertNotIn("claimed", blob)
        for key in FORBIDDEN_KEYS:
            self.assertNotIn(key, projected)
            if projected["events"]:
                self.assertNotIn(key, projected["events"][0])

    def test_wait_cursor_fields_unchanged_constant(self) -> None:
        # Sanity: Phase 2B cursor bound still exported for wait tools.
        self.assertGreater(MONITOR_CURSOR_MAX_CHARS, 0)
        wait_tool = next(
            t
            for t in mcp_server.mcp._tool_manager.list_tools()
            if t.name == "wait_for_run"
        )
        props = wait_tool.parameters["properties"]
        self.assertIn("cursor", props)
        for field in PHASE2B_WAIT_FIELDS:
            self.assertIn(field, wait_tool.description)


if __name__ == "__main__":
    unittest.main()
