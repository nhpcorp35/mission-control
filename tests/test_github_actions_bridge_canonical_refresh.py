"""Bridge public /mcp Refresh surface for the canonical HAL LegalAI catalog."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from cryptography.fernet import Fernet
from fastmcp.server.auth import AccessToken, AuthProvider
from mcp.server.auth.routes import create_protected_resource_routes
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

_BRIDGE_DIR = Path(__file__).resolve().parent.parent / "github_actions_bridge"
_BRIDGE_SERVER_ENV = {
    "GITHUB_OAUTH_CLIENT_ID": "test-client-id",
    "GITHUB_OAUTH_CLIENT_SECRET": "test-client-secret",
    "REDIS_HOST": "127.0.0.1",
    "REDIS_PORT": "6379",
    "STORAGE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "JWT_SIGNING_KEY": "test-jwt-signing-key-for-bridge",
}

OAUTH_TOKEN = "test-github-oauth-user-token"
SERVICE_TOKEN = "test-bridge-service-token-abcdef0123456789"
INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "bridge-canonical-refresh-tests", "version": "0"},
    },
}
JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
BRIDGE_ORIGIN = "https://hal-github-actions-bridge-production.up.railway.app"


def _import_bridge_server():
    for key, value in _BRIDGE_SERVER_ENV.items():
        os.environ.setdefault(key, value)
    bridge_dir = str(_BRIDGE_DIR)
    if bridge_dir not in sys.path:
        sys.path.insert(0, bridge_dir)
    import server as bridge_server

    return bridge_server


class _FakeOAuth(AuthProvider):
    def __init__(self, *, accept: str = OAUTH_TOKEN, base_url: str = BRIDGE_ORIGIN) -> None:
        super().__init__(base_url=base_url, required_scopes=["user"])
        self.accept = accept

    async def verify_token(self, token: str) -> AccessToken | None:
        if token != self.accept:
            return None
        return AccessToken(
            token=token,
            client_id="github-oauth",
            scopes=["user"],
            claims={"login": "nhpcorp35"},
        )

    def get_routes(self, mcp_path: str | None = None) -> list:
        self.set_mcp_path(mcp_path)
        resource_url = self._resource_url

        async def meta(_request):
            return JSONResponse(
                {
                    "issuer": str(self.base_url),
                    "mcp_path": mcp_path,
                    "oauth": True,
                }
            )

        routes: list = [
            Route(
                "/.well-known/oauth-authorization-server",
                endpoint=meta,
                methods=["GET"],
            )
        ]
        if resource_url is not None:
            routes.extend(
                create_protected_resource_routes(
                    resource_url=resource_url,
                    authorization_servers=[self.base_url],
                    scopes_supported=list(self.required_scopes or []),
                )
            )
        return routes


def _import_and_app(*, service_token: str | None = SERVICE_TOKEN):
    server = _import_bridge_server()
    app = server.create_http_app(
        oauth_auth=_FakeOAuth(),
        service_token=service_token,
        json_response=True,
    )
    return server, app


def _mcp_session(client: TestClient, path: str, authorization: str | None):
    headers = dict(JSON_HEADERS)
    if authorization is not None:
        headers["Authorization"] = authorization
    init = client.post(path, headers=headers, json=INIT_BODY)
    return init, headers


def _tools_list_names(client: TestClient, path: str, headers: dict[str, str]) -> list[str]:
    """Collect tool names from the actual JSON-RPC tools/list catalog."""
    names: list[str] = []
    cursor = None
    request_id = 2
    for _ in range(16):
        params: dict = {} if cursor is None else {"cursor": cursor}
        listed = client.post(
            path,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/list",
                "params": params,
            },
        )
        if listed.status_code != 200:
            raise AssertionError(
                f"tools/list returned HTTP {listed.status_code}: {listed.text}"
            )
        payload = listed.json()
        result = payload.get("result") or {}
        names.extend(tool["name"] for tool in result.get("tools") or [])
        cursor = result.get("nextCursor")
        if not cursor:
            return names
        request_id += 1
    raise AssertionError("tools/list pagination exceeded 16 pages")


class PluginRefreshUrlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _import_bridge_server()

    def test_plugin_refresh_url_is_bridge_public_mcp(self) -> None:
        from github_actions_bridge.service_auth import (
            PLUGIN_REFRESH_PROTECTED_RESOURCE_PATH,
            plugin_refresh_mcp_url,
            plugin_refresh_protected_resource_path,
        )

        self.assertEqual(
            plugin_refresh_mcp_url(BRIDGE_ORIGIN + "/"),
            f"{BRIDGE_ORIGIN}/mcp",
        )
        self.assertEqual(
            self.server.PLUGIN_REFRESH_MCP_URL,
            f"{BRIDGE_ORIGIN}/mcp",
        )
        self.assertEqual(
            plugin_refresh_protected_resource_path(),
            "/.well-known/oauth-protected-resource/mcp",
        )
        self.assertEqual(
            PLUGIN_REFRESH_PROTECTED_RESOURCE_PATH,
            "/.well-known/oauth-protected-resource/mcp",
        )


class CanonicalCatalogRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _import_bridge_server()

    def test_required_canonical_tools_cover_namespaces(self) -> None:
        required = self.server.REQUIRED_CANONICAL_CATALOG_TOOLS
        namespaces = {name.split(".", 1)[0] for name in required}
        self.assertEqual(namespaces, self.server.CANONICAL_CATALOG_NAMESPACES)
        for name in (
            "mission.submit",
            "workflow.submit",
            "workflow.status",
            "workflow.cancel",
            "case.submit",
            "case.get_artifact",
            "storage.list_inventory",
        ):
            self.assertIn(name, required)

    def test_plugin_instructions_advertise_canonical_workflow_tools(self) -> None:
        instructions = str(getattr(self.server.mcp, "instructions", "") or "")
        for name in ("workflow.submit", "workflow.cancel", "workflow.status"):
            self.assertIn(name, instructions)

    def test_registered_tools_include_canonical_and_legacy(self) -> None:
        names = asyncio.run(self.server.list_registered_tool_names())
        self.assertTrue(
            self.server.REQUIRED_PRODUCTION_TOOLS.issubset(names),
            msg=self.server.missing_required_production_tools(names),
        )
        self.assertTrue(
            self.server.REQUIRED_CANONICAL_CATALOG_TOOLS.issubset(names),
            msg=self.server.missing_required_canonical_catalog_tools(names),
        )
        asyncio.run(self.server.validate_required_production_tools())

    def test_case_and_storage_aliases_reuse_local_implementations(self) -> None:
        tools = asyncio.run(self.server.mcp.get_tools())
        self.assertIs(tools["case.submit"].fn, tools["submit_case00"].fn)
        self.assertIs(tools["storage.list_inventory"].fn, tools["list_case00_storage"].fn)
        self.assertIs(tools["case.get_artifact"].fn, tools["get_case_artifact"].fn)
        self.assertIsNot(tools["mission.submit"].fn, tools["submit_run"].fn)
        self.assertIsNot(tools["mission.status"].fn, tools["get_run"].fn)

    def test_health_exposes_refresh_url_and_canonical_tools(self) -> None:
        response = asyncio.run(self.server.health(mock.Mock()))
        payload = response.body.decode("utf-8")
        import json

        body = json.loads(payload)
        self.assertEqual(body["service"], "hal-github-actions-bridge")
        self.assertEqual(body["catalog_identity"], "HAL LegalAI Gateway")
        self.assertTrue(str(body["plugin_refresh_mcp_url"]).endswith("/mcp"))
        tools = set(body["registered_tools"])
        self.assertTrue(self.server.REQUIRED_CANONICAL_CATALOG_TOOLS.issubset(tools))
        self.assertIn("submit_run", tools)
        self.assertIn("list_case00_storage", tools)


class CanonicalForwardTargetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _import_bridge_server()

    def tearDown(self) -> None:
        self.server.reset_canonical_forward_hooks()

    def test_prefers_gateway_url_when_not_self(self) -> None:
        env = {
            self.server.HAL_LEGALAI_GATEWAY_URL_ENV: "https://hal-legalai-gateway.example",
            self.server.BRIDGE_SERVICE_TOKEN_ENV: SERVICE_TOKEN,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            target = self.server.resolved_canonical_forward_target()
        self.assertEqual(target["kind"], "gateway")
        self.assertEqual(target["base_url"], "https://hal-legalai-gateway.example")
        self.assertEqual(target["mcp_path"], "/mcp")
        self.assertTrue(target["use_canonical_names"])
        self.assertTrue(target["require_authorization"])

    def test_self_gateway_url_falls_back_to_mission_control(self) -> None:
        env = {self.server.HAL_LEGALAI_GATEWAY_URL_ENV: BRIDGE_ORIGIN}
        with mock.patch.dict(os.environ, env, clear=False):
            target = self.server.resolved_canonical_forward_target()
        self.assertEqual(target["kind"], "mission_control")
        self.assertFalse(target["use_canonical_names"])
        self.assertFalse(target["require_authorization"])
        self.assertEqual(
            target["base_url"],
            self.server.DEFAULT_MISSION_CONTROL_MCP_URL,
        )

    def test_forward_fail_closed_auth_when_gateway_token_missing(self) -> None:
        self.server._canonical_forward_test_hooks["target"] = {
            "kind": "gateway",
            "base_url": "https://gateway.example",
            "mcp_path": "/mcp",
            "use_canonical_names": True,
            "require_authorization": True,
        }
        env = os.environ.copy()
        env.pop(self.server.BRIDGE_SERVICE_TOKEN_ENV, None)
        with mock.patch.dict(os.environ, env, clear=True):
            result = asyncio.run(
                self.server.forward_canonical_catalog_tool(
                    "mission.submit",
                    "submit_run",
                    {"mission_yaml": "version: '1.0'\n"},
                )
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_stage"], "auth")
        self.assertNotIn(SERVICE_TOKEN, str(result))

    def test_forward_uses_canonical_name_on_gateway_and_redacts_secrets(self) -> None:
        calls: list[tuple[str, dict]] = []

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def call_tool(self, name, arguments, raise_on_error=False):
                calls.append((name, dict(arguments)))
                raise RuntimeError(f"unauthorized bearer {SERVICE_TOKEN}")

        self.server._canonical_forward_test_hooks["target"] = {
            "kind": "gateway",
            "base_url": "https://gateway.example",
            "mcp_path": "/mcp",
            "use_canonical_names": True,
            "require_authorization": True,
        }
        self.server._canonical_forward_test_hooks["client_factory"] = lambda: _Client()
        with mock.patch.dict(
            os.environ,
            {self.server.BRIDGE_SERVICE_TOKEN_ENV: SERVICE_TOKEN},
            clear=False,
        ):
            result = asyncio.run(
                self.server.forward_canonical_catalog_tool(
                    "workflow.submit",
                    "submit_workflow",
                    {"workflow_yaml": "secret-workflow-yaml"},
                )
            )
        self.assertEqual(calls[0][0], "workflow.submit")
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_stage"], "auth")
        blob = str(result)
        self.assertNotIn(SERVICE_TOKEN, blob)
        self.assertNotIn("secret-workflow-yaml", result["error"]["message"])

    def test_forward_mission_control_uses_downstream_tool_name(self) -> None:
        calls: list[str] = []

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def call_tool(self, name, arguments, raise_on_error=False):
                calls.append(name)
                return SimpleNamespace(
                    is_error=False,
                    data={"ok": True, "run_id": "run-1"},
                    structured_content=None,
                    content=None,
                )

        self.server._canonical_forward_test_hooks["target"] = {
            "kind": "mission_control",
            "base_url": "https://mission-control-mcp.example",
            "mcp_path": "/mcp",
            "use_canonical_names": False,
            "require_authorization": False,
        }
        self.server._canonical_forward_test_hooks["client_factory"] = lambda: _Client()
        result = asyncio.run(
            self.server.forward_canonical_catalog_tool(
                "mission.submit",
                "submit_run",
                {"mission_yaml": "title: test\n"},
            )
        )
        self.assertEqual(calls, ["submit_run"])
        self.assertTrue(result["ok"])
        self.assertIsNone(result["failure_stage"])
        self.assertEqual(result["result"]["run_id"], "run-1")

    def test_forward_fail_closed_on_connect(self) -> None:
        class _Client:
            async def __aenter__(self):
                raise httpx.ConnectError("connection refused")

            async def __aexit__(self, exc_type, exc, tb):
                return None

        self.server._canonical_forward_test_hooks["target"] = {
            "kind": "mission_control",
            "base_url": "https://mission-control-mcp.example",
            "mcp_path": "/mcp",
            "use_canonical_names": False,
            "require_authorization": False,
        }
        self.server._canonical_forward_test_hooks["client_factory"] = lambda: _Client()
        result = asyncio.run(
            self.server.forward_canonical_catalog_tool(
                "workflow.status",
                "get_workflow",
                {"workflow_id": "wf-1"},
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_stage"], "connect")

    def test_forward_cancel_uses_downstream_tool_on_mission_control(self) -> None:
        calls: list[tuple[str, dict]] = []

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def call_tool(self, name, arguments, raise_on_error=False):
                calls.append((name, dict(arguments)))
                return SimpleNamespace(
                    is_error=False,
                    data={"ok": True, "state": "cancelled"},
                    structured_content=None,
                    content=None,
                )

        self.server._canonical_forward_test_hooks["target"] = {
            "kind": "mission_control",
            "base_url": "https://mission-control-mcp.example",
            "mcp_path": "/mcp",
            "use_canonical_names": False,
            "require_authorization": False,
        }
        self.server._canonical_forward_test_hooks["client_factory"] = lambda: _Client()
        result = asyncio.run(
            self.server.forward_canonical_catalog_tool(
                "workflow.cancel",
                "cancel_workflow",
                {"workflow_id": "00000000-0000-4000-8000-000000000001"},
            )
        )
        self.assertEqual(calls, [
            (
                "cancel_workflow",
                {"workflow_id": "00000000-0000-4000-8000-000000000001"},
            )
        ])
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["state"], "cancelled")


class PublicMcpRefreshHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server, self.app = _import_and_app()
        self._client_cm = TestClient(self.app)
        self.client = self._client_cm.__enter__()

    def tearDown(self) -> None:
        self._client_cm.__exit__(None, None, None)
        self.server.reset_canonical_forward_hooks()

    def test_protected_resource_metadata_stays_on_public_mcp(self) -> None:
        response = self.client.get("/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get("resource_name"), "HAL LegalAI Gateway")
        self.assertEqual(str(payload.get("resource")).rstrip("/"), f"{BRIDGE_ORIGIN}/mcp")
        servers = payload.get("authorization_servers") or []
        self.assertTrue(servers)
        self.assertTrue(str(servers[0]).startswith(BRIDGE_ORIGIN))

    def test_oauth_authorization_server_discovery_preserved(self) -> None:
        response = self.client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("oauth"))

    def test_public_mcp_tools_list_includes_canonical_and_legacy(self) -> None:
        init, headers = _mcp_session(
            self.client,
            "/mcp",
            f"Bearer {OAUTH_TOKEN}",
        )
        self.assertEqual(init.status_code, 200)
        session_id = init.headers.get("mcp-session-id")
        self.assertTrue(session_id)
        headers = dict(headers)
        headers["mcp-session-id"] = session_id
        note = self.client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        self.assertIn(note.status_code, {200, 202})
        names = _tools_list_names(self.client, "/mcp", headers)
        for required in (
            "mission.submit",
            "workflow.submit",
            "workflow.status",
            "workflow.cancel",
            "case.submit",
            "case.get_artifact",
            "storage.list_inventory",
            "submit_run",
            "submit_case00",
            "list_case00_storage",
        ):
            self.assertIn(required, names, msg=required)
        namespaces = {name.split(".", 1)[0] for name in names if "." in name}
        self.assertTrue({"case", "storage", "mission", "workflow"}.issubset(namespaces))

    def test_public_mcp_tools_list_includes_canonical_workflow_tools(self) -> None:
        """Unnumbered plugin Refresh recaches public /mcp tools/list."""
        init, headers = _mcp_session(
            self.client,
            "/mcp",
            f"Bearer {OAUTH_TOKEN}",
        )
        self.assertEqual(init.status_code, 200)
        session_id = init.headers.get("mcp-session-id")
        self.assertTrue(session_id)
        headers = dict(headers)
        headers["mcp-session-id"] = session_id
        note = self.client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        self.assertIn(note.status_code, {200, 202})
        names = _tools_list_names(self.client, "/mcp", headers)
        for required in (
            "workflow.submit",
            "workflow.cancel",
            "workflow.status",
        ):
            self.assertIn(required, names, msg=required)

    def test_public_mcp_requires_auth(self) -> None:
        init, _ = _mcp_session(self.client, "/mcp", None)
        self.assertEqual(init.status_code, 401)

    def test_service_path_preserves_legacy_storage_tool(self) -> None:
        init, headers = _mcp_session(
            self.client,
            "/mcp/service",
            f"Bearer {SERVICE_TOKEN}",
        )
        self.assertEqual(init.status_code, 200)
        session_id = init.headers.get("mcp-session-id")
        headers = dict(headers)
        headers["mcp-session-id"] = session_id
        self.client.post(
            "/mcp/service",
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        listed = self.client.post(
            "/mcp/service",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        names = [tool["name"] for tool in listed.json()["result"]["tools"]]
        self.assertIn("list_case00_storage", names)
        self.assertIn("submit_run", names)


if __name__ == "__main__":
    unittest.main()
