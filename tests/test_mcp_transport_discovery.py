"""Transport-level MCP tool discovery tests (Railway HTTP configuration)."""

from __future__ import annotations

import os
import socket
import threading
import time
import unittest
from typing import Any

# Settings are read at import time by mcp_connector.server.
os.environ.setdefault("MISSION_CONTROL_URL", "http://mission-control.test")
os.environ.setdefault("MISSION_CONTROL_API_KEY", "mc_test_key")

import anyio
import httpx
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from mcp_connector import server as mcp_server


EXPECTED_TOOLS = list(mcp_server.EXPECTED_TOOL_NAMES)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _McpHttpServer:
    """Serve ``create_http_app()`` on a local port (same routes as Railway)."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(
            uvicorn.Config(
                mcp_server.create_http_app(),
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
            )
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                # 406 is expected without Accept: text/event-stream; any HTTP
                # response means the listener is up.
                httpx.get(f"{self.base_url}/mcp", timeout=0.2)
                return
            except httpx.ConnectError:
                time.sleep(0.05)
            except httpx.HTTPError:
                return
        raise RuntimeError("MCP HTTP test server failed to start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5.0)


async def _list_tools_streamable(url: str) -> list[str]:
    async with streamable_http_client(url) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return [tool.name for tool in result.tools]


async def _list_tools_sse(url: str) -> list[str]:
    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return [tool.name for tool in result.tools]


def _jsonrpc_tools_list(base_url: str) -> list[str]:
    """Exercise initialize + tools/list over Streamable HTTP JSON responses."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    init: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mission-control-tests", "version": "0"},
        },
    }
    with httpx.Client(timeout=10.0) as client:
        response = client.post(f"{base_url}/mcp", json=init, headers=headers)
        response.raise_for_status()
        session_id = response.headers.get("mcp-session-id")
        assert session_id, "missing mcp-session-id from initialize"
        session_headers = dict(headers)
        session_headers["mcp-session-id"] = session_id
        note = client.post(
            f"{base_url}/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=session_headers,
        )
        assert note.status_code in {200, 202}, note.text
        listed = client.post(
            f"{base_url}/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=session_headers,
        )
        listed.raise_for_status()
        payload = listed.json()
        return [tool["name"] for tool in payload["result"]["tools"]]


class TestMcpTransportDiscovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _McpHttpServer()
        cls.server.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def test_create_http_app_exposes_mcp_and_sse_routes(self) -> None:
        app = mcp_server.create_http_app()
        paths = {getattr(route, "path", None) for route in app.routes}
        self.assertIn("/mcp", paths)
        self.assertIn("/sse", paths)

    def test_streamable_http_jsonrpc_lists_expected_tools(self) -> None:
        names = _jsonrpc_tools_list(self.server.base_url)
        self.assertEqual(names, EXPECTED_TOOLS)

    def test_streamable_http_client_lists_expected_tools(self) -> None:
        names = anyio.run(_list_tools_streamable, f"{self.server.base_url}/mcp")
        self.assertEqual(names, EXPECTED_TOOLS)

    def test_sse_client_lists_expected_tools(self) -> None:
        names = anyio.run(_list_tools_sse, f"{self.server.base_url}/sse")
        self.assertEqual(names, EXPECTED_TOOLS)

    def test_sse_path_is_reachable(self) -> None:
        # ChatGPT previously pointed at /sse while only /mcp was mounted.
        with httpx.stream(
            "GET",
            f"{self.server.base_url}/sse",
            headers={"Accept": "text/event-stream"},
            timeout=2.0,
        ) as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers.get("content-type", ""))


if __name__ == "__main__":
    unittest.main()
