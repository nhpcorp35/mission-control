"""Create-only page-text indexes for promoted verified case sources."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from typing import Any

from pypdf import PdfReader

try:  # Package import for tests; flat import for the Bridge container.
    from .verified_case_reader import RangeObjectReader
except ImportError:  # pragma: no cover
    from verified_case_reader import RangeObjectReader


def build_page_records(client: Any, bucket: str, source_key: str, manifest: dict[str, Any]) -> bytes:
    """Extract plain text with exact source citations from verified PDFs only."""
    size = int(client.head_object(Bucket=bucket, Key=source_key)["ContentLength"])
    lines: list[str] = []
    with zipfile.ZipFile(io.BufferedReader(RangeObjectReader(client, bucket, source_key, size))) as archive:
        members = {item.filename.rsplit("/", 1)[-1]: item for item in archive.infolist()}
        for item in manifest.get("files", []):
            filename = str(item.get("filename", "")) if isinstance(item, dict) else ""
            document_name = filename.rsplit("/", 1)[-1]
            if not filename.lower().endswith(".pdf") or document_name not in members:
                continue
            data = archive.read(members[document_name])
            for number, page in enumerate(PdfReader(io.BytesIO(data)).pages, start=1):
                text = (page.extract_text() or "").strip()
                if text:
                    lines.append(json.dumps({"filename": document_name, "page_number": number, "text": text}, separators=(",", ":")))
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def diagnose_page_record(
    page_records: bytes,
    *,
    filename: str,
    page_number: int,
    extracted_text: str,
) -> dict[str, Any]:
    """Compare one original-PDF extraction with one derived index record.

    This is intentionally read-only and returns hashes/lengths only, so it can
    support an auditable repair decision without exposing source text.
    """
    if not isinstance(filename, str) or not filename or "/" in filename:
        raise ValueError("filename must be a safe basename")
    if not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be positive")

    direct = str(extracted_text or "")
    indexed: str | None = None
    for raw_line in page_records.splitlines():
        if not raw_line:
            continue
        row = json.loads(raw_line)
        if (
            isinstance(row, dict)
            and row.get("filename") == filename
            and row.get("page_number") == page_number
        ):
            if indexed is not None:
                raise ValueError("page index contains duplicate page records")
            value = row.get("text")
            indexed = value if isinstance(value, str) else str(value or "")

    direct_digest = hashlib.sha256(direct.encode("utf-8")).hexdigest()
    indexed_digest = (
        hashlib.sha256(indexed.encode("utf-8")).hexdigest()
        if indexed is not None
        else None
    )
    return {
        "filename": filename,
        "page_number": page_number,
        "direct_text_present": bool(direct),
        "direct_text_length": len(direct),
        "direct_text_sha256": direct_digest,
        "indexed_record_present": indexed is not None,
        "indexed_text_length": len(indexed) if indexed is not None else None,
        "indexed_text_sha256": indexed_digest,
        "matches": indexed is not None and indexed == direct,
    }
