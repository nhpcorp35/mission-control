"""Consistent Mission Control SQLite disaster-recovery backups to Backblaze B2.

The live database is opened read-only and copied with SQLite's online backup API,
so committed WAL state is included without stopping or checkpointing the service.
Temporary backup files are created outside the persistent volume and removed after
B2 verification.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from typing import BinaryIO, Iterable, Optional
from urllib.parse import quote

from mission_control.b2_storage import B2Config, create_s3_client

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/data/mission-control.db"
DEFAULT_PREFIX = "disaster-recovery/mission-control"
DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_INITIAL_DELAY_SECONDS = 30


@dataclass(frozen=True)
class BackupResult:
    key: str
    size_bytes: int
    sha256: str
    created_at: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_stream(body: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: body.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def create_consistent_sqlite_backup(source_path: Path, destination_path: Path) -> None:
    """Create and integrity-check a transactionally consistent SQLite copy."""
    if not source_path.is_file():
        raise FileNotFoundError(f"Mission Control database not found: {source_path}")

    uri = f"file:{quote(str(source_path.resolve()), safe='/')}?mode=ro"
    source = sqlite3.connect(uri, uri=True, timeout=30.0)
    destination = sqlite3.connect(str(destination_path), timeout=30.0)
    try:
        source.backup(destination)
        row = destination.execute("PRAGMA integrity_check").fetchone()
        if row is None or str(row[0]).lower() != "ok":
            raise RuntimeError("SQLite backup integrity_check failed")
    finally:
        destination.close()
        source.close()


def _build_key(prefix: str, created_at: datetime) -> str:
    clean_prefix = prefix.strip("/") or DEFAULT_PREFIX
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    date_path = created_at.strftime("%Y/%m/%d")
    return f"{clean_prefix}/{date_path}/mission-control-{stamp}.db"


def backup_database_to_b2(
    *,
    source_path: Optional[Path] = None,
    prefix: Optional[str] = None,
    client=None,
    config: Optional[B2Config] = None,
    now: Optional[datetime] = None,
) -> BackupResult:
    """Create, upload, download-verify, and clean up one SQLite backup."""
    db_path = source_path or Path(
        os.environ.get("MISSION_CONTROL_DB_PATH", DEFAULT_DB_PATH)
    )
    backup_prefix = prefix or os.environ.get(
        "MISSION_CONTROL_B2_BACKUP_PREFIX", DEFAULT_PREFIX
    )
    created = now or datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    created = created.astimezone(timezone.utc)

    b2_config = config or B2Config.from_env()
    s3 = client or create_s3_client(b2_config)
    key = _build_key(backup_prefix, created)

    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="mission-control-backup-",
            suffix=".db",
            dir="/tmp",
            delete=False,
        ) as temp:
            temp_path = Path(temp.name)

        create_consistent_sqlite_backup(db_path, temp_path)
        size_bytes = temp_path.stat().st_size
        sha256 = _sha256_file(temp_path)

        with temp_path.open("rb") as body:
            s3.put_object(
                Bucket=b2_config.bucket,
                Key=key,
                Body=body,
                ContentType="application/vnd.sqlite3",
                Metadata={
                    "sha256": sha256,
                    "source": "mission-control",
                    "created-at": created.isoformat(),
                },
            )

        head = s3.head_object(Bucket=b2_config.bucket, Key=key)
        if int(head.get("ContentLength", -1)) != size_bytes:
            raise RuntimeError("B2 backup size verification failed")
        metadata = {
            str(k).lower(): str(v) for k, v in (head.get("Metadata") or {}).items()
        }
        if metadata.get("sha256") != sha256:
            raise RuntimeError("B2 backup metadata SHA-256 verification failed")

        response = s3.get_object(Bucket=b2_config.bucket, Key=key)
        remote_sha256 = _sha256_stream(response["Body"])
        if remote_sha256 != sha256:
            raise RuntimeError("B2 backup downloaded SHA-256 verification failed")

        return BackupResult(
            key=key,
            size_bytes=size_bytes,
            sha256=sha256,
            created_at=created.isoformat(),
        )
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Failed to remove temporary Mission Control backup")


def backups_enabled() -> bool:
    return os.environ.get("MISSION_CONTROL_B2_BACKUP_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def run_daemon(
    *,
    interval_seconds: Optional[float] = None,
    initial_delay_seconds: Optional[float] = None,
    stop_event: Optional[threading.Event] = None,
) -> int:
    """Run verified backups until the process/container stops."""
    interval = interval_seconds or float(
        os.environ.get("MISSION_CONTROL_B2_BACKUP_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)
    )
    initial_delay = initial_delay_seconds
    if initial_delay is None:
        initial_delay = float(
            os.environ.get(
                "MISSION_CONTROL_B2_BACKUP_INITIAL_DELAY_SECONDS",
                DEFAULT_INITIAL_DELAY_SECONDS,
            )
        )
    if interval < 300:
        raise RuntimeError("Mission Control B2 backup interval must be at least 300 seconds")

    stopper = stop_event or threading.Event()
    if initial_delay > 0 and stopper.wait(initial_delay):
        return 0

    while not stopper.is_set():
        try:
            result = backup_database_to_b2()
            logger.info(
                "Mission Control B2 backup verified key=%s size_bytes=%s sha256=%s",
                result.key,
                result.size_bytes,
                result.sha256,
            )
        except Exception as exc:  # noqa: BLE001 — daemon survives transient failures
            logger.exception(
                "Mission Control B2 backup failed error_type=%s",
                type(exc).__name__,
            )
        if stopper.wait(interval):
            break
    return 0


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mission Control SQLite disaster-recovery backup to B2",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="once",
        choices=("once", "daemon"),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "daemon":
        return run_daemon()

    result = backup_database_to_b2()
    print(
        "backup: PASS "
        f"key={result.key} size_bytes={result.size_bytes} sha256={result.sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
