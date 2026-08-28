"""Create-only page-text indexes for promoted verified case sources."""
from __future__ import annotations

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
            if not filename.lower().endswith(".pdf") or filename not in members:
                continue
            data = archive.read(members[filename])
            for number, page in enumerate(PdfReader(io.BytesIO(data)).pages, start=1):
                text = (page.extract_text() or "").strip()
                if text:
                    lines.append(json.dumps({"filename": filename, "page_number": number, "text": text}, separators=(",", ":")))
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
