"""Tests for Bridge composite GitHub OAuth + service credential auth."""

from __future__ import annotations

import asyncio
import unittest
from fastmcp.server.auth import AccessToken, AuthProvider

from github_actions_bridge.service_auth import (
    SERVICE_CLIENT_ID,
    CompositeAuthProvider,
    ServiceTokenVerifier,
    build_bridge_auth_provider,
    is_service_access_token,
    normalize_bearer_token,
)


class _FakeOAuth(AuthProvider):
    def __init__(self, *, accept: str = "oauth-user-token") -> None:
        super().__init__()
        self.accept = accept
        self.routes_called_with: str | None = None

    async def verify_token(self, token: str) -> AccessToken | None:
        if token != self.accept:
            return None
        return AccessToken(
            token=token,
            client_id="github-oauth",
            scopes=["read"],
            claims={"login": "nhpcorp35"},
        )

    def get_routes(self, mcp_path: str | None = None) -> list:
        self.routes_called_with = mcp_path
        return ["oauth-route"]


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

    def test_composite_prefers_valid_service_token(self) -> None:
        oauth = _FakeOAuth()
        composite = CompositeAuthProvider(oauth, ServiceTokenVerifier("svc-secret"))

        async def _run():
            return await composite.verify_token("svc-secret")

        token = asyncio.run(_run())
        self.assertIsNotNone(token)
        assert token is not None
        self.assertEqual(token.claims.get("token_use"), "service")

    def test_composite_falls_back_to_github_oauth(self) -> None:
        oauth = _FakeOAuth()
        composite = CompositeAuthProvider(oauth, ServiceTokenVerifier("svc-secret"))

        async def _run():
            return await composite.verify_token("oauth-user-token")

        token = asyncio.run(_run())
        self.assertIsNotNone(token)
        assert token is not None
        self.assertEqual(token.claims.get("login"), "nhpcorp35")
        self.assertFalse(is_service_access_token(token))

    def test_composite_rejects_unknown_token(self) -> None:
        oauth = _FakeOAuth()
        composite = CompositeAuthProvider(oauth, ServiceTokenVerifier("svc-secret"))

        async def _run():
            return await composite.verify_token("nope")

        self.assertIsNone(asyncio.run(_run()))

    def test_composite_preserves_oauth_routes(self) -> None:
        oauth = _FakeOAuth()
        composite = CompositeAuthProvider(oauth, ServiceTokenVerifier("svc-secret"))
        routes = composite.get_routes(mcp_path="/mcp")
        self.assertEqual(routes, ["oauth-route"])
        self.assertEqual(oauth.routes_called_with, "/mcp")

    def test_build_bridge_auth_provider_oauth_only_when_unset(self) -> None:
        oauth = _FakeOAuth()
        provider = build_bridge_auth_provider(oauth, service_token=None)
        self.assertIs(provider, oauth)
        provider = build_bridge_auth_provider(oauth, service_token="  ")
        self.assertIs(provider, oauth)

    def test_build_bridge_auth_provider_composes_when_set(self) -> None:
        oauth = _FakeOAuth()
        provider = build_bridge_auth_provider(oauth, service_token="svc-secret")
        self.assertIsInstance(provider, CompositeAuthProvider)

        async def _run():
            return await provider.verify_token("svc-secret")

        token = asyncio.run(_run())
        self.assertTrue(is_service_access_token(token))


class BridgeRequireAllowedUserTests(unittest.TestCase):
    """Exercise Bridge principal gate with service vs OAuth tokens."""

    def test_service_principal_and_oauth_login_compatibility(self) -> None:
        # Import helpers without booting the full FastMCP Redis-backed provider.
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

        # Mirror Bridge server gate logic without importing server module.
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


if __name__ == "__main__":
    unittest.main()
