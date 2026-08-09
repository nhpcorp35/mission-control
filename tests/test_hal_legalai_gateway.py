"""Focused tests for HAL LegalAI Gateway Phase 1."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from hal_legalai_gateway import config as gateway_config
from hal_legalai_gateway.config import (
    DEFAULT_HEALTH_TIMEOUT_SECONDS,
    load_settings,
    validate_http_base_url,
)
from hal_legalai_gateway.health import (
    STAGE_CONNECT,
    STAGE_HTTP,
    STAGE_TIMEOUT,
    STATUS_HEALTHY,
    STATUS_UNHEALTHY,
    aggregate_health,
    probe_downstream,
)
from hal_legalai_gateway.registry import (
    REQUIRED_NAMESPACES,
    REQUIRED_SERVICES,
    load_registry,
    parse_registry,
)
from hal_legalai_gateway.request_context import (
    CORRELATION_ID_HEADER,
    REQUEST_ID_HEADER,
    get_correlation_id,
    get_request_id,
)
from hal_legalai_gateway.server import app, reset_settings_for_tests

REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent
    / "hal_legalai_gateway"
    / "registry.json"
)


class RegistryTests(unittest.TestCase):
    def test_bundled_registry_loads_and_has_required_namespaces(self) -> None:
        registry = load_registry(REGISTRY_PATH)
        self.assertEqual(registry.version, 1)
        self.assertEqual(REQUIRED_NAMESPACES, set(registry.namespaces))
        self.assertEqual(REQUIRED_SERVICES, set(registry.services))
        self.assertEqual(registry.namespaces["case"].downstream_service, "bridge")
        self.assertEqual(
            registry.namespaces["storage"].downstream_service, "storage"
        )
        self.assertEqual(
            registry.namespaces["mission"].downstream_service, "mission_control"
        )
        self.assertIn("submit_case00_q1", registry.namespaces["case"].tools)
        self.assertIn(
            "list_case00_storage", registry.namespaces["storage"].tools
        )
        self.assertIn("submit_run", registry.namespaces["mission"].tools)

    def test_tool_routes_point_artifacts_independently(self) -> None:
        registry = load_registry(REGISTRY_PATH)
        self.assertEqual(
            registry.downstream_for_tool("get_case_artifact"), "artifacts"
        )
        self.assertEqual(registry.downstream_for_tool("get_artifacts"), "artifacts")
        self.assertEqual(
            registry.downstream_for_tool("submit_case00_q1"), "bridge"
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
            "RAILWAY_GIT_COMMIT_SHA": "abc123deadbeef",
            "GATEWAY_HEALTH_TIMEOUT_SECONDS": "2.5",
            "GATEWAY_BRIDGE_URL": "https://bridge.example",
            "GATEWAY_STORAGE_URL": "https://storage.example",
            "GATEWAY_MISSION_CONTROL_URL": "https://mission.example",
            "GATEWAY_ARTIFACTS_URL": "https://artifacts.example",
        }
        settings = load_settings(environ=env, registry=load_registry(REGISTRY_PATH))
        self.assertEqual(settings.deployed_commit_sha, "abc123deadbeef")
        self.assertEqual(settings.health_timeout_seconds, 2.5)
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

    def test_load_settings_rejects_invalid_timeout(self) -> None:
        with self.assertRaises(RuntimeError):
            load_settings(
                environ={"GATEWAY_HEALTH_TIMEOUT_SECONDS": "0"},
                registry=load_registry(REGISTRY_PATH),
            )

    def test_default_timeout_when_unset(self) -> None:
        settings = load_settings(
            environ={}, registry=load_registry(REGISTRY_PATH)
        )
        self.assertEqual(
            settings.health_timeout_seconds, DEFAULT_HEALTH_TIMEOUT_SECONDS
        )
        self.assertEqual(
            settings.deployed_commit_sha,
            gateway_config.UNKNOWN_DEPLOYED_COMMIT_SHA,
        )


class HealthIsolationTests(unittest.TestCase):
    def _settings(self, **url_map: str):
        env = {
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
        self.assertEqual(result["failure_stage"], STAGE_TIMEOUT)
        self.assertIsInstance(result["latency_ms"], float)

    def test_aggregate_isolates_single_downstream_failure(self) -> None:
        settings = self._settings(
            GATEWAY_BRIDGE_URL="https://bridge.example",
            GATEWAY_STORAGE_URL="https://storage.example",
            GATEWAY_MISSION_CONTROL_URL="https://mission.example",
            GATEWAY_ARTIFACTS_URL="https://artifacts.example",
        )

        async def _run_direct() -> dict:
            results = {}

            async def fake_probe(downstream, timeout_seconds, client=None):
                key = downstream.key
                if key == "mission_control":
                    results[key] = {
                        "key": key,
                        "service_id": downstream.service_id,
                        "display_name": downstream.display_name,
                        "base_url": downstream.base_url,
                        "health_url": downstream.health_url,
                        "base_url_env": downstream.base_url_env,
                        "status": STATUS_UNHEALTHY,
                        "latency_ms": 3.0,
                        "failure_stage": STAGE_CONNECT,
                        "http_status": None,
                        "error": "connection refused",
                    }
                elif key == "storage":
                    results[key] = {
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
                else:
                    results[key] = {
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
                return results[key]

            with mock.patch(
                "hal_legalai_gateway.health.probe_downstream",
                side_effect=fake_probe,
            ):
                return await aggregate_health(settings)

        payload = asyncio.run(_run_direct())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["deployed_commit_sha"], "sha-for-health-tests")
        self.assertEqual(
            payload["downstream"]["bridge"]["status"], STATUS_HEALTHY
        )
        self.assertEqual(
            payload["downstream"]["artifacts"]["status"], STATUS_HEALTHY
        )
        self.assertEqual(
            payload["downstream"]["mission_control"]["failure_stage"],
            STAGE_CONNECT,
        )
        self.assertEqual(
            payload["downstream"]["storage"]["failure_stage"], STAGE_HTTP
        )
        self.assertTrue(payload["capabilities"]["case"]["available"])
        self.assertFalse(payload["capabilities"]["storage"]["available"])
        self.assertFalse(payload["capabilities"]["mission"]["available"])
        # Case remains available even though mission/storage failed.
        self.assertNotEqual(
            payload["capabilities"]["case"]["available"],
            payload["capabilities"]["mission"]["available"],
        )


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = {
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
        # Context manager runs FastAPI lifespan (settings load).
        self._client_cm = TestClient(app)
        self.client = self._client_cm.__enter__()

    def tearDown(self) -> None:
        self._client_cm.__exit__(None, None, None)
        self._probe_patch.stop()
        self._env_patch.stop()
        reset_settings_for_tests()

    def test_health_reports_commit_sha_and_downstream_map(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["service"], "hal-legalai-gateway")
        self.assertEqual(
            payload["deployed_commit_sha"],
            "deadbeefcafebabe0123456789abcdef01234567",
        )
        self.assertIn("bridge", payload["downstream"])
        self.assertIn("storage", payload["downstream"])
        self.assertIn("mission_control", payload["downstream"])
        self.assertIn("artifacts", payload["downstream"])
        self.assertIn("case", payload["capabilities"])
        self.assertIn("storage", payload["capabilities"])
        self.assertIn("mission", payload["capabilities"])
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

    def test_registry_endpoint_exposes_namespaces(self) -> None:
        response = self.client.get("/registry")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload["namespaces"]), REQUIRED_NAMESPACES)
        self.assertEqual(
            payload["resolved_downstreams"]["bridge"]["base_url"],
            "https://bridge.example",
        )


class RequestContextUnitTests(unittest.TestCase):
    def test_contextvars_default_empty(self) -> None:
        self.assertIsNone(get_request_id())
        self.assertIsNone(get_correlation_id())


if __name__ == "__main__":
    unittest.main()
