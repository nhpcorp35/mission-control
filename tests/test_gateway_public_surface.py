"""Contract tests for the hardened HAL LegalAI Gateway public surface."""

from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from cryptography.fernet import Fernet

from hal_legalai_gateway.config import load_settings
from hal_legalai_gateway.mcp_server import (
    bindings_from_registry,
    register_forwarding_tools,
)
from hal_legalai_gateway.registry import load_registry
from hal_legalai_gateway.server import (
    REQUIRED_PUBLIC_TOOL_NAMES,
    required_tool_parity,
)


REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent
    / "hal_legalai_gateway"
    / "registry.json"
)


class _CollectingMcp:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def decorator(fn: Any) -> Any:
            name = kwargs.get("name") or (args[0] if args else None)
            if isinstance(name, str):
                self.tools[name] = fn
            return fn

        return decorator


class GatewayPublicSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        encryption_key = Fernet.generate_key().decode()
        self.settings = load_settings(
            environ={
                "GITHUB_OAUTH_CLIENT_ID": "test-gateway-client-id",
                "GITHUB_OAUTH_CLIENT_SECRET": "test-gateway-client-secret",
                "GATEWAY_PUBLIC_URL": "https://gateway.example",
                "JWT_SIGNING_KEY": "test-jwt-signing-key-for-gateway",
                "REDIS_HOST": "127.0.0.1",
                "STORAGE_ENCRYPTION_KEY": encryption_key,
                "GATEWAY_BRIDGE_AUTHORIZATION": "Bearer test-bridge-token",
                "ALLOWED_GITHUB_LOGIN": "nhpcorp35",
                "GATEWAY_BRIDGE_URL": "https://bridge.example",
                "GATEWAY_STORAGE_URL": "https://storage.example",
                "GATEWAY_MISSION_CONTROL_URL": "https://mission.example",
                "GATEWAY_ARTIFACTS_URL": "https://artifacts.example",
            },
            registry=load_registry(REGISTRY_PATH),
        )
        self.mcp = _CollectingMcp()
        register_forwarding_tools(
            self.mcp,  # type: ignore[arg-type]
            self.settings,
            bindings_from_registry(self.settings.registry),
        )

    def test_gateway_diagnostics_are_registered_and_safe(self) -> None:
        self.assertIn("gateway.health", self.mcp.tools)
        self.assertIn("gateway.auth_status", self.mcp.tools)

        health = mock.AsyncMock(return_value={"ok": True, "status": "ok"})
        with mock.patch(
            "hal_legalai_gateway.mcp_server.aggregate_health",
            health,
        ):
            result = asyncio.run(self.mcp.tools["gateway.health"]())

        self.assertEqual(result, {"ok": True, "status": "ok"})
        registered_tools = health.await_args.kwargs["registered_tools"]
        self.assertIn("gateway.health", registered_tools)
        self.assertIn("gateway.auth_status", registered_tools)

        with mock.patch(
            "hal_legalai_gateway.mcp_server.get_access_token",
            return_value=None,
        ):
            auth_status = asyncio.run(self.mcp.tools["gateway.auth_status"]())

        self.assertEqual(auth_status["authenticated"], False)
        self.assertEqual(auth_status["authorized"], False)
        self.assertNotIn("token", auth_status)

    def test_readiness_requires_submit_and_cancel(self) -> None:
        self.assertTrue(
            {
                "case.submit",
                "case.status",
                "case.cancel",
                "case.list_artifacts",
            }.issubset(REQUIRED_PUBLIC_TOOL_NAMES)
        )
        parity = required_tool_parity(
            sorted(REQUIRED_PUBLIC_TOOL_NAMES - {"case.status"})
        )
        self.assertFalse(parity["ok"])
        self.assertEqual(parity["missing_tools"], ["case.status"])

    def test_contract_lookup_forwards_unprefixed_version_exactly(self) -> None:
        forward = mock.AsyncMock(return_value={"ok": True})
        with mock.patch(
            "hal_legalai_gateway.mcp_server._require_gateway_principal",
            return_value="nhpcorp35",
        ), mock.patch(
            "hal_legalai_gateway.mcp_server.forward_mcp_tool",
            forward,
        ):
            result = asyncio.run(
                self.mcp.tools["storage.get_acceptance_contract"](
                    "Case-00-Triborough",
                    "Q3",
                    "case00-triborough-q3-insurance-policy-coverage",
                    "1.0.0",
                )
            )

        self.assertEqual(result, {"ok": True})
        arguments = forward.await_args.kwargs["arguments"]
        self.assertEqual(arguments["version"], "1.0.0")

    def test_contract_lookup_rejects_prefixed_version_without_forwarding(self) -> None:
        forward = mock.AsyncMock(return_value={"ok": True})
        with mock.patch(
            "hal_legalai_gateway.mcp_server._require_gateway_principal",
            return_value="nhpcorp35",
        ), mock.patch(
            "hal_legalai_gateway.mcp_server.forward_mcp_tool",
            forward,
        ):
            result = asyncio.run(
                self.mcp.tools["storage.get_acceptance_contract"](
                    "Case-00-Triborough",
                    "Q3",
                    "case00-triborough-q3-insurance-policy-coverage",
                    "v1.0.0",
                )
            )

        self.assertEqual(result["error"], "invalid_version")
        forward.assert_not_awaited()

    def test_case_cancel_forwards_the_mission_identity(self) -> None:
        forward = mock.AsyncMock(return_value={"ok": True, "status": "cancelled"})
        with mock.patch(
            "hal_legalai_gateway.mcp_server._require_gateway_principal",
            return_value="nhpcorp35",
        ), mock.patch(
            "hal_legalai_gateway.mcp_server.forward_mcp_tool",
            forward,
        ):
            result = asyncio.run(
                self.mcp.tools["case.cancel"]("case00-cancel-test-001")
            )

        self.assertEqual(result["status"], "cancelled")
        arguments = forward.await_args.kwargs["arguments"]
        self.assertEqual(arguments, {"mission_id": "case00-cancel-test-001"})


if __name__ == "__main__":
    unittest.main()
