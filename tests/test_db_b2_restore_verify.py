from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mission_control.b2_storage import B2Config
from mission_control.db_b2_restore_verify import verify_latest_backup


class FakeS3:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.get_calls = 0

    def get_object(self, *, Bucket, Key):
        self.get_calls += 1
        return {"Body": io.BytesIO(self.data)}


class TestRestoreVerification(unittest.TestCase):
    def _fixture(self, tempdir: str):
        db = Path(tempdir) / "backup.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE records (value TEXT NOT NULL)")
        conn.execute("INSERT INTO records VALUES ('recoverable')")
        conn.commit()
        conn.close()
        data = db.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        receipt = Path(tempdir) / "receipt.json"
        receipt.write_text(json.dumps({"key":"disaster-recovery/mission-control/test.db","size_bytes":len(data),"sha256":sha,"created_at":"2026-09-07T00:00:00+00:00","verified":True}), encoding="utf-8")
        config = B2Config(key_id="id", application_key="key", bucket="bucket", endpoint="https://example.invalid", region="test")
        return receipt, data, sha, config

    def test_downloaded_backup_passes_sha_and_sqlite_integrity(self):
        with tempfile.TemporaryDirectory() as tempdir:
            receipt, data, sha, config = self._fixture(tempdir)
            result = verify_latest_backup(receipt_path=receipt, client=FakeS3(data), config=config)
            self.assertTrue(result.verified)
            self.assertEqual(result.sha256, sha)
            self.assertEqual(result.integrity_check, "ok")

    def test_sha_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            receipt, data, _, config = self._fixture(tempdir)
            payload = json.loads(receipt.read_text())
            payload["sha256"] = "0" * 64
            receipt.write_text(json.dumps(payload))
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                verify_latest_backup(receipt_path=receipt, client=FakeS3(data), config=config)

    def test_unverified_receipt_is_rejected_without_download(self):
        with tempfile.TemporaryDirectory() as tempdir:
            receipt, data, _, config = self._fixture(tempdir)
            payload = json.loads(receipt.read_text())
            payload["verified"] = False
            receipt.write_text(json.dumps(payload))
            client = FakeS3(data)
            with self.assertRaisesRegex(RuntimeError, "receipt is not verified"):
                verify_latest_backup(receipt_path=receipt, client=client, config=config)
            self.assertEqual(client.get_calls, 0)


if __name__ == "__main__":
    unittest.main()
