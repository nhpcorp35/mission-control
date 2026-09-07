"""Tests for Mission Control SQLite disaster-recovery backups."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mission_control.b2_storage import B2Config
from mission_control.db_b2_backup import (
    backup_database_to_b2,
    create_consistent_sqlite_backup,
)


class FakeS3:
    def __init__(self, *, corrupt_download: bool = False) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}
        self.corrupt_download = corrupt_download

    def put_object(self, *, Bucket, Key, Body, ContentType, Metadata):
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects[(Bucket, Key)] = (data, dict(Metadata))
        return {}

    def head_object(self, *, Bucket, Key):
        data, metadata = self.objects[(Bucket, Key)]
        return {"ContentLength": len(data), "Metadata": metadata}

    def get_object(self, *, Bucket, Key):
        data, _ = self.objects[(Bucket, Key)]
        if self.corrupt_download:
            data = data + b"corrupt"
        return {"Body": io.BytesIO(data)}


class TestConsistentBackup(unittest.TestCase):
    def test_missing_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "missing.db"
            backup = Path(tempdir) / "backup.db"
            with self.assertRaises(FileNotFoundError):
                create_consistent_sqlite_backup(source, backup)
            self.assertFalse(backup.exists())

    def test_backup_includes_committed_wal_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "source.db"
            backup = Path(tempdir) / "backup.db"
            conn = sqlite3.connect(source)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE records (value TEXT NOT NULL)")
            conn.execute("INSERT INTO records VALUES ('committed')")
            conn.commit()
            self.assertTrue(Path(str(source) + "-wal").exists())

            create_consistent_sqlite_backup(source, backup)

            copied = sqlite3.connect(backup)
            try:
                self.assertEqual(
                    copied.execute("SELECT value FROM records").fetchone()[0],
                    "committed",
                )
                self.assertEqual(
                    copied.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
            finally:
                copied.close()
                conn.close()

    def test_b2_upload_is_size_sha_and_receipt_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "source.db"
            receipt = Path(tempdir) / "last-backup.json"
            conn = sqlite3.connect(source)
            conn.execute("CREATE TABLE records (value TEXT NOT NULL)")
            conn.execute("INSERT INTO records VALUES ('important state')")
            conn.commit()
            conn.close()

            config = B2Config(
                key_id="secret-id",
                application_key="secret-key",
                bucket="legalai-corpus",
                endpoint="https://example.invalid",
                region="test",
            )
            s3 = FakeS3()
            result = backup_database_to_b2(
                source_path=source,
                prefix="disaster-recovery/mission-control",
                receipt_path=receipt,
                client=s3,
                config=config,
                now=datetime(2026, 9, 6, 23, 30, tzinfo=timezone.utc),
            )

            self.assertEqual(
                result.key,
                "disaster-recovery/mission-control/2026/09/06/"
                "mission-control-20260906T233000Z.db",
            )
            payload, metadata = s3.objects[(config.bucket, result.key)]
            self.assertEqual(len(payload), result.size_bytes)
            self.assertEqual(hashlib.sha256(payload).hexdigest(), result.sha256)
            self.assertEqual(metadata["sha256"], result.sha256)

            receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(receipt_payload["key"], result.key)
            self.assertEqual(receipt_payload["size_bytes"], result.size_bytes)
            self.assertEqual(receipt_payload["sha256"], result.sha256)
            self.assertEqual(receipt_payload["created_at"], result.created_at)
            self.assertIs(receipt_payload["verified"], True)

            copied_path = Path(tempdir) / "uploaded.db"
            copied_path.write_bytes(payload)
            copied = sqlite3.connect(copied_path)
            try:
                self.assertEqual(
                    copied.execute("SELECT value FROM records").fetchone()[0],
                    "important state",
                )
            finally:
                copied.close()

    def test_receipt_is_not_written_when_remote_sha_verification_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "source.db"
            receipt = Path(tempdir) / "last-backup.json"
            conn = sqlite3.connect(source)
            conn.execute("CREATE TABLE records (value TEXT NOT NULL)")
            conn.execute("INSERT INTO records VALUES ('important state')")
            conn.commit()
            conn.close()

            config = B2Config(
                key_id="secret-id",
                application_key="secret-key",
                bucket="legalai-corpus",
                endpoint="https://example.invalid",
                region="test",
            )
            s3 = FakeS3(corrupt_download=True)
            with self.assertRaisesRegex(RuntimeError, "downloaded SHA-256"):
                backup_database_to_b2(
                    source_path=source,
                    receipt_path=receipt,
                    client=s3,
                    config=config,
                    now=datetime(2026, 9, 6, 23, 31, tzinfo=timezone.utc),
                )
            self.assertFalse(receipt.exists())


if __name__ == "__main__":
    unittest.main()
