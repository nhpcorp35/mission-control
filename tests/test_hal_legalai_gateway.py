"""Focused tests for HAL LegalAI Gateway Phase 2 (MCP routing)."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

import httpx
from fastapi.testclient import TestClient

from hal_legalai_gateway import config as gateway_config
from hal_legalai_gateway.auth import (
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
    DEFAULT_TOOL_BINDINGS,
    create_mcp_server,
    list_registered_tool_names,
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
        self.assertEqual(REQUIRED_NAMESPACES, set(registry.namespaces))
        self.assertEqual(REQUIRED_SERVICES, set(registry.services))
        self.assertEqual(registry.namespaces["case"].downstream_service, "bridge")
        self.assertEqual(
            registry.namespaces["storage"].downstream_service, "storage"
        )
        self.assertEqual(
            registry.namespaces["mission"].downstream_service, "mission_control"
        )
        self.assertIn("case.submit_case00_q1", registry.namespaces["case"].tools)
        self.assertIn(
            "storage.list_inventory", registry.namespaces["storage"].tools
        )
        self.assertIn("mission.submit", registry.namespaces["mission"].tools)
        present = {binding.gateway_tool for binding in registry.tool_bindings}
        self.assertTrue(REQUIRED_GATEWAY_TOOLS.issubset(present))

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
        for required in REQUIRED_GATEWAY_TOOLS:
            self.assertIn(required, names)
        self.assertIn("case.submit_case00_q1", names)
        self.assertIn("storage.archive_review_packet", names)


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
        self.assertEqual(set(payload["namespaces"]), REQUIRED_NAMESPACES)
        self.assertEqual(
            payload["resolved_downstreams"]["bridge"]["base_url"],
            "https://bridge.example",
        )
        tools = {item["tool"] for item in payload["tool_bindings"]}
        self.assertTrue(REQUIRED_GATEWAY_TOOLS.issubset(tools))

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


class RequestContextUnitTests(unittest.TestCase):
    def test_contextvars_default_empty(self) -> None:
        self.assertIsNone(get_request_id())
        self.assertIsNone(get_correlation_id())


if __name__ == "__main__":
    unittest.main()
