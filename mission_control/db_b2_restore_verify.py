"""Read-only restore verification for Mission Control B2 database backups.

Downloads the object named by the verified backup receipt to /tmp, verifies its
size and SHA-256, opens the downloaded SQLite database read-only, runs
PRAGMA integrity_check, and removes the temporary file. It never writes to the
live database and never mutates or deletes B2 objects.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from urllib.parse import quote

from mission_control.b2_storage import B2Config, create_s3_client
from mission_control.db_b2_backup import DEFAULT_RECEIPT_PATH


@dataclass(frozen=True)
class RestoreVerificationResult:
    key: str
    size_bytes: int
    sha256: str
    integrity_check: str
    verified: bool = True


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_latest_backup(*, receipt_path: Path | None = None, client=None, config: B2Config | None = None) -> RestoreVerificationResult:
    receipt = receipt_path or Path(os.environ.get("MISSION_CONTROL_B2_BACKUP_RECEIPT_PATH", DEFAULT_RECEIPT_PATH))
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    if payload.get("verified") is not True:
        raise RuntimeError("Backup receipt is not verified")
    key = str(payload["key"])
    expected_size = int(payload["size_bytes"])
    expected_sha = str(payload["sha256"])

    b2_config = config or B2Config.from_env()
    s3 = client or create_s3_client(b2_config)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="mission-control-restore-verify-", suffix=".db", dir="/tmp", delete=False) as temp:
            temp_path = Path(temp.name)
            response = s3.get_object(Bucket=b2_config.bucket, Key=key)
            body = response["Body"]
            for chunk in iter(lambda: body.read(1024 * 1024), b""):
                temp.write(chunk)

        actual_size = temp_path.stat().st_size
        if actual_size != expected_size:
            raise RuntimeError("Restore verification size mismatch")
        actual_sha = _sha256_file(temp_path)
        if actual_sha != expected_sha:
            raise RuntimeError("Restore verification SHA-256 mismatch")

        uri = f"file:{quote(str(temp_path.resolve()), safe='/')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30.0)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
        integrity = "" if row is None else str(row[0])
        if integrity.lower() != "ok":
            raise RuntimeError("Restore verification SQLite integrity_check failed")

        return RestoreVerificationResult(key=key, size_bytes=actual_size, sha256=actual_sha, integrity_check=integrity)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def main() -> int:
    result = verify_latest_backup()
    print(f"restore-verify: PASS key={result.key} size_bytes={result.size_bytes} sha256={result.sha256} integrity_check={result.integrity_check}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
