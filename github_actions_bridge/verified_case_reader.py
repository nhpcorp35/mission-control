"""Bounded, generic access to promoted verified case sources."""
from __future__ import annotations

import re
import io
import zipfile
from typing import Any


CASE_ID_RE = re.compile(r"^NY-[A-Za-z]+-[0-9]{6}-[0-9]{4}-[A-Za-z0-9-]{2,80}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_source_prefix(case_id: str, source_sha256: str) -> str:
    if not CASE_ID_RE.fullmatch(case_id):
        raise ValueError("case_id has an unsupported format")
    if not SHA256_RE.fullmatch(source_sha256):
        raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
    return f"cases/{case_id}/intake/source/{source_sha256}/"


def validate_page_request(document_name: str, pages: list[int]) -> tuple[str, list[int]]:
    if not isinstance(document_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,180}\.pdf", document_name):
        raise ValueError("document_name must be a safe PDF basename")
    if not isinstance(pages, list) or not 1 <= len(pages) <= 10:
        raise ValueError("pages must contain 1 to 10 page numbers")
    cleaned = sorted(set(pages))
    if any(not isinstance(page, int) or not 1 <= page <= 5000 for page in cleaned):
        raise ValueError("pages must be positive page numbers")
    return document_name, cleaned


def read_verified_manifest(client: Any, bucket: str, case_id: str, source_sha256: str) -> tuple[str, dict[str, Any]]:
    prefix = canonical_source_prefix(case_id, source_sha256)
    identity = client.get_object(Bucket=bucket, Key=f"cases/{case_id}/intake/case_identity.json")["Body"].read()
    if not identity:
        raise ValueError("case is not promoted and verified")
    manifest = client.get_object(Bucket=bucket, Key=prefix + "contents_manifest.json")["Body"].read()
    import json
    payload = json.loads(manifest)
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        raise ValueError("verified contents manifest is invalid")
    return prefix, payload


def extract_pdf_pages(archive_bytes: bytes, document_name: str, pages: list[int]) -> list[dict[str, Any]]:
    """Extract requested text pages from one verified ZIP archive in memory."""
    document_name, pages = validate_page_request(document_name, pages)
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            names = {name.rsplit("/", 1)[-1]: name for name in archive.namelist()}
            member = names.get(document_name)
            if not member:
                raise ValueError("document is not in the verified archive")
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(archive.read(member)))
            if max(pages) > len(reader.pages):
                raise ValueError("requested page is outside the document")
            return [{"page_number": page, "text": (reader.pages[page - 1].extract_text() or "").strip()} for page in pages]
    except zipfile.BadZipFile as exc:
        raise ValueError("verified source is not a readable ZIP archive") from exc
