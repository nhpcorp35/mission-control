"""Tests for Mission Control SQLite disaster-recovery backups."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
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
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}

    def put_object(self, *, Bucket, Key, Body, ContentType, Metadata):
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects[(Bucket, Key)] = (data, dict(Metadata))
        return {}

    def head_object(self, *, Bucket, Key):
        data, metadata = self.objects[(Bucket, Key)]
        return {"ContentLength": len(data), "Metadata": metadata}

    def get_object(self, *, Bucket, Key):
        data, _ = self.objects[(Bucket, Key)]
        return {"Body": io.BytesIO(data)}


class TestConsistentBackup(unittest.TestCase):
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

    def test_b2_upload_is_size_and_sha_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "source.db"
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


if __name__ == "__main__":
    unittest.main()
