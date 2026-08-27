from __future__ import annotations

import hashlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet


def _server():
    os.environ.setdefault("GITHUB_OAUTH_CLIENT_ID", "test-client-id")
    os.environ.setdefault("GITHUB_OAUTH_CLIENT_SECRET", "test-client-secret")
    os.environ.setdefault("REDIS_HOST", "127.0.0.1")
    os.environ.setdefault("REDIS_PORT", "6379")
    os.environ.setdefault("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    os.environ.setdefault("JWT_SIGNING_KEY", "test-jwt-signing-key-for-bridge")
    path = Path(__file__).resolve().parent.parent / "github_actions_bridge"
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    import server
    return server


class PendingIntakeTests(unittest.TestCase):
    def test_stream_verifies_promotes_and_deletes_pending_object(self):
        server = _server()
        upload_id = "c" * 32
        payload = b"synthetic large-case bytes"
        pending = f"{server.PENDING_INTAKE_PREFIX}{upload_id}/{server.PENDING_INTAKE_FILENAME}"
        stored = {pending: {"payload": payload, "metadata": {}}}
        def head_object(*, Bucket, Key):
            del Bucket
            if Key not in stored:
                raise server.ClientError({"Error": {"Code": "404"}}, "HeadObject")
            item = stored[Key]
            return {"ContentLength": len(item["payload"]), "Metadata": item["metadata"]}
        def copy_object(*, Bucket, Key, CopySource, Metadata, **kwargs):
            del Bucket, kwargs
            stored[Key] = {"payload": stored[CopySource["Key"]]["payload"], "metadata": Metadata}
        def put_object(*, Bucket, Key, Body, Metadata, **kwargs):
            del Bucket, kwargs
            stored[Key] = {"payload": Body, "metadata": Metadata}
        client = mock.Mock()
        client.head_object.side_effect = head_object
        client.get_object.side_effect = lambda *, Bucket, Key: {"Body": io.BytesIO(stored[Key]["payload"])}
        client.copy_object.side_effect = copy_object
        client.put_object.side_effect = put_object
        client.delete_object.side_effect = lambda *, Bucket, Key: stored.pop(Key, None)
        with mock.patch.object(server, "_b2_client", return_value=client):
            result = server._complete_szymczyk_direct_intake(upload_id)
        self.assertTrue(result["ok"])
        self.assertNotIn(pending, stored)
        self.assertEqual(result["objects"][0]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(len(stored), 2)
