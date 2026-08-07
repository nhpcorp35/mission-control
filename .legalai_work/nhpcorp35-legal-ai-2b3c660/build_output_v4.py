#!/usr/bin/env python3
"""
Build output_v4.json by merging existing structured metadata from output_v3.json
with extracted full text from raw PDF opinions under data/raw/.

Usage:
    python3 build_output_v4.py

Output:
    data/output_v4.json

Notes:
- Prefers matching PDFs by the `file` field already present in output_v3.json.
- Extracts text with pypdf.
- Adds:
    - text
    - snippet
    - text_extracted (bool)
    - text_chars
    - extraction_warning (optional)
- Keeps existing metadata untouched.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from pypdf import PdfReader
except ImportError:
    print("Missing dependency: pypdf")
    print("Install with: pip install pypdf")
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
INPUT_JSON = DATA_DIR / "output_v3.json"
OUTPUT_JSON = DATA_DIR / "output_v4.json"

SNIPPET_MAX_LEN = 700
SNIPPET_MIN_LEN = 180

# Ordered by usefulness for legal search/snippets.
SNIPPET_KEYWORDS = [
    "summary judgment",
    "motion to dismiss",
    "motion",
    "labor law",
    "breach of contract",
    "negligence",
    "conversion",
    "fraud",
    "medical malpractice",
    "personal injury",
    "wrongful death",
    "strict liability",
    "products liability",
    "foreclosure",
    "article 78",
    "family court",
    "criminal possession",
    "suppression",
    "indictment",
    "claim",
    "claims",
    "cause of action",
    "causes of action",
    "petition",
    "complaint",
    "affirmed",
    "reversed",
    "modified",
    "granted",
    "denied",
]


def normalize_ws(text: str) -> str:
    """Collapse repeated whitespace while preserving paragraph breaks enough for splitting."""
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_inline(text: str) -> str:
    """Single-line normalization for snippets/searchable excerpts."""
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_pdf_text(pdf_path: Path) -> tuple[str, Optional[str]]:
    """
    Returns (text, warning).
    warning is set when extraction technically succeeded but showed signs of weak output.
    """
    try:
        reader = PdfReader(str(pdf_path))
        pages: List[str] = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:
                pages.append(f"\n[PAGE EXTRACTION ERROR: {exc}]\n")

        text = "\n\n".join(pages)
        text = normalize_ws(text)

        warning = None
        if not text:
            warning = "empty_text"
        elif len(text) < 300:
            warning = "very_short_text"

        return text, warning
    except Exception as exc:
        return "", f"pdf_read_error: {exc}"


def split_paragraphs(text: str) -> List[str]:
    parts = re.split(r"\n\s*\n", text)
    cleaned = [clean_inline(p) for p in parts if clean_inline(p)]
    return cleaned


def pick_snippet(text: str, outcome: Optional[str] = None) -> str:
    """
    Choose a useful snippet:
    1) first paragraph containing a strong keyword
    2) first paragraph containing outcome
    3) first substantial paragraph
    4) fallback to first N chars
    """
    if not text:
        return ""

    paragraphs = split_paragraphs(text)
    lowered_keywords = [k.lower() for k in SNIPPET_KEYWORDS]

    for para in paragraphs:
        p = para.lower()
        if any(k in p for k in lowered_keywords):
            return para[:SNIPPET_MAX_LEN]

    if outcome:
        outcome_l = outcome.lower().strip()
        for para in paragraphs:
            if outcome_l and outcome_l in para.lower():
                return para[:SNIPPET_MAX_LEN]

    for para in paragraphs:
        if len(para) >= SNIPPET_MIN_LEN:
            return para[:SNIPPET_MAX_LEN]

    fallback = clean_inline(text)
    return fallback[:SNIPPET_MAX_LEN]


def build_pdf_index(raw_dir: Path) -> Dict[str, Path]:
    """
    Map basename -> full path.
    Example:
        2024-04833__decision__2026-03-05.pdf -> /.../data/raw/.../2024-04833__decision__2026-03-05.pdf
    """
    index: Dict[str, Path] = {}
    for pdf in raw_dir.rglob("*.pdf"):
        index[pdf.name] = pdf
    return index


def load_input_records(input_json: Path) -> List[Dict[str, Any]]:
    with input_json.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(f"{input_json} does not contain a list of records")

    return data


def enrich_record(record: Dict[str, Any], pdf_index: Dict[str, Path]) -> Dict[str, Any]:
    enriched = dict(record)

    file_name = record.get("file", "")
    pdf_path = pdf_index.get(file_name)

    enriched["pdf_path"] = str(pdf_path.relative_to(REPO_ROOT)) if pdf_path else None

    if not pdf_path or not pdf_path.exists():
        enriched["text"] = ""
        enriched["snippet"] = ""
        enriched["text_extracted"] = False
        enriched["text_chars"] = 0
        enriched["extraction_warning"] = "pdf_not_found"
        return enriched

    text, warning = extract_pdf_text(pdf_path)

    enriched["text"] = text
    enriched["snippet"] = pick_snippet(text, outcome=record.get("outcome"))
    enriched["text_extracted"] = bool(text)
    enriched["text_chars"] = len(text)

    if warning:
        enriched["extraction_warning"] = warning
    else:
        enriched["extraction_warning"] = None

    return enriched


def main() -> None:
    if not INPUT_JSON.exists():
        print(f"Missing input file: {INPUT_JSON}")
        sys.exit(1)

    if not RAW_DIR.exists():
        print(f"Missing raw PDF directory: {RAW_DIR}")
        sys.exit(1)

    print(f"Loading records from {INPUT_JSON} ...")
    records = load_input_records(INPUT_JSON)
    print(f"Loaded {len(records)} records")

    print(f"Indexing PDFs under {RAW_DIR} ...")
    pdf_index = build_pdf_index(RAW_DIR)
    print(f"Indexed {len(pdf_index)} PDFs")

    enriched_records: List[Dict[str, Any]] = []

    extracted_count = 0
    missing_count = 0
    warned_count = 0

    for i, record in enumerate(records, start=1):
        enriched = enrich_record(record, pdf_index)
        enriched_records.append(enriched)

        if enriched["text_extracted"]:
            extracted_count += 1
        else:
            missing_count += 1

        if enriched.get("extraction_warning"):
            warned_count += 1

        case_number = record.get("case_number", "?")
        print(
            f"[{i:02d}/{len(records)}] {case_number} | "
            f"extracted={enriched['text_extracted']} | "
            f"chars={enriched['text_chars']} | "
            f"warning={enriched.get('extraction_warning')}"
        )

    with OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(enriched_records, f, indent=2, ensure_ascii=False)

    print("\nDone.")
    print(f"Wrote: {OUTPUT_JSON}")
    print(f"Total records: {len(enriched_records)}")
    print(f"Text extracted: {extracted_count}")
    print(f"Missing/failed: {missing_count}")
    print(f"Warnings: {warned_count}")


if __name__ == "__main__":
    main()