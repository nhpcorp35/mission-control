from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet


_BRIDGE_DIR = Path(__file__).resolve().parent.parent / "github_actions_bridge"


def _bridge_server():
    os.environ.setdefault("GITHUB_OAUTH_CLIENT_ID", "test-client-id")
    os.environ.setdefault("GITHUB_OAUTH_CLIENT_SECRET", "test-client-secret")
    os.environ.setdefault("REDIS_HOST", "127.0.0.1")
    os.environ.setdefault("REDIS_PORT", "6379")
    os.environ.setdefault("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    os.environ.setdefault("JWT_SIGNING_KEY", "test-jwt-signing-key-for-bridge")
    if str(_BRIDGE_DIR) not in sys.path:
        sys.path.insert(0, str(_BRIDGE_DIR))
    import server

    return server


class RennickDocketSupplementTests(unittest.TestCase):
    @staticmethod
    def _supplement_pair(server):
        document_bytes = {name: f"contents for {name}".encode() for name in server.RENNICK_SUPPLEMENT_FILENAMES}
        archive_stream = io.BytesIO()
        with zipfile.ZipFile(archive_stream, "w") as archive:
            for name, payload in document_bytes.items():
                archive.writestr(name, payload)
        manifest = json.dumps(
            {
                "case_id": server.RENNICK_CASE_ID,
                "supplement_id": server.RENNICK_SUPPLEMENT_ID,
                "documents": [
                    {"filename": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
                    for name, payload in document_bytes.items()
                ],
            },
            sort_keys=True,
        ).encode()
        return archive_stream.getvalue(), manifest

    def test_upload_stores_and_head_verifies_only_the_fixed_three_document_supplement(self) -> None:
        server = _bridge_server()
        archive_payload, manifest = self._supplement_pair(server)
        stored: dict[str, dict[str, object]] = {}

        def put_object(**kwargs: object) -> dict[str, object]:
            stored[str(kwargs["Key"])] = {"payload": kwargs["Body"], "metadata": kwargs["Metadata"]}
            return {}

        def head_object(*, Bucket: str, Key: str) -> dict[str, object]:
            del Bucket
            record = stored.get(Key)
            if record is None:
                raise server.ClientError({"Error": {"Code": "404"}}, "HeadObject")
            return {"ContentLength": len(record["payload"]), "ETag": '"test-etag"', "Metadata": record["metadata"]}

        client = mock.Mock()
        client.put_object.side_effect = put_object
        client.head_object.side_effect = head_object
        with mock.patch.object(server, "_b2_client", return_value=client):
            result = server._upload_rennick_docket_supplement(archive_payload, manifest)

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["objects"]), 2)
        self.assertTrue(all("/supplements/" in item["object_key"] for item in result["objects"]))

    def test_direct_upload_prepare_then_complete_reads_pending_objects_and_cleans_them(self) -> None:
        server = _bridge_server()
        archive_payload, manifest_payload = self._supplement_pair(server)
        stored: dict[str, dict[str, object]] = {}
        os.environ[server.RENNICK_DIRECT_UPLOAD_ORIGIN_ENV] = "https://hal-legalai-gateway-production.up.railway.app"

        def head_object(*, Bucket: str, Key: str) -> dict[str, object]:
            del Bucket
            record = stored.get(Key)
            if record is None:
                raise server.ClientError({"Error": {"Code": "404"}}, "HeadObject")
            return {"ContentLength": len(record["payload"]), "ETag": '"test-etag"', "Metadata": record.get("metadata", {})}

        def put_object(**kwargs: object) -> dict[str, object]:
            stored[str(kwargs["Key"])] = {"payload": kwargs["Body"], "metadata": kwargs.get("Metadata", {})}
            return {}

        def get_object(*, Bucket: str, Key: str) -> dict[str, object]:
            del Bucket
            return {"Body": io.BytesIO(stored[Key]["payload"])}

        def delete_object(*, Bucket: str, Key: str) -> dict[str, object]:
            del Bucket
            stored.pop(Key, None)
            return {}

        client = mock.Mock()
        client.generate_presigned_url.side_effect = lambda operation, **kwargs: f"https://b2.example/{kwargs['Params']['Key']}"
        client.head_object.side_effect = head_object
        client.put_object.side_effect = put_object
        client.get_object.side_effect = get_object
        client.delete_object.side_effect = delete_object
        client.get_bucket_cors.return_value = {"CORSRules": []}
        with mock.patch.object(server, "_b2_client", return_value=client), mock.patch.object(server.uuid, "uuid4", return_value=mock.Mock(hex="a" * 32)):
            plan = server._prepare_rennick_direct_supplement_upload()
            self.assertEqual(len(plan["uploads"]), 3)
            self.assertEqual(client.put_object.call_count, 0)
            self.assertTrue(all("/.pending/" in item["object_key"] for item in plan["uploads"]))
            with zipfile.ZipFile(io.BytesIO(archive_payload)) as archive:
                for item in plan["uploads"]:
                    stored[item["object_key"]] = {"payload": archive.read(item["name"])}
            result = server._complete_rennick_direct_supplement_upload(plan["upload_id"])

        self.assertTrue(result["ok"])
        client.put_bucket_cors.assert_called_once_with(
            Bucket=server.B2_BUCKET,
            CORSConfiguration={"CORSRules": [{
                "AllowedOrigins": ["https://hal-legalai-gateway-production.up.railway.app"],
                "AllowedMethods": ["PUT"],
                "AllowedHeaders": ["content-type"],
                "MaxAgeSeconds": server.RENNICK_DIRECT_UPLOAD_TTL_SECONDS,
            }]},
        )
        self.assertTrue(all(item["object_key"] not in stored for item in plan["uploads"]))
        self.assertTrue(all("/.pending/" not in key for key in stored))
        self.assertEqual(len(result["objects"]), 2)

    def test_direct_completion_cleans_pending_objects_when_canonical_keys_already_exist(self) -> None:
        server = _bridge_server()
        upload_id = "b" * 32
        prefix = f"{server.RENNICK_DIRECT_UPLOAD_PREFIX}{server.RENNICK_SUPPLEMENT_ID}/{upload_id}/"
        archive_payload, manifest_payload = self._supplement_pair(server)
        canonical_archive = f"cases/{server.RENNICK_CASE_ID}/intake/supplements/{server.RENNICK_SUPPLEMENT_ID}/{server.RENNICK_SUPPLEMENT_ARCHIVE_FILENAME}"
        stored = {
            canonical_archive: {"payload": b"already canonical", "metadata": {}},
        }
        with zipfile.ZipFile(io.BytesIO(archive_payload)) as archive:
            for name in server.RENNICK_SUPPLEMENT_FILENAMES:
                stored[prefix + "documents/" + name] = {"payload": archive.read(name), "metadata": {}}

        def head_object(*, Bucket: str, Key: str) -> dict[str, object]:
            del Bucket
            record = stored.get(Key)
            if record is None:
                raise server.ClientError({"Error": {"Code": "404"}}, "HeadObject")
            return {"ContentLength": len(record["payload"]), "ETag": '"test-etag"', "Metadata": record["metadata"]}

        client = mock.Mock()
        client.head_object.side_effect = head_object
        client.get_object.side_effect = lambda *, Bucket, Key: {"Body": io.BytesIO(stored[Key]["payload"])}
        client.delete_object.side_effect = lambda *, Bucket, Key: stored.pop(Key, None)
        with mock.patch.object(server, "_b2_client", return_value=client):
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                server._complete_rennick_direct_supplement_upload(upload_id)

        self.assertTrue(all(prefix + "documents/" + name not in stored for name in server.RENNICK_SUPPLEMENT_FILENAMES))
        self.assertIn(canonical_archive, stored)
