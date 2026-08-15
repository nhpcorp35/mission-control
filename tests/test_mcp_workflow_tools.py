"""Focused tests for MCP workflow submit/status tools (Slice B)."""

from __future__ import annotations

import os
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

# Settings are read at import time by mcp_connector.server.
os.environ.setdefault("MISSION_CONTROL_URL", "http://mission-control.test")
os.environ.setdefault("MISSION_CONTROL_API_KEY", "mc_test_key")

from mcp_connector.client import (
    MCP_WORKFLOW_MAX_IDEMPOTENCY_KEY_CHARS,
    MissionControlClient,
    normalize_mcp_workflow_id,
    normalize_mcp_workflow_idempotency_key,
    normalize_mcp_workflow_yaml,
)
from mcp_connector.config import Settings
from mcp_connector.errors import MissionControlError
from mcp_connector import server as mcp_server

TEST_SECRET_VALUE = "TEST_SECRET_VALUE_WF_MCP"
YAML_WITH_SECRET = (
    "version: '1.0'\n"
    "policy:\n"
    "  repository_name: Mission-Control\n"
    f"  token: {TEST_SECRET_VALUE}\n"
    "steps: []\n"
)
INVALID_IDEMPOTENCY_KEY = "not a valid key / secret-token"


def _settings() -> Settings:
    return Settings(
        mission_control_url="http://mission-control.test",
        mission_control_api_key="mc_test_key",
        request_timeout_seconds=5.0,
    )


def _assert_no_raw_yaml_or_secrets(payload: Any) -> None:
    blob = f"{payload!s} {payload!r}"
    assert TEST_SECRET_VALUE not in blob
    assert YAML_WITH_SECRET not in blob
    assert INVALID_IDEMPOTENCY_KEY not in blob


class _FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        status_code: int = 200,
        is_error: bool = False,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.is_error = is_error
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload


class TestNormalizeWorkflowMcpInputs(unittest.TestCase):
    def test_yaml_rejects_empty_without_echoing_input(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            normalize_mcp_workflow_yaml("  \n")
        self.assertEqual(str(ctx.exception), "workflow_yaml must not be empty")

    def test_id_rejects_empty(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            normalize_mcp_workflow_id("   ")
        self.assertEqual(str(ctx.exception), "workflow_id must not be empty")
        self.assertEqual(normalize_mcp_workflow_id("  wf-1  "), "wf-1")

    def test_idempotency_key_constraints(self) -> None:
        self.assertIsNone(normalize_mcp_workflow_idempotency_key(None))
        self.assertIsNone(normalize_mcp_workflow_idempotency_key("  "))
        self.assertEqual(
            normalize_mcp_workflow_idempotency_key(" wf-replay-01 "),
            "wf-replay-01",
        )
        self.assertEqual(
            normalize_mcp_workflow_idempotency_key("A.z_0~:1-2"),
            "A.z_0~:1-2",
        )
        with self.assertRaises(ValueError) as ctx:
            normalize_mcp_workflow_idempotency_key(INVALID_IDEMPOTENCY_KEY)
        self.assertEqual(str(ctx.exception), "idempotency_key is invalid")
        self.assertNotIn(INVALID_IDEMPOTENCY_KEY, str(ctx.exception))
        with self.assertRaises(ValueError):
            normalize_mcp_workflow_idempotency_key(
                "a" * (MCP_WORKFLOW_MAX_IDEMPOTENCY_KEY_CHARS + 1)
            )


class TestWorkflowClientForwarding(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = MissionControlClient(_settings())

    async def test_submit_forwards_post_workflows_json(self) -> None:
        accepted = {
            "workflow_id": "wf-1",
            "state": "pending",
            "idempotent_replay": False,
        }
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(return_value=accepted),
        ) as request:
            result = await self.client.submit_workflow(YAML_WITH_SECRET)
        request.assert_awaited_once()
        self.assertEqual(request.await_args.args[0], "POST")
        self.assertEqual(request.await_args.args[1], "/workflows")
        self.assertEqual(
            request.await_args.kwargs["json"],
            {"workflow_yaml": YAML_WITH_SECRET},
        )
        self.assertIsNone(request.await_args.kwargs.get("headers"))
        self.assertEqual(result, accepted)

    async def test_submit_sends_idempotency_header(self) -> None:
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(
                return_value={
                    "workflow_id": "wf-1",
                    "state": "pending",
                    "idempotent_replay": True,
                }
            ),
        ) as request:
            await self.client.submit_workflow(
                YAML_WITH_SECRET,
                idempotency_key="  wf-replay-01  ",
            )
        self.assertEqual(
            request.await_args.kwargs["headers"],
            {"Idempotency-Key": "wf-replay-01"},
        )

    async def test_submit_omits_blank_idempotency_header(self) -> None:
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(return_value={"workflow_id": "wf-1"}),
        ) as request:
            await self.client.submit_workflow(
                YAML_WITH_SECRET,
                idempotency_key="   ",
            )
        self.assertIsNone(request.await_args.kwargs.get("headers"))

    async def test_get_forwards_get_workflows_id(self) -> None:
        status = {
            "workflow_id": "wf-1",
            "state": "pending",
            "steps": [],
        }
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(return_value=status),
        ) as request:
            result = await self.client.get_workflow("  wf-1  ")
        request.assert_awaited_once_with("GET", "/workflows/wf-1")
        self.assertEqual(result, status)

    async def test_empty_inputs_do_not_call_request(self) -> None:
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(),
        ) as request:
            with self.assertRaises(ValueError):
                await self.client.submit_workflow("\n  ")
            with self.assertRaises(ValueError):
                await self.client.get_workflow("  ")
            with self.assertRaises(ValueError) as ctx:
                await self.client.submit_workflow(
                    YAML_WITH_SECRET,
                    idempotency_key=INVALID_IDEMPOTENCY_KEY,
                )
        request.assert_not_awaited()
        self.assertEqual(str(ctx.exception), "idempotency_key is invalid")
        _assert_no_raw_yaml_or_secrets(ctx.exception)

    async def test_feature_off_error_propagates(self) -> None:
        with patch.object(
            self.client,
            "_request",
            new=AsyncMock(
                side_effect=MissionControlError(
                    "Mission Control request failed",
                    status_code=403,
                    details={"detail": "Workflow orchestration is disabled"},
                )
            ),
        ):
            with self.assertRaises(MissionControlError) as ctx:
                await self.client.submit_workflow(YAML_WITH_SECRET)
        self.assertEqual(ctx.exception.status_code, 403)
        _assert_no_raw_yaml_or_secrets(ctx.exception)
        _assert_no_raw_yaml_or_secrets(ctx.exception.as_dict())

    async def test_http_preserves_auth_timeout_and_idempotency(self) -> None:
        captured: dict[str, Any] = {}

        class _FakeAsyncClient:
            def __init__(self, **kwargs: Any) -> None:
                captured["base_url"] = kwargs.get("base_url")
                captured["headers"] = dict(kwargs.get("headers") or {})
                captured["timeout"] = kwargs.get("timeout")

            async def __aenter__(self) -> "_FakeAsyncClient":
                return self

            async def __aexit__(self, *args: Any) -> bool:
                return False

            async def request(self, method: str, path: str, **kwargs: Any) -> _FakeResponse:
                captured["method"] = method
                captured["path"] = path
                captured["json"] = kwargs.get("json")
                return _FakeResponse(
                    {
                        "workflow_id": "wf-http",
                        "state": "pending",
                        "idempotent_replay": False,
                    },
                    status_code=202,
                )

        with patch(
            "mcp_connector.client.httpx.AsyncClient",
            _FakeAsyncClient,
        ):
            result = await self.client.submit_workflow(
                YAML_WITH_SECRET,
                idempotency_key="wf-key-1",
            )
        self.assertEqual(result["workflow_id"], "wf-http")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/workflows")
        self.assertEqual(captured["timeout"], 5.0)
        self.assertEqual(
            captured["base_url"],
            "http://mission-control.test",
        )
        self.assertEqual(
            captured["headers"]["Authorization"],
            "Bearer mc_test_key",
        )
        self.assertEqual(captured["headers"]["Idempotency-Key"], "wf-key-1")
        self.assertEqual(
            captured["json"],
            {"workflow_yaml": YAML_WITH_SECRET},
        )


class TestWorkflowMcpTools(unittest.IsolatedAsyncioTestCase):
    async def test_submit_tool_success_shape(self) -> None:
        payload = {
            "workflow_id": "wf-1",
            "state": "pending",
            "idempotent_replay": False,
        }
        with patch.object(
            mcp_server.client,
            "submit_workflow",
            new=AsyncMock(return_value=payload),
        ) as submit:
            result = await mcp_server.submit_workflow(
                YAML_WITH_SECRET,
                idempotency_key="wf-key-1",
            )
        submit.assert_awaited_once_with(
            YAML_WITH_SECRET,
            idempotency_key="wf-key-1",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["workflow_id"], "wf-1")
        self.assertEqual(result["state"], "pending")
        self.assertNotIn("error", result)

    async def test_get_tool_success_shape(self) -> None:
        payload = {"workflow_id": "wf-1", "state": "running", "steps": []}
        with patch.object(
            mcp_server.client,
            "get_workflow",
            new=AsyncMock(return_value=payload),
        ):
            result = await mcp_server.get_workflow("wf-1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["workflow_id"], "wf-1")
        self.assertEqual(result["state"], "running")

    async def test_validation_errors_match_tool_error_shape(self) -> None:
        empty_yaml = await mcp_server.submit_workflow("  ")
        self.assertEqual(
            set(empty_yaml.keys()),
            {"ok", "error"},
        )
        self.assertFalse(empty_yaml["ok"])
        self.assertEqual(
            set(empty_yaml["error"].keys()),
            {"message", "status_code", "details"},
        )
        self.assertIsNone(empty_yaml["error"]["status_code"])
        self.assertIn("workflow_yaml", empty_yaml["error"]["message"])
        _assert_no_raw_yaml_or_secrets(empty_yaml)

        empty_id = await mcp_server.get_workflow("\n")
        self.assertFalse(empty_id["ok"])
        self.assertIn("workflow_id", empty_id["error"]["message"])

        bad_key = await mcp_server.submit_workflow(
            YAML_WITH_SECRET,
            idempotency_key=INVALID_IDEMPOTENCY_KEY,
        )
        self.assertFalse(bad_key["ok"])
        self.assertIn("idempotency_key", bad_key["error"]["message"])
        _assert_no_raw_yaml_or_secrets(bad_key)

    async def test_feature_off_error_forwarding(self) -> None:
        feature_off = MissionControlError(
            "Mission Control request failed",
            status_code=403,
            details={"detail": "Workflow orchestration is disabled"},
        )
        with patch.object(
            mcp_server.client,
            "submit_workflow",
            new=AsyncMock(side_effect=feature_off),
        ):
            submit = await mcp_server.submit_workflow(YAML_WITH_SECRET)
        self.assertFalse(submit["ok"])
        self.assertEqual(submit["error"]["status_code"], 403)
        self.assertEqual(
            submit["error"]["details"]["detail"],
            "Workflow orchestration is disabled",
        )
        _assert_no_raw_yaml_or_secrets(submit)

        with patch.object(
            mcp_server.client,
            "get_workflow",
            new=AsyncMock(side_effect=feature_off),
        ):
            status = await mcp_server.get_workflow("wf-1")
        self.assertFalse(status["ok"])
        self.assertEqual(status["error"]["status_code"], 403)
        _assert_no_raw_yaml_or_secrets(status)


class TestWorkflowToolDiscovery(unittest.TestCase):
    def test_expected_tools_include_workflow_surface(self) -> None:
        tools = mcp_server.mcp._tool_manager.list_tools()
        names = [tool.name for tool in tools]
        self.assertEqual(names, list(mcp_server.EXPECTED_TOOL_NAMES))
        self.assertIn("submit_workflow", names)
        self.assertIn("get_workflow", names)

        submit = next(t for t in tools if t.name == "submit_workflow")
        submit_props = submit.parameters["properties"]
        self.assertIn("workflow_yaml", submit_props)
        self.assertIn("idempotency_key", submit_props)
        self.assertEqual(submit.parameters["required"], ["workflow_yaml"])
        submit_desc = submit.description or ""
        self.assertIn("MISSION_CONTROL_WORKFLOW_ORCHESTRATION", submit_desc)
        self.assertIn("fail-closed", submit_desc)
        self.assertIn("sanitized", submit_desc)
        self.assertIn("POST /workflows", submit_desc)

        get_tool = next(t for t in tools if t.name == "get_workflow")
        self.assertEqual(get_tool.parameters["required"], ["workflow_id"])
        get_desc = get_tool.description or ""
        self.assertIn("MISSION_CONTROL_WORKFLOW_ORCHESTRATION", get_desc)
        self.assertIn("fail-closed", get_desc)
        self.assertIn("sanitized", get_desc)
        self.assertIn("GET /workflows/{workflow_id}", get_desc)

        instructions = str(getattr(mcp_server.mcp, "instructions", "") or "")
        if not instructions:
            mcp_inner = getattr(mcp_server.mcp, "_mcp_server", None)
            instructions = str(getattr(mcp_inner, "instructions", "") or "")
        self.assertIn("submit_workflow", instructions)
        self.assertIn("get_workflow", instructions)
        self.assertIn("MISSION_CONTROL_WORKFLOW_ORCHESTRATION", instructions)
        self.assertIn("fail-closed", instructions)
        self.assertIn("sanitized", instructions)


if __name__ == "__main__":
    unittest.main()
