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
    def test_upload_stores_and_head_verifies_only_the_fixed_three_document_supplement(self) -> None:
        server = _bridge_server()
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
            result = server._upload_rennick_docket_supplement(archive_stream.getvalue(), manifest)

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["objects"]), 2)
        self.assertTrue(all("/supplements/" in item["object_key"] for item in result["objects"]))
