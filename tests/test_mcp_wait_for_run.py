"""Focused tests for the MCP wait_for_run tool and client polling."""

from __future__ import annotations

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
    def test_default_timeout_is_chatgpt_safe(self) -> None:
        self.assertEqual(MCP_WAIT_DEFAULT_TIMEOUT_SECONDS, 20.0)
        self.assertLessEqual(
            MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
            MCP_WAIT_MAX_TIMEOUT_SECONDS,
        )
        self.assertEqual(MCP_WAIT_DEFAULT_POLL_INTERVAL_SECONDS, 2.0)

    def test_normalize_timeout_preserves_safe_values(self) -> None:
        self.assertEqual(normalize_mcp_wait_timeout(5.0), 5.0)
        self.assertEqual(
            normalize_mcp_wait_timeout(MCP_WAIT_DEFAULT_TIMEOUT_SECONDS),
            MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            normalize_mcp_wait_timeout(MCP_WAIT_MAX_TIMEOUT_SECONDS),
            MCP_WAIT_MAX_TIMEOUT_SECONDS,
        )

    def test_normalize_timeout_caps_unsafe_large_values(self) -> None:
        self.assertEqual(normalize_mcp_wait_timeout(900.0), MCP_WAIT_MAX_TIMEOUT_SECONDS)
        self.assertEqual(normalize_mcp_wait_timeout(60.0), MCP_WAIT_MAX_TIMEOUT_SECONDS)

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


class TestWaitForRunClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = MissionControlClient(_settings())

    async def test_already_terminal_returns_immediately(self) -> None:
        payload = _run_payload("run-1", "completed", stdout="done", commit_sha="abc")
        with patch.object(
            self.client,
            "get_run",
            new=AsyncMock(return_value=payload),
        ) as get_run:
            with patch("mcp_connector.client.asyncio.sleep", new=AsyncMock()) as sleep:
                result = await self.client.wait_for_run(
                    "run-1",
                    timeout_seconds=5.0,
                    poll_interval_seconds=0.1,
                )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["run_id"], "run-1")
        self.assertEqual(result["stdout"], "done")
        self.assertEqual(result["commit_sha"], "abc")
        self.assertFalse(result["wait_expired"])
        self.assertTrue(result["reached_terminal"])
        self.assertEqual(result["timeout_seconds"], 5.0)
        get_run.assert_awaited_once_with("run-1")
        sleep.assert_not_awaited()

    async def test_transitions_from_nonterminal_to_terminal(self) -> None:
        running = _run_payload("run-2", "running")
        completed = _run_payload("run-2", "completed", stdout="finished")
        with patch.object(
            self.client,
            "get_run",
            new=AsyncMock(side_effect=[running, completed]),
        ) as get_run:
            with patch(
                "mcp_connector.client.asyncio.sleep",
                new=AsyncMock(),
            ) as sleep:
                result = await self.client.wait_for_run(
                    "run-2",
                    timeout_seconds=5.0,
                    poll_interval_seconds=0.05,
                )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stdout"], "finished")
        self.assertFalse(result["wait_expired"])
        self.assertTrue(result["reached_terminal"])
        self.assertEqual(get_run.await_count, 2)
        sleep.assert_awaited()

    async def test_wait_window_expiry_returns_structured_payload(self) -> None:
        running = _run_payload("run-3", "running", stdout="still going")
        with patch.object(
            self.client,
            "get_run",
            new=AsyncMock(return_value=running),
        ):
            with patch(
                "mcp_connector.client.asyncio.sleep",
                new=AsyncMock(),
            ):
                result = await self.client.wait_for_run(
                    "run-3",
                    timeout_seconds=0.15,
                    poll_interval_seconds=0.05,
                )

        self.assertEqual(result["run_id"], "run-3")
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["stdout"], "still going")
        self.assertTrue(result["wait_expired"])
        self.assertFalse(result["reached_terminal"])
        self.assertEqual(result["timeout_seconds"], 0.15)

    async def test_default_timeout_used_when_omitted(self) -> None:
        running = _run_payload("run-default", "queued")
        with patch.object(
            self.client,
            "get_run",
            new=AsyncMock(return_value=running),
        ):
            with patch(
                "mcp_connector.client.asyncio.sleep",
                new=AsyncMock(),
            ):
                with patch(
                    "mcp_connector.client.time.monotonic",
                    side_effect=[0.0, 0.0, MCP_WAIT_DEFAULT_TIMEOUT_SECONDS + 0.1],
                ):
                    result = await self.client.wait_for_run("run-default")

        self.assertTrue(result["wait_expired"])
        self.assertEqual(
            result["timeout_seconds"],
            MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
        )

    async def test_unsafe_timeout_is_capped(self) -> None:
        running = _run_payload("run-cap", "running")
        with patch.object(
            self.client,
            "get_run",
            new=AsyncMock(return_value=running),
        ):
            with patch(
                "mcp_connector.client.asyncio.sleep",
                new=AsyncMock(),
            ):
                with patch(
                    "mcp_connector.client.time.monotonic",
                    side_effect=[
                        0.0,
                        0.0,
                        MCP_WAIT_MAX_TIMEOUT_SECONDS + 0.1,
                    ],
                ):
                    result = await self.client.wait_for_run(
                        "run-cap",
                        timeout_seconds=900.0,
                        poll_interval_seconds=0.1,
                    )

        self.assertTrue(result["wait_expired"])
        self.assertEqual(result["timeout_seconds"], MCP_WAIT_MAX_TIMEOUT_SECONDS)

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

    async def test_transient_polling_failure_then_success(self) -> None:
        completed = _run_payload("run-4", "completed", stdout="recovered")
        transient = MissionControlError(
            "Mission Control did not respond before the timeout"
        )
        with patch.object(
            self.client,
            "get_run",
            new=AsyncMock(side_effect=[transient, completed]),
        ) as get_run:
            with patch(
                "mcp_connector.client.asyncio.sleep",
                new=AsyncMock(),
            ) as sleep:
                result = await self.client.wait_for_run(
                    "run-4",
                    timeout_seconds=5.0,
                    poll_interval_seconds=0.05,
                )

        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["wait_expired"])
        self.assertEqual(get_run.await_count, 2)
        sleep.assert_awaited()

    async def test_final_terminal_payload_includes_wait_metadata(self) -> None:
        payload = _run_payload(
            "run-5",
            "failed",
            stdout="partial",
        )
        payload["error"] = "boom"
        payload["return_code"] = 1
        with patch.object(
            self.client,
            "get_run",
            new=AsyncMock(return_value=payload),
        ):
            waited = await self.client.wait_for_run(
                "run-5",
                timeout_seconds=1.0,
                poll_interval_seconds=0.1,
            )
            fetched = await self.client.get_run("run-5")

        for key, value in fetched.items():
            self.assertEqual(waited[key], value)
        self.assertFalse(waited["wait_expired"])
        self.assertTrue(waited["reached_terminal"])
        self.assertEqual(waited["timeout_seconds"], 1.0)


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
        }
        with patch.object(
            mcp_server.client,
            "wait_for_run",
            new=AsyncMock(return_value=payload),
        ):
            result = await mcp_server.wait_for_run(
                "run-t",
                timeout_seconds=1.0,
                poll_interval_seconds=0.1,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["run_id"], "run-t")
        self.assertEqual(result["status"], "running")
        self.assertTrue(result["wait_expired"])
        self.assertEqual(result["timeout_seconds"], 1.0)

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

    def test_tool_discovery_lists_exactly_three_run_tools(self) -> None:
        tools = mcp_server.mcp._tool_manager.list_tools()
        names = [tool.name for tool in tools]
        self.assertEqual(names, ["submit_run", "get_run", "wait_for_run"])

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
        self.assertEqual(wait_tool.parameters["required"], ["run_id"])
        description = wait_tool.description or ""
        self.assertIn("repeatedly", description.lower())
        self.assertIn("wait_expired", description)


if __name__ == "__main__":
    unittest.main()
