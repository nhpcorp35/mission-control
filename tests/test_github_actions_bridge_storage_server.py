from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

from github_actions_bridge.storage_policy import CASE00_PREFIXES


def _ensure_storage_server_env() -> None:
    os.environ.setdefault("GITHUB_OAUTH_CLIENT_ID", "test-client-id")
    os.environ.setdefault("GITHUB_OAUTH_CLIENT_SECRET", "test-client-secret")
    os.environ.setdefault("REDIS_HOST", "127.0.0.1")
    os.environ.setdefault("REDIS_PORT", "6379")
    os.environ.setdefault(
        "STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    os.environ.setdefault("JWT_SIGNING_KEY", "test-jwt-signing-key")
    os.environ.setdefault("B2_ENDPOINT", "https://s3.example.test")
    os.environ.setdefault("B2_KEY_ID", "test-key-id")
    os.environ.setdefault("B2_APPLICATION_KEY", "test-app-key")
    os.environ.setdefault("B2_BUCKET", "legalai-corpus")
    os.environ.setdefault("ALLOWED_GITHUB_LOGIN", "nhpcorp35")


_ensure_storage_server_env()

from github_actions_bridge import storage_server  # noqa: E402


class _FakeAccessToken:
    def __init__(self, login: str) -> None:
        self.claims = {"login": login}


class StorageServerSurfaceTests(unittest.IsolatedAsyncioTestCase):
    def test_registers_exactly_two_case00_storage_tools(self) -> None:
        tool_names = sorted(storage_server.mcp._tool_manager._tools.keys())
        self.assertEqual(
            tool_names,
            sorted(storage_server.EXPECTED_TOOL_NAMES),
        )
        self.assertEqual(
            tool_names,
            ["archive_case00_attorney_feedback", "list_case00_storage"],
        )

    async def test_list_case00_storage_is_prefix_confined(self) -> None:
        fake_client = MagicMock()
        fake_client.list_objects_v2.return_value = {
            "Contents": [
                {
                    "Key": (
                        f"{CASE00_PREFIXES['attorney_reviews']}"
                        "review-20260802-abc/manifest.json"
                    ),
                    "Size": 12,
                    "ETag": '"etag-1"',
                    "LastModified": datetime(2026, 8, 2, tzinfo=timezone.utc),
                }
            ],
            "IsTruncated": False,
        }

        with (
            patch.object(
                storage_server,
                "get_access_token",
                return_value=_FakeAccessToken("nhpcorp35"),
            ),
            patch.object(storage_server, "_b2_client", return_value=fake_client),
        ):
            result = await storage_server.list_case00_storage.fn(
                category="attorney_reviews", max_keys=50
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["b2_bucket"], "legalai-corpus")
        self.assertEqual(result["prefix"], CASE00_PREFIXES["attorney_reviews"])
        self.assertEqual(result["count"], 1)
        self.assertTrue(
            result["objects"][0]["object_key"].startswith(
                CASE00_PREFIXES["attorney_reviews"]
            )
        )
        fake_client.list_objects_v2.assert_called_once_with(
            Bucket="legalai-corpus",
            Prefix=CASE00_PREFIXES["attorney_reviews"],
            MaxKeys=50,
        )

    async def test_archive_case00_attorney_feedback_is_verified_and_confined(
        self,
    ) -> None:
        stored: dict[str, dict[str, object]] = {}

        def put_object(**kwargs: object) -> None:
            key = kwargs["Key"]
            assert isinstance(key, str)
            body = kwargs["Body"]
            assert isinstance(body, (bytes, bytearray))
            metadata = kwargs["Metadata"]
            assert isinstance(metadata, dict)
            stored[key] = {
                "Body": bytes(body),
                "Metadata": dict(metadata),
                "ContentType": kwargs["ContentType"],
            }

        def head_object(*, Bucket: str, Key: str) -> dict[str, object]:
            self.assertEqual(Bucket, "legalai-corpus")
            item = stored[Key]
            body = item["Body"]
            assert isinstance(body, bytes)
            metadata = item["Metadata"]
            assert isinstance(metadata, dict)
            return {
                "ContentLength": len(body),
                "ETag": f'"etag-{len(body)}"',
                "Metadata": metadata,
            }

        fake_client = MagicMock()
        fake_client.put_object.side_effect = put_object
        fake_client.head_object.side_effect = head_object

        with (
            patch.object(
                storage_server,
                "get_access_token",
                return_value=_FakeAccessToken("nhpcorp35"),
            ),
            patch.object(storage_server, "_b2_client", return_value=fake_client),
        ):
            result = await storage_server.archive_case00_attorney_feedback.fn(
                evaluation_date="2026-08-02",
                original_packet_md="# Packet",
                feedback_email_md="# Feedback",
                structured_evaluation_json=json.dumps({"Q1": "incorrect"}),
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["b2_bucket"], "legalai-corpus")
        self.assertEqual(len(result["objects"]), 4)
        self.assertEqual(fake_client.put_object.call_count, 4)
        self.assertEqual(fake_client.head_object.call_count, 4)
        for item in result["objects"]:
            self.assertTrue(
                item["object_key"].startswith(CASE00_PREFIXES["attorney_reviews"])
            )
            self.assertIn(item["object_key"], stored)
            self.assertEqual(
                stored[item["object_key"]]["Metadata"]["sha256"], item["sha256"]
            )

        manifest_key = next(
            key for key in stored if key.endswith("Feedback-Preservation-Manifest.json")
        )
        manifest = json.loads(stored[manifest_key]["Body"])
        self.assertEqual(manifest["archive_id"], result["archive_id"])
        self.assertEqual(manifest["archived_by"], "nhpcorp35")
        self.assertEqual(manifest["case_id"], "Case-00-Triborough")

    async def test_rejects_unauthorized_github_login(self) -> None:
        with patch.object(
            storage_server,
            "get_access_token",
            return_value=_FakeAccessToken("intruder"),
        ):
            with self.assertRaises(PermissionError):
                await storage_server.list_case00_storage.fn()

    def test_health_route_identifies_storage_bridge(self) -> None:
        routes = [
            route
            for route in storage_server.mcp._additional_http_routes
            if getattr(route, "path", None) == "/health"
        ]
        self.assertEqual(len(routes), 1)

    async def test_health_payload_names_dedicated_service(self) -> None:
        response = await storage_server.health(MagicMock())
        payload = json.loads(response.body.decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["service"], "hal-legalai-storage-bridge")
        self.assertIn("time", payload)


if __name__ == "__main__":
    unittest.main()
