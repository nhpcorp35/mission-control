"""Regression: MCP SDK must remain FastMCP-compatible (mcp==1.29.0).

Railway previously resolved an unpinned ``mcp`` to 2.0.0, where
``mcp.server.fastmcp`` is unavailable and ``import mcp_connector.server``
raises ModuleNotFoundError. Keep the pin and the FastMCP import contract.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO_ROOT / "requirements.txt"
PINNED_MCP = "mcp==1.29.0"

# Settings are read at import time by mcp_connector.server.
os.environ.setdefault("MISSION_CONTROL_URL", "http://mission-control.test")
os.environ.setdefault("MISSION_CONTROL_API_KEY", "mc_test_key")


class TestMcpFastMcpCompat(unittest.TestCase):
    def test_requirements_pins_fastmcp_compatible_mcp(self) -> None:
        text = REQUIREMENTS.read_text(encoding="utf-8")
        pins = re.findall(r"(?m)^mcp(?:==\S+)?$", text)
        self.assertEqual(
            pins,
            [PINNED_MCP],
            f"requirements.txt must pin exactly {PINNED_MCP} (found {pins!r})",
        )

    def test_fastmcp_import_contract(self) -> None:
        # Fails if a future SDK upgrade drops mcp.server.fastmcp (as in mcp 2.x).
        from mcp.server.fastmcp import FastMCP

        self.assertTrue(callable(FastMCP))

    def test_mcp_connector_server_imports_with_fastmcp(self) -> None:
        from mcp.server.fastmcp import FastMCP
        from mcp_connector import server as mcp_server

        self.assertIsInstance(mcp_server.mcp, FastMCP)
        self.assertEqual(
            list(mcp_server.EXPECTED_TOOL_NAMES),
            [
                "submit_run",
                "submit_structured_run",
                "get_run",
                "list_run_notifications",
                "wait_for_run",
                "submit_and_wait",
                "run_repository_command",
            ],
        )

    def test_create_http_app_exposes_mcp_routes(self) -> None:
        """App factory still builds FastMCP HTTP routes (no live listen).

        Avoid starting the StreamableHTTP session manager here: it may only
        ``.run()`` once per process, and other MCP tests need that slot.
        """
        from mcp_connector import server as mcp_server

        app = mcp_server.create_http_app()
        paths = {getattr(route, "path", None) for route in app.routes}
        self.assertIn("/mcp", paths)
        self.assertIn("/sse", paths)


if __name__ == "__main__":
    unittest.main()
