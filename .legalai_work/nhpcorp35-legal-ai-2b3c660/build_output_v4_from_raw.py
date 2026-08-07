#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from pypdf import PdfReader
except ImportError:
    print("Missing dependency: pypdf")
    print("Install with: pip install pypdf")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent
RAW_DIR = REPO_ROOT / "data" / "raw" / "NY_Appellate_AD1"
OUTPUT_JSON = REPO_ROOT / "data" / "output_v4.json"

SNIPPET_MAX_LEN = 900

OUTCOME_PATTERNS = [
    ("affirmed", [r"\bunanimously affirmed\b", r"\baffirmed\b"]),
    ("reversed", [r"\bunanimously reversed\b", r"\breversed\b"]),
    ("granted", [r"\bgranted\b"]),
    ("denied", [r"\bdenied\b"]),
    ("dismissed", [r"\bdismissed\b"]),
]

MOTION_PATTERNS = [
    ("partial summary judgment", [r"\bpartial summary judgment\b"]),
    ("summary judgment", [r"\bsummary judgment\b"]),
    ("motion to dismiss", [r"\bmotion to dismiss\b"]),
    ("dismissal", [r"\bdismiss(?:ed|al)?\b"]),
]

CAUSE_ALIASES = {
    "labor law": [
        "labor law",
        "labor law 200",
        "labor law 240",
        "labor law 241",
        "labor law section 200",
        "labor law section 240",
        "labor law section 241",
        "scaffold law",
    ],
    "breach of contract": [
        "breach of contract",
        "material breach",
        "contractual breach",
        "written agreement",
        "oral agreement",
    ],
    "fraud": [
        "fraud",
        "fraudulent",
        "misrepresentation",
        "fraudulent inducement",
        "concealment",
    ],
    "conversion": [
        "conversion",
        "dominion and control",
        "wrongful possession",
        "unauthorized control",
    ],
    "negligence": [
        "negligence",
        "negligent",
        "duty of care",
        "breach of duty",
        "proximate cause",
    ],
}


def clean_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def normalize_for_search(value: str) -> str:
    value = str(value or "").lower()
    value = value.replace("§", " section ")
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def extract_pdf_text(pdf_path: Path) -> Tuple[str, Optional[str]]:
    try:
        reader = PdfReader(str(pdf_path))
        pages: List[str] = []

        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:
                pages.append(f"\n[PAGE EXTRACTION ERROR: {exc}]\n")

        text = "\n\n".join(pages)
        text = text.replace("\r", "\n")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        warning = None
        if not text:
            warning = "empty_text"
        elif len(text) < 300:
            warning = "very_short_text"

        return text, warning
    except Exception as exc:
        return "", f"pdf_read_error: {exc}"


def extract_case_number_and_date(pdf_name: str) -> Tuple[str, str]:
    # expected: 2024-04833__decision__2026-03-05.pdf
    m = re.match(r"(.+?)__[^_]+__(\d{4}-\d{2}-\d{2})\.pdf$", pdf_name)
    if m:
        return m.group(1), m.group(2)

    m2 = re.match(r"(.+?)__(\d{4}-\d{2}-\d{2})\.pdf$", pdf_name)
    if m2:
        return m2.group(1), m2.group(2)

    return pdf_name.replace(".pdf", ""), ""


def detect_record_type(pdf_path: Path, text: str) -> str:
    low_name = pdf_path.name.lower()
    low_text = normalize_for_search(text[:1500])

    if "__motion_order__" in low_name:
        return "motion_order"

    if "motion no" in low_text and "case no" in low_text and "index no" in low_text:
        return "motion_order"

    return "decision"


def detect_outcome(text: str) -> str:
    low = normalize_for_search(text[:4000])
    for outcome, patterns in OUTCOME_PATTERNS:
        for pat in patterns:
            if re.search(pat, low):
                return outcome
    return ""


def detect_motion(text: str) -> str:
    low = normalize_for_search(text[:4000])
    for motion, patterns in MOTION_PATTERNS:
        for pat in patterns:
            if re.search(pat, low):
                return motion
    return ""


def detect_primary_cause(text: str) -> str:
    low = normalize_for_search(text[:6000])
    for cause, aliases in CAUSE_ALIASES.items():
        if any(alias in low for alias in aliases):
            return cause
    return ""


def extract_citation(text: str) -> str:
    # reporter cite like 226 AD3d 499
    m = re.search(r"\b\d+\s+(?:AD2d|AD3d|NY2d|NY3d|Misc(?:\s*\d+d?)?)\s+\d+\b", text, re.IGNORECASE)
    if m:
        return clean_text(m.group(0))

    # slip op like 2026 NY Slip Op 00201
    m = re.search(r"\b20\d{2}\s+NY\s+Slip\s+Op\s+\d+\b", text, re.IGNORECASE)
    if m:
        return clean_text(m.group(0))

    return ""


def extract_caption_from_text(text: str) -> str:
    lines = [clean_text(line) for line in text.splitlines() if clean_text(line)]
    if not lines:
        return ""

    joined = " ".join(lines[:30])

    patterns = [
        r"([A-Z][A-Za-z0-9&'.,\- ]+ v\. [A-Z][A-Za-z0-9&'.,\- ]+)",
        r"([A-Z][A-Za-z0-9&'.,\- ]+ against [A-Z][A-Za-z0-9&'.,\- ]+)",
        r"(In the Matter of [A-Z][A-Za-z0-9&'.,\- ]+)",
    ]

    for pat in patterns:
        m = re.search(pat, joined, re.IGNORECASE)
        if m:
            title = clean_text(m.group(1))
            if 8 <= len(title) <= 160:
                return title

    for line in lines[:12]:
        low = line.lower()
        if any(x in low for x in [
            "appellate division", "first judicial department", "motion no",
            "index no", "case no", "entered ", "order,"
        ]):
            continue
        if 10 <= len(line) <= 140:
            return line

    return ""


def pick_snippet(text: str, query_terms: Optional[List[str]] = None) -> str:
    if not text:
        return ""

    paragraphs = [clean_text(p) for p in re.split(r"\n\s*\n", text) if clean_text(p)]
    if not paragraphs:
        return clean_text(text)[:SNIPPET_MAX_LEN]

    priority_terms = [
        "labor law",
        "summary judgment",
        "motion to dismiss",
        "breach of contract",
        "negligence",
        "fraud",
        "conversion",
        "affirmed",
        "reversed",
        "granted",
        "denied",
    ]
    if query_terms:
        priority_terms = query_terms + priority_terms

    best_para = ""
    best_score = -1

    for para in paragraphs:
        low = normalize_for_search(para)
        score = 0
        for term in priority_terms:
            term_low = normalize_for_search(term)
            if term_low and term_low in low:
                score += 3
        if len(para) > 80:
            score += 1
        if score > best_score:
            best_score = score
            best_para = para

    return clean_text(best_para)[:SNIPPET_MAX_LEN]


def build_record(pdf_path: Path) -> Optional[Dict[str, Any]]:
    text, warning = extract_pdf_text(pdf_path)
    if not text:
        return None

    case_number, date = extract_case_number_and_date(pdf_path.name)
    record_type = detect_record_type(pdf_path, text)

    if record_type != "decision":
        return None

    if len(text) < 1000:
        return None

    title = extract_caption_from_text(text)
    court = "Appellate Division, First Department"
    citation = extract_citation(text)
    outcome = detect_outcome(text)
    motion = detect_motion(text)
    primary_cause = detect_primary_cause(text)
    snippet = pick_snippet(text)

    return {
        "file": pdf_path.name,
        "pdf_path": str(pdf_path.relative_to(REPO_ROOT)),
        "case_number": clean_text(case_number),
        "motion_number": None,
        "date": clean_text(date),
        "court": court,
        "outcome": outcome,
        "title": title or f"Case {case_number} ({court}, {date})",
        "citation": citation,
        "docket": clean_text(case_number),
        "summary": "",
        "snippet": snippet,
        "text": text,
        "judges": [],
        "parties": [],
        "citations": {
            "reporters": [citation] if citation and "ad" in citation.lower() else [],
            "slip_op": [citation] if citation and "slip op" in citation.lower() else [],
        },
        "motion": motion,
        "primary_cause": primary_cause,
        "record_type": record_type,
        "text_extracted": True,
        "text_chars": len(text),
        "extraction_warning": warning,
    }


def main() -> None:
    if not RAW_DIR.exists():
        print(f"Missing raw dir: {RAW_DIR}")
        sys.exit(1)

    pdfs = sorted(RAW_DIR.rglob("*.pdf"))
    print(f"Found {len(pdfs)} PDFs under {RAW_DIR}")

    records: List[Dict[str, Any]] = []
    skipped = 0

    for i, pdf_path in enumerate(pdfs, start=1):
        rec = build_record(pdf_path)
        if rec is None:
            skipped += 1
            print(f"[{i:03d}/{len(pdfs)}] SKIP {pdf_path.name}")
            continue

        records.append(rec)
        print(
            f"[{i:03d}/{len(pdfs)}] KEEP {rec['case_number']} | "
            f"{rec['date']} | outcome={rec['outcome'] or '-'} | "
            f"motion={rec['motion'] or '-'} | cause={rec['primary_cause'] or '-'} | "
            f"chars={rec['text_chars']}"
        )

    records.sort(
        key=lambda r: (
            r.get("date", ""),
            r.get("case_number", ""),
        ),
        reverse=True,
    )

    with OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    print("\nDone.")
    print(f"Wrote: {OUTPUT_JSON}")
    print(f"Kept decisions: {len(records)}")
    print(f"Skipped: {skipped}")


if __name__ == "__main__":
    main()