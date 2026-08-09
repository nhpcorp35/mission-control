"""Tests for Bridge two-surface auth: public OAuth /mcp + service /mcp/service."""

from __future__ import annotations

import asyncio
import io
import logging
import unittest
from contextlib import redirect_stderr, redirect_stdout

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, AuthProvider
from starlette.routing import Route
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from github_actions_bridge.service_auth import (
    DEFAULT_PUBLIC_MCP_PATH,
    DEFAULT_SERVICE_MCP_PATH,
    SERVICE_CLIENT_ID,
    FailClosedTokenVerifier,
    PathAwareBearerBackend,
    ServiceTokenVerifier,
    build_service_auth_provider,
    compose_dual_mcp_http_app,
    is_service_access_token,
    normalize_bearer_token,
)

SERVICE_TOKEN = "test-bridge-service-token-abcdef0123456789"
OAUTH_TOKEN = "test-github-oauth-user-token"
INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "bridge-service-auth-tests", "version": "0"},
    },
}
JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


class _FakeOAuth(AuthProvider):
    """Stand-in for GitHubProvider: verifies one token and exposes discovery."""

    def __init__(self, *, accept: str = OAUTH_TOKEN) -> None:
        super().__init__(base_url="http://bridge.test", required_scopes=["user"])
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
        async def meta(_request):
            return JSONResponse(
                {
                    "issuer": "http://bridge.test",
                    "mcp_path": mcp_path,
                    "oauth": True,
                }
            )

        return [
            Route(
                "/.well-known/oauth-authorization-server",
                endpoint=meta,
                methods=["GET"],
            )
        ]


def _build_test_bridge_app(
    *,
    service_token: str | None = SERVICE_TOKEN,
    include_storage_tool: bool = True,
):
    oauth = _FakeOAuth()
    service_auth = build_service_auth_provider(service_token)
    mcp = FastMCP("HAL GitHub Actions Bridge Test", auth=oauth)

    if include_storage_tool:

        @mcp.tool()
        def list_case00_storage(category: str = "all", max_keys: int = 200) -> dict:
            return {
                "ok": True,
                "category": category,
                "max_keys": max_keys,
                "prefix": "Benchmarks/Case-00-Triborough/",
                "objects": [],
                "count": 0,
                "truncated": False,
            }

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request):
        return JSONResponse({"ok": True, "service": "hal-github-actions-bridge"})

    return compose_dual_mcp_http_app(
        mcp,
        oauth_auth=oauth,
        service_auth=service_auth,
        public_mcp_path=DEFAULT_PUBLIC_MCP_PATH,
        service_mcp_path=DEFAULT_SERVICE_MCP_PATH,
        json_response=True,
    )


def _mcp_session(client: TestClient, path: str, authorization: str | None):
    headers = dict(JSON_HEADERS)
    if authorization is not None:
        headers["Authorization"] = authorization
    init = client.post(path, headers=headers, json=INIT_BODY)
    return init, headers


class ServiceAuthUnitTests(unittest.TestCase):
    def test_normalize_bearer_token(self) -> None:
        self.assertEqual(normalize_bearer_token("Bearer abc"), "abc")
        self.assertEqual(normalize_bearer_token("abc"), "abc")
        self.assertIsNone(normalize_bearer_token("  "))

    def test_valid_service_token(self) -> None:
        verifier = ServiceTokenVerifier("svc-secret")

        async def _run():
            return await verifier.verify_token("Bearer svc-secret")

        token = asyncio.run(_run())
        self.assertIsNotNone(token)
        assert token is not None
        self.assertEqual(token.client_id, SERVICE_CLIENT_ID)
        self.assertEqual(token.claims.get("token_use"), "service")
        self.assertIsNone(token.expires_at)
        self.assertTrue(is_service_access_token(token))

    def test_invalid_service_token(self) -> None:
        verifier = ServiceTokenVerifier("svc-secret")

        async def _run():
            return await verifier.verify_token("other")

        self.assertIsNone(asyncio.run(_run()))

    def test_build_service_auth_fail_closed_when_unset(self) -> None:
        provider = build_service_auth_provider(None)
        self.assertIsInstance(provider, FailClosedTokenVerifier)

        async def _run():
            return await provider.verify_token("anything")

        self.assertIsNone(asyncio.run(_run()))

    def test_build_service_auth_when_set(self) -> None:
        provider = build_service_auth_provider("svc-secret")
        self.assertIsInstance(provider, ServiceTokenVerifier)

    def test_path_aware_backend_routes_by_path(self) -> None:
        oauth = _FakeOAuth()
        service = ServiceTokenVerifier(SERVICE_TOKEN)
        backend = PathAwareBearerBackend(
            oauth=oauth,
            service=service,
            service_mcp_path=DEFAULT_SERVICE_MCP_PATH,
        )
        self.assertTrue(backend._is_service_path("/mcp/service"))
        self.assertFalse(backend._is_service_path("/mcp"))
        self.assertFalse(backend._is_service_path("/.well-known/oauth-authorization-server"))


class BridgeDualSurfaceHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _build_test_bridge_app()
        self._client_cm = TestClient(self.app)
        self.client = self._client_cm.__enter__()

    def tearDown(self) -> None:
        self._client_cm.__exit__(None, None, None)

    def test_public_oauth_discovery_preserved(self) -> None:
        response = self.client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("oauth"))
        self.assertEqual(payload.get("mcp_path"), DEFAULT_PUBLIC_MCP_PATH)

    def test_service_path_has_no_oauth_discovery_dependency(self) -> None:
        paths = {getattr(route, "path", None) for route in self.app.routes}
        self.assertIn(DEFAULT_PUBLIC_MCP_PATH, paths)
        self.assertIn(DEFAULT_SERVICE_MCP_PATH, paths)
        self.assertIn("/.well-known/oauth-authorization-server", paths)
        # Service surface must not add a second OAuth metadata route keyed to itself.
        well_known = [
            getattr(route, "path", None)
            for route in self.app.routes
            if str(getattr(route, "path", "")).startswith("/.well-known/")
        ]
        self.assertEqual(well_known, ["/.well-known/oauth-authorization-server"])

    def test_public_oauth_initialize(self) -> None:
        init, _ = _mcp_session(
            self.client,
            DEFAULT_PUBLIC_MCP_PATH,
            f"Bearer {OAUTH_TOKEN}",
        )
        self.assertEqual(init.status_code, 200)
        self.assertIn("result", init.json())

    def test_public_rejects_service_token(self) -> None:
        init, _ = _mcp_session(
            self.client,
            DEFAULT_PUBLIC_MCP_PATH,
            f"Bearer {SERVICE_TOKEN}",
        )
        self.assertEqual(init.status_code, 401)

    def test_service_valid_token_initialize_list_call(self) -> None:
        init, headers = _mcp_session(
            self.client,
            DEFAULT_SERVICE_MCP_PATH,
            f"Bearer {SERVICE_TOKEN}",
        )
        self.assertEqual(init.status_code, 200)
        session_id = init.headers.get("mcp-session-id")
        self.assertTrue(session_id)
        headers = dict(headers)
        headers["mcp-session-id"] = session_id
        headers["Authorization"] = f"Bearer {SERVICE_TOKEN}"

        note = self.client.post(
            DEFAULT_SERVICE_MCP_PATH,
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        self.assertIn(note.status_code, {200, 202})

        listed = self.client.post(
            DEFAULT_SERVICE_MCP_PATH,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        self.assertEqual(listed.status_code, 200)
        names = [tool["name"] for tool in listed.json()["result"]["tools"]]
        self.assertIn("list_case00_storage", names)

        called = self.client.post(
            DEFAULT_SERVICE_MCP_PATH,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "list_case00_storage",
                    "arguments": {"category": "all", "max_keys": 10},
                },
            },
        )
        self.assertEqual(called.status_code, 200)
        payload = called.json()
        self.assertIn("result", payload)
        blob = str(payload)
        self.assertNotIn(SERVICE_TOKEN, blob)

    def test_service_missing_token_401(self) -> None:
        init, _ = _mcp_session(self.client, DEFAULT_SERVICE_MCP_PATH, None)
        self.assertEqual(init.status_code, 401)
        self.assertNotIn(SERVICE_TOKEN, init.text)

    def test_service_invalid_token_401(self) -> None:
        init, _ = _mcp_session(
            self.client,
            DEFAULT_SERVICE_MCP_PATH,
            "Bearer definitely-not-valid",
        )
        self.assertEqual(init.status_code, 401)
        self.assertNotIn(SERVICE_TOKEN, init.text)
        self.assertNotIn("definitely-not-valid", init.json().get("error_description", ""))

    def test_service_rejects_oauth_user_token(self) -> None:
        init, _ = _mcp_session(
            self.client,
            DEFAULT_SERVICE_MCP_PATH,
            f"Bearer {OAUTH_TOKEN}",
        )
        self.assertEqual(init.status_code, 401)

    def test_health_open_and_redacts_nothing_secret(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(SERVICE_TOKEN, response.text)


class BridgeServiceFailClosedTests(unittest.TestCase):
    def test_unset_service_token_rejects_all(self) -> None:
        app = _build_test_bridge_app(service_token=None)
        with TestClient(app) as client:
            init, _ = _mcp_session(
                client,
                DEFAULT_SERVICE_MCP_PATH,
                f"Bearer {SERVICE_TOKEN}",
            )
            self.assertEqual(init.status_code, 401)


class GatewayStorageInventoryViaServicePathTests(unittest.TestCase):
    """Gateway storage.list_inventory must target the service-only MCP path."""

    def test_forward_uses_service_path_and_succeeds(self) -> None:
        from hal_legalai_gateway.forwarding import (
            ToolBinding,
            forward_mcp_tool,
            mcp_endpoint_url,
        )
        from github_actions_bridge.service_auth import DEFAULT_SERVICE_MCP_PATH

        app = _build_test_bridge_app()
        # Drive the ASGI app in-process via httpx ASGI transport through a
        # real Streamable HTTP client against /mcp/service.
        import httpx
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        binding = ToolBinding(
            gateway_tool="storage.list_inventory",
            namespace="storage",
            downstream_service="storage",
            downstream_tool="list_case00_storage",
        )

        log_buffer = io.StringIO()
        handler = logging.StreamHandler(log_buffer)
        logger = logging.getLogger("hal_legalai_gateway.forwarding")
        logger.addHandler(handler)
        try:
            with TestClient(app) as test_client:
                # Use the in-process ASGI app: wrap TestClient transport.
                def client_factory():
                    transport = StreamableHttpTransport(
                        f"http://test{DEFAULT_SERVICE_MCP_PATH}",
                        headers={
                            "Authorization": f"Bearer {SERVICE_TOKEN}",
                            "Accept": "application/json, text/event-stream",
                        },
                        httpx_client_factory=lambda **kwargs: httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=app),
                            base_url="http://test",
                            **kwargs,
                        ),
                    )
                    return Client(transport, timeout=10.0)

                result = asyncio.run(
                    forward_mcp_tool(
                        binding=binding,
                        arguments={"category": "all", "max_keys": 5},
                        base_url="http://test",
                        authorization=f"Bearer {SERVICE_TOKEN}",
                        connect_timeout_seconds=2.0,
                        read_timeout_seconds=5.0,
                        mcp_path=DEFAULT_SERVICE_MCP_PATH,
                        client_factory=client_factory,
                        extra_secrets=(SERVICE_TOKEN,),
                    )
                )
                _ = test_client  # keep pattern explicit
        finally:
            logger.removeHandler(handler)

        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["failure_stage"])
        self.assertEqual(
            mcp_endpoint_url("http://test", DEFAULT_SERVICE_MCP_PATH),
            f"http://test{DEFAULT_SERVICE_MCP_PATH}",
        )
        logs = log_buffer.getvalue()
        self.assertNotIn(SERVICE_TOKEN, logs)
        blob = str(result)
        self.assertNotIn(SERVICE_TOKEN, blob)

    def test_gateway_default_mcp_path_is_service_surface(self) -> None:
        from cryptography.fernet import Fernet

        from hal_legalai_gateway.config import (
            DEFAULT_MCP_PATH,
            DEFAULT_MISSION_CONTROL_MCP_PATH,
            load_settings,
        )
        from hal_legalai_gateway.registry import load_registry

        self.assertEqual(DEFAULT_MCP_PATH, "/mcp/service")
        self.assertEqual(DEFAULT_MISSION_CONTROL_MCP_PATH, "/mcp")
        env = {
            "GITHUB_OAUTH_CLIENT_ID": "id",
            "GITHUB_OAUTH_CLIENT_SECRET": "secret",
            "GATEWAY_PUBLIC_URL": "https://gateway.example",
            "JWT_SIGNING_KEY": "jwt",
            "REDIS_HOST": "127.0.0.1",
            "STORAGE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "GATEWAY_BRIDGE_AUTHORIZATION": f"Bearer {SERVICE_TOKEN}",
        }
        settings = load_settings(environ=env, registry=load_registry())
        self.assertEqual(settings.mcp_path, "/mcp/service")
        self.assertEqual(settings.mission_control_mcp_path, "/mcp")
        self.assertEqual(settings.mcp_path_for_service("bridge"), "/mcp/service")
        self.assertEqual(settings.mcp_path_for_service("storage"), "/mcp/service")
        self.assertEqual(settings.mcp_path_for_service("artifacts"), "/mcp/service")
        self.assertEqual(
            settings.mcp_path_for_service("mission_control"), "/mcp"
        )


class BridgeRequireAllowedUserTests(unittest.TestCase):
    """Exercise Bridge principal gate with service vs OAuth tokens."""

    def test_service_principal_and_oauth_login_compatibility(self) -> None:
        from github_actions_bridge import service_auth as sa

        service_token = AccessToken(
            token="svc",
            client_id=sa.SERVICE_CLIENT_ID,
            scopes=[],
            claims={"token_use": "service", "client_id": sa.SERVICE_CLIENT_ID},
        )
        oauth_token = AccessToken(
            token="oauth",
            client_id="github",
            scopes=[],
            claims={"login": "nhpcorp35"},
        )
        other_oauth = AccessToken(
            token="oauth2",
            client_id="github",
            scopes=[],
            claims={"login": "someone-else"},
        )

        self.assertTrue(sa.is_service_access_token(service_token))
        self.assertFalse(sa.is_service_access_token(oauth_token))
        self.assertFalse(sa.is_service_access_token(other_oauth))

        allowed = "nhpcorp35"

        def require_allowed(token: AccessToken | None) -> str:
            if sa.is_service_access_token(token):
                claims = token.claims if token is not None else {}
                client_id = (claims or {}).get("client_id") or (
                    token.client_id if token is not None else "service"
                )
                return f"service:{client_id}"
            login = token.claims.get("login") if token is not None else None
            if login != allowed:
                raise PermissionError("authenticated GitHub user is not authorized")
            return str(login)

        self.assertEqual(
            require_allowed(service_token), f"service:{sa.SERVICE_CLIENT_ID}"
        )
        self.assertEqual(require_allowed(oauth_token), "nhpcorp35")
        with self.assertRaises(PermissionError):
            require_allowed(other_oauth)


class CredentialRedactionTests(unittest.TestCase):
    def test_responses_and_logs_hide_service_token(self) -> None:
        app = _build_test_bridge_app()
        stream = io.StringIO()
        with redirect_stdout(stream), redirect_stderr(stream):
            with TestClient(app) as client:
                for auth in (None, "Bearer wrong", f"Bearer {SERVICE_TOKEN}"):
                    init, _ = _mcp_session(client, DEFAULT_SERVICE_MCP_PATH, auth)
                    self.assertNotIn(SERVICE_TOKEN, init.text)
        self.assertNotIn(SERVICE_TOKEN, stream.getvalue())


if __name__ == "__main__":
    unittest.main()
