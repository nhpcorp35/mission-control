"""Focused tests for the MCP wait_for_run tool and Phase 2B wait forwarding."""

from __future__ import annotations

import json
import os
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

# Settings are read at import time by mcp_connector.server.
os.environ.setdefault("MISSION_CONTROL_URL", "http://mission-control.test")
os.environ.setdefault("MISSION_CONTROL_API_KEY", "mc_test_key")

from mcp_connector.client import (
    MCP_WAIT_DEFAULT_POLL_INTERVAL_SECONDS,
    MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
    MCP_WAIT_MAX_POLL_INTERVAL_SECONDS,
    MCP_WAIT_MAX_TIMEOUT_SECONDS,
    MCP_WAIT_MIN_POLL_INTERVAL_SECONDS,
    MCP_WAIT_MIN_TIMEOUT_SECONDS,
    MissionControlClient,
    normalize_mcp_wait_cursor,
    normalize_mcp_wait_poll_interval,
    normalize_mcp_wait_timeout,
)
from mcp_connector.config import Settings
from mcp_connector.errors import MissionControlError
from mcp_connector import server as mcp_server


def _settings() -> Settings:
    return Settings(
        mission_control_url="http://mission-control.test",
        mission_control_api_key="mc_test_key",
        request_timeout_seconds=5.0,
    )


def _run_payload(
    run_id: str,
    status: str,
    *,
    stdout: str = "",
    commit_sha: str | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "status": status,
        "created_at": "2026-07-22T00:00:00+00:00",
        "started_at": "2026-07-22T00:00:01+00:00",
        "completed_at": (
            "2026-07-22T00:00:02+00:00"
            if status in {"completed", "failed", "timed_out"}
            else None
        ),
        "elapsed_seconds": 1.0 if status in {"completed", "failed", "timed_out"} else None,
        "stdout": stdout,
        "stderr": "",
        "error": None,
        "return_code": 0 if status == "completed" else None,
        "commit_sha": commit_sha,
    }


class TestNormalizeMcpWaitBounds(unittest.TestCase):
    def test_default_timeout_within_bounds(self) -> None:
        self.assertEqual(MCP_WAIT_DEFAULT_TIMEOUT_SECONDS, 20.0)
        self.assertLessEqual(
            MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
            MCP_WAIT_MAX_TIMEOUT_SECONDS,
        )
        self.assertEqual(MCP_WAIT_MAX_TIMEOUT_SECONDS, 3600.0)
        self.assertEqual(MCP_WAIT_DEFAULT_POLL_INTERVAL_SECONDS, 2.0)

    def test_normalize_timeout_preserves_safe_values(self) -> None:
        self.assertEqual(normalize_mcp_wait_timeout(5.0), 5.0)
        self.assertEqual(
            normalize_mcp_wait_timeout(MCP_WAIT_DEFAULT_TIMEOUT_SECONDS),
            MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
        )
        self.assertEqual(normalize_mcp_wait_timeout(900.0), 900.0)
        self.assertEqual(
            normalize_mcp_wait_timeout(MCP_WAIT_MAX_TIMEOUT_SECONDS),
            MCP_WAIT_MAX_TIMEOUT_SECONDS,
        )

    def test_normalize_timeout_caps_above_max(self) -> None:
        self.assertEqual(
            normalize_mcp_wait_timeout(MCP_WAIT_MAX_TIMEOUT_SECONDS + 1),
            MCP_WAIT_MAX_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            normalize_mcp_wait_timeout(10_000.0),
            MCP_WAIT_MAX_TIMEOUT_SECONDS,
        )

    def test_normalize_timeout_rejects_invalid(self) -> None:
        for value in (0, -1, -0.5, MCP_WAIT_MIN_TIMEOUT_SECONDS - 0.05):
            with self.subTest(timeout_seconds=value):
                with self.assertRaises(ValueError) as ctx:
                    normalize_mcp_wait_timeout(value)
                self.assertIn("timeout_seconds", str(ctx.exception))

    def test_normalize_poll_interval_caps_and_rejects(self) -> None:
        self.assertEqual(normalize_mcp_wait_poll_interval(1.0), 1.0)
        self.assertEqual(
            normalize_mcp_wait_poll_interval(100.0),
            MCP_WAIT_MAX_POLL_INTERVAL_SECONDS,
        )
        for value in (0, -1, MCP_WAIT_MIN_POLL_INTERVAL_SECONDS - 0.01):
            with self.subTest(poll_interval_seconds=value):
                with self.assertRaises(ValueError) as ctx:
                    normalize_mcp_wait_poll_interval(value)
                self.assertIn("poll_interval_seconds", str(ctx.exception))

    def test_normalize_cursor_bounds(self) -> None:
        from mission_control.monitoring import MONITOR_CURSOR_MAX_CHARS

        self.assertIsNone(normalize_mcp_wait_cursor(None))
        self.assertIsNone(normalize_mcp_wait_cursor("  "))
        self.assertEqual(normalize_mcp_wait_cursor(" abc "), "abc")
        with self.assertRaises(ValueError) as ctx:
            normalize_mcp_wait_cursor("x" * (MONITOR_CURSOR_MAX_CHARS + 1))
        self.assertIn("cursor", str(ctx.exception))


class TestSubmitStructuredRunClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = MissionControlClient(_settings())

    async def test_client_serializes_nested_approval_without_flat_default(
        self,
    ) -> None:
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(return_value={"run_id": "r1", "status": "queued"}),
        ) as request:
            await self.client.submit_structured_run(
                mission_id="m1",
                title="T",
                instructions="Do it",
                deliverables=["summary"],
                create_files=True,
                modify_files=False,
                approval={"platform_push_approved": True},
            )
        request.assert_awaited_once()
        payload = request.await_args.kwargs["json"]
        self.assertNotIn("platform_push_approved", payload)
        self.assertEqual(
            payload["approval"],
            {"platform_push_approved": True},
        )
        self.assertNotIn("persistence_mode", payload)

    async def test_client_serializes_flat_platform_push_approved(self) -> None:
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(return_value={"run_id": "r1", "status": "queued"}),
        ) as request:
            await self.client.submit_structured_run(
                mission_id="m1",
                title="T",
                instructions="Do it",
                deliverables=["summary"],
                create_files=True,
                modify_files=False,
                platform_push_approved=True,
            )
        payload = request.await_args.kwargs["json"]
        self.assertEqual(payload["platform_push_approved"], True)
        self.assertNotIn("approval", payload)
        self.assertNotIn("persistence_mode", payload)

    async def test_client_omits_persistence_mode_when_unset(self) -> None:
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(return_value={"run_id": "r1", "status": "queued"}),
        ) as request:
            await self.client.submit_structured_run(
                mission_id="m1",
                title="T",
                instructions="Do it",
                deliverables=["summary"],
                create_files=True,
                modify_files=False,
            )
        payload = request.await_args.kwargs["json"]
        self.assertNotIn("persistence_mode", payload)

    async def test_client_sends_explicit_persistence_mode(self) -> None:
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(return_value={"run_id": "r1", "status": "queued"}),
        ) as request:
            await self.client.submit_structured_run(
                mission_id="m1",
                title="T",
                instructions="Do it",
                deliverables=["summary"],
                create_files=True,
                modify_files=False,
                persistence_mode="none",
            )
        payload = request.await_args.kwargs["json"]
        self.assertEqual(payload["persistence_mode"], "none")

    async def test_client_maps_legal_ai_repository_routing_fields(self) -> None:
        """MCP structured wrapper must forward LegalAI routing fields unchanged."""
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(return_value={"run_id": "r-legal", "status": "queued"}),
        ) as request:
            await self.client.submit_structured_run(
                mission_id="2026-08-08-legalai",
                title="LegalAI routing",
                instructions="Fix LegalAI",
                deliverables=["summary"],
                create_files=True,
                modify_files=True,
                persistence_mode="commit",
                repository_name="nhpcorp35/legal-ai",
                repository_path=".",
                base_branch="main",
            )
        request.assert_awaited_once()
        self.assertEqual(request.await_args.args[0], "POST")
        self.assertEqual(request.await_args.args[1], "/runs/structured")
        payload = request.await_args.kwargs["json"]
        self.assertEqual(payload["repository_name"], "nhpcorp35/legal-ai")
        self.assertEqual(payload["repository_path"], ".")
        self.assertEqual(payload["base_branch"], "main")
        self.assertEqual(payload["persistence_mode"], "commit")


class TestWaitForRunClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = MissionControlClient(_settings())

    def _phase2b_payload(
        self,
        run_id: str,
        status: str,
        *,
        wait_expired: bool,
        cursor: str = "cursor-abc",
        **extra: Any,
    ) -> dict[str, Any]:
        payload = {
            **_run_payload(run_id, status, **extra),
            "wait_expired": wait_expired,
            "reached_terminal": not wait_expired,
            "timeout_seconds": 5.0,
            "heartbeat_health": "terminal" if not wait_expired else "healthy",
            "stale_heartbeat": False,
            "monitoring_history": [
                {
                    "at": "2026-08-13T00:00:00+00:00",
                    "status": status,
                    "phase": "agent_execution",
                    "progress": {"step": "agent_execution", "detail": "ok"},
                    "heartbeat_health": (
                        "terminal" if not wait_expired else "healthy"
                    ),
                }
            ],
            "cursor": cursor,
            "stale_threshold_seconds": 30.0,
        }
        return payload

    async def test_forwards_to_server_wait_and_preserves_monitoring(
        self,
    ) -> None:
        payload = self._phase2b_payload(
            "run-1",
            "completed",
            wait_expired=False,
            stdout="done",
            commit_sha="abc",
        )
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(return_value=payload),
        ) as request:
            result = await self.client.wait_for_run(
                "run-1",
                timeout_seconds=5.0,
                poll_interval_seconds=0.1,
            )

        self.assertEqual(result, payload)
        request.assert_awaited_once_with(
            "POST",
            "/runs/run-1/wait",
            json={
                "timeout_seconds": 5.0,
                "poll_interval_seconds": 0.1,
            },
            timeout=60.0,
        )
        for field in (
            "heartbeat_health",
            "stale_heartbeat",
            "monitoring_history",
            "cursor",
            "stale_threshold_seconds",
        ):
            self.assertIn(field, result)

    async def test_wait_expired_cursor_output_forwarded_unchanged(self) -> None:
        payload = self._phase2b_payload(
            "run-3",
            "running",
            wait_expired=True,
            cursor="resume-me",
            stdout="still going",
        )
        payload["timeout_seconds"] = 0.15
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(return_value=payload),
        ) as request:
            result = await self.client.wait_for_run(
                "run-3",
                timeout_seconds=0.15,
                poll_interval_seconds=0.05,
            )

        self.assertTrue(result["wait_expired"])
        self.assertFalse(result["reached_terminal"])
        self.assertEqual(result["cursor"], "resume-me")
        self.assertEqual(result["heartbeat_health"], "healthy")
        self.assertEqual(result["monitoring_history"], payload["monitoring_history"])
        self.assertEqual(
            request.await_args.kwargs["json"]["timeout_seconds"],
            0.15,
        )

    async def test_cursor_input_resume_forwarded(self) -> None:
        payload = self._phase2b_payload(
            "run-2",
            "completed",
            wait_expired=False,
            cursor="next-cursor",
        )
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(return_value=payload),
        ) as request:
            result = await self.client.wait_for_run(
                "run-2",
                timeout_seconds=5.0,
                poll_interval_seconds=0.05,
                cursor="  prior-cursor  ",
            )

        self.assertEqual(result["cursor"], "next-cursor")
        self.assertEqual(
            request.await_args.kwargs["json"],
            {
                "timeout_seconds": 5.0,
                "poll_interval_seconds": 0.05,
                "cursor": "prior-cursor",
            },
        )

    async def test_legacy_caller_without_cursor_omits_cursor_field(self) -> None:
        payload = self._phase2b_payload(
            "run-legacy",
            "completed",
            wait_expired=False,
        )
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(return_value=payload),
        ) as request:
            await self.client.wait_for_run(
                "run-legacy",
                timeout_seconds=5.0,
                poll_interval_seconds=0.1,
            )
        self.assertNotIn("cursor", request.await_args.kwargs["json"])

    async def test_default_timeout_used_when_omitted(self) -> None:
        payload = self._phase2b_payload(
            "run-default",
            "queued",
            wait_expired=True,
        )
        payload["timeout_seconds"] = MCP_WAIT_DEFAULT_TIMEOUT_SECONDS
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(return_value=payload),
        ) as request:
            result = await self.client.wait_for_run("run-default")

        self.assertTrue(result["wait_expired"])
        self.assertEqual(
            request.await_args.kwargs["json"]["timeout_seconds"],
            MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            request.await_args.kwargs["timeout"],
            60.0,
        )

    async def test_timeout_above_former_25s_cap_is_honored(self) -> None:
        """Requested budgets like 900s must not stop at the old ~25s cutoff."""
        payload = self._phase2b_payload(
            "run-long",
            "completed",
            wait_expired=False,
            stdout="finished-after-25s",
        )
        payload["timeout_seconds"] = 900.0
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(return_value=payload),
        ) as request:
            result = await self.client.wait_for_run(
                "run-long",
                timeout_seconds=900.0,
                poll_interval_seconds=0.1,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["timeout_seconds"], 900.0)
        self.assertEqual(
            request.await_args.kwargs["json"]["timeout_seconds"],
            900.0,
        )
        self.assertEqual(request.await_args.kwargs["timeout"], 930.0)

    async def test_oversized_timeout_is_capped_to_max(self) -> None:
        payload = self._phase2b_payload(
            "run-cap",
            "running",
            wait_expired=True,
        )
        payload["timeout_seconds"] = MCP_WAIT_MAX_TIMEOUT_SECONDS
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(return_value=payload),
        ) as request:
            result = await self.client.wait_for_run(
                "run-cap",
                timeout_seconds=MCP_WAIT_MAX_TIMEOUT_SECONDS + 100.0,
                poll_interval_seconds=0.1,
            )

        self.assertTrue(result["wait_expired"])
        self.assertEqual(
            request.await_args.kwargs["json"]["timeout_seconds"],
            MCP_WAIT_MAX_TIMEOUT_SECONDS,
        )

    async def test_invalid_timeout_seconds_rejected(self) -> None:
        for value in (0, -1, -0.5):
            with self.subTest(timeout_seconds=value):
                with self.assertRaises(ValueError) as ctx:
                    await self.client.wait_for_run(
                        "run-x",
                        timeout_seconds=value,
                        poll_interval_seconds=1.0,
                    )
                self.assertIn("timeout_seconds", str(ctx.exception))

    async def test_invalid_poll_interval_seconds_rejected(self) -> None:
        for value in (0, -1, -0.25):
            with self.subTest(poll_interval_seconds=value):
                with self.assertRaises(ValueError) as ctx:
                    await self.client.wait_for_run(
                        "run-x",
                        timeout_seconds=10.0,
                        poll_interval_seconds=value,
                    )
                self.assertIn("poll_interval_seconds", str(ctx.exception))

    async def test_oversized_cursor_rejected_before_forward(self) -> None:
        from mission_control.monitoring import MONITOR_CURSOR_MAX_CHARS

        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(),
        ) as request:
            with self.assertRaises(ValueError) as ctx:
                await self.client.wait_for_run(
                    "run-x",
                    timeout_seconds=10.0,
                    poll_interval_seconds=1.0,
                    cursor="x" * (MONITOR_CURSOR_MAX_CHARS + 1),
                )
        self.assertIn("cursor", str(ctx.exception))
        request.assert_not_awaited()

    async def test_final_terminal_payload_includes_wait_metadata(self) -> None:
        payload = self._phase2b_payload(
            "run-5",
            "failed",
            wait_expired=False,
            stdout="partial",
        )
        payload["error"] = "boom"
        payload["return_code"] = 1
        payload["timeout_seconds"] = 1.0
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(return_value=payload),
        ):
            waited = await self.client.wait_for_run(
                "run-5",
                timeout_seconds=1.0,
                poll_interval_seconds=0.1,
            )

        self.assertFalse(waited["wait_expired"])
        self.assertTrue(waited["reached_terminal"])
        self.assertEqual(waited["timeout_seconds"], 1.0)
        self.assertEqual(waited["heartbeat_health"], "terminal")
        self.assertEqual(waited["cursor"], "cursor-abc")


class TestWaitForRunMcpTool(unittest.IsolatedAsyncioTestCase):
    async def test_tool_success_wraps_terminal_payload(self) -> None:
        payload = {
            **_run_payload("run-t", "completed", stdout="ok"),
            "wait_expired": False,
            "timeout_seconds": MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
            "reached_terminal": True,
        }
        with patch.object(
            mcp_server.client,
            "wait_for_run",
            new=AsyncMock(return_value=payload),
        ):
            result = await mcp_server.wait_for_run("run-t")

        self.assertTrue(result["ok"])
        self.assertEqual(result["run_id"], "run-t")
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["wait_expired"])
        self.assertEqual(
            result["timeout_seconds"],
            MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
        )

    async def test_tool_maps_wait_expired_as_ok_payload(self) -> None:
        payload = {
            **_run_payload("run-t", "running"),
            "wait_expired": True,
            "timeout_seconds": 1.0,
            "reached_terminal": False,
            "heartbeat_health": "healthy",
            "stale_heartbeat": False,
            "monitoring_history": [],
            "cursor": "cursor-1",
            "stale_threshold_seconds": 30.0,
        }
        with patch.object(
            mcp_server.client,
            "wait_for_run",
            new=AsyncMock(return_value=payload),
        ) as wait_for_run:
            result = await mcp_server.wait_for_run(
                "run-t",
                timeout_seconds=1.0,
                poll_interval_seconds=0.1,
                cursor="cursor-0",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["run_id"], "run-t")
        self.assertEqual(result["status"], "running")
        self.assertTrue(result["wait_expired"])
        self.assertEqual(result["timeout_seconds"], 1.0)
        self.assertEqual(result["cursor"], "cursor-1")
        self.assertEqual(result["heartbeat_health"], "healthy")
        wait_for_run.assert_awaited_once_with(
            "run-t",
            timeout_seconds=1.0,
            poll_interval_seconds=0.1,
            cursor="cursor-0",
        )

    async def test_tool_rejects_oversized_cursor_via_client(self) -> None:
        from mission_control.monitoring import MONITOR_CURSOR_MAX_CHARS

        result = await mcp_server.wait_for_run(
            "run-t",
            timeout_seconds=10.0,
            poll_interval_seconds=1.0,
            cursor="x" * (MONITOR_CURSOR_MAX_CHARS + 1),
        )
        self.assertFalse(result["ok"])
        self.assertIn("cursor", result["error"]["message"])

    async def test_tool_rejects_invalid_timeout_via_client(self) -> None:
        result = await mcp_server.wait_for_run(
            "run-t",
            timeout_seconds=0,
            poll_interval_seconds=1.0,
        )
        self.assertFalse(result["ok"])
        self.assertIn("timeout_seconds", result["error"]["message"])

    async def test_tool_rejects_invalid_poll_interval_via_client(self) -> None:
        result = await mcp_server.wait_for_run(
            "run-t",
            timeout_seconds=10.0,
            poll_interval_seconds=-2.0,
        )
        self.assertFalse(result["ok"])
        self.assertIn("poll_interval_seconds", result["error"]["message"])

    def test_tool_discovery_lists_run_tools_including_structured(self) -> None:
        tools = mcp_server.mcp._tool_manager.list_tools()
        names = [tool.name for tool in tools]
        self.assertEqual(
            names,
            [
                "submit_run",
                "submit_structured_run",
                "get_run",
                "wait_for_run",
                "submit_and_wait",
                "run_repository_command",
            ],
        )

        wait_tool = next(tool for tool in tools if tool.name == "wait_for_run")
        props = wait_tool.parameters["properties"]
        self.assertEqual(
            props["timeout_seconds"]["default"],
            MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            props["poll_interval_seconds"]["default"],
            MCP_WAIT_DEFAULT_POLL_INTERVAL_SECONDS,
        )
        self.assertIn("cursor", props)
        self.assertEqual(wait_tool.parameters["required"], ["run_id"])
        description = wait_tool.description or ""
        self.assertIn("POST /runs/{run_id}/wait", description)
        self.assertIn("wait_expired", description)
        self.assertIn("cursor", description)
        self.assertIn("heartbeat_health", description)
        self.assertIn("cancelled", description)
        self.assertIn("3600", description)

        structured = next(
            tool for tool in tools if tool.name == "submit_structured_run"
        )
        structured_props = structured.parameters["properties"]
        for required in (
            "mission_id",
            "title",
            "instructions",
            "deliverables",
            "create_files",
            "modify_files",
        ):
            self.assertIn(required, structured_props)
        self.assertEqual(
            set(structured.parameters["required"]),
            {
                "mission_id",
                "title",
                "instructions",
                "deliverables",
                "create_files",
                "modify_files",
            },
        )
        # ChatGPT / OpenAI MCP clients reject array schemas with empty items {}.
        # list[Any] previously emitted items:{}; list[str] emits a typed items.
        self.assertEqual(
            structured_props["deliverables"].get("items"),
            {"type": "string"},
        )
        self.assertNotEqual(structured_props["deliverables"].get("items"), {})
        # Nested approval.platform_push_approved is part of the tool surface.
        self.assertIn("platform_push_approved", structured_props)
        self.assertIn("approval", structured_props)
        approval_defs = structured.parameters.get("$defs", {})
        approval_model = None
        for name, definition in approval_defs.items():
            if "platform_push_approved" in definition.get("properties", {}):
                approval_model = definition
                break
        self.assertIsNotNone(
            approval_model,
            msg="submit_structured_run must expose nested approval fields",
        )
        assert approval_model is not None
        self.assertEqual(
            approval_model["properties"]["platform_push_approved"].get(
                "type"
            ),
            "boolean",
        )
        description = (structured.description or "").lower()
        self.assertIn("approval.platform_push_approved", description)

        submit_and_wait = next(
            tool for tool in tools if tool.name == "submit_and_wait"
        )
        saw_props = submit_and_wait.parameters["properties"]
        self.assertEqual(
            set(submit_and_wait.parameters["required"]),
            {"mission_yaml"},
        )
        self.assertEqual(
            saw_props["timeout_seconds"]["default"],
            MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            saw_props["poll_interval_seconds"]["default"],
            MCP_WAIT_DEFAULT_POLL_INTERVAL_SECONDS,
        )
        self.assertIn("cursor", saw_props)
        saw_description = submit_and_wait.description or ""
        self.assertIn("submit_run", saw_description)
        self.assertIn("wait_for_run", saw_description)
        self.assertIn("wait_expired", saw_description)
        self.assertIn("cursor", saw_description)


class TestSubmitAndWaitClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = MissionControlClient(_settings())

    async def test_successful_completion_returns_run_payload(self) -> None:
        accepted = {"run_id": "run-saw-1", "status": "queued"}
        terminal = {
            **_run_payload("run-saw-1", "completed", stdout="done", commit_sha="abc"),
            "wait_expired": False,
            "timeout_seconds": 5.0,
            "reached_terminal": True,
        }
        with patch.object(
            self.client,
            "submit_run",
            new=AsyncMock(return_value=accepted),
        ) as submit_run:
            with patch.object(
                self.client,
                "wait_for_run",
                new=AsyncMock(return_value=terminal),
            ) as wait_for_run:
                result = await self.client.submit_and_wait(
                    "mission: yaml",
                    timeout_seconds=5.0,
                    poll_interval_seconds=0.1,
                )

        self.assertEqual(result["run_id"], "run-saw-1")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stdout"], "done")
        self.assertEqual(result["commit_sha"], "abc")
        self.assertFalse(result["wait_expired"])
        self.assertTrue(result["reached_terminal"])
        submit_run.assert_awaited_once_with("mission: yaml")
        wait_for_run.assert_awaited_once_with(
            "run-saw-1",
            timeout_seconds=5.0,
            poll_interval_seconds=0.1,
            cursor=None,
        )

    async def test_submission_failure_skips_wait(self) -> None:
        rejection = {
            "ok": False,
            "stdout": "",
            "stderr": "",
            "error": "invalid mission",
            "error_detail": None,
        }
        with patch.object(
            self.client,
            "submit_run",
            new=AsyncMock(return_value=rejection),
        ) as submit_run:
            with patch.object(
                self.client,
                "wait_for_run",
                new=AsyncMock(),
            ) as wait_for_run:
                result = await self.client.submit_and_wait(
                    "bad: yaml",
                    timeout_seconds=5.0,
                    poll_interval_seconds=0.1,
                )

        self.assertEqual(result, rejection)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid mission")
        submit_run.assert_awaited_once_with("bad: yaml")
        wait_for_run.assert_not_awaited()

    async def test_terminal_immediate_via_wait_path(self) -> None:
        accepted = {"run_id": "run-saw-term", "status": "queued"}
        already_terminal = {
            **_run_payload("run-saw-term", "completed", stdout="instant"),
            "wait_expired": False,
            "timeout_seconds": 10.0,
            "reached_terminal": True,
        }
        with patch.object(
            self.client,
            "submit_run",
            new=AsyncMock(return_value=accepted),
        ):
            with patch.object(
                self.client,
                "wait_for_run",
                new=AsyncMock(return_value=already_terminal),
            ) as wait_for_run:
                result = await self.client.submit_and_wait(
                    "mission: yaml",
                    timeout_seconds=10.0,
                    poll_interval_seconds=0.05,
                )

        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["wait_expired"])
        self.assertTrue(result["reached_terminal"])
        wait_for_run.assert_awaited_once()

    async def test_wait_expiration_returns_structured_payload(self) -> None:
        accepted = {"run_id": "run-saw-exp", "status": "queued"}
        expired = {
            **_run_payload("run-saw-exp", "running", stdout="still going"),
            "wait_expired": True,
            "timeout_seconds": 0.15,
            "reached_terminal": False,
        }
        with patch.object(
            self.client,
            "submit_run",
            new=AsyncMock(return_value=accepted),
        ):
            with patch.object(
                self.client,
                "wait_for_run",
                new=AsyncMock(return_value=expired),
            ):
                result = await self.client.submit_and_wait(
                    "mission: yaml",
                    timeout_seconds=0.15,
                    poll_interval_seconds=0.05,
                )

        self.assertEqual(result["run_id"], "run-saw-exp")
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["stdout"], "still going")
        self.assertTrue(result["wait_expired"])
        self.assertFalse(result["reached_terminal"])
        self.assertEqual(result["timeout_seconds"], 0.15)

    async def test_invalid_timeout_rejected_before_submit(self) -> None:
        with patch.object(
            self.client,
            "submit_run",
            new=AsyncMock(),
        ) as submit_run:
            with self.assertRaises(ValueError) as ctx:
                await self.client.submit_and_wait(
                    "mission: yaml",
                    timeout_seconds=0,
                    poll_interval_seconds=1.0,
                )
        self.assertIn("timeout_seconds", str(ctx.exception))
        submit_run.assert_not_awaited()


class TestSubmitAndWaitMcpTool(unittest.IsolatedAsyncioTestCase):
    async def test_tool_success_wraps_terminal_payload(self) -> None:
        payload = {
            **_run_payload("run-saw-t", "completed", stdout="ok"),
            "wait_expired": False,
            "timeout_seconds": 5.0,
            "reached_terminal": True,
        }
        with patch.object(
            mcp_server.client,
            "submit_and_wait",
            new=AsyncMock(return_value=payload),
        ):
            result = await mcp_server.submit_and_wait(
                "mission: yaml",
                timeout_seconds=5.0,
                poll_interval_seconds=0.1,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["run_id"], "run-saw-t")
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["wait_expired"])

    async def test_tool_maps_submission_failure(self) -> None:
        rejection = {
            "ok": False,
            "stdout": "",
            "stderr": "",
            "error": "invalid mission",
            "error_detail": None,
        }
        with patch.object(
            mcp_server.client,
            "submit_and_wait",
            new=AsyncMock(return_value=rejection),
        ) as submit_and_wait:
            result = await mcp_server.submit_and_wait("bad: yaml")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid mission")
        submit_and_wait.assert_awaited_once()

    async def test_tool_maps_wait_expired_as_ok_payload(self) -> None:
        payload = {
            **_run_payload("run-saw-t", "running"),
            "wait_expired": True,
            "timeout_seconds": 1.0,
            "reached_terminal": False,
        }
        with patch.object(
            mcp_server.client,
            "submit_and_wait",
            new=AsyncMock(return_value=payload),
        ):
            result = await mcp_server.submit_and_wait(
                "mission: yaml",
                timeout_seconds=1.0,
                poll_interval_seconds=0.1,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["run_id"], "run-saw-t")
        self.assertTrue(result["wait_expired"])


class TestMcpRunRepositoryCommandDurablePrefix(unittest.IsolatedAsyncioTestCase):
    """MCP wrapper forwards Case-00 durable prefix argv; secrets stay client-side."""

    async def test_mcp_run_repository_command_forwards_candidate_b2_prefix(
        self,
    ) -> None:
        canonical = (
            "Benchmarks/Case-00-Triborough/derived/attorney-feedback-eval/"
            "candidate-answers/"
        )
        argv = [
            "python3",
            "scripts/run_case00_b2_q1.py",
            "--case-root",
            "data/case-00-triborough",
            "--question-id",
            "Q1",
            "--required-commit",
            "a" * 40,
            "--candidate-output-root",
            "out/case00-b2-q1",
            "--candidate-b2-prefix",
            canonical,
            "--authorization-confirmed",
            "--generation-only",
        ]
        api_payload = {
            "ok": True,
            "run_id": "repo-cmd-1",
            "checkout_commit": "a" * 40,
            "argv": list(argv),
            "stdout": json.dumps(
                {
                    "ok": True,
                    "durable_artifacts": {
                        "prefix": canonical,
                        "object_keys": [canonical + "q1/Q1_candidate_answer.json"],
                    },
                }
            ),
            "stderr": "",
            "exit_code": 0,
            "elapsed_seconds": 1.2,
            "artifact_paths": ["/tmp/ephemeral/case00_b2_q1.json"],
            "persistence": {
                "mode": "none",
                "attempted": False,
                "ok": True,
                "commit_sha": None,
                "pushed": False,
            },
            "error": None,
            "error_code": None,
        }
        with patch.object(
            mcp_server.client,
            "run_repository_command",
            new=AsyncMock(return_value=api_payload),
        ) as mocked:
            result = await mcp_server.run_repository_command(
                repository="nhpcorp35/legal-ai",
                ref="a" * 40,
                argv=argv,
                working_directory=".",
                timeout_seconds=30.0,
                allowed_env_names=["B2_KEY_ID", "B2_APPLICATION_KEY", "OPENAI_API_KEY"],
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["persistence"]["mode"], "none")
        self.assertFalse(result["persistence"]["attempted"])
        self.assertIn("--candidate-b2-prefix", result["argv"])
        self.assertIn(canonical, result["argv"])
        self.assertIn("durable_artifacts", result["stdout"])
        # Local artifact_paths remain ephemeral; durable proof is wrapper JSON.
        self.assertTrue(result["artifact_paths"])
        mocked.assert_awaited_once()
        kwargs = mocked.await_args.kwargs
        self.assertEqual(kwargs["argv"], argv)
        self.assertEqual(
            kwargs["allowed_env_names"],
            ["B2_KEY_ID", "B2_APPLICATION_KEY", "OPENAI_API_KEY"],
        )
        # Wrapper must not invent or echo credential values.
        rendered = json.dumps(result)
        self.assertNotIn("B2_APPLICATION_KEY=", rendered)
        self.assertNotIn("sk-", rendered)


if __name__ == "__main__":
    unittest.main()
