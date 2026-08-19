"""Contract tests for gateway-native case.resolve_commit."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from typing import Any
from unittest import mock

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from hal_legalai_gateway.auth import FixedTokenAuthProvider
from hal_legalai_gateway.case_resolve_commit import (
    COMMIT_SHA_RE,
    ERROR_INVALID_REF,
    ERROR_NOT_FOUND,
    ERROR_RESOLUTION_FAILED,
    ERROR_UNAUTHORIZED,
    FIXED_REPOSITORY,
    SUCCESS_KEYS,
    CaseResolveCommitContractError,
    failure_response,
    resolve_legalai_commit,
    validate_public_input,
    validate_public_success,
    validate_ref,
)
from hal_legalai_gateway.config import load_settings
from hal_legalai_gateway.mcp_server import (
    DEFAULT_TOOL_BINDINGS,
    create_mcp_server,
    list_registered_tool_names,
)
from hal_legalai_gateway.registry import load_registry
from hal_legalai_gateway.server import create_app, reset_settings_for_tests

REGISTRY_PATH = (
    __import__("pathlib").Path(__file__).resolve().parent.parent
    / "hal_legalai_gateway"
    / "registry.json"
)

TEST_GATEWAY_OAUTH_TOKEN = "test-gateway-oauth-token"
TEST_STORAGE_ENCRYPTION_KEY = Fernet.generate_key().decode()
REQUIRED_SECRETS = {
    "GITHUB_OAUTH_CLIENT_ID": "test-gateway-client-id",
    "GITHUB_OAUTH_CLIENT_SECRET": "test-gateway-client-secret",
    "GATEWAY_PUBLIC_URL": "https://gateway.example",
    "JWT_SIGNING_KEY": "test-jwt-signing-key-for-gateway",
    "REDIS_HOST": "127.0.0.1",
    "STORAGE_ENCRYPTION_KEY": TEST_STORAGE_ENCRYPTION_KEY,
    "GATEWAY_BRIDGE_AUTHORIZATION": "Bearer test-bridge-service-token",
    "ALLOWED_GITHUB_LOGIN": "nhpcorp35",
}

MAIN_SHA = "fd7b8e0385af0123456789abcdef0123456789ab"
VALID_SHA = "49f6881c08e7e4fdf76d8500d52a27d057c0804b"
MISSING_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class _MockTransport:
    def __init__(self, routes: dict[tuple[str, str], tuple[int, dict[str, Any] | None]]):
        self.routes = routes

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = (request.method.upper(), request.url.path)
        status, body = self.routes.get(key, (500, None))
        return httpx.Response(status, json=body)


def _client_factory(routes: dict[tuple[str, str], tuple[int, dict[str, Any] | None]]):
    transport = httpx.MockTransport(_MockTransport(routes).handler)

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport)

    return factory


class CaseResolveCommitContractTests(unittest.TestCase):
    def test_validate_ref_accepts_main_and_lowercase_sha(self) -> None:
        self.assertEqual(validate_ref("main"), "main")
        self.assertEqual(validate_ref(VALID_SHA), VALID_SHA)

    def test_validate_ref_rejects_invalid_values(self) -> None:
        for bad in ("", "master", "MAIN", VALID_SHA.upper(), "deadbeef", "feature/x"):
            with self.subTest(ref=bad):
                with self.assertRaises(CaseResolveCommitContractError):
                    validate_ref(bad)

    def test_validate_public_input_rejects_repository_and_remote_fields(self) -> None:
        with self.assertRaises(CaseResolveCommitContractError):
            validate_public_input({"ref": "main", "repository": "other/repo"})
        with self.assertRaises(CaseResolveCommitContractError):
            validate_public_input({"ref": "main", "owner": "nhpcorp35"})
        with self.assertRaises(CaseResolveCommitContractError):
            validate_public_input({"ref": "main", "url": "https://github.com/x/y"})
        with self.assertRaises(CaseResolveCommitContractError):
            validate_public_input({"ref": "main", "path": "/tmp/x"})

    def test_success_output_keys_are_exact(self) -> None:
        payload = validate_public_success(
            {
                "ok": True,
                "repository": FIXED_REPOSITORY,
                "ref": "main",
                "commit_sha": VALID_SHA,
            }
        )
        self.assertEqual(set(payload), SUCCESS_KEYS)
        self.assertEqual(payload["repository"], FIXED_REPOSITORY)
        self.assertTrue(COMMIT_SHA_RE.fullmatch(payload["commit_sha"]))

    def test_success_output_rejects_wrong_repository(self) -> None:
        with self.assertRaises(CaseResolveCommitContractError):
            validate_public_success(
                {
                    "ok": True,
                    "repository": "other/repo",
                    "ref": "main",
                    "commit_sha": VALID_SHA,
                }
            )

    def test_main_resolution_returns_fixed_repository_and_sha(self) -> None:
        routes = {
            ("GET", f"/repos/{FIXED_REPOSITORY}/commits/main"): (
                200,
                {"sha": MAIN_SHA},
            )
        }
        payload = asyncio.run(
            resolve_legalai_commit("main", client_factory=_client_factory(routes))
        )
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["repository"], FIXED_REPOSITORY)
        self.assertEqual(payload["ref"], "main")
        self.assertEqual(payload["commit_sha"], MAIN_SHA)
        self.assertEqual(set(payload), SUCCESS_KEYS)

    def test_valid_immutable_sha_is_verified(self) -> None:
        routes = {
            ("GET", f"/repos/{FIXED_REPOSITORY}/commits/{VALID_SHA}"): (
                200,
                {"sha": VALID_SHA},
            )
        }
        payload = asyncio.run(
            resolve_legalai_commit(VALID_SHA, client_factory=_client_factory(routes))
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["ref"], VALID_SHA)
        self.assertEqual(payload["commit_sha"], VALID_SHA)

    def test_invalid_ref_fail_closed(self) -> None:
        payload = asyncio.run(resolve_legalai_commit("not-a-ref"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], ERROR_INVALID_REF)

    def test_nonexistent_sha_maps_to_not_found(self) -> None:
        routes = {
            ("GET", f"/repos/{FIXED_REPOSITORY}/commits/{MISSING_SHA}"): (
                422,
                {"message": "No commit found for SHA"},
            )
        }
        payload = asyncio.run(
            resolve_legalai_commit(
                MISSING_SHA, client_factory=_client_factory(routes)
            )
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], ERROR_NOT_FOUND)
        self.assertNotIn("message", payload)
        self.assertNotIn("No commit found", json.dumps(payload))

    def test_resolution_failure_is_bounded(self) -> None:
        routes = {
            ("GET", f"/repos/{FIXED_REPOSITORY}/commits/main"): (
                500,
                {"message": "internal explosion with token ghp_secret"},
            )
        }
        payload = asyncio.run(
            resolve_legalai_commit("main", client_factory=_client_factory(routes))
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], ERROR_RESOLUTION_FAILED)
        blob = json.dumps(payload)
        self.assertNotIn("ghp_secret", blob)
        self.assertNotIn("internal explosion", blob)


class CaseResolveCommitRegistrationTests(unittest.TestCase):
    def test_registry_and_defaults_expose_case_resolve_commit(self) -> None:
        registry = load_registry(REGISTRY_PATH)
        self.assertIn("case.resolve_commit", registry.namespaces["case"].tools)
        binding = next(
            item
            for item in registry.tool_bindings
            if item.gateway_tool == "case.resolve_commit"
        )
        self.assertEqual(binding.namespace, "case")
        self.assertEqual(binding.downstream_tool, "resolve_commit")
        default = next(
            item
            for item in DEFAULT_TOOL_BINDINGS
            if item.gateway_tool == "case.resolve_commit"
        )
        self.assertEqual(default.gateway_tool, "case.resolve_commit")

    def test_create_mcp_server_registers_case_resolve_commit(self) -> None:
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
        auth = FixedTokenAuthProvider(
            TEST_GATEWAY_OAUTH_TOKEN,
            claims={"login": "nhpcorp35"},
        )
        mcp = create_mcp_server(settings, auth=auth)
        names = asyncio.run(list_registered_tool_names(mcp))
        self.assertIn("case.resolve_commit", names)


class CaseResolveCommitGatewayApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = {
            **REQUIRED_SECRETS,
            "GATEWAY_BRIDGE_URL": "https://bridge.example",
            "GATEWAY_STORAGE_URL": "https://storage.example",
            "GATEWAY_MISSION_CONTROL_URL": "https://mission.example",
            "GATEWAY_ARTIFACTS_URL": "https://artifacts.example",
        }
        self._env_patch = mock.patch.dict(os.environ, self.env, clear=False)
        self._env_patch.start()
        reset_settings_for_tests()
        self._app = create_app(
            auth_override=FixedTokenAuthProvider(
                TEST_GATEWAY_OAUTH_TOKEN,
                claims={"login": "nhpcorp35"},
            )
        )
        self._client_cm = TestClient(self._app)
        self.client = self._client_cm.__enter__()

    def tearDown(self) -> None:
        self._client_cm.__exit__(None, None, None)
        self._env_patch.stop()
        reset_settings_for_tests()

    def test_health_lists_case_resolve_commit(self) -> None:
        with mock.patch(
            "hal_legalai_gateway.health.probe_downstream",
            new_callable=mock.AsyncMock,
        ) as probe_mock:
            probe_mock.return_value = {
                "key": "bridge",
                "service_id": "hal-github-actions-bridge",
                "display_name": "HAL GitHub Actions Bridge",
                "base_url": "https://bridge.example",
                "health_url": "https://bridge.example/health",
                "base_url_env": "GATEWAY_BRIDGE_URL",
                "status": "healthy",
                "latency_ms": 1.0,
                "failure_stage": None,
                "http_status": 200,
                "error": None,
            }
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("case.resolve_commit", payload["registered_tools"])

    def test_mcp_tool_main_resolution(self) -> None:
        routes = {
            ("GET", f"/repos/{FIXED_REPOSITORY}/commits/main"): (
                200,
                {"sha": MAIN_SHA},
            )
        }
        with mock.patch(
            "hal_legalai_gateway.case_resolve_commit._github_get_commit",
            new_callable=mock.AsyncMock,
            return_value=(200, MAIN_SHA, None),
        ):
            response = self.client.post(
                "/mcp",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {TEST_GATEWAY_OAUTH_TOKEN}",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "case.resolve_commit",
                        "arguments": {"ref": "main"},
                    },
                },
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        result = payload["result"]["structuredContent"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["repository"], FIXED_REPOSITORY)
        self.assertEqual(result["ref"], "main")
        self.assertEqual(result["commit_sha"], MAIN_SHA)
        self.assertEqual(set(result), SUCCESS_KEYS)

    def test_unauthorized_call_is_fail_closed(self) -> None:
        response = self.client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "case.resolve_commit",
                    "arguments": {"ref": "main"},
                },
            },
        )
        self.assertIn(response.status_code, {401, 403})


if __name__ == "__main__":
    unittest.main()
