"""Focused tests for HAL LegalAI Gateway Phase 2 (MCP routing)."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from cryptography.fernet import Fernet

import httpx
from fastapi.testclient import TestClient

from hal_legalai_gateway import config as gateway_config
from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.http import set_http_request
from starlette.requests import Request

from hal_legalai_gateway.auth import (
    SERVICE_CLIENT_ID,
    FixedTokenAuthProvider,
    ServiceTokenVerifier,
    normalize_bearer_token,
    redact_secrets,
    service_authorization_header,
)
from hal_legalai_gateway.config import (
    DEFAULT_HEALTH_TIMEOUT_SECONDS,
    load_settings,
    validate_http_base_url,
)
from hal_legalai_gateway.forwarding import (
    STAGE_AUTH,
    STAGE_CONNECT,
    STAGE_TIMEOUT,
    STAGE_TOOL,
    STAGE_UNCONFIGURED,
    ToolBinding,
    forward_mcp_tool,
    mcp_endpoint_url,
    resolve_authorization_for_service,
)
from hal_legalai_gateway.health import (
    STAGE_CONNECT as HEALTH_STAGE_CONNECT,
    STAGE_HTTP,
    STATUS_HEALTHY,
    STATUS_UNHEALTHY,
    aggregate_health,
    probe_downstream,
)
from hal_legalai_gateway.mcp_server import (
    CANONICAL_GATEWAY_DISPLAY_NAME,
    CANONICAL_GATEWAY_IDENTITY_VERSION,
    CANONICAL_GATEWAY_INSTRUCTIONS,
    CANONICAL_GATEWAY_SERVICE_ID,
    CANONICAL_INBOUND_MCP_PATH,
    DEFAULT_TOOL_BINDINGS,
    bindings_from_registry,
    build_inbound_auth_provider,
    canonical_gateway_identity,
    create_mcp_server,
    list_registered_tool_names,
    register_forwarding_tools,
)
from hal_legalai_gateway.registry import (
    REQUIRED_GATEWAY_TOOLS,
    REQUIRED_NAMESPACES,
    REQUIRED_SERVICES,
    load_registry,
    parse_registry,
)
from hal_legalai_gateway.request_context import (
    CORRELATION_ID_HEADER,
    REQUEST_ID_HEADER,
    bind_request_ids,
    get_correlation_id,
    get_request_id,
    reset_request_ids,
)
from hal_legalai_gateway.server import create_app, reset_settings_for_tests

REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent
    / "hal_legalai_gateway"
    / "registry.json"
)

TEST_STORAGE_ENCRYPTION_KEY = Fernet.generate_key().decode()
TEST_GATEWAY_OAUTH_TOKEN = "test-gateway-oauth-token"
TEST_BRIDGE_SERVICE_TOKEN = "test-bridge-service-token"
TEST_SECRET_VALUE_WF_GW = "TEST_SECRET_VALUE_WF_GW"
WORKFLOW_YAML_FIXTURE = (
    "version: '1.0'\n"
    "policy:\n"
    "  repository_name: Mission-Control\n"
    f"  token: {TEST_SECRET_VALUE_WF_GW}\n"
    "steps: []\n"
)
WORKFLOW_IDEMPOTENCY_KEY = "wf-replay-01"
CANONICAL_WORKFLOW_ID = "00000000-0000-4000-8000-000000000001"
EXPECTED_NAMESPACES = REQUIRED_NAMESPACES | {"workflow"}
FORBIDDEN_WORKFLOW_TOOLS = (
    "workflow.wait",
    "workflow.history",
)

REQUIRED_SECRETS = {
    "GITHUB_OAUTH_CLIENT_ID": "test-gateway-client-id",
    "GITHUB_OAUTH_CLIENT_SECRET": "test-gateway-client-secret",
    "GATEWAY_PUBLIC_URL": "https://gateway.example",
    "JWT_SIGNING_KEY": "test-jwt-signing-key-for-gateway",
    "REDIS_HOST": "127.0.0.1",
    "STORAGE_ENCRYPTION_KEY": TEST_STORAGE_ENCRYPTION_KEY,
    "GATEWAY_BRIDGE_AUTHORIZATION": f"Bearer {TEST_BRIDGE_SERVICE_TOKEN}",
    "ALLOWED_GITHUB_LOGIN": "nhpcorp35",
}

def _test_inbound_auth() -> FixedTokenAuthProvider:
    return FixedTokenAuthProvider(
        TEST_GATEWAY_OAUTH_TOKEN,
        claims={"login": "nhpcorp35"},
    )


class RegistryTests(unittest.TestCase):
    def test_bundled_registry_loads_and_has_required_namespaces(self) -> None:
        registry = load_registry(REGISTRY_PATH)
        self.assertEqual(registry.version, 2)
        self.assertEqual(EXPECTED_NAMESPACES, set(registry.namespaces))
        self.assertTrue(REQUIRED_NAMESPACES.issubset(set(registry.namespaces)))
        self.assertEqual(REQUIRED_SERVICES, set(registry.services))
        self.assertEqual(registry.namespaces["case"].downstream_service, "bridge")
        self.assertEqual(
            registry.namespaces["storage"].downstream_service, "storage"
        )
        self.assertEqual(
            registry.namespaces["mission"].downstream_service, "mission_control"
        )
        self.assertEqual(
            registry.namespaces["workflow"].downstream_service,
            "mission_control",
        )
        self.assertIn("case.submit_case00_q1", registry.namespaces["case"].tools)
        self.assertIn(
            "storage.list_inventory", registry.namespaces["storage"].tools
        )
        self.assertIn("mission.submit", registry.namespaces["mission"].tools)
        self.assertEqual(
            registry.namespaces["workflow"].tools,
            ("workflow.submit", "workflow.status", "workflow.cancel"),
        )
        present = {binding.gateway_tool for binding in registry.tool_bindings}
        self.assertTrue(REQUIRED_GATEWAY_TOOLS.issubset(present))
        self.assertIn("workflow.submit", present)
        self.assertIn("workflow.status", present)
        self.assertIn("workflow.cancel", present)
        for forbidden in FORBIDDEN_WORKFLOW_TOOLS:
            self.assertNotIn(forbidden, present)
            self.assertNotIn(forbidden, registry.namespaces["workflow"].tools)

    def test_tool_routes_and_bindings_point_artifacts_independently(self) -> None:
        registry = load_registry(REGISTRY_PATH)
        self.assertEqual(
            registry.downstream_for_tool("case.get_artifact"), "artifacts"
        )
        self.assertEqual(
            registry.downstream_for_tool("case.get_artifacts"), "artifacts"
        )
        self.assertEqual(
            registry.downstream_tool_for_gateway_tool("case.get_artifact"),
            "get_case_artifact",
        )
        self.assertEqual(
            registry.downstream_tool_for_gateway_tool("storage.verify_archive"),
            "list_case00_storage",
        )
        self.assertEqual(
            registry.downstream_tool_for_gateway_tool("storage.archive_feedback"),
            "archive_case00_attorney_feedback",
        )
        self.assertEqual(
            registry.downstream_tool_for_gateway_tool(
                "storage.archive_acceptance_contract"
            ),
            "archive_acceptance_contract",
        )
        self.assertEqual(
            registry.downstream_tool_for_gateway_tool(
                "storage.verify_acceptance_contract"
            ),
            "verify_acceptance_contract",
        )
        self.assertEqual(
            registry.downstream_tool_for_gateway_tool(
                "storage.list_acceptance_contracts"
            ),
            "list_acceptance_contracts",
        )
        self.assertEqual(
            registry.downstream_tool_for_gateway_tool(
                "storage.get_acceptance_contract_template"
            ),
            "get_acceptance_contract_template",
        )
        self.assertEqual(
            registry.downstream_tool_for_gateway_tool(
                "storage.get_acceptance_contract"
            ),
            "get_acceptance_contract",
        )
        self.assertEqual(
            registry.downstream_tool_for_gateway_tool("mission.submit"),
            "submit_run",
        )
        self.assertEqual(
            registry.downstream_tool_for_gateway_tool("mission.status"),
            "get_run",
        )
        self.assertEqual(
            registry.downstream_for_tool("case.submit_case00_q1"), "bridge"
        )
        self.assertEqual(
            registry.downstream_tool_for_gateway_tool("workflow.submit"),
            "submit_workflow",
        )
        self.assertEqual(
            registry.downstream_tool_for_gateway_tool("workflow.status"),
            "get_workflow",
        )
        self.assertEqual(
            registry.downstream_tool_for_gateway_tool("workflow.cancel"),
            "cancel_workflow",
        )
        self.assertEqual(
            registry.downstream_for_tool("workflow.submit"),
            "mission_control",
        )
        self.assertEqual(
            registry.downstream_for_tool("workflow.status"),
            "mission_control",
        )
        self.assertEqual(
            registry.downstream_for_tool("workflow.cancel"),
            "mission_control",
        )

    def test_parse_registry_rejects_missing_namespace(self) -> None:
        document = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        del document["namespaces"]["mission"]
        with self.assertRaises(RuntimeError) as ctx:
            parse_registry(document)
        self.assertIn("mission", str(ctx.exception))

    def test_parse_registry_rejects_unknown_downstream(self) -> None:
        document = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        document["namespaces"]["case"]["downstream_service"] = "missing"
        with self.assertRaises(RuntimeError) as ctx:
            parse_registry(document)
        self.assertIn("missing", str(ctx.exception))

    def test_parse_registry_rejects_missing_required_gateway_tools(self) -> None:
        document = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        document["tool_bindings"] = [
            binding
            for binding in document["tool_bindings"]
            if binding["tool"] != "case.get_artifact"
        ]
        with self.assertRaises(RuntimeError) as ctx:
            parse_registry(document)
        self.assertIn("case.get_artifact", str(ctx.exception))


class ConfigTests(unittest.TestCase):
    def test_validate_http_base_url_accepts_https(self) -> None:
        url = validate_http_base_url(
            "https://example.up.railway.app/", env_name="TEST_URL"
        )
        self.assertEqual(url, "https://example.up.railway.app")

    def test_validate_http_base_url_rejects_non_http(self) -> None:
        with self.assertRaises(RuntimeError):
            validate_http_base_url("ftp://example.com", env_name="TEST_URL")

    def test_load_settings_uses_env_overrides_and_sha(self) -> None:
        env = {
            **REQUIRED_SECRETS,
            "RAILWAY_GIT_COMMIT_SHA": "abc123deadbeef",
            "GATEWAY_HEALTH_TIMEOUT_SECONDS": "2.5",
            "GATEWAY_CONNECT_TIMEOUT_SECONDS": "1.5",
            "GATEWAY_READ_TIMEOUT_SECONDS": "12",
            "GATEWAY_BRIDGE_URL": "https://bridge.example",
            "GATEWAY_STORAGE_URL": "https://storage.example",
            "GATEWAY_MISSION_CONTROL_URL": "https://mission.example",
            "GATEWAY_ARTIFACTS_URL": "https://artifacts.example",
        }
        settings = load_settings(environ=env, registry=load_registry(REGISTRY_PATH))
        self.assertEqual(settings.deployed_commit_sha, "abc123deadbeef")
        self.assertEqual(settings.health_timeout_seconds, 2.5)
        self.assertEqual(settings.connect_timeout_seconds, 1.5)
        self.assertEqual(settings.read_timeout_seconds, 12.0)
        self.assertEqual(settings.github_oauth_client_id, "test-gateway-client-id")
        self.assertEqual(
            normalize_bearer_token(settings.bridge_authorization),
            TEST_BRIDGE_SERVICE_TOKEN,
        )
        by_key = {item.key: item for item in settings.downstreams}
        self.assertEqual(by_key["bridge"].base_url, "https://bridge.example")
        self.assertEqual(
            by_key["bridge"].health_url, "https://bridge.example/health"
        )
        self.assertEqual(by_key["storage"].base_url, "https://storage.example")
        self.assertEqual(
            by_key["mission_control"].base_url, "https://mission.example"
        )
        self.assertEqual(
            by_key["artifacts"].base_url, "https://artifacts.example"
        )
        self.assertEqual(settings.mcp_path, "/mcp/service")
        self.assertEqual(settings.mission_control_mcp_path, "/mcp")
        self.assertEqual(settings.mcp_path_for_service("storage"), "/mcp/service")
        self.assertEqual(
            settings.mcp_path_for_service("mission_control"), "/mcp"
        )

    def test_load_settings_rejects_invalid_timeout(self) -> None:
        with self.assertRaises(RuntimeError):
            load_settings(
                environ={
                    **REQUIRED_SECRETS,
                    "GATEWAY_HEALTH_TIMEOUT_SECONDS": "0",
                },
                registry=load_registry(REGISTRY_PATH),
            )

    def test_load_settings_fail_closed_without_github_oauth(self) -> None:
        env = {**REQUIRED_SECRETS}
        del env["GITHUB_OAUTH_CLIENT_ID"]
        with self.assertRaises(RuntimeError) as ctx:
            load_settings(
                environ=env,
                registry=load_registry(REGISTRY_PATH),
            )
        self.assertIn("GITHUB_OAUTH_CLIENT_ID", str(ctx.exception))

    def test_load_settings_fail_closed_without_bridge_authorization(self) -> None:
        env = {**REQUIRED_SECRETS}
        del env["GATEWAY_BRIDGE_AUTHORIZATION"]
        with self.assertRaises(RuntimeError) as ctx:
            load_settings(
                environ=env,
                registry=load_registry(REGISTRY_PATH),
            )
        self.assertIn("GATEWAY_BRIDGE_AUTHORIZATION", str(ctx.exception))

    def test_load_settings_fail_closed_without_public_url(self) -> None:
        env = {**REQUIRED_SECRETS}
        del env["GATEWAY_PUBLIC_URL"]
        with self.assertRaises(RuntimeError) as ctx:
            load_settings(
                environ=env,
                registry=load_registry(REGISTRY_PATH),
            )
        self.assertIn("GATEWAY_PUBLIC_URL", str(ctx.exception))

    def test_default_timeout_when_unset(self) -> None:
        settings = load_settings(
            environ=dict(REQUIRED_SECRETS),
            registry=load_registry(REGISTRY_PATH),
        )
        self.assertEqual(
            settings.health_timeout_seconds, DEFAULT_HEALTH_TIMEOUT_SECONDS
        )
        self.assertEqual(
            settings.deployed_commit_sha,
            gateway_config.UNKNOWN_DEPLOYED_COMMIT_SHA,
        )


class AuthForwardingTests(unittest.TestCase):
    def test_service_authorization_header_formats_bearer(self) -> None:
        self.assertEqual(
            service_authorization_header("service-token"),
            "Bearer service-token",
        )
        self.assertEqual(
            service_authorization_header("Bearer already"),
            "Bearer already",
        )

    def test_resolve_authorization_uses_service_credential_only(self) -> None:
        resolved = resolve_authorization_for_service(
            downstream_service="bridge",
            bridge_authorization="Bearer service-token",
        )
        self.assertEqual(resolved, "Bearer service-token")
        self.assertIsNone(
            resolve_authorization_for_service(
                downstream_service="mission_control",
                bridge_authorization="Bearer service-token",
            )
        )

    def test_redact_secrets_strips_bearer_material(self) -> None:
        raw = "Authorization: Bearer super-secret-token failed"
        redacted = redact_secrets(raw, extra_secrets=("super-secret-token",))
        self.assertNotIn("super-secret-token", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_mcp_endpoint_url_join(self) -> None:
        self.assertEqual(
            mcp_endpoint_url("https://bridge.example", "/mcp/service"),
            "https://bridge.example/mcp/service",
        )
        self.assertEqual(
            mcp_endpoint_url("https://bridge.example/mcp/service", "/mcp/service"),
            "https://bridge.example/mcp/service",
        )
        self.assertEqual(
            mcp_endpoint_url("https://mission.example", "/mcp"),
            "https://mission.example/mcp",
        )


class ForwardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.binding = ToolBinding(
            gateway_tool="case.get_artifact",
            namespace="case",
            downstream_service="artifacts",
            downstream_tool="get_case_artifact",
        )
        self.tokens = bind_request_ids(
            request_id="req-forward-1", correlation_id="corr-forward-1"
        )

    def tearDown(self) -> None:
        reset_request_ids(self.tokens)

    def test_unconfigured_stage(self) -> None:
        payload = asyncio.run(
            forward_mcp_tool(
                binding=self.binding,
                arguments={"mission_id": "m1", "filename": "Q1_candidate_answer.md"},
                base_url=None,
                authorization="Bearer t",
                connect_timeout_seconds=1.0,
                read_timeout_seconds=2.0,
            )
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failure_stage"], STAGE_UNCONFIGURED)
        self.assertEqual(payload["request_id"], "req-forward-1")
        self.assertEqual(payload["correlation_id"], "corr-forward-1")
        self.assertEqual(payload["downstream_tool"], "get_case_artifact")

    def test_auth_stage_when_authorization_missing(self) -> None:
        payload = asyncio.run(
            forward_mcp_tool(
                binding=self.binding,
                arguments={"mission_id": "m1"},
                base_url="https://artifacts.example",
                authorization=None,
                connect_timeout_seconds=1.0,
                read_timeout_seconds=2.0,
                require_authorization=True,
            )
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failure_stage"], STAGE_AUTH)

    def test_timeout_failure_stage(self) -> None:
        class BoomClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def call_tool(self, *args, **kwargs):
                raise httpx.ReadTimeout("slow")

        payload = asyncio.run(
            forward_mcp_tool(
                binding=self.binding,
                arguments={"mission_id": "m1"},
                base_url="https://artifacts.example",
                authorization="Bearer t",
                connect_timeout_seconds=0.2,
                read_timeout_seconds=0.2,
                client_factory=BoomClient,
            )
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failure_stage"], STAGE_TIMEOUT)
        self.assertIsInstance(payload["duration_ms"], float)
        self.assertNotIn("Bearer", json.dumps(payload))

    def test_connect_failure_stage_isolated(self) -> None:
        class BoomClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def call_tool(self, *args, **kwargs):
                raise httpx.ConnectError("refused")

        payload = asyncio.run(
            forward_mcp_tool(
                binding=self.binding,
                arguments={"mission_id": "m1"},
                base_url="https://artifacts.example",
                authorization="Bearer t",
                connect_timeout_seconds=0.2,
                read_timeout_seconds=0.2,
                client_factory=BoomClient,
            )
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failure_stage"], STAGE_CONNECT)

    def test_tool_error_stage(self) -> None:
        class FakeResult:
            is_error = True
            data = None

        class OkClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def call_tool(self, name, arguments, raise_on_error=False):
                self.name = name
                self.arguments = arguments
                return FakeResult()

        client = OkClient()
        payload = asyncio.run(
            forward_mcp_tool(
                binding=self.binding,
                arguments={"mission_id": "m1"},
                base_url="https://artifacts.example",
                authorization="Bearer t",
                connect_timeout_seconds=1.0,
                read_timeout_seconds=2.0,
                client_factory=lambda: client,
            )
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failure_stage"], STAGE_TOOL)
        self.assertEqual(client.name, "get_case_artifact")

    def test_tool_error_preserves_safe_structured_details(self) -> None:
        class FakeResult:
            is_error = True
            data = None
            structured_content = {
                "ok": False,
                "error_code": "ref_not_in_repository",
                "message": "commit deadbeef was not found in nhpcorp35/legal-ai",
            }
            content = [
                json.dumps(
                    {
                        "ok": False,
                        "error_code": "ref_not_in_repository",
                        "message": "commit deadbeef was not found in nhpcorp35/legal-ai",
                    }
                )
            ]

        class OkClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def call_tool(self, name, arguments, raise_on_error=False):
                return FakeResult()

        payload = asyncio.run(
            forward_mcp_tool(
                binding=ToolBinding(
                    gateway_tool="case.submit_case00_q1",
                    namespace="case",
                    downstream_service="bridge",
                    downstream_tool="submit_case00_q1",
                ),
                arguments={
                    "ref": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "authorization_confirmed": True,
                },
                base_url="https://bridge.example",
                authorization="Bearer bridge-service-secret-token",
                connect_timeout_seconds=1.0,
                read_timeout_seconds=2.0,
                client_factory=OkClient,
                extra_secrets=("bridge-service-secret-token",),
            )
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failure_stage"], STAGE_TOOL)
        self.assertEqual(payload["error"]["error_code"], "ref_not_in_repository")
        self.assertIn("nhpcorp35/legal-ai", payload["error"]["message"])
        blob = json.dumps(payload)
        self.assertNotIn("bridge-service-secret-token", blob)
        self.assertNotIn("Bearer", blob)

    def test_get_acceptance_contract_auth_and_tool_error_propagation(self) -> None:
        binding = ToolBinding(
            gateway_tool="storage.get_acceptance_contract",
            namespace="storage",
            downstream_service="storage",
            downstream_tool="get_acceptance_contract",
        )
        args = {
            "benchmark_id": "synth-benchmark-alpha",
            "question_id": "Q-SYNTH-01",
            "contract_id": "contract-synth-alpha-q01",
            "version": "1.0.0",
        }
        auth_failed = asyncio.run(
            forward_mcp_tool(
                binding=binding,
                arguments=args,
                base_url="https://storage.example",
                authorization=None,
                connect_timeout_seconds=1.0,
                read_timeout_seconds=2.0,
                require_authorization=True,
            )
        )
        self.assertFalse(auth_failed["ok"])
        self.assertEqual(auth_failed["failure_stage"], STAGE_AUTH)
        self.assertEqual(auth_failed["downstream_service"], "storage")
        self.assertEqual(auth_failed["downstream_tool"], "get_acceptance_contract")
        self.assertEqual(auth_failed["request_id"], "req-forward-1")
        self.assertEqual(auth_failed["correlation_id"], "corr-forward-1")
        self.assertIsInstance(auth_failed["duration_ms"], (int, float))

        class FakeResult:
            is_error = True
            data = None
            structured_content = {
                "ok": False,
                "error_code": "object_not_found",
                "message": "acceptance contract object was not found",
            }
            content = None

        class OkClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def call_tool(self, name, arguments, raise_on_error=False):
                self.name = name
                self.arguments = arguments
                return FakeResult()

        client = OkClient()
        tool_failed = asyncio.run(
            forward_mcp_tool(
                binding=binding,
                arguments=args,
                base_url="https://storage.example",
                authorization="Bearer storage-service-token",
                connect_timeout_seconds=1.0,
                read_timeout_seconds=2.0,
                client_factory=lambda: client,
                extra_secrets=("storage-service-token",),
            )
        )
        self.assertFalse(tool_failed["ok"])
        self.assertEqual(tool_failed["failure_stage"], STAGE_TOOL)
        self.assertEqual(client.name, "get_acceptance_contract")
        self.assertEqual(client.arguments, args)
        self.assertEqual(tool_failed["error"]["error_code"], "object_not_found")
        self.assertNotIn("storage-service-token", json.dumps(tool_failed))

    def test_tool_error_redacts_secret_bearing_messages(self) -> None:
        class FakeResult:
            is_error = True
            data = None
            structured_content = {
                "error_code": "dispatch_failed",
                "message": "Authorization: Bearer super-secret-token exploded",
            }
            content = None

        class OkClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def call_tool(self, *args, **kwargs):
                return FakeResult()

        payload = asyncio.run(
            forward_mcp_tool(
                binding=self.binding,
                arguments={"mission_id": "m1"},
                base_url="https://artifacts.example",
                authorization="Bearer t",
                connect_timeout_seconds=1.0,
                read_timeout_seconds=2.0,
                client_factory=OkClient,
                extra_secrets=("super-secret-token",),
            )
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["error_code"], "dispatch_failed")
        self.assertNotIn("super-secret-token", json.dumps(payload))

    def test_success_metadata_envelope(self) -> None:
        class FakeResult:
            is_error = False
            data = {"ok": True, "verified": True}

        class OkClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def call_tool(self, name, arguments, raise_on_error=False):
                return FakeResult()

        payload = asyncio.run(
            forward_mcp_tool(
                binding=self.binding,
                arguments={"mission_id": "m1"},
                base_url="https://artifacts.example",
                authorization="Bearer t",
                connect_timeout_seconds=1.0,
                read_timeout_seconds=2.0,
                client_factory=OkClient,
            )
        )
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["failure_stage"])
        self.assertEqual(payload["gateway_tool"], "case.get_artifact")
        self.assertEqual(payload["downstream_service"], "artifacts")
        self.assertEqual(payload["result"]["verified"], True)

    def test_isolation_one_failure_does_not_affect_other_binding(self) -> None:
        """Two sequential forwards: mission failure must not alter case success."""

        class FlakyFactory:
            def __init__(self):
                self.calls = 0

            def __call__(self):
                self.calls += 1
                outer = self

                class Client:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *args):
                        return False

                    async def call_tool(self, name, arguments, raise_on_error=False):
                        if name == "submit_run":
                            raise httpx.ConnectError("mission down")

                        class Ok:
                            is_error = False
                            data = {"ok": True, "name": name}

                        return Ok()

                return Client()

        factory = FlakyFactory()
        mission_binding = ToolBinding(
            gateway_tool="mission.submit",
            namespace="mission",
            downstream_service="mission_control",
            downstream_tool="submit_run",
        )
        failed = asyncio.run(
            forward_mcp_tool(
                binding=mission_binding,
                arguments={"mission_yaml": "id: x"},
                base_url="https://mission.example",
                authorization=None,
                connect_timeout_seconds=1.0,
                read_timeout_seconds=2.0,
                require_authorization=False,
                client_factory=factory,
            )
        )
        ok = asyncio.run(
            forward_mcp_tool(
                binding=self.binding,
                arguments={"mission_id": "m1"},
                base_url="https://artifacts.example",
                authorization="Bearer t",
                connect_timeout_seconds=1.0,
                read_timeout_seconds=2.0,
                client_factory=factory,
            )
        )
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["failure_stage"], STAGE_CONNECT)
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["downstream_tool"], "get_case_artifact")


class HealthIsolationTests(unittest.TestCase):
    def _settings(self, **url_map: str):
        env = {
            **REQUIRED_SECRETS,
            "RAILWAY_GIT_COMMIT_SHA": "sha-for-health-tests",
            "GATEWAY_HEALTH_TIMEOUT_SECONDS": "1",
        }
        env.update(url_map)
        return load_settings(environ=env, registry=load_registry(REGISTRY_PATH))

    def test_probe_classifies_timeout(self) -> None:
        settings = self._settings(
            GATEWAY_BRIDGE_URL="https://bridge.example",
            GATEWAY_STORAGE_URL="https://storage.example",
            GATEWAY_MISSION_CONTROL_URL="https://mission.example",
            GATEWAY_ARTIFACTS_URL="https://artifacts.example",
        )
        bridge = settings.downstream_by_key("bridge")

        async def _run() -> dict:
            transport = httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow"))
            )
            async with httpx.AsyncClient(transport=transport) as client:
                return await probe_downstream(
                    bridge, timeout_seconds=0.2, client=client
                )

        result = asyncio.run(_run())
        self.assertEqual(result["status"], STATUS_UNHEALTHY)
        self.assertEqual(result["failure_stage"], "timeout")
        self.assertIsInstance(result["latency_ms"], float)

    def test_aggregate_isolates_single_downstream_failure(self) -> None:
        settings = self._settings(
            GATEWAY_BRIDGE_URL="https://bridge.example",
            GATEWAY_STORAGE_URL="https://storage.example",
            GATEWAY_MISSION_CONTROL_URL="https://mission.example",
            GATEWAY_ARTIFACTS_URL="https://artifacts.example",
        )

        async def _run_direct() -> dict:
            async def fake_probe(downstream, timeout_seconds, client=None):
                key = downstream.key
                if key == "mission_control":
                    return {
                        "key": key,
                        "service_id": downstream.service_id,
                        "display_name": downstream.display_name,
                        "base_url": downstream.base_url,
                        "health_url": downstream.health_url,
                        "base_url_env": downstream.base_url_env,
                        "status": STATUS_UNHEALTHY,
                        "latency_ms": 3.0,
                        "failure_stage": HEALTH_STAGE_CONNECT,
                        "http_status": None,
                        "error": "connection refused",
                    }
                if key == "storage":
                    return {
                        "key": key,
                        "service_id": downstream.service_id,
                        "display_name": downstream.display_name,
                        "base_url": downstream.base_url,
                        "health_url": downstream.health_url,
                        "base_url_env": downstream.base_url_env,
                        "status": STATUS_UNHEALTHY,
                        "latency_ms": 4.0,
                        "failure_stage": STAGE_HTTP,
                        "http_status": 503,
                        "error": "health endpoint returned HTTP 503",
                    }
                return {
                    "key": key,
                    "service_id": downstream.service_id,
                    "display_name": downstream.display_name,
                    "base_url": downstream.base_url,
                    "health_url": downstream.health_url,
                    "base_url_env": downstream.base_url_env,
                    "status": STATUS_HEALTHY,
                    "latency_ms": 1.0,
                    "failure_stage": None,
                    "http_status": 200,
                    "error": None,
                }

            with mock.patch(
                "hal_legalai_gateway.health.probe_downstream",
                side_effect=fake_probe,
            ):
                return await aggregate_health(
                    settings,
                    registered_tools=["case.get_artifact", "mission.submit"],
                )

        payload = asyncio.run(_run_direct())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["phase"], 2)
        self.assertEqual(payload["deployed_commit_sha"], "sha-for-health-tests")
        self.assertEqual(
            payload["registered_tools"],
            ["case.get_artifact", "mission.submit"],
        )
        self.assertTrue(payload["capabilities"]["case"]["available"])
        self.assertFalse(payload["capabilities"]["storage"]["available"])
        self.assertFalse(payload["capabilities"]["mission"]["available"])


class McpRegistrationTests(unittest.TestCase):
    def test_default_bindings_include_settled_minimum(self) -> None:
        names = {binding.gateway_tool for binding in DEFAULT_TOOL_BINDINGS}
        self.assertTrue(REQUIRED_GATEWAY_TOOLS.issubset(names))

    def test_create_mcp_server_registers_exact_names(self) -> None:
        settings = load_settings(
            environ={
                **REQUIRED_SECRETS,
                "GATEWAY_BRIDGE_URL": "https://bridge.example",
                "GATEWAY_STORAGE_URL": "https://storage.example",
                "GATEWAY_MISSION_CONTROL_URL": "https://mission.example",
                "GATEWAY_ARTIFACTS_URL": "https://artifacts.example",
            },
            registry=load_registry(REGISTRY_PATH),
        )
        mcp = create_mcp_server(settings, auth=_test_inbound_auth())
        names = asyncio.run(list_registered_tool_names(mcp))
        expected = sorted(binding.gateway_tool for binding in DEFAULT_TOOL_BINDINGS)
        self.assertEqual(names, expected)
        for required in REQUIRED_GATEWAY_TOOLS:
            self.assertIn(required, names)
        self.assertIn("case.submit_case00_q1", names)
        self.assertIn("storage.archive_review_packet", names)
        self.assertIn("storage.archive_acceptance_contract", names)
        self.assertIn("storage.verify_acceptance_contract", names)
        self.assertIn("storage.list_acceptance_contracts", names)
        self.assertIn("storage.get_acceptance_contract_template", names)
        self.assertIn("storage.get_acceptance_contract", names)
        self.assertIn("workflow.submit", names)
        self.assertIn("workflow.status", names)
        self.assertIn("workflow.cancel", names)
        for forbidden in FORBIDDEN_WORKFLOW_TOOLS:
            self.assertNotIn(forbidden, names)

    def test_case_get_artifact_filename_schema_is_question_agnostic(self) -> None:
        """Unified case.get_artifact must not hardcode a Q1-only filename enum."""
        settings = load_settings(
            environ={
                **REQUIRED_SECRETS,
                "GATEWAY_BRIDGE_URL": "https://bridge.example",
                "GATEWAY_STORAGE_URL": "https://storage.example",
                "GATEWAY_MISSION_CONTROL_URL": "https://mission.example",
                "GATEWAY_ARTIFACTS_URL": "https://artifacts.example",
            },
            registry=load_registry(REGISTRY_PATH),
        )
        mcp = create_mcp_server(settings, auth=_test_inbound_auth())
        tools = asyncio.run(mcp.get_tools())
        tool = tools["case.get_artifact"]
        schema = tool.parameters
        filename_schema = schema["properties"]["filename"]
        self.assertEqual(filename_schema.get("type"), "string")
        self.assertNotIn("enum", filename_schema)
        self.assertNotIn("Q1_candidate_answer.json", json.dumps(filename_schema))
        binding = next(
            b for b in DEFAULT_TOOL_BINDINGS if b.gateway_tool == "case.get_artifact"
        )
        self.assertIn("Q<N>", binding.description)
        self.assertIn("generation_manifest.json", binding.description)
        self.assertIn("case00_attorney_review_packet.md", binding.description)


def _gateway_settings(**url_overrides: str):
    environ = {
        **REQUIRED_SECRETS,
        "GATEWAY_BRIDGE_URL": "https://bridge.example",
        "GATEWAY_STORAGE_URL": "https://storage.example",
        "GATEWAY_MISSION_CONTROL_URL": "https://mission.example",
        "GATEWAY_ARTIFACTS_URL": "https://artifacts.example",
        **url_overrides,
    }
    return load_settings(environ=environ, registry=load_registry(REGISTRY_PATH))


class CanonicalIdentityTests(unittest.TestCase):
    """Lock update-safe MCP/OAuth identity and the complete namespaced catalog."""

    def test_canonical_identity_rejects_unified_suffixes_and_library_version(self) -> None:
        identity = canonical_gateway_identity(public_url="https://gateway.example/")
        self.assertEqual(identity["display_name"], "HAL LegalAI Gateway")
        self.assertEqual(identity["service_id"], CANONICAL_GATEWAY_SERVICE_ID)
        self.assertEqual(identity["identity_version"], CANONICAL_GATEWAY_IDENTITY_VERSION)
        self.assertEqual(identity["mcp_path"], CANONICAL_INBOUND_MCP_PATH)
        self.assertEqual(identity["website_url"], "https://gateway.example")
        self.assertEqual(identity["mcp_url"], "https://gateway.example/mcp")
        self.assertEqual(identity["resource"], identity["mcp_url"])
        self.assertEqual(identity["resource_name"], CANONICAL_GATEWAY_DISPLAY_NAME)
        self.assertNotRegex(identity["display_name"], r"Unified\d*$")
        self.assertNotIn("Unified", identity["display_name"])
        import fastmcp

        self.assertNotEqual(identity["identity_version"], fastmcp.__version__)

    def test_create_mcp_server_pins_identity_and_complete_tool_catalog(self) -> None:
        settings = _gateway_settings()
        mcp = create_mcp_server(settings, auth=_test_inbound_auth())
        self.assertEqual(mcp.name, CANONICAL_GATEWAY_DISPLAY_NAME)
        self.assertEqual(mcp.version, CANONICAL_GATEWAY_IDENTITY_VERSION)
        self.assertEqual(mcp.website_url, "https://gateway.example")
        self.assertEqual(mcp.instructions, CANONICAL_GATEWAY_INSTRUCTIONS)

        names = asyncio.run(list_registered_tool_names(mcp))
        expected = sorted(binding.gateway_tool for binding in DEFAULT_TOOL_BINDINGS)
        self.assertEqual(names, expected)
        by_namespace: dict[str, list[str]] = {}
        for binding in DEFAULT_TOOL_BINDINGS:
            by_namespace.setdefault(binding.namespace, []).append(binding.gateway_tool)
        self.assertEqual(
            set(by_namespace),
            {"case", "storage", "mission", "workflow"},
        )
        for namespace, tools in by_namespace.items():
            self.assertTrue(tools, msg=f"{namespace} must expose tools")
            for tool in tools:
                self.assertIn(tool, names)
        self.assertTrue(REQUIRED_GATEWAY_TOOLS.issubset(set(names)))
        for forbidden in FORBIDDEN_WORKFLOW_TOOLS:
            self.assertNotIn(forbidden, names)

    def test_oauth_protected_resource_metadata_uses_canonical_resource_name(self) -> None:
        from starlette.applications import Starlette
        from starlette.testclient import TestClient as StarletteClient

        settings = _gateway_settings()
        provider = build_inbound_auth_provider(settings)
        routes = provider.get_routes(CANONICAL_INBOUND_MCP_PATH)
        app = Starlette(routes=list(routes))
        with StarletteClient(app) as client:
            response = client.get("/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get("resource_name"), CANONICAL_GATEWAY_DISPLAY_NAME)
        self.assertEqual(str(payload.get("resource")).rstrip("/"), "https://gateway.example/mcp")
        servers = payload.get("authorization_servers") or []
        self.assertTrue(servers)
        self.assertTrue(
            str(servers[0]).startswith("https://gateway.example"),
            msg=servers,
        )


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = {
            **REQUIRED_SECRETS,
            "RAILWAY_GIT_COMMIT_SHA": "deadbeefcafebabe0123456789abcdef01234567",
            "GATEWAY_HEALTH_TIMEOUT_SECONDS": "1",
            "GATEWAY_BRIDGE_URL": "https://bridge.example",
            "GATEWAY_STORAGE_URL": "https://storage.example",
            "GATEWAY_MISSION_CONTROL_URL": "https://mission.example",
            "GATEWAY_ARTIFACTS_URL": "https://artifacts.example",
        }
        self._env_patch = mock.patch.dict(os.environ, self.env, clear=False)
        self._env_patch.start()

        async def fake_probe(downstream, timeout_seconds, client=None):
            return {
                "key": downstream.key,
                "service_id": downstream.service_id,
                "display_name": downstream.display_name,
                "base_url": downstream.base_url,
                "health_url": downstream.health_url,
                "base_url_env": downstream.base_url_env,
                "status": STATUS_HEALTHY,
                "latency_ms": 1.5,
                "failure_stage": None,
                "http_status": 200,
                "error": None,
            }

        self._probe_patch = mock.patch(
            "hal_legalai_gateway.health.probe_downstream",
            side_effect=fake_probe,
        )
        self._probe_patch.start()
        reset_settings_for_tests()
        self._app = create_app(auth_override=_test_inbound_auth())
        self._client_cm = TestClient(self._app)
        self.client = self._client_cm.__enter__()

    def tearDown(self) -> None:
        self._client_cm.__exit__(None, None, None)
        self._probe_patch.stop()
        self._env_patch.stop()
        reset_settings_for_tests()

    def test_health_reports_commit_sha_tools_and_downstream_map(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["service"], "hal-legalai-gateway")
        self.assertEqual(payload["phase"], 2)
        self.assertEqual(
            payload["deployed_commit_sha"],
            "deadbeefcafebabe0123456789abcdef01234567",
        )
        self.assertIn("bridge", payload["downstream"])
        self.assertIn("case.get_artifact", payload["registered_tools"])
        self.assertIn("mission.submit", payload["registered_tools"])
        self.assertIn("storage.verify_archive", payload["registered_tools"])
        self.assertTrue(response.headers.get(REQUEST_ID_HEADER))
        self.assertTrue(response.headers.get(CORRELATION_ID_HEADER))

    def test_health_preserves_incoming_request_ids(self) -> None:
        response = self.client.get(
            "/health",
            headers={
                REQUEST_ID_HEADER: "req-fixed-1",
                CORRELATION_ID_HEADER: "corr-fixed-1",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers[REQUEST_ID_HEADER], "req-fixed-1")
        self.assertEqual(
            response.headers[CORRELATION_ID_HEADER], "corr-fixed-1"
        )
        payload = response.json()
        self.assertEqual(payload["request_id"], "req-fixed-1")
        self.assertEqual(payload["correlation_id"], "corr-fixed-1")

    def test_registry_endpoint_exposes_namespaces_and_bindings(self) -> None:
        response = self.client.get("/registry")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload["namespaces"]), EXPECTED_NAMESPACES)
        self.assertEqual(
            payload["resolved_downstreams"]["bridge"]["base_url"],
            "https://bridge.example",
        )
        tools = {item["tool"] for item in payload["tool_bindings"]}
        self.assertTrue(REQUIRED_GATEWAY_TOOLS.issubset(tools))
        self.assertIn("workflow.submit", tools)
        self.assertIn("workflow.status", tools)
        self.assertIn("workflow.cancel", tools)
        self.assertEqual(
            payload["namespaces"]["workflow"]["downstream_service"],
            "mission_control",
        )

    def test_mcp_requires_authorization(self) -> None:
        response = self.client.post(
            "/mcp",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        self.assertIn(response.status_code, {401, 403})

    def test_mcp_rejects_invalid_token(self) -> None:
        response = self.client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer definitely-not-valid",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            }},
        )
        self.assertIn(response.status_code, {401, 403})

    def test_mcp_accepts_authenticated_gateway_token(self) -> None:
        response = self.client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {TEST_GATEWAY_OAUTH_TOKEN}",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            }},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get("jsonrpc"), "2.0")
        self.assertIn("result", payload)
        server_info = payload["result"]["serverInfo"]
        self.assertEqual(server_info["name"], CANONICAL_GATEWAY_DISPLAY_NAME)
        self.assertEqual(server_info["version"], CANONICAL_GATEWAY_IDENTITY_VERSION)
        website = server_info.get("websiteUrl") or server_info.get("website_url")
        self.assertEqual(str(website).rstrip("/"), "https://gateway.example")

    def test_http_surfaces_canonical_identity_and_tool_catalog(self) -> None:
        expected_tools = sorted(
            binding.gateway_tool for binding in DEFAULT_TOOL_BINDINGS
        )
        identity = canonical_gateway_identity(public_url="https://gateway.example")
        for path in ("/", "/health", "/registry"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, msg=path)
            payload = response.json()
            self.assertEqual(payload["identity"], identity, msg=path)
            self.assertEqual(
                payload["identity"]["display_name"],
                "HAL LegalAI Gateway",
                msg=path,
            )
            self.assertNotIn("Unified", payload["identity"]["display_name"])
            if path in {"/", "/health"}:
                self.assertEqual(
                    sorted(payload["registered_tools"]),
                    expected_tools,
                    msg=path,
                )
        registry = self.client.get("/registry").json()
        catalog = {item["tool"] for item in registry["tool_bindings"]}
        self.assertEqual(catalog, set(expected_tools))
        self.assertEqual(set(registry["namespaces"]), EXPECTED_NAMESPACES)
        for namespace in ("case", "storage", "mission", "workflow"):
            self.assertTrue(
                registry["namespaces"][namespace]["tools"],
                msg=namespace,
            )

    def test_health_reports_auth_mode_without_secrets(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["auth"]["inbound"], "github_oauth")
        self.assertEqual(payload["auth"]["downstream_bridge"], "service_credential")
        blob = str(payload)
        self.assertNotIn(TEST_BRIDGE_SERVICE_TOKEN, blob)
        self.assertNotIn(REQUIRED_SECRETS["GITHUB_OAUTH_CLIENT_SECRET"], blob)
        self.assertNotIn(REQUIRED_SECRETS["JWT_SIGNING_KEY"], blob)


class ServiceCredentialTests(unittest.TestCase):
    def test_valid_service_token_verifies(self) -> None:
        verifier = ServiceTokenVerifier(TEST_BRIDGE_SERVICE_TOKEN)

        async def _run():
            return await verifier.verify_token(TEST_BRIDGE_SERVICE_TOKEN)

        token = __import__("asyncio").run(_run())
        self.assertIsNotNone(token)
        self.assertEqual(token.claims.get("token_use"), "service")
        self.assertIsNone(token.expires_at)

    def test_invalid_service_token_rejected(self) -> None:
        verifier = ServiceTokenVerifier(TEST_BRIDGE_SERVICE_TOKEN)

        async def _run():
            return await verifier.verify_token("wrong-token")

        token = __import__("asyncio").run(_run())
        self.assertIsNone(token)


class GatewayBridgeCase00RefIntegrationTests(unittest.TestCase):
    """Gateway→Bridge submit with main resolves an immutable LegalAI SHA."""

    LEGALAI_SHA = "49f6881c08e7e4fdf76d8500d52a27d057c0804b"

    def test_gateway_main_submission_returns_resolved_immutable_sha(self) -> None:
        import sys
        from pathlib import Path

        from cryptography.fernet import Fernet

        bridge_dir = Path(__file__).resolve().parent.parent / "github_actions_bridge"
        for key, value in {
            "GITHUB_OAUTH_CLIENT_ID": "test-client-id",
            "GITHUB_OAUTH_CLIENT_SECRET": "test-client-secret",
            "REDIS_HOST": "127.0.0.1",
            "REDIS_PORT": "6379",
            "STORAGE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "JWT_SIGNING_KEY": "test-jwt-signing-key-for-bridge",
            "GITHUB_TOKEN": "test-github-token-not-for-production",
        }.items():
            os.environ.setdefault(key, value)
        if str(bridge_dir) not in sys.path:
            sys.path.insert(0, str(bridge_dir))
        import server as bridge_server

        dispatches: list[dict] = []
        orig_repo = bridge_server.REPOSITORY
        orig_branch = bridge_server.CASE00_WORKFLOW_BRANCH
        orig_workflow = bridge_server.CASE00_WORKFLOW
        bridge_server.REPOSITORY = "nhpcorp35/legal-ai"
        bridge_server.CASE00_WORKFLOW_BRANCH = "main"
        bridge_server.CASE00_WORKFLOW = "hal-case00-q1.yml"

        async def fake_github_json(method, path, **kwargs):
            class Resp:
                def __init__(self, status_code: int):
                    self.status_code = status_code

            if method == "GET" and path.endswith("/commits/main"):
                return Resp(200), {"sha": self.LEGALAI_SHA}, None
            if method == "POST" and path.endswith("/dispatches"):
                dispatches.append(kwargs.get("json") or {})
                return Resp(204), None, None
            return Resp(500), {"message": "unexpected"}, None

        class BridgeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def call_tool(self, name, arguments, raise_on_error=False):
                assert name == "submit_case00_q1"
                from fastmcp.exceptions import ToolError

                with mock.patch.object(
                    bridge_server, "_require_allowed_user", return_value="service:gateway"
                ), mock.patch.object(
                    bridge_server, "_github_json", side_effect=fake_github_json
                ):
                    submit = getattr(
                        bridge_server.submit_case00_q1,
                        "fn",
                        bridge_server.submit_case00_q1,
                    )
                    try:
                        raw = await submit(**arguments)
                    except ToolError as exc:
                        payload = json.loads(str(exc))

                        class ErrResult:
                            is_error = True
                            data = payload
                            structured_content = payload
                            content = [str(exc)]

                        return ErrResult()

                class Result:
                    is_error = False
                    data = raw
                    structured_content = raw
                    content = None

                return Result()

        binding = ToolBinding(
            gateway_tool="case.submit_case00_q1",
            namespace="case",
            downstream_service="bridge",
            downstream_tool="submit_case00_q1",
        )
        tokens = bind_request_ids(
            request_id="req-case00-main", correlation_id="corr-case00-main"
        )
        try:
            payload = asyncio.run(
                forward_mcp_tool(
                    binding=binding,
                    arguments={
                        "ref": "main",
                        "authorization_confirmed": True,
                        "mission_id": "mission-gateway-main",
                    },
                    base_url="https://bridge.example",
                    authorization="Bearer bridge-service-token",
                    connect_timeout_seconds=1.0,
                    read_timeout_seconds=2.0,
                    client_factory=BridgeClient,
                    extra_secrets=("bridge-service-token", "test-github-token-not-for-production"),
                )
            )
        finally:
            reset_request_ids(tokens)
            bridge_server.REPOSITORY = orig_repo
            bridge_server.CASE00_WORKFLOW_BRANCH = orig_branch
            bridge_server.CASE00_WORKFLOW = orig_workflow

        self.assertTrue(payload["ok"], msg=payload)
        self.assertIsNone(payload["failure_stage"])
        result = payload["result"]
        self.assertEqual(result["requested_ref"], "main")
        self.assertEqual(result["resolved_ref"], self.LEGALAI_SHA)
        self.assertEqual(result["repository"], "nhpcorp35/legal-ai")
        self.assertEqual(result["workflow"], "hal-case00-q1.yml")
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(dispatches[0]["inputs"]["legalai_ref"], self.LEGALAI_SHA)
        blob = json.dumps(payload)
        self.assertNotIn("bridge-service-token", blob)
        self.assertNotIn("test-github-token-not-for-production", blob)


class RequestContextUnitTests(unittest.TestCase):
    def test_contextvars_default_empty(self) -> None:
        self.assertIsNone(get_request_id())
        self.assertIsNone(get_correlation_id())


class WorkflowGatewaySliceDTests(unittest.TestCase):
    """Unified gateway workflow.submit / workflow.status / workflow.cancel thin forwarders."""

    def setUp(self) -> None:
        self.settings = load_settings(
            environ={
                **REQUIRED_SECRETS,
                "GATEWAY_BRIDGE_URL": "https://bridge.example",
                "GATEWAY_STORAGE_URL": "https://storage.example",
                "GATEWAY_MISSION_CONTROL_URL": "https://mission.example",
                "GATEWAY_ARTIFACTS_URL": "https://artifacts.example",
            },
            registry=load_registry(REGISTRY_PATH),
        )
        self.tokens = bind_request_ids(
            request_id="req-workflow-1",
            correlation_id="corr-workflow-1",
        )

    def tearDown(self) -> None:
        reset_request_ids(self.tokens)

    def _collect_tools(self, *gateway_tools: str) -> dict[str, Any]:
        collector: dict[str, Any] = {}

        class _Mcp:
            def tool(self, *args: Any, **kwargs: Any):
                def decorator(fn: Any) -> Any:
                    name = kwargs.get("name") or (args[0] if args else None)
                    if name in gateway_tools:
                        collector[name] = fn
                    return fn

                return decorator

        register_forwarding_tools(
            _Mcp(),  # type: ignore[arg-type]
            self.settings,
            bindings_from_registry(self.settings.registry),
        )
        return collector

    def test_registry_and_defaults_agree_exactly(self) -> None:
        registry = load_registry(REGISTRY_PATH)
        registry_ids = [
            (
                binding.gateway_tool,
                binding.namespace,
                binding.downstream_service,
                binding.downstream_tool,
            )
            for binding in registry.tool_bindings
        ]
        default_ids = [
            (
                binding.gateway_tool,
                binding.namespace,
                binding.downstream_service,
                binding.downstream_tool,
            )
            for binding in DEFAULT_TOOL_BINDINGS
        ]
        self.assertEqual(registry_ids, default_ids)
        merged = bindings_from_registry(registry)
        merged_by_name = {binding.gateway_tool: binding for binding in merged}
        defaults_by_name = {
            binding.gateway_tool: binding for binding in DEFAULT_TOOL_BINDINGS
        }
        self.assertEqual(set(merged_by_name), set(defaults_by_name))
        for name in ("workflow.submit", "workflow.status"):
            registry_binding = next(
                binding
                for binding in registry.tool_bindings
                if binding.gateway_tool == name
            )
            default_binding = defaults_by_name[name]
            self.assertEqual(registry_binding.description, default_binding.description)
            self.assertEqual(
                merged_by_name[name].description, default_binding.description
            )
            self.assertEqual(
                registry_binding.downstream_service, "mission_control"
            )
            for text in (
                registry_binding.description,
                default_binding.description,
            ):
                self.assertIn("MISSION_CONTROL_WORKFLOW_ORCHESTRATION", text)
                self.assertIn("fail-closed", text.lower())
                self.assertNotIn("workflow.wait", text)
                self.assertNotIn("workflow.cancel", text)
        cancel_binding = defaults_by_name["workflow.cancel"]
        self.assertEqual(cancel_binding.downstream_tool, "cancel_workflow")
        self.assertEqual(cancel_binding.downstream_service, "mission_control")
        self.assertIn(
            "MISSION_CONTROL_WORKFLOW_ORCHESTRATION", cancel_binding.description
        )
        self.assertIn("fail-closed", cancel_binding.description.lower())
        self.assertIn("cancel_workflow", cancel_binding.description)
        self.assertNotIn("workflow.wait", cancel_binding.description)
        self.assertNotIn("workflow.history", cancel_binding.description)
        self.assertEqual(
            defaults_by_name["workflow.submit"].downstream_tool,
            "submit_workflow",
        )
        self.assertEqual(
            defaults_by_name["workflow.status"].downstream_tool,
            "get_workflow",
        )
        preserved_namespaces = {binding.namespace for binding in merged}
        self.assertEqual(preserved_namespaces, EXPECTED_NAMESPACES)
        for required in REQUIRED_GATEWAY_TOOLS:
            self.assertIn(required, defaults_by_name)

    def test_existing_namespaces_are_unchanged(self) -> None:
        registry = load_registry(REGISTRY_PATH)
        self.assertEqual(
            list(registry.namespaces["case"].tools),
            [
                "case.submit",
                "case.status",
                "case.cancel",
                "case.list_artifacts",
                "case.submit_case00_q1",
                "case.get_case00_q1_run",
                "case.cancel_case00_q1_run",
                "case.get_case00_q1_artifacts",
                "case.get_artifact",
                "case.get_artifacts",
            ],
        )
        self.assertEqual(
            list(registry.namespaces["mission"].tools),
            [
                "mission.submit",
                "mission.submit_structured",
                "mission.status",
                "mission.list_notifications",
                "mission.wait",
                "mission.submit_and_wait",
                "mission.run_repository_command",
            ],
        )
        names = {binding.gateway_tool for binding in DEFAULT_TOOL_BINDINGS}
        self.assertIn("mission.wait", names)
        self.assertIn("case.get_artifact", names)
        for forbidden in FORBIDDEN_WORKFLOW_TOOLS:
            self.assertNotIn(forbidden, names)

    def test_submit_forwards_yaml_and_optional_idempotency_key(self) -> None:
        collector = self._collect_tools("workflow.submit")
        with mock.patch(
            "hal_legalai_gateway.mcp_server._require_gateway_principal",
            return_value="nhpcorp35",
        ), mock.patch(
            "hal_legalai_gateway.mcp_server.forward_mcp_tool",
            new_callable=mock.AsyncMock,
            return_value={"ok": True, "result": {"workflow_id": "wf-1"}},
        ) as forward_mock:
            result = asyncio.run(
                collector["workflow.submit"](
                    WORKFLOW_YAML_FIXTURE,
                    idempotency_key=WORKFLOW_IDEMPOTENCY_KEY,
                )
            )
        self.assertTrue(result["ok"])
        kwargs = forward_mock.await_args.kwargs
        self.assertEqual(kwargs["binding"].gateway_tool, "workflow.submit")
        self.assertEqual(kwargs["binding"].downstream_tool, "submit_workflow")
        self.assertEqual(kwargs["binding"].downstream_service, "mission_control")
        self.assertEqual(
            kwargs["arguments"],
            {
                "workflow_yaml": WORKFLOW_YAML_FIXTURE,
                "idempotency_key": WORKFLOW_IDEMPOTENCY_KEY,
            },
        )
        self.assertIsNone(kwargs["authorization"])
        self.assertFalse(kwargs["require_authorization"])
        self.assertEqual(kwargs["mcp_path"], "/mcp")
        self.assertNotIn(TEST_GATEWAY_OAUTH_TOKEN, str(kwargs["authorization"]))
        self.assertNotIn(TEST_BRIDGE_SERVICE_TOKEN, str(kwargs["authorization"]))
        extra = kwargs["extra_secrets"]
        self.assertIn(WORKFLOW_YAML_FIXTURE, extra)
        self.assertIn(WORKFLOW_IDEMPOTENCY_KEY, extra)

        forward_mock.reset_mock()
        with mock.patch(
            "hal_legalai_gateway.mcp_server._require_gateway_principal",
            return_value="nhpcorp35",
        ), mock.patch(
            "hal_legalai_gateway.mcp_server.forward_mcp_tool",
            new_callable=mock.AsyncMock,
            return_value={"ok": True, "result": {"workflow_id": "wf-2"}},
        ) as omit_mock:
            asyncio.run(collector["workflow.submit"](WORKFLOW_YAML_FIXTURE))
        self.assertEqual(
            omit_mock.await_args.kwargs["arguments"],
            {"workflow_yaml": WORKFLOW_YAML_FIXTURE},
        )
        self.assertNotIn(
            "idempotency_key", omit_mock.await_args.kwargs["arguments"]
        )

    def test_status_forwards_canonical_workflow_id(self) -> None:
        collector = self._collect_tools("workflow.status")
        sanitized = {
            "ok": True,
            "workflow_id": CANONICAL_WORKFLOW_ID,
            "state": "pending",
            "steps": [{"step_type": "implementation", "status": "pending"}],
        }
        with mock.patch(
            "hal_legalai_gateway.mcp_server._require_gateway_principal",
            return_value="nhpcorp35",
        ), mock.patch(
            "hal_legalai_gateway.mcp_server.forward_mcp_tool",
            new_callable=mock.AsyncMock,
            return_value={"ok": True, "result": sanitized},
        ) as forward_mock:
            result = asyncio.run(
                collector["workflow.status"](CANONICAL_WORKFLOW_ID)
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"], sanitized)
        kwargs = forward_mock.await_args.kwargs
        self.assertEqual(kwargs["binding"].downstream_tool, "get_workflow")
        self.assertEqual(
            kwargs["arguments"], {"workflow_id": CANONICAL_WORKFLOW_ID}
        )
        self.assertIsNone(kwargs["authorization"])
        self.assertFalse(kwargs["require_authorization"])

    def test_cancel_forwards_canonical_workflow_id(self) -> None:
        collector = self._collect_tools("workflow.cancel")
        sanitized = {
            "ok": True,
            "workflow_id": CANONICAL_WORKFLOW_ID,
            "state": "cancelled",
            "steps": [{"step_type": "implementation", "status": "cancelled"}],
        }
        with mock.patch(
            "hal_legalai_gateway.mcp_server._require_gateway_principal",
            return_value="nhpcorp35",
        ), mock.patch(
            "hal_legalai_gateway.mcp_server.forward_mcp_tool",
            new_callable=mock.AsyncMock,
            return_value={"ok": True, "result": sanitized},
        ) as forward_mock:
            result = asyncio.run(
                collector["workflow.cancel"](CANONICAL_WORKFLOW_ID)
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"], sanitized)
        kwargs = forward_mock.await_args.kwargs
        self.assertEqual(kwargs["binding"].gateway_tool, "workflow.cancel")
        self.assertEqual(kwargs["binding"].downstream_tool, "cancel_workflow")
        self.assertEqual(kwargs["binding"].downstream_service, "mission_control")
        self.assertEqual(
            kwargs["arguments"], {"workflow_id": CANONICAL_WORKFLOW_ID}
        )
        self.assertIsNone(kwargs["authorization"])
        self.assertFalse(kwargs["require_authorization"])

    def test_inbound_oauth_required_and_service_token_rejected(self) -> None:
        collector = self._collect_tools(
            "workflow.submit", "workflow.status", "workflow.cancel"
        )
        unauthorized = asyncio.run(
            collector["workflow.submit"](WORKFLOW_YAML_FIXTURE)
        )
        self.assertFalse(unauthorized["ok"])
        self.assertEqual(unauthorized["failure_stage"], "auth")
        self.assertEqual(unauthorized["gateway_tool"], "workflow.submit")
        blob = json.dumps(unauthorized)
        self.assertNotIn(WORKFLOW_YAML_FIXTURE, blob)
        self.assertNotIn(TEST_SECRET_VALUE_WF_GW, blob)
        self.assertNotIn(TEST_BRIDGE_SERVICE_TOKEN, blob)
        self.assertNotIn(TEST_GATEWAY_OAUTH_TOKEN, blob)

        service_token = AccessToken(
            token=TEST_BRIDGE_SERVICE_TOKEN,
            client_id=SERVICE_CLIENT_ID,
            scopes=[],
            claims={"token_use": "service", "client_id": SERVICE_CLIENT_ID},
        )
        with mock.patch(
            "hal_legalai_gateway.mcp_server.get_access_token",
            return_value=service_token,
        ), mock.patch(
            "hal_legalai_gateway.mcp_server.forward_mcp_tool",
            new_callable=mock.AsyncMock,
        ) as forward_mock:
            rejected = asyncio.run(
                collector["workflow.cancel"](CANONICAL_WORKFLOW_ID)
            )
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["failure_stage"], "auth")
        forward_mock.assert_not_awaited()

    def test_disabled_feature_403_envelope_is_forwarded(self) -> None:
        downstream = {
            "ok": False,
            "error": {
                "message": "Mission Control request failed",
                "status_code": 403,
                "details": {"detail": "Workflow orchestration is disabled"},
            },
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

        client = FakeClient()
        payload = asyncio.run(
            forward_mcp_tool(
                binding=ToolBinding(
                    gateway_tool="workflow.submit",
                    namespace="workflow",
                    downstream_service="mission_control",
                    downstream_tool="submit_workflow",
                ),
                arguments={
                    "workflow_yaml": WORKFLOW_YAML_FIXTURE,
                    "idempotency_key": WORKFLOW_IDEMPOTENCY_KEY,
                },
                base_url="https://mission.example",
                authorization=None,
                connect_timeout_seconds=1.0,
                read_timeout_seconds=2.0,
                require_authorization=False,
                client_factory=lambda: client,
                extra_secrets=(WORKFLOW_YAML_FIXTURE, WORKFLOW_IDEMPOTENCY_KEY),
            )
        )
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["failure_stage"])
        self.assertEqual(client.name, "submit_workflow")
        self.assertEqual(
            client.arguments["workflow_yaml"], WORKFLOW_YAML_FIXTURE
        )
        self.assertEqual(payload["result"]["error"]["status_code"], 403)
        self.assertEqual(
            payload["result"]["error"]["details"]["detail"],
            "Workflow orchestration is disabled",
        )
        self.assertEqual(payload["request_id"], "req-workflow-1")
        self.assertEqual(payload["correlation_id"], "corr-workflow-1")
        blob = json.dumps(payload)
        self.assertNotIn(TEST_BRIDGE_SERVICE_TOKEN, blob)
        self.assertNotIn(TEST_GATEWAY_OAUTH_TOKEN, blob)

    def test_malicious_downstream_errors_are_redacted(self) -> None:
        malicious_message = (
            f"failed yaml={WORKFLOW_YAML_FIXTURE} "
            f"idempotency_key={WORKFLOW_IDEMPOTENCY_KEY} "
            f"api_key=TEST_MC_API_KEY_NOT_REAL "
            "Authorization: Bearer TEST_OAUTH_SESSION_TOKEN_NOT_REAL "
            "child_mission_yaml: create_files: true\nstdout: leaked-out "
            "stderr: leaked-err exception: secret-bearing-trace"
        )

        class FakeResult:
            is_error = True
            data = None
            structured_content = {
                "error_code": "orchestration_disabled",
                "message": malicious_message,
            }
            content = None

        class OkClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def call_tool(self, *args, **kwargs):
                return FakeResult()

        payload = asyncio.run(
            forward_mcp_tool(
                binding=ToolBinding(
                    gateway_tool="workflow.status",
                    namespace="workflow",
                    downstream_service="mission_control",
                    downstream_tool="get_workflow",
                ),
                arguments={"workflow_id": CANONICAL_WORKFLOW_ID},
                base_url="https://mission.example",
                authorization=None,
                connect_timeout_seconds=1.0,
                read_timeout_seconds=2.0,
                require_authorization=False,
                client_factory=OkClient,
                extra_secrets=(
                    WORKFLOW_YAML_FIXTURE,
                    WORKFLOW_IDEMPOTENCY_KEY,
                    TEST_SECRET_VALUE_WF_GW,
                    "TEST_MC_API_KEY_NOT_REAL",
                    "TEST_OAUTH_SESSION_TOKEN_NOT_REAL",
                    TEST_BRIDGE_SERVICE_TOKEN,
                    TEST_GATEWAY_OAUTH_TOKEN,
                    *self.settings.secret_values_for_redaction(),
                ),
            )
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failure_stage"], STAGE_TOOL)
        blob = json.dumps(payload)
        self.assertNotIn(WORKFLOW_YAML_FIXTURE, blob)
        self.assertNotIn(TEST_SECRET_VALUE_WF_GW, blob)
        self.assertNotIn(WORKFLOW_IDEMPOTENCY_KEY, blob)
        self.assertNotIn("TEST_MC_API_KEY_NOT_REAL", blob)
        self.assertNotIn("TEST_OAUTH_SESSION_TOKEN_NOT_REAL", blob)
        self.assertNotIn(TEST_BRIDGE_SERVICE_TOKEN, blob)
        self.assertNotIn(TEST_GATEWAY_OAUTH_TOKEN, blob)
        self.assertNotIn("Bearer ", blob)
        self.assertNotIn(
            REQUIRED_SECRETS["GITHUB_OAUTH_CLIENT_SECRET"], blob
        )

        collector = self._collect_tools("workflow.submit")
        with mock.patch(
            "hal_legalai_gateway.mcp_server._require_gateway_principal",
            return_value="nhpcorp35",
        ), mock.patch(
            "hal_legalai_gateway.mcp_server.forward_mcp_tool",
            new_callable=mock.AsyncMock,
            return_value=payload,
        ):
            wrapped = asyncio.run(
                collector["workflow.submit"](
                    WORKFLOW_YAML_FIXTURE,
                    idempotency_key=WORKFLOW_IDEMPOTENCY_KEY,
                )
            )
        wrapped_blob = json.dumps(wrapped)
        self.assertNotIn(WORKFLOW_YAML_FIXTURE, wrapped_blob)
        self.assertNotIn(TEST_SECRET_VALUE_WF_GW, wrapped_blob)
        self.assertNotIn("leaked-out", wrapped_blob)
        self.assertNotIn("leaked-err", wrapped_blob)
        self.assertNotIn("secret-bearing-trace", wrapped_blob)
        self.assertEqual(
            wrapped["error"]["message"], "downstream tool returned an error"
        )

    def test_timeout_does_not_echo_yaml_or_secrets(self) -> None:
        class BoomClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def call_tool(self, *args, **kwargs):
                raise httpx.ReadTimeout(
                    f"slow yaml={WORKFLOW_YAML_FIXTURE} "
                    f"Bearer {TEST_GATEWAY_OAUTH_TOKEN} "
                    f"api_key={TEST_SECRET_VALUE_WF_GW}"
                )

        payload = asyncio.run(
            forward_mcp_tool(
                binding=ToolBinding(
                    gateway_tool="workflow.submit",
                    namespace="workflow",
                    downstream_service="mission_control",
                    downstream_tool="submit_workflow",
                ),
                arguments={
                    "workflow_yaml": WORKFLOW_YAML_FIXTURE,
                    "idempotency_key": WORKFLOW_IDEMPOTENCY_KEY,
                },
                base_url="https://mission.example",
                authorization=None,
                connect_timeout_seconds=0.2,
                read_timeout_seconds=0.2,
                require_authorization=False,
                client_factory=BoomClient,
                extra_secrets=(
                    WORKFLOW_YAML_FIXTURE,
                    WORKFLOW_IDEMPOTENCY_KEY,
                    TEST_SECRET_VALUE_WF_GW,
                    TEST_GATEWAY_OAUTH_TOKEN,
                ),
            )
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failure_stage"], STAGE_TIMEOUT)
        blob = json.dumps(payload)
        self.assertNotIn(WORKFLOW_YAML_FIXTURE, blob)
        self.assertNotIn(TEST_SECRET_VALUE_WF_GW, blob)
        self.assertNotIn(WORKFLOW_IDEMPOTENCY_KEY, blob)
        self.assertNotIn(TEST_GATEWAY_OAUTH_TOKEN, blob)
        self.assertEqual(payload["request_id"], "req-workflow-1")
        self.assertEqual(payload["correlation_id"], "corr-workflow-1")

        collector = self._collect_tools("workflow.submit")
        with mock.patch(
            "hal_legalai_gateway.mcp_server._require_gateway_principal",
            return_value="nhpcorp35",
        ), mock.patch(
            "hal_legalai_gateway.mcp_server.forward_mcp_tool",
            new_callable=mock.AsyncMock,
            return_value=payload,
        ):
            wrapped = asyncio.run(
                collector["workflow.submit"](
                    WORKFLOW_YAML_FIXTURE,
                    idempotency_key=WORKFLOW_IDEMPOTENCY_KEY,
                )
            )
        wrapped_blob = json.dumps(wrapped)
        self.assertEqual(
            wrapped["error"]["message"], "downstream tool returned an error"
        )
        self.assertNotIn("Bearer", wrapped_blob)
        self.assertNotIn(WORKFLOW_YAML_FIXTURE, wrapped_blob)
        self.assertNotIn(TEST_SECRET_VALUE_WF_GW, wrapped_blob)

    def test_mission_control_authorization_resolver_stays_isolated(self) -> None:
        self.assertIsNone(
            resolve_authorization_for_service(
                downstream_service="mission_control",
                bridge_authorization=f"Bearer {TEST_BRIDGE_SERVICE_TOKEN}",
            )
        )
        self.assertEqual(
            resolve_authorization_for_service(
                downstream_service="bridge",
                bridge_authorization=f"Bearer {TEST_BRIDGE_SERVICE_TOKEN}",
            ),
            f"Bearer {TEST_BRIDGE_SERVICE_TOKEN}",
        )


INBOUND_OAUTH_TOKEN = "inbound-gateway-oauth-session-token"
INBOUND_SESSION_COOKIE = "mcp_sid=inbound-session-cookie"
INBOUND_X_API_KEY = "inbound-x-api-key-value"
INBOUND_GITHUB_TOKEN = "inbound-x-github-token-value"
INBOUND_RUNNER_SESSION = "inbound-x-runner-session-value"
_SAFE_OUTBOUND_FACTORY_HEADER_NAMES = frozenset(
    {"accept", "x-request-id", "x-correlation-id"}
)
_INBOUND_CREDENTIAL_ALIASES = (
    INBOUND_OAUTH_TOKEN,
    INBOUND_SESSION_COOKIE,
    INBOUND_X_API_KEY,
    INBOUND_GITHUB_TOKEN,
    INBOUND_RUNNER_SESSION,
)


def _inbound_oauth_request() -> Request:
    """Starlette request carrying inbound OAuth/session credential headers."""
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "query_string": b"",
            "headers": [
                (b"authorization", f"Bearer {INBOUND_OAUTH_TOKEN}".encode()),
                (b"cookie", INBOUND_SESSION_COOKIE.encode()),
                (b"x-api-key", INBOUND_X_API_KEY.encode()),
                (b"x-github-token", INBOUND_GITHUB_TOKEN.encode()),
                (b"x-runner-session", INBOUND_RUNNER_SESSION.encode()),
                (b"host", b"gateway.example"),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("gateway.example", 443),
        }
    )


class OutboundHeaderIsolationTests(unittest.TestCase):
    """Live Streamable HTTP construction must not inherit inbound credentials."""

    def setUp(self) -> None:
        self.settings = load_settings(
            environ={
                **REQUIRED_SECRETS,
                "GATEWAY_BRIDGE_URL": "https://bridge.example",
                "GATEWAY_STORAGE_URL": "https://storage.example",
                "GATEWAY_MISSION_CONTROL_URL": "https://mission.example",
                "GATEWAY_ARTIFACTS_URL": "https://artifacts.example",
            },
            registry=load_registry(REGISTRY_PATH),
        )
        self.tokens = bind_request_ids(
            request_id="req-isolate-1",
            correlation_id="corr-isolate-1",
        )

    def tearDown(self) -> None:
        reset_request_ids(self.tokens)

    def _collect_tools(self, *gateway_tools: str) -> dict[str, Any]:
        collector: dict[str, Any] = {}

        class _Mcp:
            def tool(self, *args: Any, **kwargs: Any):
                def decorator(fn: Any) -> Any:
                    name = kwargs.get("name") or (args[0] if args else None)
                    if name in gateway_tools:
                        collector[name] = fn
                    return fn

                return decorator

        register_forwarding_tools(
            _Mcp(),  # type: ignore[arg-type]
            self.settings,
            bindings_from_registry(self.settings.registry),
        )
        return collector

    def _capturing_httpx_factory(self) -> tuple[
        Callable[..., httpx.AsyncClient],
        list[httpx.Request],
        list[dict[str, str]],
    ]:
        captured_requests: list[httpx.Request] = []
        captured_factory_headers: list[dict[str, str]] = []

        def factory(**kwargs: Any) -> httpx.AsyncClient:
            raw = kwargs.get("headers") or {}
            captured_factory_headers.append(
                {str(name): str(value) for name, value in dict(raw).items()}
            )

            def handler(request: httpx.Request) -> httpx.Response:
                captured_requests.append(request)
                return httpx.Response(
                    400,
                    json={"error": "captured"},
                    headers={"Content-Type": "application/json"},
                )

            filtered = dict(kwargs)
            filtered.pop("transport", None)
            return httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                **filtered,
            )

        return factory, captured_requests, captured_factory_headers

    def _assert_inbound_headers_reachable(self, inbound: dict[str, str]) -> None:
        self.assertEqual(
            inbound.get("authorization"),
            f"Bearer {INBOUND_OAUTH_TOKEN}",
        )
        self.assertEqual(inbound.get("cookie"), INBOUND_SESSION_COOKIE)
        self.assertEqual(inbound.get("x-api-key"), INBOUND_X_API_KEY)
        self.assertEqual(inbound.get("x-github-token"), INBOUND_GITHUB_TOKEN)
        self.assertEqual(inbound.get("x-runner-session"), INBOUND_RUNNER_SESSION)

    def _assert_credential_aliases_absent(self, blob: str, names: set[str]) -> None:
        for alias in _INBOUND_CREDENTIAL_ALIASES:
            self.assertNotIn(alias, blob)
        self.assertNotIn("cookie", names)
        self.assertNotIn("set-cookie", names)
        self.assertNotIn("x-api-key", names)
        self.assertNotIn("x-github-token", names)
        self.assertNotIn("x-runner-session", names)

    def _assert_factory_headers_allowlisted(
        self,
        factory_headers: dict[str, str],
    ) -> None:
        names = {name.lower() for name in factory_headers}
        self.assertTrue(
            names <= _SAFE_OUTBOUND_FACTORY_HEADER_NAMES,
            msg=(
                f"factory header names {sorted(names)} are not a subset of "
                f"{sorted(_SAFE_OUTBOUND_FACTORY_HEADER_NAMES)}"
            ),
        )
        values = " ".join(str(value) for value in factory_headers.values())
        self._assert_credential_aliases_absent(values, names)
        self.assertNotIn("authorization", names)

    def _assert_inbound_credentials_absent(
        self,
        request: httpx.Request,
        *,
        factory_headers: dict[str, str] | None = None,
    ) -> None:
        blob = " ".join(str(value) for value in request.headers.values())
        names = {name.lower() for name in request.headers.keys()}
        self._assert_credential_aliases_absent(blob, names)
        authorization = request.headers.get("authorization") or ""
        self.assertNotIn(INBOUND_OAUTH_TOKEN, authorization)
        if factory_headers is not None:
            self._assert_factory_headers_allowlisted(factory_headers)

    def test_workflow_submit_outbound_strips_inbound_oauth_session_headers(
        self,
    ) -> None:
        collector = self._collect_tools("workflow.submit")
        factory, captured_requests, captured_factory_headers = (
            self._capturing_httpx_factory()
        )
        real_forward = forward_mcp_tool

        async def forward_with_capture(*args: Any, **kwargs: Any) -> dict[str, Any]:
            kwargs["httpx_client_factory"] = factory
            kwargs["connect_timeout_seconds"] = 0.5
            kwargs["read_timeout_seconds"] = 0.5
            return await real_forward(*args, **kwargs)

        async def _run() -> dict[str, Any]:
            with set_http_request(_inbound_oauth_request()):
                inbound = get_http_headers()
                self._assert_inbound_headers_reachable(inbound)
                with mock.patch(
                    "hal_legalai_gateway.mcp_server._require_gateway_principal",
                    return_value="nhpcorp35",
                ), mock.patch(
                    "hal_legalai_gateway.mcp_server.forward_mcp_tool",
                    side_effect=forward_with_capture,
                ):
                    return await collector["workflow.submit"](WORKFLOW_YAML_FIXTURE)

        asyncio.run(_run())
        self.assertTrue(captured_requests)
        self.assertTrue(captured_factory_headers)
        for factory_headers in captured_factory_headers:
            self._assert_factory_headers_allowlisted(factory_headers)
            self._assert_inbound_credentials_absent(
                captured_requests[0], factory_headers=factory_headers
            )
        for request in captured_requests:
            self._assert_inbound_credentials_absent(request)
            names = {name.lower() for name in request.headers.keys()}
            self.assertNotIn("authorization", names)
            self.assertEqual(request.headers.get("x-request-id"), "req-isolate-1")
            self.assertEqual(
                request.headers.get("x-correlation-id"), "corr-isolate-1"
            )
        blob = json.dumps(
            [dict(request.headers) for request in captured_requests]
        )
        for alias in _INBOUND_CREDENTIAL_ALIASES:
            self.assertNotIn(alias, blob)
        self.assertNotIn(TEST_BRIDGE_SERVICE_TOKEN, blob)
        self.assertNotIn(TEST_GATEWAY_OAUTH_TOKEN, blob)

    def test_bridge_outbound_keeps_only_service_bearer(self) -> None:
        factory, captured_requests, captured_factory_headers = (
            self._capturing_httpx_factory()
        )

        async def _run() -> dict[str, Any]:
            with set_http_request(_inbound_oauth_request()):
                inbound = get_http_headers()
                self._assert_inbound_headers_reachable(inbound)
                return await forward_mcp_tool(
                    binding=ToolBinding(
                        gateway_tool="storage.list_inventory",
                        namespace="storage",
                        downstream_service="storage",
                        downstream_tool="list_case00_storage",
                    ),
                    arguments={"category": "all", "max_keys": 5},
                    base_url="https://bridge.example",
                    authorization=f"Bearer {TEST_BRIDGE_SERVICE_TOKEN}",
                    connect_timeout_seconds=0.5,
                    read_timeout_seconds=0.5,
                    require_authorization=True,
                    httpx_client_factory=factory,
                    extra_secrets=(
                        TEST_BRIDGE_SERVICE_TOKEN,
                        INBOUND_OAUTH_TOKEN,
                        INBOUND_X_API_KEY,
                        INBOUND_GITHUB_TOKEN,
                        INBOUND_RUNNER_SESSION,
                    ),
                )

        asyncio.run(_run())
        self.assertTrue(captured_requests)
        self.assertTrue(captured_factory_headers)
        for request in captured_requests:
            self._assert_inbound_credentials_absent(request)
            self.assertEqual(
                request.headers.get("authorization"),
                f"Bearer {TEST_BRIDGE_SERVICE_TOKEN}",
            )
            names = {name.lower() for name in request.headers.keys()}
            self.assertIn("authorization", names)
            self.assertNotIn("cookie", names)
            self.assertNotIn("x-api-key", names)
            self.assertNotIn("x-github-token", names)
            self.assertNotIn("x-runner-session", names)
        for factory_headers in captured_factory_headers:
            self._assert_factory_headers_allowlisted(factory_headers)
        blob = " ".join(
            " ".join(str(value) for value in request.headers.values())
            for request in captured_requests
        )
        for alias in _INBOUND_CREDENTIAL_ALIASES:
            self.assertNotIn(alias, blob)


if __name__ == "__main__":
    unittest.main()
