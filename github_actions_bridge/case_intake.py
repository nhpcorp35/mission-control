"""Fail-closed verification for a pre-uploaded active-case intake bundle."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import pathlib
import re
import zipfile
from typing import Any


CASE_ID_RE = re.compile(r"^NY-[A-Za-z]+-[0-9]{6}-[0-9]{4}-[A-Za-z0-9-]{2,80}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$")
MAX_BUNDLE_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 1 * 1024 * 1024
GENERIC_DIRECT_MAX_BUNDLE_BYTES = 1024 * 1024 * 1024
GENERIC_SOURCE_EXTENSIONS = frozenset({".pdf", ".jpg", ".jpeg", ".wav"})


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


def decode_base64_upload(value: str, *, label: str, max_size: int) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is required")
    try:
        payload = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{label} must be valid base64") from exc
    if not 1 <= len(payload) <= max_size:
        raise ValueError(f"{label} size is outside the allowed range")
    return payload


def normalized_generic_contents_manifest(case_id: str, source: bytes, manifest: bytes) -> bytes:
    """Return a canonical manifest only for a complete hash-verified source ZIP.

    The full attorney-supplied set may include JPG/JPEG or WAV exhibits.  Those
    files remain hash-verified and immutable; downstream indexing deliberately
    indexes PDFs only.
    """
    try:
        payload = json.loads(manifest)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("intake manifest is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("case_id") != case_id:
        raise ValueError("intake manifest case_id does not match")
    documents = payload.get("documents")
    if not isinstance(documents, list) or not documents:
        raise ValueError("intake manifest must contain documents")

    expected: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(documents):
        if not isinstance(item, dict):
            raise ValueError(f"intake manifest documents[{index}] is invalid")
        filename = str(item.get("filename") or "")
        path = pathlib.PurePosixPath(filename)
        if (
            not filename
            or filename != path.name
            or path.is_absolute()
            or ".." in path.parts
            or path.suffix.lower() not in GENERIC_SOURCE_EXTENSIONS
            or filename in expected
        ):
            raise ValueError("intake manifest contains an unsafe, unsupported, or duplicate filename")
        size = item.get("size_bytes", item.get("size"))
        digest = str(item.get("sha256") or "").lower()
        if not isinstance(size, int) or size < 1:
            raise ValueError("intake manifest file size is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("intake manifest file SHA-256 is invalid")
        expected[filename] = {"filename": filename, "size": size, "sha256": digest}

    found: set[str] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(source)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                member_path = pathlib.PurePosixPath(info.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError("source ZIP contains an unsafe path")
                filename = member_path.name
                if filename not in expected or filename in found:
                    raise ValueError("source ZIP does not exactly match the manifest")
                expected_item = expected[filename]
                if info.file_size != expected_item["size"]:
                    raise ValueError("source ZIP file size does not match the manifest")
                if hashlib.sha256(archive.read(info)).hexdigest() != expected_item["sha256"]:
                    raise ValueError("source ZIP file hash does not match the manifest")
                found.add(filename)
    except zipfile.BadZipFile as exc:
        raise ValueError("source bundle is not a valid ZIP") from exc
    if found != set(expected):
        raise ValueError("source ZIP is missing manifest files")
    return json.dumps(
        {"schema_version": "case-contents.v1", "files": [expected[name] for name in sorted(expected)]},
        sort_keys=True,
    ).encode()


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
    try:
        head = client.head_object(Bucket=bucket, Key=object_key)
    except Exception as exc:
        error = getattr(exc, "response", {}) or {}
        code = str((error.get("Error") or {}).get("Code", ""))
        if code not in {"404", "NoSuchKey", "NotFound"}:
            raise
        prefix = object_key.rsplit("/", 1)[0] + "/"
        listing = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=20)
        observed = [
            {"object_key": item.get("Key"), "size": item.get("Size")}
            for item in listing.get("Contents", [])
        ]
        return {
            "object_key": object_key,
            "verified": False,
            "error": "object_not_found",
            "observed_prefix": prefix,
            "observed_objects": observed,
            "truncated": bool(listing.get("IsTruncated")),
        }
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
        "verified": True,
        "size": read_size,
        "sha256": expected_sha256,
        "etag": (head.get("ETag") or "").strip('"'),
    }
