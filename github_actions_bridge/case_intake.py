"""Fail-closed verification for a pre-uploaded active-case intake bundle."""

from __future__ import annotations

import hashlib
import re
from typing import Any


CASE_ID_RE = re.compile(r"^NY-[A-Za-z]+-[0-9]{6}-[0-9]{4}-[A-Za-z0-9-]{2,80}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$")
MAX_BUNDLE_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 1 * 1024 * 1024


def intake_keys(case_id: str, source_filename: str, manifest_filename: str) -> tuple[str, str]:
    if not CASE_ID_RE.fullmatch(case_id):
        raise ValueError("case_id has an unsupported format")
    if not FILENAME_RE.fullmatch(source_filename) or not source_filename.endswith(".zip"):
        raise ValueError("source_filename must be a safe .zip basename")
    if not FILENAME_RE.fullmatch(manifest_filename) or not manifest_filename.endswith(".json"):
        raise ValueError("manifest_filename must be a safe .json basename")
    prefix = f"cases/{case_id}/intake/"
    return prefix + source_filename, prefix + manifest_filename


def validate_digest(value: str, label: str) -> str:
    if not SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def verify_object(
    client: Any,
    *,
    bucket: str,
    object_key: str,
    expected_size: int,
    expected_sha256: str,
    max_size: int,
) -> dict[str, Any]:
    if not isinstance(expected_size, int) or expected_size < 1 or expected_size > max_size:
        raise ValueError("expected object size is outside the allowed range")
    validate_digest(expected_sha256, "expected_sha256")
    head = client.head_object(Bucket=bucket, Key=object_key)
    if head.get("ContentLength") != expected_size:
        raise ValueError("B2 object size mismatch")
    response = client.get_object(Bucket=bucket, Key=object_key)
    stream = response["Body"]
    digest = hashlib.sha256()
    read_size = 0
    try:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            read_size += len(chunk)
            if read_size > max_size:
                raise ValueError("B2 object exceeds the allowed size")
            digest.update(chunk)
    finally:
        stream.close()
    if read_size != expected_size or digest.hexdigest() != expected_sha256:
        raise ValueError("B2 object SHA-256 mismatch")
    return {
        "object_key": object_key,
        "size": read_size,
        "sha256": expected_sha256,
        "etag": (head.get("ETag") or "").strip('"'),
    }
