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
os.environ.setdefault("STORAGE_ENCRYPTION_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())

from server import _activity_poll  # noqa: E402


class FakeB2:
    def __init__(self):
        self.saved = None

    def get_object(self, **kwargs):
        error = {"Error": {"Code": "NoSuchKey"}}
        from botocore.exceptions import ClientError
        raise ClientError(error, "GetObject")

    def put_object(self, **kwargs):
        self.saved = kwargs


class ActivityPollTests(unittest.TestCase):
    def test_first_poll_baselines_without_replaying_history(self):
        client = FakeB2()
        historic = [{"event_id": "draft:case:draft-1-aaaaaaaaaaaa:READY:10", "event_type": "draft_status", "occurred_at": 10}]
        with patch("server._require_allowed_user"), patch("server._mcp_draft_reviewer", return_value="reviewer@example.com"), patch("server._b2_client", return_value=client), patch("server._activity_events", return_value=historic):
            result = asyncio.run(_activity_poll("watch", 50))
        self.assertTrue(result["initialized"])
        self.assertEqual(result["events"], [])
        self.assertIn(b"legalai-activity-cursor.v1", client.saved["Body"])


if __name__ == "__main__":
    unittest.main()
