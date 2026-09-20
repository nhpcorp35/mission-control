import asyncio
import base64
import os
from pathlib import Path
import secrets
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("GITHUB_OAUTH_CLIENT_ID", "test-" + secrets.token_urlsafe(8))
os.environ.setdefault("GITHUB_OAUTH_CLIENT_SECRET", secrets.token_urlsafe(24))
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault(
    "STORAGE_ENCRYPTION_KEY",
    base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
)

from server import _operator_regenerate_draft  # noqa: E402


class OperatorRegenerationTests(unittest.TestCase):
    def test_preserves_original_reviewer(self):
        source_id = "draft-1-aaaaaaaaaaaa"
        original = {
            "question": "What issues are weakest?",
            "requested_by": "john@example.com",
        }
        with patch("server._validate_draft_case_id", return_value="case"), \
             patch("server._validate_draft_request_id", return_value=source_id), \
             patch("server._mcp_draft_reviewer", return_value="operator@example.com"), \
             patch("server._b2_client", return_value=object()), \
             patch("server._draft_request_entry", return_value=original), \
             patch("server._draft_status_entry", return_value={"status": "READY"}), \
             patch("server._existing_regenerated_successor", return_value=None), \
             patch("server._create_mcp_draft", return_value={
                 "ok": True,
                 "case_id": "case",
                 "request_id": "draft-2-bbbbbbbbbbbb",
                 "status": "QUEUED",
                 "reused": False,
             }) as create:
            result = asyncio.run(_operator_regenerate_draft("case", source_id))

        create.assert_awaited_once_with(
            "case",
            original["question"],
            "john@example.com",
            source_id,
            regeneration_operator="operator@example.com",
        )
        self.assertEqual(result["reviewer"], "john@example.com")
        self.assertEqual(result["regenerated_from_request_id"], source_id)

    def test_reuses_existing_successor(self):
        source_id = "draft-1-aaaaaaaaaaaa"
        existing = {
            "ok": True,
            "case_id": "case",
            "request_id": "draft-2-bbbbbbbbbbbb",
            "status": "READY",
            "reused": True,
            "reviewer": "john@example.com",
            "regenerated_from_request_id": source_id,
        }
        with patch("server._validate_draft_case_id", return_value="case"), \
             patch("server._validate_draft_request_id", return_value=source_id), \
             patch("server._mcp_draft_reviewer", return_value="operator@example.com"), \
             patch("server._b2_client", return_value=object()), \
             patch("server._draft_request_entry", return_value={
                 "question": "What issues are weakest?",
                 "requested_by": "john@example.com",
             }), \
             patch("server._draft_status_entry", return_value={"status": "READY"}), \
             patch("server._existing_regenerated_successor", return_value=existing), \
             patch("server._create_mcp_draft") as create:
            result = asyncio.run(_operator_regenerate_draft("case", source_id))

        self.assertEqual(result, existing)
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
