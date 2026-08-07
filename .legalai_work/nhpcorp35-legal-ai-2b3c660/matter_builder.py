# matter_builder.py
from pathlib import Path
import hashlib
import json
import os
import re

from engines.issue_engine import build_issue_analysis
from engines.entity_graph_engine import build_entity_graph
from engines.contradiction_index import build_contradiction_analysis
from engines.drafting_engine import build_retrieval_grounded_qa

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

try:
    from docx import Document
except Exception:
    Document = None

try:
    import pytesseract
except Exception:
    pytesseract = None

try:
    from pdf2image import convert_from_path
except Exception:
    convert_from_path = None


DEFAULT_MATTER_FOLDER = Path("matter_docs")

# Configurable corpus / inventory roots (Railway / executor).
LEGALAI_MATTER_FOLDER_ENV = "LEGALAI_MATTER_FOLDER"
LEGALAI_NYSCEF_INVENTORY_PATH_ENV = "LEGALAI_NYSCEF_INVENTORY_PATH"

# Safe default inventory path for Case-00 when that corpus is explicitly selected
# via LEGALAI_NYSCEF_INVENTORY_PATH (or an explicit inventory_path argument).
CASE_00_TRIBOROUGH_INVENTORY_PATH = Path(
    "data/case-00-triborough/nyscef_filing_inventory.json"
)


DOCUMENT_GROUPS = {
    "selected_case": "Selected Case",
    "complaint": "Complaint",
    "answer": "Answer",
    "motion": "Motions",
    "affirmation": "Affirmations",
    "opposition": "Oppositions",
    "reply": "Replies",
    "exhibit": "Exhibits",
    "memo": "Memoranda of Law",
    "order": "Orders / Decisions",
    "other": "Other Documents",
}


ALLOWED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".doc",
    ".txt",
    ".rtf",
}


SKIP_FOLDERS = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "node_modules",
}


COURT_HEADER_WORDS = {
    "supreme court",
    "civil court",
    "county court",
    "surrogate",
    "appellate division",
    "state of new york",
    "united states",
}


OCR_MIN_TEXT_LENGTH = 120

# Sparse cover pages used as a conservative exhibit-boundary signal.
EXHIBIT_SPARSE_COVER_MAX_CHARS = 220

# Minimum confidence required to assert an embedded exhibit boundary.
# Weaker candidates are retained as uncertain_exhibit_boundaries.
EXHIBIT_BOUNDARY_ASSERT_CONFIDENCE = {"high", "medium"}

# Used only for deterministic page_id formatting when no verified NYSCEF
# document number is available. Never invents a provenance document number.
UNKNOWN_NYSCEF_DOCUMENT_NUMBER = 0

# Conservative exhibit cover / heading detectors (page-local).
EXHIBIT_COVER_HEADING_RE = re.compile(
    r"(?is)^\s*(?:EXHIBIT|EXH\.?|EX\.)\s+([A-Z0-9]{1,4})\b"
    r"(?:\s*[-–—:;]\s*|\s+)(?P<title>[^\n]{0,120})?"
)
EXHIBIT_COVER_ONLY_RE = re.compile(
    r"(?is)^\s*(?:EXHIBIT|EXH\.?|EX\.)\s+([A-Z0-9]{1,4})\b\s*$"
)
EXHIBIT_NEAR_TOP_RE = re.compile(
    r"(?i)^(?:.{0,80}?)(?:EXHIBIT|EXH\.?|EX\.)\s+([A-Z]|[0-9]{1,3})\b"
)
EXHIBIT_PROSE_REFERENCE_RE = re.compile(
    r"(?i)\b(?:see|attached|annexed|marked|true\s+copy\s+of|copy\s+of)\s+"
    r"(?:as\s+)?(?:EXHIBIT|EXH\.?|EX\.)\s+[A-Z0-9]{1,4}\b"
)
EXHIBIT_BARE_WORD_RE = re.compile(r"(?is)^\s*EXHIBITS?\b\s*$")

# Benchmark folder naming from scraper/utils.js buildFilename:
#   {caseNumber}__{docType}__{date}.pdf
# That pattern carries an index/case id, not a NYSCEF document number.
BENCHMARK_FILENAME_RE = re.compile(
    r"^\d{4}-\d+__.+__\d{4}-\d{2}-\d{2}$",
    re.IGNORECASE,
)

NYSCEF_FILENAME_PATTERNS = [
    re.compile(
        r"(?i)\bnyscef[\s_-]*(?:doc(?:ument)?[\s_-]*)?(?:no\.?[\s_-]*)?(\d+)(?!\d)"
    ),
    re.compile(r"(?i)\bdoc(?:ument)?[\s_-]*no\.?[\s_-]*(\d+)(?!\d)"),
    re.compile(r"(?i)^(?:nyscef|doc)[\s_-]*(\d+)(?!\d)"),
]


def clean_text(value):
    return " ".join(str(value or "").split()).strip()


def coerce_nyscef_document_number(value):
    if value is None or value == "":
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_nyscef_document_number_from_filename(filename):
    """
    Conservatively parse a NYSCEF document number from a filename.

    Matches explicit NYSCEF / Doc No patterns only. Does not treat the
    repository benchmark pattern {case}__{type}__{date}.pdf as a document
    number.
    """
    name = Path(str(filename or "")).name
    stem = Path(name).stem

    if not stem:
        return None

    if BENCHMARK_FILENAME_RE.match(stem):
        return None

    if "__" in stem and re.match(r"^\d{4}-\d+", stem):
        return None

    for pattern in NYSCEF_FILENAME_PATTERNS:
        match = pattern.search(stem)
        if match:
            return coerce_nyscef_document_number(match.group(1))

    return None


def resolve_nyscef_document_number(document=None, filename=None):
    """Prefer explicit metadata; otherwise try a conservative filename parse."""
    if isinstance(document, dict) and "nyscef_document_number" in document:
        return coerce_nyscef_document_number(document.get("nyscef_document_number"))

    name = filename
    if name is None and isinstance(document, dict):
        name = document.get("filename") or document.get("name") or document.get("title")

    return parse_nyscef_document_number_from_filename(name)


def resolve_matter_folder(matter_folder=None):
    """
    Resolve the matter/corpus root.

    Precedence: explicit argument > LEGALAI_MATTER_FOLDER > matter_docs.
    """
    if matter_folder is not None:
        return Path(matter_folder)

    env_value = os.environ.get(LEGALAI_MATTER_FOLDER_ENV)
    if env_value:
        return Path(env_value)

    return DEFAULT_MATTER_FOLDER


def resolve_inventory_path(inventory_path=None):
    """
    Resolve an optional NYSCEF filing inventory path.

    Precedence: explicit argument > LEGALAI_NYSCEF_INVENTORY_PATH > None.
    Unrelated matters stay inventory-free unless configuration selects one.
    The Case-00 aliases `case-00-triborough` / `case-00` resolve to
    CASE_00_TRIBOROUGH_INVENTORY_PATH when that corpus is explicitly selected.
    """
    if inventory_path is not None:
        raw = inventory_path
    else:
        raw = os.environ.get(LEGALAI_NYSCEF_INVENTORY_PATH_ENV)

    if raw is None or raw == "":
        return None

    text = str(raw).strip()
    if text in {"case-00-triborough", "case-00", "triborough"}:
        return CASE_00_TRIBOROUGH_INVENTORY_PATH

    return Path(text)


def load_nyscef_filing_inventory(inventory_path):
    """Load a canonical NYSCEF filing inventory JSON, or None if unavailable."""
    if not inventory_path:
        return None

    path = Path(inventory_path)
    if not path.is_file():
        print(f"INVENTORY MISSING [{path}]")
        return None

    try:
        payload = json.loads(path.read_text())
    except Exception as exc:
        print(f"INVENTORY LOAD FAILED [{path}] -> {exc}")
        return None

    if not isinstance(payload, dict):
        print(f"INVENTORY INVALID [{path}] expected object")
        return None

    filings = payload.get("filings")
    if not isinstance(filings, list):
        print(f"INVENTORY INVALID [{path}] missing filings list")
        return None

    return payload


def index_inventory_by_filename(inventory):
    """Map exact filename -> list of filing records."""
    index = {}
    if not inventory:
        return index

    for entry in inventory.get("filings") or []:
        if not isinstance(entry, dict):
            continue
        filename = entry.get("filename")
        if not filename:
            continue
        index.setdefault(str(filename), []).append(entry)

    return index


def compute_file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def lookup_inventory_provenance(path, inventory_by_filename):
    """
    Match a physical file to inventory by exact filename and verify SHA-256.

    Never invents a NYSCEF number from an unverified filename. Returns a
    provenance dict with status:
      verified | non_canonical_duplicate | hash_mismatch | missing | ambiguous
    """
    path = Path(path)
    filename = path.name
    entries = list(inventory_by_filename.get(filename) or [])

    if not entries:
        return {
            "status": "missing",
            "nyscef_document_number": None,
            "ingest_canonical": None,
            "inventory_entry": None,
        }

    if len(entries) > 1:
        return {
            "status": "ambiguous",
            "nyscef_document_number": None,
            "ingest_canonical": None,
            "inventory_entry": None,
        }

    entry = entries[0]
    expected_sha = str(entry.get("sha256") or "").lower()
    try:
        actual_sha = compute_file_sha256(path).lower()
    except Exception as exc:
        print(f"INVENTORY HASH FAILED [{filename}] -> {exc}")
        return {
            "status": "hash_mismatch",
            "nyscef_document_number": None,
            "ingest_canonical": bool(entry.get("ingest_canonical"))
            if "ingest_canonical" in entry
            else None,
            "inventory_entry": entry,
        }

    if not expected_sha or actual_sha != expected_sha:
        print(
            f"INVENTORY HASH MISMATCH [{filename}] "
            f"expected={expected_sha or '—'} actual={actual_sha}"
        )
        return {
            "status": "hash_mismatch",
            "nyscef_document_number": None,
            "ingest_canonical": bool(entry.get("ingest_canonical"))
            if "ingest_canonical" in entry
            else None,
            "inventory_entry": entry,
        }

    ingest_canonical = bool(entry.get("ingest_canonical", True))
    if not ingest_canonical:
        return {
            "status": "non_canonical_duplicate",
            "nyscef_document_number": coerce_nyscef_document_number(
                entry.get("nyscef_document_number")
            ),
            "ingest_canonical": False,
            "inventory_entry": entry,
        }

    return {
        "status": "verified",
        "nyscef_document_number": coerce_nyscef_document_number(
            entry.get("nyscef_document_number")
        ),
        "ingest_canonical": True,
        "inventory_entry": entry,
    }


def make_page_id(nyscef_document_number, page_number):
    doc_no = coerce_nyscef_document_number(nyscef_document_number)
    if doc_no is None:
        doc_no = UNKNOWN_NYSCEF_DOCUMENT_NUMBER

    return f"nyscef-{doc_no:03d}-page-{int(page_number):04d}"


def build_page_record(page_number, text, extraction_method, nyscef_document_number=None):
    return {
        "page_number": int(page_number),
        "page_id": make_page_id(nyscef_document_number, page_number),
        "text": text if isinstance(text, str) else str(text or ""),
        "extraction_method": extraction_method,
    }


def aggregate_page_text(pages):
    return clean_text("\n".join(
        (page.get("text") or "") if isinstance(page, dict) else ""
        for page in (pages or [])
    ))


def normalize_page_record(page, nyscef_document_number=None):
    page = page or {}
    page_number = int(page.get("page_number") or 0)
    raw_text = page.get("text", "")
    text = clean_text(raw_text) if raw_text else ""

    extraction_method = page.get("extraction_method")
    if extraction_method not in {"native", "ocr", "empty"}:
        if text:
            extraction_method = "native"
        else:
            extraction_method = "empty"

    page_id = page.get("page_id") or make_page_id(nyscef_document_number, page_number)

    return {
        "page_number": page_number,
        "page_id": page_id,
        "text": text,
        "extraction_method": extraction_method,
    }


def make_segment_id(nyscef_document_number, segment_index):
    """Deterministic segment ID aligned with NYSCEF page_id formatting."""
    doc_no = coerce_nyscef_document_number(nyscef_document_number)
    if doc_no is None:
        doc_no = UNKNOWN_NYSCEF_DOCUMENT_NUMBER

    return f"nyscef-{doc_no:03d}-segment-{int(segment_index):04d}"


def normalize_exhibit_label(value):
    value = clean_text(value).upper()
    value = value.replace("EXHIBIT", "").replace("EXH.", "").replace("EXH", "")
    value = re.sub(r"[^A-Z0-9]", "", value)
    if value:
        return value[:8]
    return None


def normalize_exhibit_title(value):
    title = clean_text(value)
    if not title:
        return None
    title = re.sub(r"(?i)^(to|:|-|–|—)\s*", "", title).strip()
    if not title or title.upper() in {"EXHIBIT", "EXH", "EX"}:
        return None
    return title[:160]


def _page_raw_text(page):
    if not isinstance(page, dict):
        return ""
    text = page.get("text")
    return text if isinstance(text, str) else str(text or "")


def detect_page_exhibit_signals(page):
    """
    Collect conservative exhibit-boundary signals for a single page.

    Strong signals favor cover/heading patterns. Prose references alone are
    ignored so we do not invent boundaries from weak evidence.
    """
    raw = _page_raw_text(page)
    if not raw or not raw.strip():
        return []

    # Preserve line structure when present; cleaned aggregates still work.
    stripped = raw.strip()
    first_line = stripped.splitlines()[0].strip() if stripped else ""
    cleaned = clean_text(raw)
    char_count = len(cleaned)
    signals = []

    # Prose citations ("see Exhibit A") are not boundary evidence unless the
    # page itself opens with an exhibit cover/heading line.
    prose_ref = EXHIBIT_PROSE_REFERENCE_RE.search(cleaned)
    opens_with_cover = bool(
        EXHIBIT_COVER_HEADING_RE.match(first_line)
        or EXHIBIT_COVER_ONLY_RE.match(stripped)
        or EXHIBIT_COVER_HEADING_RE.match(stripped)
    )
    if prose_ref and not opens_with_cover:
        return []

    cover_only = EXHIBIT_COVER_ONLY_RE.match(stripped) or EXHIBIT_COVER_ONLY_RE.match(
        cleaned
    )
    if cover_only:
        label = normalize_exhibit_label(cover_only.group(1))
        if label:
            signals.append(
                {
                    "kind": "cover_label",
                    "strength": "strong",
                    "exhibit_label": label,
                    "exhibit_title": None,
                    "detail": f"Sparse/cover page labeled Exhibit {label}",
                }
            )

    heading = EXHIBIT_COVER_HEADING_RE.match(stripped) or EXHIBIT_COVER_HEADING_RE.match(
        first_line
    )
    if heading and not cover_only:
        label = normalize_exhibit_label(heading.group(1))
        title = normalize_exhibit_title(heading.group("title"))
        if label:
            strength = (
                "strong" if char_count <= EXHIBIT_SPARSE_COVER_MAX_CHARS else "medium"
            )
            kind = "titled_cover" if title else "heading_label"
            signals.append(
                {
                    "kind": kind,
                    "strength": strength,
                    "exhibit_label": label,
                    "exhibit_title": title,
                    "detail": (
                        f"Exhibit {label} heading at page start"
                        + (f" titled '{title}'" if title else "")
                    ),
                }
            )

    if char_count <= EXHIBIT_SPARSE_COVER_MAX_CHARS and EXHIBIT_BARE_WORD_RE.match(
        stripped
    ):
        signals.append(
            {
                "kind": "separator_cover",
                "strength": "weak",
                "exhibit_label": None,
                "exhibit_title": None,
                "detail": "Sparse separator page containing only 'Exhibit(s)'",
            }
        )

    if not signals:
        # Near-top mention without clear cover heading → uncertain candidate only.
        near = EXHIBIT_NEAR_TOP_RE.search(cleaned[:300])
        if near and not EXHIBIT_PROSE_REFERENCE_RE.search(cleaned[:300]):
            label = normalize_exhibit_label(near.group(1))
            if label:
                signals.append(
                    {
                        "kind": "near_top_mention",
                        "strength": "weak",
                        "exhibit_label": label,
                        "exhibit_title": None,
                        "detail": (
                            f"Exhibit {label} mentioned near top without cover pattern"
                        ),
                    }
                )

    # Reinforce sparseness only for cover/heading hits — never for weak
    # near-top mentions, which would otherwise inflate confidence to assert.
    cover_kinds = {"cover_label", "titled_cover", "heading_label"}
    if char_count <= EXHIBIT_SPARSE_COVER_MAX_CHARS and any(
        s.get("kind") in cover_kinds and s.get("exhibit_label") for s in signals
    ):
        if not any(s["kind"] == "sparse_cover_context" for s in signals):
            label = next(
                s["exhibit_label"]
                for s in signals
                if s.get("kind") in cover_kinds and s.get("exhibit_label")
            )
            signals.append(
                {
                    "kind": "sparse_cover_context",
                    "strength": "medium",
                    "exhibit_label": label,
                    "exhibit_title": None,
                    "detail": "Short page consistent with an exhibit cover sheet",
                }
            )

    return signals


def score_exhibit_boundary_confidence(signals):
    if not signals:
        return None

    strengths = {s.get("strength") for s in signals}
    kinds = {s.get("kind") for s in signals}

    if "strong" in strengths and (
        "cover_label" in kinds
        or "titled_cover" in kinds
        or "heading_label" in kinds
    ):
        if "sparse_cover_context" in kinds or "cover_label" in kinds:
            return "high"
        return "high" if "strong" in strengths else "medium"

    if "medium" in strengths and (
        "heading_label" in kinds
        or "titled_cover" in kinds
        or "sparse_cover_context" in kinds
    ):
        return "medium"

    if strengths.intersection({"strong", "medium", "weak"}):
        return "low"

    return None


def _primary_signal_label_title(signals):
    label = None
    title = None
    for signal in signals:
        if label is None and signal.get("exhibit_label"):
            label = signal["exhibit_label"]
        if title is None and signal.get("exhibit_title"):
            title = signal["exhibit_title"]
    return label, title


def segment_embedded_exhibits(pages, nyscef_document_number=None):
    """
    Segment a filing's pages into parent material and embedded exhibits.

    Conservative: only assert boundaries with medium/high confidence.
    Weak candidates are returned under uncertain_boundaries and do not split
    segments. Every page is assigned to exactly one primary segment; pages are
    never dropped or duplicated.
    """
    normalized_pages = [
        normalize_page_record(page, nyscef_document_number) for page in (pages or [])
    ]

    uncertain_boundaries = []
    asserted_starts = []  # list of (page_index, label, title, confidence, signals)

    for index, page in enumerate(normalized_pages):
        signals = detect_page_exhibit_signals(page)
        if not signals:
            continue

        confidence = score_exhibit_boundary_confidence(signals)
        label, title = _primary_signal_label_title(signals)
        page_number = page["page_number"]
        evidence = [
            {
                "kind": s.get("kind"),
                "strength": s.get("strength"),
                "detail": s.get("detail"),
                "exhibit_label": s.get("exhibit_label"),
            }
            for s in signals
        ]

        candidate = {
            "page_number": page_number,
            "page_id": page["page_id"],
            "exhibit_label": label,
            "exhibit_title": title,
            "boundary_confidence": confidence,
            "boundary_evidence": evidence,
        }

        if confidence in EXHIBIT_BOUNDARY_ASSERT_CONFIDENCE and label:
            # Avoid re-asserting the same label on the immediately continued page
            # when the heading merely repeats with no new cover evidence change.
            if asserted_starts:
                prev = asserted_starts[-1]
                if prev["exhibit_label"] == label and prev["page_index"] == index - 1:
                    # Treat repeated label on next page as continuation noise unless
                    # this page is itself a sparse cover for a *different* span.
                    if not any(s.get("kind") == "cover_label" for s in signals):
                        uncertain_boundaries.append(candidate)
                        continue
            asserted_starts.append(
                {
                    "page_index": index,
                    "exhibit_label": label,
                    "exhibit_title": title,
                    "boundary_confidence": confidence,
                    "boundary_evidence": evidence,
                }
            )
        else:
            uncertain_boundaries.append(candidate)

    segments = []
    segment_index = 1
    page_count = len(normalized_pages)

    def _emit_segment(start_idx, end_idx, segment_type, label, title, confidence, evidence):
        nonlocal segment_index
        if start_idx > end_idx or start_idx < 0 or end_idx >= page_count:
            return

        slice_pages = normalized_pages[start_idx : end_idx + 1]
        segment = {
            "segment_id": make_segment_id(nyscef_document_number, segment_index),
            "nyscef_document_number": coerce_nyscef_document_number(
                nyscef_document_number
            ),
            "segment_type": segment_type,
            "exhibit_label": label,
            "exhibit_title": title,
            "start_page": slice_pages[0]["page_number"],
            "end_page": slice_pages[-1]["page_number"],
            "page_ids": [p["page_id"] for p in slice_pages],
            "boundary_confidence": confidence,
            "boundary_evidence": list(evidence or []),
        }
        segments.append(segment)
        segment_index += 1

    if not asserted_starts:
        if page_count:
            _emit_segment(
                0,
                page_count - 1,
                "parent",
                None,
                None,
                "high",
                [
                    {
                        "kind": "no_embedded_exhibit",
                        "strength": "strong",
                        "detail": "No evidence-backed embedded exhibit boundary detected",
                        "exhibit_label": None,
                    }
                ],
            )
        return {
            "segments": segments,
            "uncertain_boundaries": uncertain_boundaries,
        }

    # Parent material before the first asserted exhibit, if any.
    first_start = asserted_starts[0]["page_index"]
    if first_start > 0:
        _emit_segment(
            0,
            first_start - 1,
            "parent",
            None,
            None,
            "high",
            [
                {
                    "kind": "parent_prefix",
                    "strength": "strong",
                    "detail": "Filing material preceding first embedded exhibit",
                    "exhibit_label": None,
                }
            ],
        )

    for i, start in enumerate(asserted_starts):
        start_idx = start["page_index"]
        if i + 1 < len(asserted_starts):
            end_idx = asserted_starts[i + 1]["page_index"] - 1
        else:
            end_idx = page_count - 1

        _emit_segment(
            start_idx,
            end_idx,
            "exhibit",
            start["exhibit_label"],
            start["exhibit_title"],
            start["boundary_confidence"],
            start["boundary_evidence"],
        )

    # Integrity: every page id appears exactly once across primary segments.
    assigned = [page_id for seg in segments for page_id in seg["page_ids"]]
    expected = [p["page_id"] for p in normalized_pages]
    if assigned != expected:
        # Repair by collapsing to a single parent segment rather than dropping
        # or duplicating pages. Uncertain candidates are still returned.
        segments = []
        segment_index = 1
        _emit_segment(
            0,
            page_count - 1,
            "parent",
            None,
            None,
            "low",
            [
                {
                    "kind": "integrity_repair",
                    "strength": "strong",
                    "detail": "Segment boundaries repaired to preserve page coverage",
                    "exhibit_label": None,
                }
            ],
        )

    return {
        "segments": segments,
        "uncertain_boundaries": uncertain_boundaries,
    }


def clean_case_party(value):
    value = clean_text(value)

    value = re.sub(r"(?i)\bSUPREME COURT OF THE STATE OF NEW YORK\b", "", value)
    value = re.sub(r"(?i)\bSTATE OF NEW YORK\b", "", value)
    value = re.sub(r"(?i)\bCOUNTY OF [A-Z\s]+\b", "", value)
    value = re.sub(r"(?i)\bINDEX\s*(NO\.?|NUMBER)?\s*[:#]?\s*[0-9]{4,8}/?[0-9]{0,4}\b", "", value)
    value = re.sub(r"(?i)\bthird[\s-]+party\s+plaintiffs?\b", "", value)
    value = re.sub(r"(?i)\bthird[\s-]+party\s+defendants?\b", "", value)
    value = re.sub(r"(?i)\brespondents?\s+on\s+(?:the\s+)?appeal\b", "", value)
    value = re.sub(r"(?i)\bPlaintiff[s]?\b", "", value)
    value = re.sub(r"(?i)\bDefendant[s]?\b", "", value)
    value = re.sub(r"(?i)\bPetitioner[s]?\b", "", value)
    value = re.sub(r"(?i)\bRespondent[s]?\b", "", value)
    value = re.sub(r"(?i)\bAppellants?\b", "", value)
    value = re.sub(r"(?i)\bAppellees?\b", "", value)

    value = value.replace(" -against- ", " ")
    value = value.replace(" against ", " ")

    value = re.sub(r"[_|]+", " ", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip(" ,.-")


def classify_by_filename(filename):
    name = filename.lower()

    if "selected case" in name or "search result" in name:
        return "selected_case"

    if "complaint" in name:
        return "complaint"

    if "answer" in name:
        return "answer"

    if "notice of motion" in name or "motion" in name:
        return "motion"

    if "affirmation" in name or "affidavit" in name or "declaration" in name:
        return "affirmation"

    if "opposition" in name or "opp" in name:
        return "opposition"

    if "reply" in name:
        return "reply"

    if "exhibit" in name or "exh" in name:
        return "exhibit"

    if "memo" in name or "memorandum" in name or "memorandum of law" in name:
        return "memo"

    if "order" in name or "decision" in name or "judgment" in name:
        return "order"

    return "other"


def extract_txt(path):
    try:
        return path.read_text(errors="ignore")
    except Exception:
        return ""


def extract_pdf_native_pages(path):
    """Return one native text entry per physical PDF page (including empties)."""
    if PdfReader is None:
        return []

    try:
        reader = PdfReader(str(path))
        pages = []

        for index, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            pages.append({"page_number": index, "text": text})

        print(f"PDF NATIVE [{Path(path).name}] pages={len(pages)}")
        return pages

    except Exception as e:
        print(f"PDF NATIVE FAILED [{Path(path).name}] -> {e}")
        return []


def extract_pdf_ocr_page(path, page_number):
    """OCR a single physical PDF page. No document-wide page cap."""
    if pytesseract is None or convert_from_path is None:
        print(f"OCR UNAVAILABLE [{Path(path).name}]")
        return ""

    try:
        print(f"OCR PAGE {page_number} [{Path(path).name}]")

        images = convert_from_path(
            str(path),
            dpi=250,
            first_page=page_number,
            last_page=page_number,
        )

        if not images:
            return ""

        return pytesseract.image_to_string(images[0]) or ""

    except Exception as e:
        print(f"OCR FAILED [{Path(path).name} page={page_number}] -> {e}")
        return ""


def extract_pdf_document(path, nyscef_document_number=None, *, allow_filename_nyscef_parse=True):
    """
    Extract every physical PDF page with per-page OCR fallback.

    Returns text, pages, page_count, and nyscef_document_number.
    """
    path = Path(path)

    if nyscef_document_number is not None:
        nyscef_document_number = coerce_nyscef_document_number(nyscef_document_number)
    elif allow_filename_nyscef_parse:
        nyscef_document_number = parse_nyscef_document_number_from_filename(path.name)
    else:
        nyscef_document_number = None

    native_pages = extract_pdf_native_pages(path)
    page_records = []

    for native in native_pages:
        page_number = native["page_number"]
        native_text = clean_text(native.get("text"))

        if len(native_text) >= OCR_MIN_TEXT_LENGTH:
            page_records.append(
                build_page_record(
                    page_number,
                    native_text,
                    "native",
                    nyscef_document_number,
                )
            )
            continue

        print(
            f"PDF LOW TEXT [{path.name}] page={page_number} "
            f"chars={len(native_text)} attempting OCR fallback"
        )

        ocr_text = clean_text(extract_pdf_ocr_page(path, page_number))

        if len(ocr_text) > len(native_text):
            print(f"PDF OCR SUCCESS [{path.name}] page={page_number}")
            page_records.append(
                build_page_record(
                    page_number,
                    ocr_text,
                    "ocr",
                    nyscef_document_number,
                )
            )
        elif native_text:
            page_records.append(
                build_page_record(
                    page_number,
                    native_text,
                    "native",
                    nyscef_document_number,
                )
            )
        else:
            page_records.append(
                build_page_record(
                    page_number,
                    "",
                    "empty",
                    nyscef_document_number,
                )
            )

    aggregate = aggregate_page_text(page_records)

    if page_records:
        print(
            f"PDF OK [{path.name}] pages={len(page_records)} "
            f"chars={len(aggregate)}"
        )

    return {
        "text": aggregate,
        "pages": page_records,
        "page_count": len(page_records),
        "nyscef_document_number": nyscef_document_number,
    }


def extract_pdf(path):
    """Backward-compatible aggregate text extraction."""
    return extract_pdf_document(path)["text"]


def extract_docx(path):
    if Document is None:
        return ""

    try:
        doc = Document(str(path))
        paragraphs = []

        for paragraph in doc.paragraphs:
            text = paragraph.text.strip()

            if text:
                paragraphs.append(text)

        return "\n".join(paragraphs)

    except Exception:
        return ""


def extract_text(path):
    suffix = path.suffix.lower()

    if suffix == ".txt":
        return extract_txt(path)

    if suffix == ".pdf":
        return extract_pdf(path)

    if suffix == ".docx":
        return extract_docx(path)

    return ""


def should_skip_path(path):
    parts = set(path.parts)

    for folder in SKIP_FOLDERS:
        if folder in parts:
            return True

    if path.name.startswith("."):
        return True

    return False


def find_matter_files(folder_path):
    folder = Path(folder_path)

    if not folder.exists() or not folder.is_dir():
        return []

    files = []

    for path in folder.rglob("*"):
        if should_skip_path(path):
            continue

        if not path.is_file():
            continue

        if path.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue

        files.append(path)

    return sorted(files, key=lambda p: str(p).lower())


def read_matter_folder(folder_path=None, inventory_path=None):
    folder = resolve_matter_folder(folder_path)
    files = find_matter_files(folder)

    resolved_inventory_path = resolve_inventory_path(inventory_path)
    inventory = load_nyscef_filing_inventory(resolved_inventory_path)
    inventory_by_filename = index_inventory_by_filename(inventory)
    inventory_enabled = inventory is not None

    documents = []

    for path in files:
        print(f"\nPROCESSING FILE: {path.name}")

        provenance = None
        verified_nyscef = None

        if inventory_enabled and path.suffix.lower() == ".pdf":
            provenance = lookup_inventory_provenance(path, inventory_by_filename)

            if provenance["status"] == "non_canonical_duplicate":
                print(
                    f"INVENTORY SKIP DUPLICATE [{path.name}] "
                    f"nyscef={provenance.get('nyscef_document_number')}"
                )
                continue

            if provenance["status"] == "verified":
                verified_nyscef = provenance.get("nyscef_document_number")
            else:
                print(
                    f"INVENTORY PROVENANCE UNRESOLVED [{path.name}] "
                    f"status={provenance['status']}"
                )
                # Do not assign a guessed NYSCEF number from the filename.
                verified_nyscef = None

        doc_type = classify_by_filename(path.name)

        document = {
            "filename": path.name,
            "title": path.name,
            "path": str(path),
            "relative_path": str(path.relative_to(folder)) if folder.exists() else str(path),
            "folder": str(path.parent),
            "type": doc_type,
            "category": doc_type,
            "group": DOCUMENT_GROUPS.get(doc_type, DOCUMENT_GROUPS["other"]),
            "source": "folder",
        }

        if path.suffix.lower() == ".pdf":
            if inventory_enabled:
                if provenance is not None and provenance.get("status") == "verified":
                    pdf_doc = extract_pdf_document(
                        path,
                        nyscef_document_number=verified_nyscef,
                    )
                else:
                    # Inventory configured but provenance unresolved: never guess.
                    pdf_doc = extract_pdf_document(
                        path,
                        nyscef_document_number=None,
                        allow_filename_nyscef_parse=False,
                    )
            else:
                pdf_doc = extract_pdf_document(path)

            extracted_text = pdf_doc["text"]
            document["text"] = extracted_text
            document["preview"] = extracted_text[:800]
            document["pages"] = pdf_doc["pages"]
            document["page_count"] = pdf_doc["page_count"]
            document["nyscef_document_number"] = pdf_doc["nyscef_document_number"]
            if provenance is not None:
                document["nyscef_provenance_status"] = provenance["status"]
        else:
            extracted_text = clean_text(extract_text(path))
            document["text"] = extracted_text
            document["preview"] = extracted_text[:800]

        print(
            f"CLASSIFIED [{path.name}] "
            f"type={doc_type} "
            f"chars={len(document['text'])}"
        )

        documents.append(document)

    return documents


def selected_case_to_document(selected_case):
    if not selected_case:
        return None

    title = clean_text(selected_case.get("title") or selected_case.get("case_name") or "Selected Case")
    court = clean_text(selected_case.get("court"))
    date = clean_text(selected_case.get("date"))
    citation = clean_text(selected_case.get("citation"))
    outcome = clean_text(selected_case.get("outcome"))
    motion = clean_text(selected_case.get("motion"))
    cause = clean_text(selected_case.get("primary_cause"))
    holding = clean_text(selected_case.get("holding"))
    rule = clean_text(selected_case.get("rule"))

    text = clean_text(
        selected_case.get("formatted_text")
        or selected_case.get("text")
        or selected_case.get("summary")
        or selected_case.get("snippet")
        or ""
    )

    metadata_lines = []

    if title:
        metadata_lines.append(title)

    if court:
        metadata_lines.append(f"Court: {court}")

    if date:
        metadata_lines.append(f"Date: {date}")

    if citation:
        metadata_lines.append(f"Citation: {citation}")

    if motion:
        metadata_lines.append(f"Motion: {motion}")

    if outcome:
        metadata_lines.append(f"Outcome: {outcome}")

    if cause:
        metadata_lines.append(f"Cause: {cause}")

    if rule:
        metadata_lines.append(f"Rule: {rule}")

    if holding:
        metadata_lines.append(f"Holding: {holding}")

    combined = clean_text("\n".join(metadata_lines + [text]))

    return {
        "filename": f"Selected Case - {title}",
        "title": title,
        "path": "",
        "relative_path": "Selected from search results",
        "folder": "",
        "type": "selected_case",
        "category": "selected_case",
        "group": DOCUMENT_GROUPS["selected_case"],
        "text": combined,
        "preview": combined[:800],
        "source": "selected_case",
        "court": court,
        "date": date,
        "citation": citation,
        "motion": motion,
        "outcome": outcome,
        "primary_cause": cause,
        "holding": holding,
        "rule": rule,
        "case_id": clean_text(selected_case.get("case_id")),
    }


def normalize_document(document, *, include_exhibit_segments=None):
    filename = clean_text(
        document.get("filename")
        or document.get("name")
        or document.get("title")
        or "Untitled Document"
    )

    doc_type = (
        document.get("type")
        or document.get("category")
        or classify_by_filename(filename)
    )

    group = DOCUMENT_GROUPS.get(doc_type, DOCUMENT_GROUPS["other"])

    nyscef_document_number = None
    if "nyscef_document_number" in document:
        nyscef_document_number = coerce_nyscef_document_number(
            document.get("nyscef_document_number")
        )
    else:
        nyscef_document_number = parse_nyscef_document_number_from_filename(filename)

    pages = None
    page_count = None

    if "pages" in document and document.get("pages") is not None:
        pages = [
            normalize_page_record(page, nyscef_document_number)
            for page in document.get("pages") or []
        ]
        page_count = document.get("page_count")
        if page_count is None:
            page_count = len(pages)
        else:
            try:
                page_count = int(page_count)
            except (TypeError, ValueError):
                page_count = len(pages)
        text = aggregate_page_text(pages)
    else:
        text = clean_text(document.get("text", ""))
        if "page_count" in document and document.get("page_count") is not None:
            try:
                page_count = int(document.get("page_count"))
            except (TypeError, ValueError):
                page_count = None

    normalized = {
        "filename": filename,
        "title": clean_text(document.get("title") or filename),
        "path": document.get("path", ""),
        "relative_path": document.get("relative_path", document.get("path", "")),
        "folder": document.get("folder", ""),
        "type": doc_type,
        "category": doc_type,
        "group": group,
        "text": text,
        "preview": text[:800],
        "source": document.get("source", "manual"),
        "court": document.get("court", ""),
        "date": document.get("date", ""),
        "citation": document.get("citation", ""),
        "motion": document.get("motion", ""),
        "outcome": document.get("outcome", ""),
        "primary_cause": document.get("primary_cause", ""),
        "holding": document.get("holding", ""),
        "rule": document.get("rule", ""),
        "case_id": document.get("case_id", ""),
    }

    if "nyscef_document_number" in document or nyscef_document_number is not None:
        normalized["nyscef_document_number"] = nyscef_document_number

    if pages is not None:
        normalized["pages"] = pages

    if page_count is not None:
        normalized["page_count"] = page_count

    # Additive opt-in: kwarg wins; otherwise honor document flag. Default off
    # so existing consumers receive unchanged document/page structures.
    # If a prior normalize already attached exhibit_segments, keep opting in so
    # group_documents / re-normalize paths do not silently drop them.
    if include_exhibit_segments is None:
        if "include_exhibit_segments" in document:
            include_exhibit_segments = bool(document.get("include_exhibit_segments"))
        else:
            include_exhibit_segments = "exhibit_segments" in document

    if include_exhibit_segments and pages is not None:
        segmentation = segment_embedded_exhibits(pages, nyscef_document_number)
        normalized["exhibit_segments"] = segmentation["segments"]
        if segmentation["uncertain_boundaries"]:
            normalized["uncertain_exhibit_boundaries"] = segmentation[
                "uncertain_boundaries"
            ]

    return normalized


def group_documents(documents):
    grouped = {label: [] for label in DOCUMENT_GROUPS.values()}

    for document in documents:
        normalized = normalize_document(document)
        grouped[normalized["group"]].append(normalized)

    return grouped


def combined_text(documents, limit=50000):
    chunks = []

    for doc in documents:
        text = clean_text(doc.get("text", ""))

        if text:
            chunks.append(text)

    return clean_text(" ".join(chunks))[:limit]


def extract_index_number(text):
    patterns = [
        r"index\\s*(?:no\\.?|number)?\\s*[:#]?\\s*([0-9]{4,8}/[0-9]{4})",
        r"index\\s*(?:no\\.?|number)?\\s*[:#]?\\s*([0-9]{5,8})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            return clean_text(match.group(1))

    return "—"


def extract_case_name(text):
    patterns = [
        r"([A-Z][A-Za-z0-9&.,'\\-\\s]{2,80})\\s+v\\.?\\s+([A-Z][A-Za-z0-9&.,'\\-\\s]{2,80})",
        r"([A-Z][A-Za-z0-9&.,'\\-\\s]{2,80})\\s+against\\s+([A-Z][A-Za-z0-9&.,'\\-\\s]{2,80})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            left = clean_case_party(match.group(1))
            right = clean_case_party(match.group(2))

            if left and right:
                return f"{left} v. {right}"

    return "Matter Builder"


def extract_parties(case_name):
    if " v. " not in case_name:
        return {"plaintiff": "—", "defendant": "—"}

    left, right = case_name.split(" v. ", 1)

    return {
        "plaintiff": clean_case_party(left) or "—",
        "defendant": clean_case_party(right) or "—",
    }


def detect_motion_posture(documents, text):
    names = " ".join(doc.get("filename", "") for doc in documents).lower()

    haystack = f"{names} {text.lower()}"

    if "summary judgment" in haystack:
        return "Summary judgment motion"

    if "dismiss" in haystack or "3211" in haystack:
        return "Motion to dismiss"

    if "default judgment" in haystack:
        return "Default judgment motion"

    if "discovery" in haystack or "compel" in haystack:
        return "Discovery motion"

    if "opposition" in haystack:
        return "Opposition papers"

    return "—"


def detect_procedural_posture(text):
    lower = text.lower()

    if "complaint" in lower and "answer" in lower and "motion" in lower:
        return "Pleadings and motion papers are present."

    if "complaint" in lower and "motion" in lower:
        return "Complaint and motion papers are present."

    if "order" in lower or "decision" in lower:
        return "Prior order or decision appears to be present."

    return "Procedural posture not yet detected from extracted text."


def strongest_motion_documents(documents):
    ranked = []

    weights = {
        "selected_case": 120,
        "motion": 100,
        "opposition": 90,
        "affirmation": 80,
        "memo": 75,
        "reply": 70,
        "order": 60,
        "complaint": 40,
        "answer": 35,
        "exhibit": 20,
        "other": 10,
    }

    for doc in documents:
        doc_type = doc.get("type", "other")
        score = weights.get(doc_type, 0)

        ranked.append(
            {
                "filename": doc.get("filename", ""),
                "type": doc_type,
                "group": doc.get("group", ""),
                "score": score,
            }
        )

    ranked.sort(key=lambda item: item["score"], reverse=True)

    return ranked[:5]


def selected_case_summary(documents):
    for doc in documents:
        if doc.get("type") == "selected_case":
            return {
                "title": doc.get("title", ""),
                "court": doc.get("court", ""),
                "date": doc.get("date", ""),
                "citation": doc.get("citation", ""),
                "motion": doc.get("motion", ""),
                "outcome": doc.get("outcome", ""),
                "primary_cause": doc.get("primary_cause", ""),
                "holding": doc.get("holding", ""),
                "rule": doc.get("rule", ""),
                "case_id": doc.get("case_id", ""),
            }

    return None


# ---------------------------------------------------------------------------
# Citation-grounded litigation case map (opt-in / additive)
# ---------------------------------------------------------------------------
#
# Built from canonical page records and embedded-exhibit segments. Conservative
# deterministic helpers surface record-supported candidates; they do not produce
# attorney-level semantic conclusions. Default get_matter consumers are unchanged.

CASE_MAP_ASSERTION_KINDS = (
    "verified_record_fact",
    "party_allegation",
    "legal_position",
    "inference",
    "unknown",
)

CASE_MAP_NODE_COLLECTIONS = (
    "parties",
    "policies",
    "claims",
    "defenses",
    "allegations",
    "evidence",
    "timeline_events",
    "motions",
    "court_orders",
)

CASE_MAP_NODE_TYPE_TO_COLLECTION = {
    "party": "parties",
    "policy": "policies",
    "claim": "claims",
    "defense": "defenses",
    "allegation": "allegations",
    "evidence": "evidence",
    "timeline_event": "timeline_events",
    "motion": "motions",
    "court_order": "court_orders",
}

CASE_MAP_EXCERPT_MAX = 240

CAUSE_OF_ACTION_RE = re.compile(
    r"(?is)\b(?:(?P<ordinal>first|second|third|fourth|fifth|sixth|"
    r"seventh|eighth|ninth|tenth|\d+(?:st|nd|rd|th)?)\s+)?"
    r"cause\s+of\s+action\b(?:\s*[-–—:]\s*|\s+for\s+)?(?P<title>[^\n.]{0,80})?"
)

AFFIRMATIVE_DEFENSE_RE = re.compile(
    r"(?is)\b(?:(?P<ordinal>first|second|third|fourth|fifth|sixth|"
    r"seventh|eighth|ninth|tenth|\d+(?:st|nd|rd|th)?)\s+)?"
    r"(?:affirmative\s+)?defense\b(?:\s*[-–—:]\s*|\s+of\s+|\s+for\s+)?"
    r"(?P<title>[^\n.]{0,80})?"
)

POLICY_REF_RE = re.compile(
    r"(?i)\b(?:insurance\s+)?polic(?:y|ies)\b"
    r"(?:\s+(?:number|no\.?|#)\s*[:#]?\s*([A-Z0-9][-A-Z0-9/]{2,24}))?"
)

ALLEGATION_SPEAKER_RE = re.compile(
    r"(?i)\b(?P<speaker>plaintiffs?|defendants?|petitioners?|respondents?)\b"
    r"(?:\s*,)?\s+"
    r"(?:alleges?|contends?|claims?|avers?)(?:\s+that)?\b"
)

# Procedural roles supported for party extraction (name-first and role-first).
_PARTY_ROLE_LABEL = (
    r"third[\s-]+party\s+plaintiffs?|"
    r"third[\s-]+party\s+defendants?|"
    r"respondents?\s+on\s+(?:the\s+)?appeal|"
    r"plaintiffs?|"
    r"defendants?|"
    r"petitioners?|"
    r"respondents?|"
    r"appellants?|"
    r"appellees?"
)

# Role-first covers "Plaintiff Acme LLC is ..."; name-first requires a comma
# ("Acme LLC, plaintiff") so section headings like PARTIES are not captured.
# (?-i:[A-Z]) keeps a true capital start even though roles are case-insensitive.
PARTY_ROLE_RE = re.compile(
    r"(?:"
    r"\b(?P<role_leading>" + _PARTY_ROLE_LABEL + r")\s+"
    r"(?P<name_leading>(?-i:[A-Z])[A-Za-z0-9&.,' -]{0,80}?)"
    r"(?=\s+(?:is|was|are|were|has|have|brings|commenced|,|\.|$|;))|"
    r"\b(?P<name>(?-i:[A-Z])[A-Za-z0-9&.,' -]{0,80}?),\s*"
    r"(?P<role>" + _PARTY_ROLE_LABEL + r")\b"
    r")",
    re.IGNORECASE,
)

_PARTY_NAME_BLOCKLIST = frozenset(
    {
        "parties",
        "wherefore",
        "venue",
        "jurisdiction",
        "introduction",
        "preliminary statement",
        "nature of the action",
        "nature of action",
        "verification",
    }
)

_PARTY_NAME_SUFFIX_ONLY = frozenset(
    {
        "inc",
        "llc",
        "lp",
        "llp",
        "co",
        "corp",
        "ltd",
        "pc",
        "pa",
        "fund",
        "company",
        "corporation",
        "partnership",
    }
)

# Role-bearing pleading body / PARTIES-section cues (retrieval preference).
PARTY_ROLE_BEARING_RE = re.compile(
    r"(?i)\b(?:"
    r"parties\b|"
    r"third[\s-]+party\s+(?:plaintiffs?|defendants?)|"
    r"plaintiffs?\b|"
    r"defendants?\b|"
    r"petitioners?\b|"
    r"respondents?(?:\s+on\s+(?:the\s+)?appeal)?\b|"
    r"appellants?\b|"
    r"appellees?\b|"
    r"limited\s+liability\s+(?:company|corporation)|"
    r"sued\s+herein|"
    r"joined\s+(?:herein|as\s+a\s+party)|"
    r"necessary\s+party|"
    r"real\s+party\s+in\s+interest|"
    r"notice\s+defendants?|"
    r"named\s+insured|"
    r"additional\s+insured|"
    r"principal\s+place\s+of\s+business|"
    r"place\s+of\s+business|"
    r"residen(?:t|ce|ts)\b|"
    r"resid(?:es|ed|ing)\b|"
    r"\bindividuals?\b|"
    r"(?:domestic|foreign)\s+corporation"
    r")"
)

# Optional page / section / article / Roman-numeral / punctuation prefix before a
# pleading section heading (e.g. "14 PARTIES", "SECTION 2 — PARTIES", "ARTICLE III:").
_SECTION_HEADING_PREFIX = (
    r"(?:"
    r"(?:section|article|part)\s+[ivxlcdm\d]+(?:\s*[.:=\-—–]\s*|\s+)|"
    r"(?:[ivxlcdm]+|\d+)(?:\.\d+)*[.)]?\s+"
    r")?"
)

# Section-heading boundaries: start of string/unit, newline, sentence end, or a
# colon-style lead-in such as "allege as follows: INTRODUCTION".
_SECTION_HEADING_BOUNDARY = r"(?:^|[\n\r]|(?<=\.)\s|(?<=:)\s*)"

# Contiguous PARTIES-section heading (works on newline or whitespace-collapsed text).
PARTIES_SECTION_HEADING_RE = re.compile(
    r"(?i)(?:^|[\n\r])\s*" + _SECTION_HEADING_PREFIX + r"(?:the\s+)?parties(?:\s+to\s+(?:this\s+)?"
    r"(?:action|proceeding|litigation))?\s*:?(?=\s*(?:$|\d+\.|"
    r"(?:plaintiffs?|defendants?|petitioners?|respondents?|third\b)))"
)

# Concise opening sections retained for party-role evidence (not hard stops).
_PARTY_ROLE_RETAINABLE_SECTION_NAMES = (
    r"nature\s+of\s+(?:the\s+)?action|"
    r"preliminary\s+statement|"
    r"introduction"
)

# Detailed narrative / claim sections that end party-role evidence retention.
_PARTY_ROLE_HARD_STOP_SECTION_NAMES = (
    r"facts?(?:\s+common\s+to\s+all\s+(?:counts|claims))?|"
    r"factual\s+background|"
    r"background|"
    r"general\s+allegations|"
    r"causes?\s+of\s+action|"
    r"(?:first|second|third|fourth|fifth)\s+cause\s+of\s+action|"
    r"count\s+(?:[ivxlcdm]+|\d+)|"
    r"as\s+and\s+for\s+(?:a\s+)?(?:first\s+)?cause\s+of\s+action|"
    r"wherefore|"
    r"prayer\s+for\s+relief|"
    r"affirmative\s+defenses|"
    r"verification"
)

# Jurisdiction/venue headings: page-expansion stops, but passage extraction may
# continue to keep party-tied forum allegations and drop generic ones.
_PARTY_ROLE_JURISDICTION_VENUE_SECTION_NAMES = (
    r"jurisdiction(?:\s+and\s+venue)?|"
    r"venue"
)

_MAJOR_PLEADING_SECTION_NAMES = (
    _PARTY_ROLE_JURISDICTION_VENUE_SECTION_NAMES
    + r"|"
    + _PARTY_ROLE_HARD_STOP_SECTION_NAMES
    + r"|"
    + _PARTY_ROLE_RETAINABLE_SECTION_NAMES
)

# Require a true capital after the heading so numbered allegations such as
# "6. Venue is proper because Defendant..." are not treated as section heads.
_SECTION_HEADING_TAIL = r"\s*:?(?=\s*(?:$|\d+\.|(?-i:[A-Z(\"'])))"

# Major pleading sections that end contiguous PARTIES expansion.
MAJOR_PLEADING_SECTION_HEADING_RE = re.compile(
    r"(?i)" + _SECTION_HEADING_BOUNDARY + _SECTION_HEADING_PREFIX + r"(?:"
    + _MAJOR_PLEADING_SECTION_NAMES
    + r")"
    + _SECTION_HEADING_TAIL
)

_MAJOR_SECTION_START_RE = re.compile(
    r"(?i)^\s*" + _SECTION_HEADING_PREFIX + r"(?:"
    + _MAJOR_PLEADING_SECTION_NAMES
    + r")"
    + _SECTION_HEADING_TAIL
)

_PARTY_ROLE_RETAINABLE_SECTION_HEADING_RE = re.compile(
    r"(?i)" + _SECTION_HEADING_BOUNDARY + _SECTION_HEADING_PREFIX + r"(?:"
    + _PARTY_ROLE_RETAINABLE_SECTION_NAMES
    + r")"
    + _SECTION_HEADING_TAIL
)

_PARTY_ROLE_RETAINABLE_SECTION_START_RE = re.compile(
    r"(?i)^\s*" + _SECTION_HEADING_PREFIX + r"(?:"
    + _PARTY_ROLE_RETAINABLE_SECTION_NAMES
    + r")"
    + _SECTION_HEADING_TAIL
)

_PARTY_ROLE_HARD_STOP_SECTION_HEADING_RE = re.compile(
    r"(?i)" + _SECTION_HEADING_BOUNDARY + _SECTION_HEADING_PREFIX + r"(?:"
    + _PARTY_ROLE_HARD_STOP_SECTION_NAMES
    + r")"
    + _SECTION_HEADING_TAIL
)

_PARTY_ROLE_HARD_STOP_SECTION_START_RE = re.compile(
    r"(?i)^\s*" + _SECTION_HEADING_PREFIX + r"(?:"
    + _PARTY_ROLE_HARD_STOP_SECTION_NAMES
    + r")"
    + _SECTION_HEADING_TAIL
)

_PARTY_ROLE_JURISDICTION_VENUE_SECTION_START_RE = re.compile(
    r"(?i)^\s*" + _SECTION_HEADING_PREFIX + r"(?:"
    + _PARTY_ROLE_JURISDICTION_VENUE_SECTION_NAMES
    + r")"
    + _SECTION_HEADING_TAIL
)

# Ends concise intro / nature-of-action retention when walking across pages.
_PARTY_ROLE_INTRO_END_SECTION_NAMES = (
    r"(?:the\s+)?parties|"
    + _PARTY_ROLE_HARD_STOP_SECTION_NAMES
    + r"|"
    + _PARTY_ROLE_JURISDICTION_VENUE_SECTION_NAMES
)

_PARTY_ROLE_INTRO_END_SECTION_HEADING_RE = re.compile(
    r"(?i)" + _SECTION_HEADING_BOUNDARY + _SECTION_HEADING_PREFIX + r"(?:"
    + _PARTY_ROLE_INTRO_END_SECTION_NAMES
    + r")"
    + _SECTION_HEADING_TAIL
)

_PARTY_ROLE_INTRO_END_SECTION_START_RE = re.compile(
    r"(?i)^\s*" + _SECTION_HEADING_PREFIX + r"(?:"
    + _PARTY_ROLE_INTRO_END_SECTION_NAMES
    + r")"
    + _SECTION_HEADING_TAIL
)

_PARTIES_HEADING_START_RE = re.compile(
    r"(?i)^\s*" + _SECTION_HEADING_PREFIX + r"(?:the\s+)?parties\b"
)

# Body markers that end a pleading caption block.
PLEADING_CAPTION_END_RE = re.compile(
    r"(?i)" + _SECTION_HEADING_BOUNDARY + r"\s*" + _SECTION_HEADING_PREFIX + r"(?:"
    r"parties|"
    r"the\s+parties|"
    r"jurisdiction|"
    r"venue|"
    r"preliminary\s+statement|"
    r"nature\s+of\s+(?:the\s+)?action|"
    r"introduction|"
    r"summons|"
    r"complaint|"
    r"verified\s+complaint|"
    r"petition|"
    r"to\s+the\s+above[\s-]+named|"
    r"please\s+take\s+notice|"
    r"the\s+undersigned|"
    r"plaintiffs?\s*,?\s+by(?:\s+and\s+through)?\s+(?:their|its)\s+attorneys?"
    r")\b"
)

# Affirmation / service filings must not receive complete-caption treatment.
_AFFIRMATION_OR_SERVICE_FILING_RE = re.compile(
    r"(?i)\b(?:"
    r"affirmation(?:\s+of\s+(?:service|mailing|good\s+faith))?|"
    r"affidavit(?:\s+of\s+service)?|"
    r"proof\s+of\s+service|"
    r"admission\s+of\s+service|"
    r"affidavit\s+of\s+mailing|"
    r"certificate\s+of\s+service"
    r")\b"
)

# Concise party-role identity / qualification passage cues.
PARTY_ROLE_PASSAGE_RE = re.compile(
    r"(?i)\b(?:"
    r"plaintiffs?|defendants?|petitioners?|respondents?|appellants?|appellees?|"
    r"third[\s-]+party\s+(?:plaintiffs?|defendants?)|"
    r"notice\s+defendants?|named\s+insured|additional\s+insured|"
    r"joined(?:\s+herein|\s+as)?|sued\s+herein|necessary\s+party|"
    r"real\s+party\s+in\s+interest|"
    r"limited\s+liability\s+(?:company|corporation|partnership)|"
    r"domestic\s+corporation|foreign\s+corporation|"
    r"authorized\s+to\s+do\s+business|organized\s+(?:under|to)|"
    r"incorrectly\s+named|substituted\s+as|capacity|"
    r"is\s+a\s+(?:corporation|partnership|limited)|"
    r"are\s+(?:corporations|partnerships|limited)|"
    r"principal\s+place\s+of\s+business|place\s+of\s+business|"
    r"residen(?:t|ce|ts|cies)|resid(?:es|ed|ing)\b|"
    r"\bindividuals?\b|"
    r"(?:domestic|foreign)\s+(?:limited\s+liability\s+)?"
    r"(?:company|corporation|partnership)|"
    r"was\s+and\s+still\s+is\s+a\b|"
    r"duly\s+authorized\s+and\s+existing|"
    r"transacted\s+business|conducted\s+business|"
    r"engaged\s+in\s+(?:a\s+)?(?:business|commerce)|"
    r"doing\s+business\s+(?:in|within)|"
    r"venue\s+is\s+proper|jurisdiction\s+(?:and\s+venue\s+)?(?:is|are)\s+proper"
    r")"
)

# Forum business / activity cues tied to a pleaded party.
_PARTY_ROLE_FORUM_BUSINESS_RE = re.compile(
    r"(?i)\b(?:"
    r"transacted\s+business|"
    r"conducted\s+business|"
    r"engaged\s+in\s+(?:a\s+)?(?:business|commerce)|"
    r"doing\s+business\s+(?:in|within)|"
    r"systematically\s+(?:and\s+continuously\s+)?(?:transacted|conducted)|"
    r"business\s+(?:in|within)\s+(?:this|the)\s+"
    r"(?:state|county|city|forum|commonwealth)|"
    r"derives?\s+(?:substantial\s+)?(?:revenue|income)\s+from\b"
    r")"
)

# Party-tied jurisdiction / venue facts (not generic court-power boilerplate).
_PARTY_ROLE_PARTY_TIED_JURISDICTION_VENUE_RE = re.compile(
    r"(?i)\b(?:"
    r"venue\s+is\s+proper|"
    r"jurisdiction\s+(?:and\s+venue\s+)?(?:is|are)\s+proper|"
    r"(?:plaintiffs?|defendants?|petitioners?|respondents?)\s+"
    r"(?:resides?|resided|maintains?|maintained|is\s+found|may\s+be\s+found)|"
    r"resides?\s+in\s+(?:the\s+)?(?:county|state|city)|"
    r"principal\s+place\s+of\s+business|"
    r"transacted\s+business\s+in\s+(?:this|the)\s+(?:county|state|city)|"
    r"events?\s+(?:giving\s+rise|complained\s+of)\s+"
    r"(?:to\s+(?:the\s+)?(?:claim|action)\s+)?occurred\s+in"
    r")\b"
)

# Generic jurisdiction allegations with no party-specific forum tie.
_PARTY_ROLE_GENERIC_JURISDICTION_RE = re.compile(
    r"(?i)\b(?:"
    r"this\s+court\s+has\s+(?:personal\s+)?jurisdiction|"
    r"this\s+court\s+possesses?\s+(?:personal\s+)?jurisdiction|"
    r"jurisdiction\s+(?:is\s+)?(?:conferred|invoked|exists)|"
    r"jurisdiction\s+over\s+(?:the\s+)?(?:subject\s+matter|this\s+action)|"
    r"subject[\s-]matter\s+jurisdiction|"
    r"personal\s+jurisdiction(?:\s+over\s+(?:the\s+)?"
    r"(?:defendants?|plaintiffs?|petitioners?|respondents?))?|"
    r"jurisdiction\s+over\s+(?:the\s+)?"
    r"(?:defendants?|plaintiffs?|petitioners?|respondents?)\b"
    r")"
)

# Collective procedural roles are not named-party identity for forum retention.
_PARTY_ROLE_BARE_COLLECTIVE_ROLE_RE = re.compile(
    r"(?i)\b(?:the\s+)?(?:plaintiffs?|defendants?|petitioners?|respondents?)\b"
)

# Legal-entity / proper-name cues that establish named-party identity.
_PARTY_ROLE_NAMED_PARTY_ENTITY_RE = re.compile(
    r"\b(?-i:[A-Z][A-Za-z0-9&.,'-]+(?:\s+[A-Z][A-Za-z0-9&.,'-]+){0,7})\s+"
    r"(?i:LLC|L\.L\.C\.|Inc\.?|Incorporated|Corp\.?|Corporation|Ltd\.?|"
    r"LP|L\.P\.|LLP|L\.L\.P\.|PC|P\.C\.|PLLC|P\.L\.L\.C\.|"
    r"Company|Partnership|Associates|Trust|Group|Partners)\b"
)

_PARTY_ROLE_NAMED_AFTER_ROLE_RE = re.compile(
    r"(?i)\b(?:plaintiffs?|defendants?|petitioners?|respondents?)\s+"
    r"(?!herein\b|above[\s-]named\b|below\b|thereof\b|therein\b|"
    r"respectively\b|the\b|a\b|an\b|this\b|that\b|said\b|each\b|"
    r"all\b|both\b|named\b)"
    r"((?-i:[A-Z][A-Za-z0-9&.,'-]*(?:\s+[A-Z][A-Za-z0-9&.,'-]*){0,5}))"
)

_PARTY_ROLE_PARTY_ANCHOR_RE = re.compile(
    r"(?i)\b(?:"
    r"plaintiffs?|defendants?|petitioners?|respondents?|"
    r"appellants?|appellees?|third[\s-]+party|"
    r"herein|aforesaid|above[\s-]named"
    r")\b|"
    r"\b(?-i:[A-Z][A-Za-z0-9&.,'-]{1,60})\b"
)

# Words commonly fractured by OCR; used only for match-time healing.
_OCR_PARTY_ROLE_JOIN_WORDS = frozenset(
    {
        "additional",
        "association",
        "authorized",
        "business",
        "companies",
        "company",
        "condominium",
        "construction",
        "corporation",
        "corporations",
        "declaration",
        "defendant",
        "defendants",
        "domestic",
        "existing",
        "fictitious",
        "foreign",
        "individual",
        "individuals",
        "industries",
        "insured",
        "liability",
        "limited",
        "maintained",
        "named",
        "notice",
        "organized",
        "partnership",
        "partnerships",
        "plaintiff",
        "plaintiffs",
        "policies",
        "principal",
        "resident",
        "residents",
        "residing",
        "residence",
        "underwriters",
    }
)

# Identity / entity / residence cues matched with optional intra-word OCR spaces.
_PARTY_ROLE_ENTITY_RESIDENCE_PHRASES = (
    "principal place of business",
    "place of business",
    "domestic corporation",
    "foreign corporation",
    "domestic limited liability company",
    "foreign limited liability company",
    "limited liability company",
    "limited liability corporation",
    "limited liability partnership",
    "duly authorized and existing",
    "authorized to do business",
    "notice defendant",
    "notice defendants",
    "named insured",
    "additional insured",
    "resident of",
    "residents of",
    "residing in",
    "resides in",
    "is a resident",
    "is an individual",
    "are individuals",
    "was and still is a",
)


def _ocr_flexible_phrase_re(phrase):
    """Build a regex that tolerates OCR spaces inside and between words."""
    words = [w for w in re.split(r"\s+", str(phrase or "").strip()) if w]
    if not words:
        return None
    word_patterns = []
    for word in words:
        letters = [re.escape(ch) for ch in word if ch.isalnum() or ch in {"'", "-"}]
        if not letters:
            continue
        word_patterns.append(r"\s*".join(letters))
    if not word_patterns:
        return None
    return re.compile(r"(?i)" + r"\s+".join(word_patterns))


_PARTY_ROLE_ENTITY_RESIDENCE_OCR_RES = tuple(
    pat
    for pat in (_ocr_flexible_phrase_re(p) for p in _PARTY_ROLE_ENTITY_RESIDENCE_PHRASES)
    if pat is not None
)


def heal_ocr_intra_word_spaces(text):
    """
    Join alphabetic fragments split by OCR for matching only.

    Examples: "domesti c" -> "domestic", "com pany" -> "company".
    Does not invent content; only merges when the joined token is a known
    party-role vocabulary word.
    """
    raw = str(text or "")
    if not raw:
        return ""

    def _pass(value):
        tokens = re.findall(r"\S+|\s+", value)
        if len(tokens) <= 1:
            return value
        out = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.isspace() or i + 2 >= len(tokens):
                out.append(tok)
                i += 1
                continue
            nxt = tokens[i + 2] if tokens[i + 1].isspace() else None
            if nxt is None:
                out.append(tok)
                i += 1
                continue
            left_m = re.match(r"^([^A-Za-z]*)([A-Za-z]+)([^A-Za-z]*)$", tok)
            right_m = re.match(r"^([^A-Za-z]*)([A-Za-z]+)([^A-Za-z]*)$", nxt)
            if not left_m or not right_m or right_m.group(1) or left_m.group(3):
                out.append(tok)
                i += 1
                continue
            joined_alpha = f"{left_m.group(2)}{right_m.group(2)}".lower()
            if joined_alpha in _OCR_PARTY_ROLE_JOIN_WORDS:
                out.append(
                    f"{left_m.group(1)}{left_m.group(2)}"
                    f"{right_m.group(2)}{right_m.group(3)}"
                )
                i += 3
                continue
            out.append(tok)
            i += 1
        return "".join(out)

    prev = None
    current = raw
    # Bounded passes for multi-fragment breaks (e.g. "li a bility").
    for _ in range(6):
        if current == prev:
            break
        prev = current
        current = _pass(current)
    return current


def _party_role_unit_has_identity_signal(unit):
    """True when a passage unit carries party identity/role/entity/residence cues."""
    text = unit or ""
    if not text.strip():
        return False
    if PARTY_ROLE_PASSAGE_RE.search(text) or PARTY_ROLE_BEARING_RE.search(text):
        if (
            PARTY_ROLE_PASSAGE_RE.search(text)
            or re.search(
                r"(?i)\b(?:is|are|was|were|named|joined|sued|authorized|"
                r"organized|corporation|partnership|company|individual|"
                r"resident|residing|resides|residence|"
                r"principal\s+place|place\s+of\s+business|"
                r"notice\s+defendant|named\s+insured|"
                r"transacted\s+business|conducted\s+business|"
                r"venue\s+is\s+proper)\b",
                text,
            )
        ):
            return True
    for pattern in _PARTY_ROLE_ENTITY_RESIDENCE_OCR_RES:
        if pattern.search(text):
            return True
    healed = heal_ocr_intra_word_spaces(text)
    if healed != text:
        if PARTY_ROLE_PASSAGE_RE.search(healed) or PARTY_ROLE_BEARING_RE.search(healed):
            if (
                PARTY_ROLE_PASSAGE_RE.search(healed)
                or re.search(
                    r"(?i)\b(?:is|are|was|were|named|joined|sued|authorized|"
                    r"organized|corporation|partnership|company|individual|"
                    r"resident|residing|resides|residence|"
                    r"principal\s+place|place\s+of\s+business|"
                    r"notice\s+defendant|named\s+insured|"
                    r"transacted\s+business|conducted\s+business|"
                    r"venue\s+is\s+proper)\b",
                    healed,
                )
            ):
                return True
        for pattern in _PARTY_ROLE_ENTITY_RESIDENCE_OCR_RES:
            if pattern.search(healed):
                return True
    return False


def _party_role_unit_has_named_party_identity(unit):
    """
    True when a unit identifies a concrete named party.

    Bare collective roles (Plaintiffs/Defendants/Petitioners/Respondents) are
    not sufficient; require an entity/proper-name signal.
    """
    text = unit or ""
    if not text.strip():
        return False
    haystacks = [text]
    healed = heal_ocr_intra_word_spaces(text)
    if healed != text:
        haystacks.append(healed)
    for hay in haystacks:
        if _PARTY_ROLE_NAMED_PARTY_ENTITY_RE.search(hay):
            return True
        for match in _PARTY_ROLE_NAMED_AFTER_ROLE_RE.finditer(hay):
            name = (match.group(1) or "").strip()
            if not name:
                continue
            # Reject residual role-only or courtish tokens after the role label.
            first = name.split()[0].lower()
            if first in {
                "plaintiff",
                "plaintiffs",
                "defendant",
                "defendants",
                "petitioner",
                "petitioners",
                "respondent",
                "respondents",
                "court",
                "county",
                "state",
                "city",
                "forum",
            }:
                continue
            if re.search(r"[A-Za-z]{2,}", name):
                return True
    return False


def _party_role_unit_has_forum_business_signal(unit):
    """True for named-party forum business / activity allegations."""
    text = unit or ""
    if not text.strip():
        return False
    hay = text
    healed = heal_ocr_intra_word_spaces(text)
    if not (
        _PARTY_ROLE_FORUM_BUSINESS_RE.search(hay)
        or _PARTY_ROLE_FORUM_BUSINESS_RE.search(healed)
    ):
        return False
    # Bare collective roles are not enough to retain forum-business boilerplate.
    return _party_role_unit_has_named_party_identity(text)


def _party_role_unit_has_party_tied_jurisdiction_venue(unit):
    """True for jurisdiction/venue facts materially tied to a named party."""
    text = unit or ""
    if not text.strip():
        return False
    hay = text
    healed = heal_ocr_intra_word_spaces(text)
    if _PARTY_ROLE_GENERIC_JURISDICTION_RE.search(hay) or _PARTY_ROLE_GENERIC_JURISDICTION_RE.search(
        healed
    ):
        # Generic court-power language survives only with a concrete named-party tie.
        if not (
            _PARTY_ROLE_PARTY_TIED_JURISDICTION_VENUE_RE.search(hay)
            or _PARTY_ROLE_PARTY_TIED_JURISDICTION_VENUE_RE.search(healed)
        ):
            return False
        return _party_role_unit_has_named_party_identity(text)
    if not (
        _PARTY_ROLE_PARTY_TIED_JURISDICTION_VENUE_RE.search(hay)
        or _PARTY_ROLE_PARTY_TIED_JURISDICTION_VENUE_RE.search(healed)
    ):
        return False
    return _party_role_unit_has_named_party_identity(text)


def _party_role_unit_is_generic_jurisdiction(unit):
    """True when a unit is a generic jurisdiction allegation without named-party tie."""
    text = unit or ""
    if not text.strip():
        return False
    if not (
        _PARTY_ROLE_GENERIC_JURISDICTION_RE.search(text)
        or _PARTY_ROLE_GENERIC_JURISDICTION_RE.search(heal_ocr_intra_word_spaces(text))
    ):
        return False
    return not _party_role_unit_has_party_tied_jurisdiction_venue(text)


def _party_role_unit_is_collective_forum_boilerplate(unit):
    """
    True for collective-role forum business/venue boilerplate.

    Excludes generic personal-jurisdiction-over-collective-role language and
    bare Plaintiffs/Defendants/Petitioners/Respondents venue or business claims
    that lack named-party identity. Does not treat entity/authorization /
    residence identity paragraphs as forum boilerplate merely because they
    also mention a principal place of business.
    """
    text = unit or ""
    if not text.strip():
        return False
    if _party_role_unit_has_named_party_identity(text):
        return False
    hay = text
    healed = heal_ocr_intra_word_spaces(text)
    # Core identity / entity / authorization cues are not forum boilerplate.
    if re.search(
        r"(?i)\b(?:"
        r"domestic\s+corporation|foreign\s+corporation|"
        r"limited\s+liability\s+(?:company|corporation|partnership)|"
        r"authorized\s+to\s+do\s+business|organized\s+(?:under|to)|"
        r"was\s+and\s+still\s+is|duly\s+authorized\s+and\s+existing|"
        r"is\s+an?\s+individual|are\s+individuals|"
        r"notice\s+defendants?|named\s+insured|additional\s+insured|"
        r"incorrectly\s+named|substituted\s+as|necessary\s+party|"
        r"joined(?:\s+herein|\s+as)?|sued\s+herein"
        r")\b",
        hay,
    ) or re.search(
        r"(?i)\b(?:"
        r"domestic\s+corporation|foreign\s+corporation|"
        r"limited\s+liability\s+(?:company|corporation|partnership)|"
        r"authorized\s+to\s+do\s+business|organized\s+(?:under|to)|"
        r"was\s+and\s+still\s+is|duly\s+authorized\s+and\s+existing|"
        r"is\s+an?\s+individual|are\s+individuals|"
        r"notice\s+defendants?|named\s+insured|additional\s+insured|"
        r"incorrectly\s+named|substituted\s+as|necessary\s+party|"
        r"joined(?:\s+herein|\s+as)?|sued\s+herein"
        r")\b",
        healed,
    ):
        return False
    forumish = (
        _PARTY_ROLE_FORUM_BUSINESS_RE.search(hay)
        or _PARTY_ROLE_FORUM_BUSINESS_RE.search(healed)
        or _PARTY_ROLE_PARTY_TIED_JURISDICTION_VENUE_RE.search(hay)
        or _PARTY_ROLE_PARTY_TIED_JURISDICTION_VENUE_RE.search(healed)
        or _PARTY_ROLE_GENERIC_JURISDICTION_RE.search(hay)
        or _PARTY_ROLE_GENERIC_JURISDICTION_RE.search(healed)
        or re.search(r"(?i)\b(?:venue|jurisdiction|personal\s+jurisdiction)\b", hay)
    )
    if not forumish:
        return False
    # Require a collective-role cue or standalone venue/jurisdiction boilerplate.
    return bool(
        _PARTY_ROLE_BARE_COLLECTIVE_ROLE_RE.search(hay)
        or _PARTY_ROLE_BARE_COLLECTIVE_ROLE_RE.search(healed)
        or re.search(
            r"(?i)\b(?:venue\s+is\s+proper|jurisdiction\s+(?:and\s+venue\s+)?"
            r"(?:is|are)\s+proper|personal\s+jurisdiction)\b",
            hay,
        )
    )


def _party_role_unit_in_evidence_scope(unit, *, in_intro_section=False):
    """
    Decide whether a passage unit belongs in party-role evidence scope.

    Keeps identity/role/entity/residence/authorization cues, named-party forum
    business and jurisdiction/venue facts, and concise introduction /
    nature-of-action body text. Excludes generic untied jurisdiction and
    collective-role forum/venue boilerplate.
    """
    if not (unit or "").strip():
        return False
    if _party_role_unit_is_generic_jurisdiction(unit):
        return False
    if _party_role_unit_is_collective_forum_boilerplate(unit):
        return False
    if _party_role_unit_has_identity_signal(unit):
        return True
    if _party_role_unit_has_forum_business_signal(unit):
        return True
    if _party_role_unit_has_party_tied_jurisdiction_venue(unit):
        return True
    if in_intro_section:
        # Concise opening-section body: keep short non-narrative units.
        cleaned = normalize_retrieval_text(unit)
        if not cleaned:
            return False
        if len(cleaned) > 700:
            return False
        return True
    return False


MOTION_HEADING_RE = re.compile(
    r"(?i)\b(?:notice\s+of\s+motion|motion\s+for\s+(?:an\s+)?"
    r"(?:summary\s+judgment|default\s+judgment|dismissal|leave)|"
    r"motion\s+to\s+(?:dismiss|compel|vacate|renew|reargue))\b"
)

ORDER_HEADING_RE = re.compile(
    r"(?i)\b(?:it\s+is\s+(?:hereby\s+)?ordered|ordered\s+that|"
    r"decision\s+and\s+order|order\s+to\s+show\s+cause)\b"
)

TIMELINE_DATE_RE = re.compile(
    r"(?i)\b(?P<date>"
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},\s+\d{4}"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
    r")\b"
)

CONFLICT_NEGATION_RE = re.compile(
    r"(?i)\b(?:did\s+not|does\s+not|failed\s+to|never|no\s+longer|"
    r"without|denies?|denied)\b"
)


def empty_case_map():
    return {
        "parties": [],
        "policies": [],
        "claims": [],
        "defenses": [],
        "allegations": [],
        "evidence": [],
        "timeline_events": [],
        "motions": [],
        "court_orders": [],
        "relationships": [],
        "review_candidates": [],
        "validation": {
            "ok": True,
            "errors": [],
            "warnings": [],
        },
    }


def slugify_case_map_key(value):
    text = clean_text(value).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:80] or "unknown"


def make_case_map_node_id(node_type, nyscef_document_number, stable_key):
    doc_no = coerce_nyscef_document_number(nyscef_document_number)
    if doc_no is None:
        doc_no = UNKNOWN_NYSCEF_DOCUMENT_NUMBER
    return f"cmap-{node_type}-nyscef-{doc_no:03d}-{slugify_case_map_key(stable_key)}"


def make_case_map_relationship_id(relation_type, source_id, target_id):
    return (
        f"cmap-rel-{slugify_case_map_key(relation_type)}-"
        f"{slugify_case_map_key(source_id)}-"
        f"{slugify_case_map_key(target_id)}"
    )


def make_record_support(
    nyscef_document_number,
    page_ids,
    excerpt=None,
    *,
    segment_id=None,
    exhibit_label=None,
):
    doc_no = coerce_nyscef_document_number(nyscef_document_number)
    support = {
        "nyscef_document_number": doc_no,
        "page_ids": list(page_ids or []),
    }
    if excerpt is not None:
        support["excerpt"] = clean_text(excerpt)[:CASE_MAP_EXCERPT_MAX]
    if segment_id is not None:
        support["segment_id"] = segment_id
    if exhibit_label is not None:
        support["exhibit_label"] = exhibit_label
    return support


def _excerpt_around_match(text, match, radius=110):
    if not text or match is None:
        return clean_text(text)[:CASE_MAP_EXCERPT_MAX]
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    return clean_text(text[start:end])[:CASE_MAP_EXCERPT_MAX]


def build_case_map_node(
    node_type,
    label,
    *,
    nyscef_document_number,
    page_ids,
    assertion_kind,
    stable_key=None,
    excerpt=None,
    speaker_party=None,
    procedural_posture=None,
    confidence="low",
    extraction_signals=None,
    requires_review=True,
    status="candidate",
    segment_id=None,
    exhibit_label=None,
    extra=None,
):
    if assertion_kind not in CASE_MAP_ASSERTION_KINDS:
        raise ValueError(f"unsupported assertion_kind: {assertion_kind}")
    if node_type not in CASE_MAP_NODE_TYPE_TO_COLLECTION:
        raise ValueError(f"unsupported node_type: {node_type}")

    key = stable_key or label or node_type
    node = {
        "id": make_case_map_node_id(node_type, nyscef_document_number, key),
        "node_type": node_type,
        "label": clean_text(label) if label is not None else "",
        "assertion_kind": assertion_kind,
        "speaker_party": speaker_party,
        "procedural_posture": procedural_posture,
        "confidence": confidence,
        "extraction_signals": list(extraction_signals or []),
        "requires_review": bool(requires_review),
        "status": status,
        "conflicts_with": [],
        "record_support": [
            make_record_support(
                nyscef_document_number,
                page_ids,
                excerpt,
                segment_id=segment_id,
                exhibit_label=exhibit_label,
            )
        ],
    }
    if extra:
        for field_name, value in extra.items():
            if field_name not in node:
                node[field_name] = value
    return node


def build_case_map_relationship(
    relation_type,
    source_id,
    target_id,
    *,
    nyscef_document_number,
    page_ids,
    assertion_kind="inference",
    excerpt=None,
    confidence="low",
    requires_review=True,
    extraction_signals=None,
    segment_id=None,
    exhibit_label=None,
):
    if assertion_kind not in CASE_MAP_ASSERTION_KINDS:
        raise ValueError(f"unsupported assertion_kind: {assertion_kind}")

    return {
        "id": make_case_map_relationship_id(relation_type, source_id, target_id),
        "relation_type": relation_type,
        "source_id": source_id,
        "target_id": target_id,
        "assertion_kind": assertion_kind,
        "confidence": confidence,
        "requires_review": bool(requires_review),
        "extraction_signals": list(extraction_signals or []),
        "record_support": [
            make_record_support(
                nyscef_document_number,
                page_ids,
                excerpt,
                segment_id=segment_id,
                exhibit_label=exhibit_label,
            )
        ],
    }


def _iter_document_pages(document):
    pages = document.get("pages")
    if pages is None:
        return []
    nyscef = coerce_nyscef_document_number(document.get("nyscef_document_number"))
    return [normalize_page_record(page, nyscef) for page in pages]


def _page_index_by_id(pages):
    return {page["page_id"]: page for page in pages if page.get("page_id")}


def _first_matching_page(pages, pattern):
    for page in pages:
        text = page.get("text") or ""
        match = pattern.search(text)
        if match:
            return page, match
    return None, None


def _normalize_speaker_party(value):
    value = clean_text(value).lower()
    if value.startswith("plaintiff") or value.startswith("petitioner"):
        return "plaintiff"
    if value.startswith("defendant") or value.startswith("respondent"):
        return "defendant"
    return value or None


def _normalize_party_role(value):
    """
    Normalize procedural party roles for case-map extraction.

    Returns a supported role label, or None when the evidence is unclear.
    Does not invent roles or collapse distinct appellate/petition postures.
    """
    role = clean_text(value).lower()
    role = re.sub(r"\s+", " ", role).strip(" .,;:")
    if not role:
        return None
    role = role.replace("third party", "third-party")
    if role.startswith("third-party plaintiff"):
        return "third-party plaintiff"
    if role.startswith("third-party defendant"):
        return "third-party defendant"
    if re.match(r"respondents?\s+on\s+(?:the\s+)?appeal$", role):
        return "respondent on appeal"
    if role.startswith("plaintiff"):
        return "plaintiff"
    if role.startswith("defendant"):
        return "defendant"
    if role.startswith("petitioner"):
        return "petitioner"
    if role.startswith("respondent"):
        return "respondent"
    if role.startswith("appellant"):
        return "appellant"
    if role.startswith("appellee"):
        return "appellee"
    return None


def _party_role_match_groups(match):
    """Extract (name, role_raw) from PARTY_ROLE_RE match variants."""
    name = match.groupdict().get("name") or match.groupdict().get("name_leading")
    role_raw = match.groupdict().get("role") or match.groupdict().get("role_leading")
    return name, role_raw


def _is_plausible_party_name(name):
    """Reject section headings and bare corporate suffixes as party names."""
    cleaned = clean_case_party(name)
    if not cleaned or len(cleaned) < 3:
        return False
    lowered = cleaned.lower().strip(" .,")
    if lowered in _PARTY_NAME_BLOCKLIST:
        return False
    # Sentence fragments glued across a prior period are not party names.
    if ". " in cleaned or re.search(r"\d+\.", cleaned):
        return False
    if re.match(
        r"(?i)^(is|was|are|were|has|have|seeks?|brings?|joined|authorized)\b",
        cleaned,
    ):
        return False
    tokens = [tok for tok in re.split(r"[\s,]+", lowered) if tok]
    if not tokens:
        return False
    if len(tokens) == 1 and tokens[0] in _PARTY_NAME_SUFFIX_ONLY:
        return False
    if all(tok in _PARTY_NAME_SUFFIX_ONLY for tok in tokens):
        return False
    return True


def _append_unique_node(collection, node, by_id):
    existing = by_id.get(node["id"])
    if existing is None:
        collection.append(node)
        by_id[node["id"]] = node
        return node

    # Incremental provenance merge into the existing node.
    seen_supports = {
        (
            s.get("nyscef_document_number"),
            tuple(s.get("page_ids") or []),
            s.get("excerpt"),
            s.get("segment_id"),
            s.get("exhibit_label"),
        )
        for s in existing.get("record_support") or []
    }
    for support in node.get("record_support") or []:
        key = (
            support.get("nyscef_document_number"),
            tuple(support.get("page_ids") or []),
            support.get("excerpt"),
            support.get("segment_id"),
            support.get("exhibit_label"),
        )
        if key not in seen_supports:
            existing.setdefault("record_support", []).append(support)
            seen_supports.add(key)

    for signal in node.get("extraction_signals") or []:
        if signal not in existing.setdefault("extraction_signals", []):
            existing["extraction_signals"].append(signal)

    for conflict_id in node.get("conflicts_with") or []:
        if conflict_id not in existing.setdefault("conflicts_with", []):
            existing["conflicts_with"].append(conflict_id)

    if node.get("requires_review"):
        existing["requires_review"] = True

    return existing


def _append_unique_relationship(relationships, relationship, by_id):
    existing = by_id.get(relationship["id"])
    if existing is None:
        relationships.append(relationship)
        by_id[relationship["id"]] = relationship
        return relationship

    seen_supports = {
        (
            s.get("nyscef_document_number"),
            tuple(s.get("page_ids") or []),
            s.get("excerpt"),
        )
        for s in existing.get("record_support") or []
    }
    for support in relationship.get("record_support") or []:
        key = (
            support.get("nyscef_document_number"),
            tuple(support.get("page_ids") or []),
            support.get("excerpt"),
        )
        if key not in seen_supports:
            existing.setdefault("record_support", []).append(support)
            seen_supports.add(key)
    return existing


def _mark_review_candidate(case_map, node_or_rel, reason):
    entry = {
        "id": node_or_rel.get("id"),
        "reason": reason,
        "assertion_kind": node_or_rel.get("assertion_kind"),
    }
    if entry not in case_map["review_candidates"]:
        case_map["review_candidates"].append(entry)


def _extract_parties_for_case_map(case_map, document, pages, by_id):
    nyscef = coerce_nyscef_document_number(document.get("nyscef_document_number"))
    if not pages:
        return

    # Caption-style extraction stays on early pages; role evidence may appear later.
    caption_pages = pages[:3]
    caption_text = "\n".join(page.get("text") or "" for page in caption_pages)
    case_name = extract_case_name(caption_text)
    parties = extract_parties(case_name) if case_name != "Matter Builder" else None

    if parties and parties.get("plaintiff") not in {"", "—"}:
        page, match = _first_matching_page(
            caption_pages,
            re.compile(re.escape(parties["plaintiff"].split()[0]), re.IGNORECASE),
        )
        page = page or caption_pages[0]
        excerpt = _excerpt_around_match(page.get("text") or "", match) if match else (
            page.get("text") or ""
        )[:CASE_MAP_EXCERPT_MAX]
        node = build_case_map_node(
            "party",
            parties["plaintiff"],
            nyscef_document_number=nyscef,
            page_ids=[page["page_id"]],
            assertion_kind="verified_record_fact",
            stable_key=f"plaintiff:{parties['plaintiff']}",
            excerpt=excerpt,
            speaker_party=None,
            procedural_posture="caption",
            confidence="medium",
            extraction_signals=["caption_party_v"],
            requires_review=True,
            status="known",
            extra={"role": "plaintiff"},
        )
        _append_unique_node(case_map["parties"], node, by_id)
        _mark_review_candidate(case_map, node, "Confirm caption party spelling/role")

    if parties and parties.get("defendant") not in {"", "—"}:
        page = caption_pages[0]
        node = build_case_map_node(
            "party",
            parties["defendant"],
            nyscef_document_number=nyscef,
            page_ids=[page["page_id"]],
            assertion_kind="verified_record_fact",
            stable_key=f"defendant:{parties['defendant']}",
            excerpt=(page.get("text") or "")[:CASE_MAP_EXCERPT_MAX],
            procedural_posture="caption",
            confidence="medium",
            extraction_signals=["caption_party_v"],
            requires_review=True,
            status="known",
            extra={"role": "defendant"},
        )
        _append_unique_node(case_map["parties"], node, by_id)
        _mark_review_candidate(case_map, node, "Confirm caption party spelling/role")

    # Role-tagged lines from caption and later pleading pages (e.g. PARTIES).
    caption_page_ids = {page.get("page_id") for page in caption_pages}
    for page in pages:
        page_text = page.get("text") or ""
        parties_heading = bool(
            re.search(r"(?im)^\s*parties\b", page_text)
            or re.search(r"(?i)\bparties\b", page_text[:80])
        )
        if page.get("page_id") in caption_page_ids:
            posture = "caption"
        elif parties_heading:
            posture = "parties_section"
        else:
            posture = "pleading_body"

        for match in PARTY_ROLE_RE.finditer(page_text):
            raw_name, raw_role = _party_role_match_groups(match)
            name = clean_case_party(raw_name)
            role = _normalize_party_role(raw_role)
            if not _is_plausible_party_name(name):
                continue
            # Do not invent a role when the matched label is unclear.
            if role is None:
                node = build_case_map_node(
                    "party",
                    name,
                    nyscef_document_number=nyscef,
                    page_ids=[page["page_id"]],
                    assertion_kind="unknown",
                    stable_key=f"unassigned-role:{name}",
                    excerpt=_excerpt_around_match(page_text, match),
                    procedural_posture=posture,
                    confidence="low",
                    extraction_signals=["role_tagged_party_unclear"],
                    requires_review=True,
                    status="candidate",
                    extra={"role": None, "role_qualification": "unclear_from_record"},
                )
                _append_unique_node(case_map["parties"], node, by_id)
                _mark_review_candidate(
                    case_map, node, "Party name found but role unclear from record"
                )
                continue
            node = build_case_map_node(
                "party",
                name,
                nyscef_document_number=nyscef,
                page_ids=[page["page_id"]],
                assertion_kind="verified_record_fact",
                stable_key=f"{role}:{name}",
                excerpt=_excerpt_around_match(page_text, match),
                procedural_posture=posture,
                confidence="medium" if posture == "parties_section" else "low",
                extraction_signals=["role_tagged_party"],
                requires_review=True,
                status="candidate",
                extra={"role": role},
            )
            _append_unique_node(case_map["parties"], node, by_id)
            _mark_review_candidate(case_map, node, "Role-tagged party needs attorney review")


def _ensure_unknown_party_placeholder(case_map, documents, by_id):
    """If no parties were grounded, retain an explicit unknown rather than inventing names."""
    if case_map["parties"]:
        return
    for document in documents or []:
        pages = _iter_document_pages(document)
        if not pages:
            continue
        nyscef = coerce_nyscef_document_number(document.get("nyscef_document_number"))
        unknown = build_case_map_node(
            "party",
            "",
            nyscef_document_number=nyscef,
            page_ids=[pages[0]["page_id"]],
            assertion_kind="unknown",
            stable_key="unresolved-parties",
            excerpt=(pages[0].get("text") or "")[:CASE_MAP_EXCERPT_MAX],
            confidence="low",
            extraction_signals=["parties_unresolved"],
            requires_review=True,
            status="unknown",
            extra={"role": None},
        )
        _append_unique_node(case_map["parties"], unknown, by_id)
        _mark_review_candidate(case_map, unknown, "Parties unresolved from record text")
        return

def _extract_policies_for_case_map(case_map, document, pages, by_id):
    nyscef = coerce_nyscef_document_number(document.get("nyscef_document_number"))
    for page in pages:
        text = page.get("text") or ""
        for match in POLICY_REF_RE.finditer(text):
            policy_no = clean_text(match.group(1) or "")
            label = f"Policy {policy_no}" if policy_no else "Insurance policy (number unknown)"
            assertion = "verified_record_fact" if policy_no else "unknown"
            status = "candidate" if policy_no else "unknown"
            node = build_case_map_node(
                "policy",
                label,
                nyscef_document_number=nyscef,
                page_ids=[page["page_id"]],
                assertion_kind=assertion,
                stable_key=policy_no or f"policy-ref:{page['page_id']}",
                excerpt=_excerpt_around_match(text, match),
                confidence="medium" if policy_no else "low",
                extraction_signals=["policy_reference"],
                requires_review=True,
                status=status,
                extra={"policy_number": policy_no or None},
            )
            _append_unique_node(case_map["policies"], node, by_id)
            _mark_review_candidate(
                case_map,
                node,
                "Confirm policy number/terms; deterministic match is not a holding",
            )


def _extract_claims_and_defenses(case_map, document, pages, by_id):
    nyscef = coerce_nyscef_document_number(document.get("nyscef_document_number"))
    doc_type = document.get("type") or "other"
    procedural = detect_procedural_posture(document.get("text") or "")

    for page in pages:
        text = page.get("text") or ""
        if doc_type in {"complaint", "motion", "affirmation", "other", "memo"}:
            for match in CAUSE_OF_ACTION_RE.finditer(text):
                ordinal = clean_text(match.group("ordinal") or "")
                title = clean_text(match.group("title") or "")
                label = clean_text(
                    f"{ordinal} cause of action {title}".strip()
                ) or "Cause of action"
                node = build_case_map_node(
                    "claim",
                    label,
                    nyscef_document_number=nyscef,
                    page_ids=[page["page_id"]],
                    assertion_kind="party_allegation",
                    stable_key=f"claim:{ordinal}:{title}:{page['page_id']}",
                    excerpt=_excerpt_around_match(text, match),
                    speaker_party="plaintiff",
                    procedural_posture=procedural,
                    confidence="medium",
                    extraction_signals=["cause_of_action_heading"],
                    requires_review=True,
                    status="candidate",
                    extra={"ordinal": ordinal or None, "title": title or None},
                )
                _append_unique_node(case_map["claims"], node, by_id)
                _mark_review_candidate(
                    case_map, node, "Claim heading is a party contention, not a finding"
                )

        if doc_type in {"answer", "opposition", "motion", "other", "memo"}:
            for match in AFFIRMATIVE_DEFENSE_RE.finditer(text):
                # Avoid treating "defense counsel" prose as a pleaded defense.
                window = text[max(0, match.start() - 20) : match.end() + 40].lower()
                if "counsel" in window and "affirmative" not in window:
                    continue
                ordinal = clean_text(match.group("ordinal") or "")
                title = clean_text(match.group("title") or "")
                if title.lower() in {"counsel", "attorney", "attorneys"}:
                    continue
                label = clean_text(f"{ordinal} defense {title}".strip()) or "Defense"
                node = build_case_map_node(
                    "defense",
                    label,
                    nyscef_document_number=nyscef,
                    page_ids=[page["page_id"]],
                    assertion_kind="legal_position",
                    stable_key=f"defense:{ordinal}:{title}:{page['page_id']}",
                    excerpt=_excerpt_around_match(text, match),
                    speaker_party="defendant",
                    procedural_posture=procedural,
                    confidence="medium",
                    extraction_signals=["affirmative_defense_heading"],
                    requires_review=True,
                    status="candidate",
                    extra={"ordinal": ordinal or None, "title": title or None},
                )
                _append_unique_node(case_map["defenses"], node, by_id)
                _mark_review_candidate(
                    case_map, node, "Defense is a legal position requiring attorney review"
                )


def _extract_allegations(case_map, document, pages, by_id):
    nyscef = coerce_nyscef_document_number(document.get("nyscef_document_number"))
    procedural = detect_procedural_posture(document.get("text") or "")

    for page in pages:
        text = page.get("text") or ""
        for match in ALLEGATION_SPEAKER_RE.finditer(text):
            speaker = _normalize_speaker_party(match.group("speaker"))
            excerpt = _excerpt_around_match(text, match, radius=140)
            # Never promote allegations to verified facts.
            node = build_case_map_node(
                "allegation",
                excerpt,
                nyscef_document_number=nyscef,
                page_ids=[page["page_id"]],
                assertion_kind="party_allegation",
                stable_key=f"allegation:{speaker}:{page['page_id']}:{match.start()}",
                excerpt=excerpt,
                speaker_party=speaker,
                procedural_posture=procedural,
                confidence="medium",
                extraction_signals=["speaker_allegation_verb"],
                requires_review=True,
                status="candidate",
            )
            stored = _append_unique_node(case_map["allegations"], node, by_id)
            _mark_review_candidate(
                case_map, stored, "Allegation must not be treated as established fact"
            )


def _link_conflicting_allegations(case_map, by_id):
    """Conflicting assertions coexist and link; they are not reconciled."""
    allegation_nodes = list(case_map["allegations"])
    for i, left in enumerate(allegation_nodes):
        left_text = (left.get("label") or "").lower()
        left_neg = bool(CONFLICT_NEGATION_RE.search(left_text))
        left_tokens = {
            token
            for token in re.findall(r"[a-z]{4,}", left_text)
            if token
            not in {
                "plaintiff",
                "defendant",
                "alleges",
                "allege",
                "contends",
                "claims",
                "that",
                "this",
            }
        }
        for right in allegation_nodes[i + 1 :]:
            if left.get("speaker_party") and right.get("speaker_party"):
                if left["speaker_party"] == right["speaker_party"]:
                    continue
            right_text = (right.get("label") or "").lower()
            right_neg = bool(CONFLICT_NEGATION_RE.search(right_text))
            right_tokens = set(re.findall(r"[a-z]{4,}", right_text))
            overlap = left_tokens & right_tokens
            if len(overlap) < 2:
                continue
            if left_neg == right_neg:
                continue
            if right["id"] not in left.setdefault("conflicts_with", []):
                left["conflicts_with"].append(right["id"])
            if left["id"] not in right.setdefault("conflicts_with", []):
                right["conflicts_with"].append(left["id"])
            support_pages = []
            support_nyscef = None
            for node in (left, right):
                for support in node.get("record_support") or []:
                    support_pages.extend(support.get("page_ids") or [])
                    if support_nyscef is None:
                        support_nyscef = support.get("nyscef_document_number")
            rel = build_case_map_relationship(
                "conflicts_with",
                left["id"],
                right["id"],
                nyscef_document_number=support_nyscef,
                page_ids=support_pages[:4],
                assertion_kind="inference",
                excerpt="Conflicting party allegations retained without reconciliation",
                confidence="low",
                requires_review=True,
                extraction_signals=["allegation_conflict_heuristic"],
            )
            # Preserve each side's provenance separately when filings differ.
            rel["record_support"] = []
            for node in (left, right):
                for support in node.get("record_support") or []:
                    rel["record_support"].append(dict(support))
            if not rel["record_support"]:
                rel["record_support"] = [
                    make_record_support(
                        support_nyscef,
                        support_pages[:4],
                        "Conflicting party allegations retained without reconciliation",
                    )
                ]
            _append_unique_relationship(case_map["relationships"], rel, by_id)
            _mark_review_candidate(
                case_map, rel, "Conflicting allegations require attorney comparison"
            )

def _extract_evidence_from_exhibits(case_map, document, pages, by_id):
    nyscef = coerce_nyscef_document_number(document.get("nyscef_document_number"))
    segments = document.get("exhibit_segments") or []
    page_by_id = _page_index_by_id(pages)

    # Filing node anchor for relationships (document-as-filing).
    filing_page_ids = [p["page_id"] for p in pages[:1]] or []
    filing_node = None
    if filing_page_ids:
        filing_label = clean_text(
            document.get("title") or document.get("filename") or f"NYSCEF {nyscef}"
        )
        filing_node = build_case_map_node(
            "evidence",
            f"Filing: {filing_label}",
            nyscef_document_number=nyscef,
            page_ids=filing_page_ids,
            assertion_kind="verified_record_fact",
            stable_key=f"filing:{nyscef}",
            excerpt=(pages[0].get("text") if pages else "")[:CASE_MAP_EXCERPT_MAX],
            procedural_posture=document.get("type"),
            confidence="high",
            extraction_signals=["filing_record"],
            requires_review=False,
            status="known",
            extra={"kind": "filing"},
        )
        filing_node = _append_unique_node(case_map["evidence"], filing_node, by_id)

    for segment in segments:
        if segment.get("segment_type") != "exhibit":
            continue
        page_ids = list(segment.get("page_ids") or [])
        if not page_ids:
            continue
        label = segment.get("exhibit_label") or "unknown"
        title = segment.get("exhibit_title")
        display = f"Exhibit {label}" + (f": {title}" if title else "")
        first_page = page_by_id.get(page_ids[0], {})
        node = build_case_map_node(
            "evidence",
            display,
            nyscef_document_number=nyscef,
            page_ids=page_ids,
            assertion_kind="verified_record_fact",
            stable_key=f"exhibit:{label}:{segment.get('segment_id')}",
            excerpt=(first_page.get("text") or "")[:CASE_MAP_EXCERPT_MAX],
            procedural_posture="embedded_exhibit",
            confidence=segment.get("boundary_confidence") or "medium",
            extraction_signals=["exhibit_segment"],
            requires_review=True,
            status="known",
            segment_id=segment.get("segment_id"),
            exhibit_label=label,
            extra={
                "kind": "exhibit",
                "segment_id": segment.get("segment_id"),
                "exhibit_label": label,
                "exhibit_title": title,
            },
        )
        stored = _append_unique_node(case_map["evidence"], node, by_id)
        _mark_review_candidate(
            case_map,
            stored,
            "Exhibit existence is record-verified; content truth is not",
        )
        if filing_node is not None:
            rel = build_case_map_relationship(
                "attached_as_exhibit",
                filing_node["id"],
                stored["id"],
                nyscef_document_number=nyscef,
                page_ids=page_ids[:1],
                assertion_kind="verified_record_fact",
                excerpt=display,
                confidence=segment.get("boundary_confidence") or "medium",
                requires_review=False,
                extraction_signals=["exhibit_segment_link"],
                segment_id=segment.get("segment_id"),
                exhibit_label=label,
            )
            _append_unique_relationship(case_map["relationships"], rel, by_id)


def _extract_motions_and_orders(case_map, document, pages, by_id):
    nyscef = coerce_nyscef_document_number(document.get("nyscef_document_number"))
    doc_type = document.get("type") or "other"
    procedural = detect_motion_posture([document], document.get("text") or "")

    for page in pages:
        text = page.get("text") or ""
        motion_match = MOTION_HEADING_RE.search(text)
        if motion_match or doc_type == "motion":
            if motion_match or page is pages[0]:
                match = motion_match
                label = (
                    clean_text(match.group(0))
                    if match
                    else clean_text(document.get("title") or document.get("filename") or "Motion")
                )
                node = build_case_map_node(
                    "motion",
                    label,
                    nyscef_document_number=nyscef,
                    page_ids=[page["page_id"]],
                    assertion_kind="verified_record_fact",
                    stable_key=f"motion:{label}:{page['page_id']}",
                    excerpt=_excerpt_around_match(text, match) if match else text[:CASE_MAP_EXCERPT_MAX],
                    procedural_posture=procedural,
                    confidence="high" if match else "low",
                    extraction_signals=(
                        ["motion_heading"] if match else ["motion_document_type"]
                    ),
                    requires_review=not bool(match),
                    status="known" if match else "candidate",
                )
                stored = _append_unique_node(case_map["motions"], node, by_id)
                if stored.get("requires_review"):
                    _mark_review_candidate(
                        case_map, stored, "Motion classification needs attorney confirmation"
                    )
                if match:
                    break

    for page in pages:
        text = page.get("text") or ""
        order_match = ORDER_HEADING_RE.search(text)
        if order_match or doc_type == "order":
            if order_match or page is pages[0]:
                match = order_match
                label = (
                    clean_text(match.group(0))
                    if match
                    else clean_text(document.get("title") or document.get("filename") or "Order")
                )
                # Court orders are verified as filings; holdings remain review-gated.
                node = build_case_map_node(
                    "court_order",
                    label,
                    nyscef_document_number=nyscef,
                    page_ids=[page["page_id"]],
                    assertion_kind="verified_record_fact",
                    stable_key=f"order:{label}:{page['page_id']}",
                    excerpt=_excerpt_around_match(text, match) if match else text[:CASE_MAP_EXCERPT_MAX],
                    procedural_posture="order",
                    confidence="high" if match else "low",
                    extraction_signals=(
                        ["order_heading"] if match else ["order_document_type"]
                    ),
                    requires_review=True,
                    status="known" if match else "candidate",
                )
                stored = _append_unique_node(case_map["court_orders"], node, by_id)
                _mark_review_candidate(
                    case_map,
                    stored,
                    "Order text identified; do not invent holdings from heading alone",
                )
                if match:
                    break


def _extract_timeline_events(case_map, document, pages, by_id):
    nyscef = coerce_nyscef_document_number(document.get("nyscef_document_number"))
    procedural_tokens = (
        "filed",
        "served",
        "heard",
        "order",
        "motion",
        "accident",
        "occurrence",
        "executed",
        "signed",
    )

    for page in pages:
        text = page.get("text") or ""
        for match in TIMELINE_DATE_RE.finditer(text):
            excerpt = _excerpt_around_match(text, match, radius=100)
            lower_excerpt = excerpt.lower()
            if not any(token in lower_excerpt for token in procedural_tokens):
                # Date alone is insufficient to assert a timeline event.
                continue
            date_text = clean_text(match.group("date"))
            node = build_case_map_node(
                "timeline_event",
                excerpt,
                nyscef_document_number=nyscef,
                page_ids=[page["page_id"]],
                assertion_kind="inference",
                stable_key=f"timeline:{date_text}:{page['page_id']}:{match.start()}",
                excerpt=excerpt,
                procedural_posture=document.get("type"),
                confidence="low",
                extraction_signals=["dated_procedural_context"],
                requires_review=True,
                status="candidate",
                extra={"event_date": date_text},
            )
            stored = _append_unique_node(case_map["timeline_events"], node, by_id)
            _mark_review_candidate(
                case_map,
                stored,
                "Timeline date/context is a candidate inference, not a finding",
            )


def _link_issues_to_filings(case_map, by_id):
    """Additive relationships among already-extracted map nodes."""
    claims = list(case_map["claims"])
    allegations = list(case_map["allegations"])
    exhibits = []
    for node in case_map["evidence"]:
        label = node.get("exhibit_label")
        if not label:
            for support in node.get("record_support") or []:
                if support.get("exhibit_label"):
                    label = support.get("exhibit_label")
                    break
        if label or (node.get("label") or "").lower().startswith("exhibit "):
            exhibits.append(node)

    for claim in claims:
        claim_pages = []
        claim_nyscef = None
        for support in claim.get("record_support") or []:
            claim_pages.extend(support.get("page_ids") or [])
            if claim_nyscef is None:
                claim_nyscef = support.get("nyscef_document_number")
        for allegation in allegations:
            if allegation.get("speaker_party") != "plaintiff":
                continue
            alg_nyscef = None
            alg_pages = []
            for support in allegation.get("record_support") or []:
                alg_pages.extend(support.get("page_ids") or [])
                if alg_nyscef is None:
                    alg_nyscef = support.get("nyscef_document_number")
            if claim_nyscef is not None and alg_nyscef is not None and claim_nyscef != alg_nyscef:
                continue
            rel = build_case_map_relationship(
                "raises_issue",
                claim["id"],
                allegation["id"],
                nyscef_document_number=claim_nyscef if claim_nyscef is not None else alg_nyscef,
                page_ids=(claim_pages or alg_pages)[:2],
                assertion_kind="inference",
                excerpt="Claim/allegation co-occurrence within filing",
                confidence="low",
                requires_review=True,
                extraction_signals=["claim_allegation_link"],
            )
            _append_unique_relationship(case_map["relationships"], rel, by_id)
            _mark_review_candidate(case_map, rel, "Issue linkage is heuristic only")

    for exhibit in exhibits:
        exhibit_nyscef = None
        exhibit_pages = []
        exhibit_label = None
        segment_id = None
        for support in exhibit.get("record_support") or []:
            exhibit_pages.extend(support.get("page_ids") or [])
            if exhibit_nyscef is None:
                exhibit_nyscef = support.get("nyscef_document_number")
            exhibit_label = exhibit_label or support.get("exhibit_label")
            segment_id = segment_id or support.get("segment_id")
        for claim in claims:
            claim_nyscef = None
            for support in claim.get("record_support") or []:
                if claim_nyscef is None:
                    claim_nyscef = support.get("nyscef_document_number")
            if (
                claim_nyscef is not None
                and exhibit_nyscef is not None
                and claim_nyscef != exhibit_nyscef
            ):
                continue
            rel = build_case_map_relationship(
                "potentially_supports",
                exhibit["id"],
                claim["id"],
                nyscef_document_number=exhibit_nyscef if exhibit_nyscef is not None else claim_nyscef,
                page_ids=exhibit_pages[:2],
                assertion_kind="inference",
                excerpt="Exhibit co-filed with claim; support not established",
                confidence="low",
                requires_review=True,
                extraction_signals=["exhibit_claim_cofiling"],
                segment_id=segment_id,
                exhibit_label=exhibit_label,
            )
            _append_unique_relationship(case_map["relationships"], rel, by_id)
            _mark_review_candidate(
                case_map, rel, "Exhibit-to-issue support requires attorney review"
            )


def iter_case_map_nodes(case_map):
    for collection in CASE_MAP_NODE_COLLECTIONS:
        for node in case_map.get(collection) or []:
            yield collection, node


def validate_case_map(case_map, documents=None):
    """
    Validate citation grounding and graph integrity.

    Rejects/flags unsupported substantive assertions, invalid page IDs,
    duplicate IDs, dangling relationships, and provenance mismatches.
    """
    case_map = case_map or empty_case_map()
    errors = []
    warnings = []

    known_page_ids = set()
    page_id_to_nyscef = {}
    if documents is not None:
        for document in documents:
            nyscef = coerce_nyscef_document_number(document.get("nyscef_document_number"))
            for page in _iter_document_pages(document):
                page_id = page.get("page_id")
                if not page_id:
                    continue
                known_page_ids.add(page_id)
                page_id_to_nyscef[page_id] = nyscef

    seen_ids = {}
    node_ids = set()

    def _validate_support(owner_id, assertion_kind, supports, *, is_relationship=False):
        substantive = assertion_kind != "unknown"
        if substantive and not supports:
            errors.append(
                {
                    "code": "unsupported_assertion",
                    "id": owner_id,
                    "message": "Substantive assertion lacks record_support",
                }
            )
            return

        for support in supports or []:
            doc_no = support.get("nyscef_document_number")
            page_ids = support.get("page_ids") or []
            if substantive and doc_no is None:
                errors.append(
                    {
                        "code": "missing_nyscef",
                        "id": owner_id,
                        "message": "Record support missing nyscef_document_number",
                    }
                )
            if substantive and not page_ids:
                errors.append(
                    {
                        "code": "unsupported_assertion",
                        "id": owner_id,
                        "message": "Substantive assertion lacks page_ids",
                    }
                )
            for page_id in page_ids:
                if not isinstance(page_id, str) or not page_id.startswith("nyscef-"):
                    errors.append(
                        {
                            "code": "invalid_page_id",
                            "id": owner_id,
                            "page_id": page_id,
                            "message": "page_id is not a deterministic NYSCEF page id",
                        }
                    )
                    continue
                if known_page_ids and page_id not in known_page_ids:
                    errors.append(
                        {
                            "code": "invalid_page_id",
                            "id": owner_id,
                            "page_id": page_id,
                            "message": "page_id not present in provided documents",
                        }
                    )
                expected = page_id_to_nyscef.get(page_id)
                if (
                    expected is not None
                    and doc_no is not None
                    and int(expected) != int(doc_no)
                ):
                    errors.append(
                        {
                            "code": "provenance_mismatch",
                            "id": owner_id,
                            "page_id": page_id,
                            "message": (
                                f"page_id nyscef {expected} does not match "
                                f"support nyscef {doc_no}"
                            ),
                        }
                    )
                # Even without a document corpus, page_id prefix must agree.
                prefix_match = re.match(r"^nyscef-(\d+)-page-", str(page_id))
                if prefix_match and doc_no is not None:
                    if int(prefix_match.group(1)) != int(doc_no):
                        errors.append(
                            {
                                "code": "provenance_mismatch",
                                "id": owner_id,
                                "page_id": page_id,
                                "message": "page_id prefix disagrees with nyscef_document_number",
                            }
                        )

    for collection, node in iter_case_map_nodes(case_map):
        node_id = node.get("id")
        if not node_id:
            errors.append(
                {
                    "code": "missing_id",
                    "collection": collection,
                    "message": "Node missing id",
                }
            )
            continue
        if node_id in seen_ids:
            errors.append(
                {
                    "code": "duplicate_id",
                    "id": node_id,
                    "message": f"Duplicate id across {seen_ids[node_id]} and {collection}",
                }
            )
        else:
            seen_ids[node_id] = collection
        node_ids.add(node_id)

        assertion_kind = node.get("assertion_kind")
        if assertion_kind not in CASE_MAP_ASSERTION_KINDS:
            errors.append(
                {
                    "code": "invalid_assertion_kind",
                    "id": node_id,
                    "message": f"Invalid assertion_kind {assertion_kind}",
                }
            )
        # Allegations must never be classified as established facts.
        if node.get("node_type") == "allegation" and assertion_kind == "verified_record_fact":
            errors.append(
                {
                    "code": "allegation_promoted_to_fact",
                    "id": node_id,
                    "message": "Allegation cannot be verified_record_fact",
                }
            )

        _validate_support(node_id, assertion_kind, node.get("record_support") or [])

        for conflict_id in node.get("conflicts_with") or []:
            # Conflict targets are checked after all nodes are indexed.
            node.setdefault("_pending_conflicts", []).append(conflict_id)

    for collection, node in iter_case_map_nodes(case_map):
        for conflict_id in node.pop("_pending_conflicts", []):
            if conflict_id not in node_ids:
                errors.append(
                    {
                        "code": "dangling_relationship",
                        "id": node.get("id"),
                        "target_id": conflict_id,
                        "message": "conflicts_with points to missing node",
                    }
                )

    rel_ids = set()
    for relationship in case_map.get("relationships") or []:
        rel_id = relationship.get("id")
        if not rel_id:
            errors.append({"code": "missing_id", "message": "Relationship missing id"})
            continue
        if rel_id in seen_ids or rel_id in rel_ids:
            errors.append(
                {
                    "code": "duplicate_id",
                    "id": rel_id,
                    "message": "Duplicate relationship id",
                }
            )
        rel_ids.add(rel_id)

        source_id = relationship.get("source_id")
        target_id = relationship.get("target_id")
        if source_id not in node_ids:
            errors.append(
                {
                    "code": "dangling_relationship",
                    "id": rel_id,
                    "source_id": source_id,
                    "message": "Relationship source_id not found",
                }
            )
        if target_id not in node_ids:
            errors.append(
                {
                    "code": "dangling_relationship",
                    "id": rel_id,
                    "target_id": target_id,
                    "message": "Relationship target_id not found",
                }
            )

        _validate_support(
            rel_id,
            relationship.get("assertion_kind"),
            relationship.get("record_support") or [],
            is_relationship=True,
        )

    validation = {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
    }
    case_map["validation"] = validation
    return validation


def merge_case_maps(base_map, addition_map):
    """
    Incrementally merge case maps by deterministic ids without dropping provenance.
    """
    merged = empty_case_map()
    by_id = {}

    for source in (base_map or empty_case_map(), addition_map or empty_case_map()):
        for collection in CASE_MAP_NODE_COLLECTIONS:
            for node in source.get(collection) or []:
                # Copy to avoid mutating caller structures unexpectedly.
                node_copy = dict(node)
                node_copy["record_support"] = [
                    dict(support) for support in node.get("record_support") or []
                ]
                node_copy["extraction_signals"] = list(node.get("extraction_signals") or [])
                node_copy["conflicts_with"] = list(node.get("conflicts_with") or [])
                _append_unique_node(merged[collection], node_copy, by_id)

        for relationship in source.get("relationships") or []:
            rel_copy = dict(relationship)
            rel_copy["record_support"] = [
                dict(support) for support in relationship.get("record_support") or []
            ]
            rel_copy["extraction_signals"] = list(
                relationship.get("extraction_signals") or []
            )
            _append_unique_relationship(merged["relationships"], rel_copy, by_id)

        for candidate in source.get("review_candidates") or []:
            if candidate not in merged["review_candidates"]:
                merged["review_candidates"].append(dict(candidate))

    validate_case_map(merged)
    return merged


def build_case_map_from_documents(documents, *, validate=True):
    """
    Construct a citation-grounded litigation case map from normalized documents.

    Conservative deterministic extraction only. Semantic/attorney conclusions are
    exposed as review_candidates rather than established facts.
    """
    case_map = empty_case_map()
    by_id = {}

    for document in documents or []:
        # Ensure exhibit segments exist when pages are present so evidence
        # provenance can flow through without requiring a separate pass.
        working = document
        if (
            document.get("pages") is not None
            and "exhibit_segments" not in document
        ):
            working = normalize_document(document, include_exhibit_segments=True)

        pages = _iter_document_pages(working)
        if not pages:
            # Without page anchors we cannot ground substantive map nodes.
            continue

        _extract_parties_for_case_map(case_map, working, pages, by_id)
        _extract_policies_for_case_map(case_map, working, pages, by_id)
        _extract_claims_and_defenses(case_map, working, pages, by_id)
        _extract_allegations(case_map, working, pages, by_id)
        _extract_evidence_from_exhibits(case_map, working, pages, by_id)
        _extract_motions_and_orders(case_map, working, pages, by_id)
        _extract_timeline_events(case_map, working, pages, by_id)

    _ensure_unknown_party_placeholder(case_map, documents, by_id)
    _link_conflicting_allegations(case_map, by_id)
    _link_issues_to_filings(case_map, by_id)

    if validate:
        validate_case_map(case_map, documents)
    else:
        case_map["validation"] = {"ok": True, "errors": [], "warnings": []}

    return case_map


# ---------------------------------------------------------------------------
# Canonical record retrieval (opt-in)
#
# Page- and exhibit-segment–granular search over matter documents with
# case-map signals. Returns evidence citations only — never legal conclusions.
# Default get_matter / normalize_document consumers are unchanged unless the
# caller opts in via retrieve_canonical_records / canonical_retrieval_query.
# No external vector DB: hybrid lexical + metadata + relationship ranking only.
# ---------------------------------------------------------------------------

RETRIEVAL_CLASSIFICATIONS = (
    "review_candidate",
    "allegation",
    "legal_position",
    "inference",
    "unknown",
    "verified_fact",
)

ASSERTION_KIND_TO_CLASSIFICATION = {
    "verified_record_fact": "verified_fact",
    "party_allegation": "allegation",
    "legal_position": "legal_position",
    "inference": "inference",
    "unknown": "unknown",
}

RETRIEVAL_EXCERPT_RADIUS = 110
RETRIEVAL_EXCERPT_MAX = 240
RETRIEVAL_SCORE_PRECISION = 6

# Party-role evidence completeness: focused excerpts may exceed the default
# query-centered 240-character window so captions and role passages stay intact.
PARTY_ROLE_CAPTION_EXCERPT_MAX = 3500
PARTY_ROLE_PASSAGE_EXCERPT_MAX = 2500
PARTY_ROLE_COMBINED_EXCERPT_MAX = 4000
# Contiguous PARTIES-section expansion hard cap (pages per pleading span).
PARTY_ROLE_SECTION_EXPAND_MAX_PAGES = 6

# Transparent hybrid weights (sum intentionally > 1; absolute scale is relative).
# boilerplate_penalty is applied as a negative component when justified.
RETRIEVAL_WEIGHTS = {
    "exact_phrase": 40.0,
    "token_coverage": 28.0,
    "metadata": 12.0,
    "exhibit": 10.0,
    "case_map": 14.0,
    "relationship": 8.0,
    "boilerplate_penalty": 16.0,
    "party_role_pleading": 14.0,
}

# Case-map / relationship boosts require at least this excerpt grounding ratio
# (distinctive query terms or multiword phrases present in the excerpt window).
RETRIEVAL_CASE_MAP_GROUNDING_THRESHOLD = 0.2

CATEGORY_QUERY_HINTS = {
    "motion": ("motion", "notice of motion", "summary judgment", "movant"),
    "order": ("order", "ordered", "decision and order", "it is hereby ordered"),
    "complaint": ("complaint", "cause of action", "plaintiff alleges"),
    "answer": ("answer", "affirmative defense", "defendant alleges"),
    "exhibit": ("exhibit",),
    "affirmation": ("affirmation", "affidavit"),
    "opposition": ("opposition", "opposes"),
    "reply": ("reply",),
    "memo": ("memorandum", "memo of law"),
    "policy": ("policy", "coverage", "insured"),
}

CASE_MAP_CATEGORY_HINTS = {
    "parties": ("party", "plaintiff", "defendant", "petitioner", "respondent"),
    "policies": ("policy", "coverage", "insured"),
    "claims": ("cause of action", "claim", "breach"),
    "defenses": ("defense", "affirmative defense"),
    "allegations": ("alleges", "alleges that", "contends", "avers"),
    "evidence": ("exhibit", "evidence", "annexed"),
    "timeline_events": ("dated", "on or about", "occurred"),
    "motions": ("motion", "notice of motion", "summary judgment"),
    "court_orders": ("order", "ordered", "decision and order"),
}

# Multiword / distinctive legal phrases prioritized when present in the query.
RETRIEVAL_LEGAL_PHRASES = (
    "void ab initio",
    "it is hereby ordered",
    "decision and order",
    "affirmative defense",
    "cause of action",
    "notice of motion",
    "summary judgment",
    "motion to dismiss",
    "affidavit of service",
    "admission of service",
    "declaratory relief",
    "wherefore",
)

# Procedural boilerplate page markers (stamp / service / caption shells).
RETRIEVAL_BOILERPLATE_PATTERNS = (
    r"\baffidavit of service\b",
    r"\badmission of service\b",
    r"\baffixing to (the )?door\b",
    r"\bactual place of business within\b",
    r"\bfiled:\s*[a-z]+\s+county\s+clerk\b",
    r"\breceived nyscef\b",
    r"\bnyscef doc\.?\s*no\.?\b",
    r"\bsupreme court of the state of new york\b",
)

RETRIEVAL_SERVICE_QUERY_HINTS = (
    "affidavit of service",
    "admission of service",
    "proof of service",
    "served upon",
    "service of process",
    "service of the summons",
)

RETRIEVAL_SUBSTANTIVE_QUERY_HINTS = (
    "plaintiff",
    "defendant",
    "party",
    "parties",
    "caption",
    "wherefore",
    "relief",
    "void ab initio",
    "policy",
    "policies",
    "coverage",
    "cause of action",
    "claim",
    "claims",
    "defense",
    "defenses",
    "allegation",
    "allegations",
    "order",
    "ordered",
    "decision and order",
    "indemnif",
    "misrepresentation",
)

# Party-and-role identity questions (intent-specific pleading preference).
PARTY_ROLE_QUERY_PHRASES = (
    "party role",
    "party roles",
    "roles of the parties",
    "parties and their roles",
    "parties and roles",
    "who are the parties",
    "who is the plaintiff",
    "who is the defendant",
    "identify the parties",
    "identify parties",
    "named parties",
    "parties to the action",
    "parties to this action",
    "procedural roles",
    "each party's role",
    "plaintiff and defendant",
    "petitioner and respondent",
    "third-party plaintiff",
    "third-party defendant",
    "respondent on appeal",
)



def normalize_retrieval_text(value):
    """Lowercase whitespace-normalized text for deterministic matching."""
    return " ".join(str(value or "").lower().split()).strip()


def tokenize_retrieval_query(query):
    """
    Split a query into normalized phrase + tokens.

    Preserves multi-digit/date-like tokens and letter-number exhibit labels.
    """
    normalized = normalize_retrieval_text(query)
    if not normalized:
        return "", []
    tokens = re.findall(r"[a-z0-9][a-z0-9'/-]*", normalized)
    # Drop ultra-common closed-class noise while keeping legal/factual terms.
    stop = {
        "a", "an", "the", "of", "or", "and", "to", "in", "on", "for", "by",
        "is", "was", "are", "be", "as", "at", "that", "this", "with", "from",
        "which", "where", "what", "when", "who", "whom", "how", "does", "do",
        "did", "any", "among", "into", "about", "than", "then", "also", "such",
        "other", "under", "over", "between", "within", "without", "whether",
        "regarding", "including", "related", "record", "pages", "page",
        "filing", "filings", "contain", "contains", "identify", "identified",
        "language", "stating", "seeking", "sought", "address", "discuss",
        "discussion", "passages", "text", "named",
    }
    tokens = [t for t in tokens if t not in stop]
    return normalized, tokens


def _distinctive_retrieval_tokens(tokens):
    """Tokens that carry party/policy/relief/order signal (not tiny closed-class)."""
    weak = {
        "it", "its", "no", "not", "yes", "said", "herein", "thereof", "therein",
    }
    distinctive = []
    for token in tokens or []:
        if token in weak:
            continue
        if len(token) <= 2 and not any(ch.isdigit() for ch in token):
            continue
        distinctive.append(token)
    return distinctive


def _extract_retrieval_phrases(normalized_query, tokens):
    """
    Multiword / distinctive legal phrases drawn from the query.

    Full-query matching alone rarely fires on long diagnostic questions; these
    subphrases restore exact legal wording (WHEREFORE, void ab initio, etc.)
    without rewarding token-scattered or long-page stuffing.
    """
    joined = normalized_query or ""
    known_phrases = []
    support_phrases = []
    seen = set()

    def _add(bucket, phrase):
        phrase = normalize_retrieval_text(phrase)
        if not phrase or phrase in seen:
            return
        seen.add(phrase)
        bucket.append(phrase)

    for known in RETRIEVAL_LEGAL_PHRASES:
        if known in joined:
            _add(known_phrases, known)

    distinctive = _distinctive_retrieval_tokens(tokens)
    # Contiguous distinctive bigrams/trigrams — support anchoring only; known
    # legal phrases remain the primary exact-phrase evidence.
    for size in (3, 2):
        for index in range(0, max(0, len(distinctive) - size + 1)):
            candidate = " ".join(distinctive[index : index + size])
            if candidate in joined:
                _add(support_phrases, candidate)

    known_phrases.sort(key=lambda item: (-len(item.split()), -len(item), item))
    support_phrases.sort(key=lambda item: (-len(item.split()), -len(item), item))
    return {
        "known": known_phrases,
        "support": support_phrases,
        "all": known_phrases + support_phrases,
    }


def _phrase_lists(phrases):
    """Normalize phrase payload from extract helper or a bare list."""
    if isinstance(phrases, dict):
        known = list(phrases.get("known") or [])
        support = list(phrases.get("support") or [])
        all_phrases = list(phrases.get("all") or (known + support))
        return known, support, all_phrases
    all_phrases = list(phrases or [])
    known = [p for p in all_phrases if p in RETRIEVAL_LEGAL_PHRASES]
    support = [p for p in all_phrases if p not in RETRIEVAL_LEGAL_PHRASES]
    return known, support, all_phrases


def _query_seeks_procedural_service(normalized_query, tokens, hints):
    joined = normalized_query or ""
    if any(hint in joined for hint in RETRIEVAL_SERVICE_QUERY_HINTS):
        return True
    token_set = set(tokens or [])
    if "service" in token_set and (
        "affidavit" in token_set
        or "admission" in token_set
        or "proof" in token_set
        or "summons" in token_set
        or "process" in token_set
    ):
        return True
    # Discovery / procedural inventory queries should keep procedural pages.
    if "discovery" in token_set or "discovery" in (hints.get("document_types") or []):
        return True
    if any(
        hint in joined
        for hint in (
            "document request",
            "document demands",
            "discovery demand",
            "interrogator",
            "notice to admit",
        )
    ):
        return True
    return False


def _query_seeks_substantive_content(normalized_query, tokens, hints):
    joined = normalized_query or ""
    if any(hint in joined for hint in RETRIEVAL_SUBSTANTIVE_QUERY_HINTS):
        return True
    case_map_cats = set(hints.get("case_map_categories") or [])
    if case_map_cats.intersection(
        {
            "parties",
            "policies",
            "claims",
            "defenses",
            "allegations",
            "court_orders",
        }
    ):
        return True
    doc_types = set(hints.get("document_types") or [])
    if doc_types.intersection({"order", "policy", "complaint", "answer"}):
        return True
    return False


def _query_explicitly_targets_answer_pleading(normalized_query, tokens):
    """True only when the query is about an answer filing, not incidental 'answer'."""
    joined = normalized_query or ""
    token_set = set(tokens or [])
    if "affirmative defense" in joined or (
        "affirmative" in token_set and "defense" in token_set
    ):
        return True
    if re.search(
        r"\b(?:verified\s+)?answer\s+to\s+(?:the\s+)?complaint\b", joined
    ):
        return True
    if re.search(r"\b(?:defendant'?s?|verified)\s+answer\b", joined):
        return True
    if re.search(r"\banswer\s+(?:filing|pleading|papers?)\b", joined):
        return True
    return False


def _query_seeks_motion_primary(normalized_query, tokens, hints):
    """Motion-record questions should keep motion priority over party-role pleading."""
    joined = normalized_query or ""
    token_set = set(tokens or [])
    doc_types = set((hints or {}).get("document_types") or [])
    motion_phrases = (
        "notice of motion",
        "summary judgment",
        "motion to dismiss",
        "motion for",
        "returnable",
    )
    if any(phrase in joined for phrase in motion_phrases):
        return True
    if "motion" in doc_types or "motion" in token_set:
        # Explicit party-role framing can coexist in rare queries; treat strong
        # motion wording as motion-primary so pleading preference stays gated.
        if any(phrase in joined for phrase in PARTY_ROLE_QUERY_PHRASES):
            return False
        return True
    return False


def _detect_party_role_query_intent(normalized_query, tokens, hints=None):
    """
    Detect party-and-role identity questions using general language.

    Intent-specific: does not fire for motion-primary queries, so motion
    records continue to receive ordinary motion metadata priority.
    """
    joined = normalized_query or ""
    token_set = set(tokens or [])
    hints = hints or {}

    if _query_seeks_motion_primary(joined, token_set, hints):
        return False

    if any(phrase in joined for phrase in PARTY_ROLE_QUERY_PHRASES):
        return True

    if "parties" in token_set and any(
        cue in joined
        for cue in ("role", "roles", "who", "identify", "named", "caption")
    ):
        return True

    role_identity = token_set.intersection(
        {
            "plaintiff",
            "defendant",
            "petitioner",
            "respondent",
            "appellant",
            "appellee",
        }
    )
    if role_identity and any(
        cue in joined
        for cue in (
            "who is",
            "who are",
            "role",
            "roles",
            "named as",
            "identify",
            "caption",
            "parties",
        )
    ):
        return True

    if "plaintiff" in token_set and "defendant" in token_set:
        return True
    if "petitioner" in token_set and "respondent" in token_set:
        return True
    if "third-party" in joined or (
        "third" in token_set and "party" in token_set
    ):
        if role_identity or "party" in token_set or "parties" in token_set:
            return True

    return False


def _pleading_kind_for_party_role(entry, text=""):
    """Classify a page's filing for soft party-role source priority."""
    doc_type = normalize_retrieval_text(entry.get("document_type") or "")
    filename = normalize_retrieval_text(entry.get("filename") or "")
    document = entry.get("document") or {}
    title = normalize_retrieval_text(
        document.get("title") or document.get("name") or ""
    )
    hay = f"{filename} {title} {doc_type}"
    body_head = normalize_retrieval_text((text or "")[:240])

    if "rji" in hay or "request for judicial intervention" in hay:
        return "rji"
    if doc_type == "motion" or "notice of motion" in hay or (
        re.search(r"\bmotion\b", hay) and "summons" not in hay
    ):
        return "motion"
    if "amended" in hay or "amended" in body_head:
        if any(
            token in hay or token in body_head
            for token in (
                "complaint",
                "petition",
                "answer",
                "summons",
                "pleading",
            )
        ):
            return "amended_pleading"
    if doc_type == "complaint" or any(
        token in hay for token in ("complaint", "summons", "petition")
    ):
        return "initiating"
    if doc_type == "answer" or re.search(r"\banswers?\b", hay):
        return "answer"
    return "other"


def _page_has_role_bearing_language(text):
    return bool(text and PARTY_ROLE_BEARING_RE.search(text))


def _party_role_pleading_priority_score(entry, text, hints):
    """
    Soft source priority for party-role intent only.

    Prefers initiating/operative pleadings and role-bearing pages; does not
    let incidental answer metadata outrank a controlling complaint.
    """
    if not (hints or {}).get("party_role_intent"):
        return 0.0

    kind = _pleading_kind_for_party_role(entry, text)
    role_bearing = _page_has_role_bearing_language(text)

    if kind in {"motion", "rji"}:
        return 0.0
    if kind == "initiating":
        return 1.0 if role_bearing else 0.55
    if kind == "amended_pleading":
        return 0.92 if role_bearing else 0.5
    if kind == "answer":
        # Operative pleading, but below initiating/complaint role pages.
        return 0.4 if role_bearing else 0.15
    if role_bearing:
        return 0.25
    return 0.0


def _truncate_at_token_boundary(text, max_len):
    """Truncate without cutting through a party name or other token."""
    raw = text or ""
    if max_len is None or max_len <= 0 or len(raw) <= max_len:
        return raw
    cut = raw[:max_len]
    if max_len < len(raw) and not raw[max_len].isspace():
        space = cut.rfind(" ")
        if space >= max(1, max_len // 2):
            cut = cut[:space]
    return cut.rstrip()


def _is_affirmation_or_service_filing(entry, text=""):
    """True for affirmations / service papers (no complete-caption treatment)."""
    doc_type = normalize_retrieval_text(entry.get("document_type") or "")
    filename = normalize_retrieval_text(entry.get("filename") or "")
    document = entry.get("document") or {}
    title = normalize_retrieval_text(
        document.get("title") or document.get("name") or ""
    )
    hay = f"{filename} {title} {doc_type} {normalize_retrieval_text((text or '')[:320])}"
    if doc_type in {"affirmation", "affidavit"}:
        return True
    return bool(_AFFIRMATION_OR_SERVICE_FILING_RE.search(hay))


def _is_operative_pleading_kind(kind):
    return kind in {"initiating", "amended_pleading", "answer"}


def _page_has_parties_section_heading(text):
    return bool(text and PARTIES_SECTION_HEADING_RE.search(text))


def _page_has_retainable_intro_section_heading(text):
    """True when a concise intro / nature / preliminary heading is present."""
    if not text:
        return False
    return bool(
        _PARTY_ROLE_RETAINABLE_SECTION_HEADING_RE.search(text)
        or _PARTY_ROLE_RETAINABLE_SECTION_START_RE.match(clean_text(text))
    )


def _page_starts_major_pleading_section(text):
    """True when the page opens on a major non-PARTIES section heading."""
    cleaned = clean_text(text or "")
    if not cleaned:
        return False
    # Prefixed PARTIES headings (e.g. "14 PARTIES") are not stop headings.
    if _PARTIES_HEADING_START_RE.match(cleaned):
        return False
    return bool(_MAJOR_SECTION_START_RE.match(cleaned))


def _page_starts_intro_retention_end(text):
    """True when a page opens on PARTIES / facts / jurisdiction (ends intro)."""
    cleaned = clean_text(text or "")
    if not cleaned:
        return False
    return bool(_PARTY_ROLE_INTRO_END_SECTION_START_RE.match(cleaned))


def _page_has_intro_retention_boundary_after_heading(text):
    """True when intro retention should stop after the current page's heading."""
    if not text:
        return False
    retain = _PARTY_ROLE_RETAINABLE_SECTION_HEADING_RE.search(text)
    if not retain:
        retain = _PARTY_ROLE_RETAINABLE_SECTION_START_RE.match(clean_text(text))
        start_at = retain.end() if retain else 0
    else:
        start_at = retain.end()
    return bool(_PARTY_ROLE_INTRO_END_SECTION_HEADING_RE.search(text, start_at))


def _page_has_major_section_after_parties(text):
    """True when a major section heading appears after a PARTIES block starts."""
    if not text:
        return False
    parties_match = PARTIES_SECTION_HEADING_RE.search(text)
    start_at = parties_match.end() if parties_match else 0
    return bool(MAJOR_PLEADING_SECTION_HEADING_RE.search(text, start_at))


def _looks_like_caption_bearing_page(text, page_number=None, kind=None):
    """
    Detect caption-bearing pages on initiating/operative pleadings only.

    Affirmations and service papers are excluded by caller.
    """
    if not _is_operative_pleading_kind(kind):
        return False
    cleaned = clean_text(text or "")
    if not cleaned:
        return False
    if page_number is not None:
        try:
            if int(page_number) > 3:
                return False
        except (TypeError, ValueError):
            pass
    hay = normalize_retrieval_text(cleaned)
    has_versus = bool(re.search(r"\bv\.|\bagainst\b", hay))
    has_courtish = any(
        token in hay
        for token in (
            "supreme court",
            "index no",
            "county of",
            "plaintiff",
            "defendant",
            "petitioner",
            "respondent",
        )
    )
    if not (has_versus and has_courtish):
        return False
    return True


def _extract_complete_pleading_caption(text):
    """
    Preserve the complete caption party list for an initiating/operative pleading.

    Stops at the first body/section marker after caption content. Never truncates
    a party name mid-token when applying an explicit length bound.
    """
    cleaned = clean_text(text or "")
    if not cleaned:
        return ""
    end_match = PLEADING_CAPTION_END_RE.search(cleaned)
    if end_match and end_match.start() > 40:
        caption = cleaned[: end_match.start()].rstrip()
    else:
        # Fall back to the leading block before a blank-line body break.
        parts = re.split(r"\n\s*\n", cleaned, maxsplit=1)
        caption = parts[0].rstrip() if parts else cleaned
    return _truncate_at_token_boundary(caption, PARTY_ROLE_CAPTION_EXCERPT_MAX)


def _split_passage_units(text):
    """Split page text into concise paragraph / sentence units."""
    cleaned = clean_text(text or "")
    if not cleaned:
        return []
    # Numbered pleading paragraphs are the primary unit.
    # Limit to 1-4 digits so ZIP codes like "11354." are not treated as markers.
    if re.search(r"\b\d{1,4}\.\s+", cleaned):
        parts = re.split(r"(?=\b\d{1,4}\.\s+)", cleaned)
        return [part.strip() for part in parts if part and part.strip()]
    units = []
    # Avoid splitting on entity abbreviations such as "Inc." / "LLC."
    pieces = re.split(
        r"(?<!\bInc)(?<!\bLLC)(?<!\bLLP)(?<!\bCorp)(?<!\bLtd)(?<!\bCo)"
        r"(?<=[.;])\s+(?=(?:[A-Z\"(]|\d{1,4}\.))",
        cleaned,
    )
    for piece in pieces:
        piece = piece.strip()
        if piece:
            units.append(piece)
    return units


def _extract_party_role_passages(text, *, start_in_intro_section=False):
    """
    Keep concise party-role evidence passages from an initiating pleading page.

    Preserves caption-adjacent introduction / nature-of-action content, PARTIES
    identity/role/entity/residence/authorization allegations, and party-tied
    forum business / jurisdiction / venue facts. Stops at the transition into
    detailed factual background or other claim narrative sections. Does not
    ingest the complete factual narrative.

    When start_in_intro_section is True, the page is treated as a continuation of
    a concise opening section that began on a prior page (no repeated heading).
    """
    units = _split_passage_units(text)
    kept = []
    in_intro_section = bool(start_in_intro_section)

    def _append_scoped(candidate, *, intro=False):
        candidate = (candidate or "").strip()
        if candidate and _party_role_unit_in_evidence_scope(
            candidate, in_intro_section=intro
        ):
            kept.append(candidate)

    for unit in units:
        stripped = unit.strip()
        if not stripped:
            continue

        # Mid-unit PARTIES heading: keep prior intro/body, then enter PARTIES.
        parties_mid = re.search(
            r"(?i)" + _SECTION_HEADING_BOUNDARY + _SECTION_HEADING_PREFIX
            + r"(?:the\s+)?parties\b",
            stripped,
        )
        if parties_mid and parties_mid.start() > 0:
            before = stripped[: parties_mid.start()].strip()
            _append_scoped(before, intro=in_intro_section)
            in_intro_section = False
            kept.append("PARTIES")
            remainder = stripped[parties_mid.end() :].strip()
            remainder = re.sub(r"^[:.\-—–]\s*", "", remainder)
            if remainder:
                stripped = remainder
                unit = remainder
            else:
                continue

        if _PARTIES_HEADING_START_RE.match(stripped):
            in_intro_section = False
            # Preserve the section marker when it is its own unit or prefix.
            kept.append("PARTIES")
            # Continue into role content on the same unit after the heading.
            remainder = _PARTIES_HEADING_START_RE.sub("", stripped, count=1).strip()
            remainder = re.sub(r"^[:.\-—–]\s*", "", remainder)
            if remainder:
                unit = remainder
                stripped = remainder
            else:
                continue

        # Hard-stop at detailed facts / causes / prayer even mid-unit.
        hard_mid = _PARTY_ROLE_HARD_STOP_SECTION_HEADING_RE.search(stripped)
        stop_after = False
        if hard_mid and hard_mid.start() > 0:
            stripped = stripped[: hard_mid.start()].strip()
            unit = stripped
            stop_after = True
            if not stripped:
                break
        elif _PARTY_ROLE_HARD_STOP_SECTION_START_RE.match(
            stripped
        ) or _PARTY_ROLE_HARD_STOP_SECTION_HEADING_RE.match(stripped):
            break

        # Retainable intro / nature-of-action headings: keep marker, continue.
        if _PARTY_ROLE_RETAINABLE_SECTION_START_RE.match(
            stripped
        ) or _PARTY_ROLE_RETAINABLE_SECTION_HEADING_RE.match(stripped):
            in_intro_section = True
            heading_match = _PARTY_ROLE_RETAINABLE_SECTION_START_RE.match(
                stripped
            ) or _PARTY_ROLE_RETAINABLE_SECTION_HEADING_RE.match(stripped)
            heading_text = heading_match.group(0).strip() if heading_match else stripped
            heading_text = re.sub(r"^[\n\r.\s]+", "", heading_text).strip(" :.-—–")
            if heading_text:
                kept.append(heading_text)
            remainder = stripped[heading_match.end() :].strip() if heading_match else ""
            remainder = re.sub(r"^[:.\-—–]\s*", "", remainder)
            if remainder:
                unit = remainder
                stripped = remainder
            else:
                continue
        else:
            retain_mid = _PARTY_ROLE_RETAINABLE_SECTION_HEADING_RE.search(stripped)
            if retain_mid and retain_mid.start() > 0:
                before = stripped[: retain_mid.start()].strip()
                _append_scoped(before, intro=in_intro_section)
                in_intro_section = True
                heading_text = retain_mid.group(0).strip()
                heading_text = re.sub(r"^[\n\r.\s]+", "", heading_text).strip(" :.-—–")
                if heading_text:
                    kept.append(heading_text)
                remainder = stripped[retain_mid.end() :].strip()
                remainder = re.sub(r"^[:.\-—–]\s*", "", remainder)
                if remainder:
                    unit = remainder
                    stripped = remainder
                else:
                    continue

        # Jurisdiction/venue section headings are not hard stops; skip the
        # bare heading and evaluate following allegations individually.
        juris_mid = re.search(
            r"(?i)" + _SECTION_HEADING_BOUNDARY + _SECTION_HEADING_PREFIX
            + r"(?:"
            + _PARTY_ROLE_JURISDICTION_VENUE_SECTION_NAMES
            + r")"
            + _SECTION_HEADING_TAIL,
            stripped,
        )
        if juris_mid and juris_mid.start() > 0:
            before = stripped[: juris_mid.start()].strip()
            _append_scoped(before, intro=in_intro_section)
            in_intro_section = False
            remainder = stripped[juris_mid.end() :].strip()
            remainder = re.sub(r"^[:.\-—–]\s*", "", remainder)
            if remainder:
                unit = remainder
                stripped = remainder
            else:
                continue
        elif _PARTY_ROLE_JURISDICTION_VENUE_SECTION_START_RE.match(stripped):
            in_intro_section = False
            remainder = _PARTY_ROLE_JURISDICTION_VENUE_SECTION_START_RE.sub(
                "", stripped, count=1
            ).strip()
            remainder = re.sub(r"^[:.\-—–]\s*", "", remainder)
            if remainder:
                unit = remainder
                stripped = remainder
            else:
                continue

        _append_scoped(unit, intro=in_intro_section)
        if stop_after:
            break
    if not kept:
        return ""
    # De-duplicate while preserving order.
    deduped = []
    seen = set()
    for item in kept:
        key = normalize_retrieval_text(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    joined = "\n".join(deduped)
    return _truncate_at_token_boundary(joined, PARTY_ROLE_PASSAGE_EXCERPT_MAX)


# Procedural boilerplate removed only from party-role evidence excerpts.
# Line patterns drop pure stamp/summons/admin lines; span patterns strip the same
# material from newline-collapsed page text without discarding caption/PARTIES.
_PARTY_ROLE_PROCEDURAL_BOILERPLATE_LINE_RES = (
    re.compile(r"(?i)^\s*FILED\s*:"),
    re.compile(r"(?i)^\s*RECEIVED\s+NYSCEF\s*:"),
    re.compile(r"(?i)^\s*NYSCEF\s+DOC\.?\s*NO\.?\s*[:#]?\s*\d+\s*$"),
    re.compile(r"(?i)^\s*INDEX\s+NO\.?\s*[:#]?\s*[\dA-Z/\-]+\s*$"),
    re.compile(
        r"(?i)^\s*\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}(?::\d{2})?\s*[ap]m\s*$"
    ),
    # Standalone page footers such as "2 of 15".
    re.compile(r"(?i)^\s*\d{1,4}\s+of\s+\d{1,4}\s*$"),
    # Residual summons / directed-appearance headings.
    re.compile(r"(?i)^\s*SUMMONS\s*:?\s*$"),
    re.compile(r"(?i)^\s*TO\s+THE\s+ABOVE\s+NAMED\s+DEFENDANTS?\s*:?\s*$"),
    re.compile(r"(?i)^\s*you\s+are\s+hereby\s+summoned\b"),
    re.compile(
        r"(?i)\bwithin\s+(?:twenty|thirty|20|30)\s*"
        r"(?:\([^)]*\))?\s*days?\s+after\s+(?:the\s+)?service\b"
    ),
    re.compile(r"(?i)\bserve\s+a\s+copy\s+of\s+(?:your|the)\s+answer\b"),
    re.compile(
        r"(?i)\b(?:must|shall)\s+appear\s+(?:and|or)\s+(?:answer|defend)\b"
    ),
    re.compile(
        r"(?i)\byou\s+(?:are\s+)?(?:hereby\s+)?required\s+to\s+(?:appear|answer)\b"
    ),
    re.compile(
        r"(?i)\bthe\s+place\s+of\s+trial\s+(?:is|shall\s+be)\s+(?:hereby\s+)?"
        r"designated\b"
    ),
    re.compile(r"(?i)\bthe\s+basis\s+for\s+venue\s+is\b"),
    re.compile(
        r"(?i)\b(?:venue|place\s+of\s+trial)\s+(?:is|shall\s+be)\s+"
        r"(?:hereby\s+)?(?:designated|laid)\b"
    ),
    re.compile(
        r"(?i)\bjudgment\s+will\s+be\s+taken\s+against\s+you\s+by\s+default\b"
    ),
    re.compile(r"(?i)\bdefault\s+will\s+be\s+taken\s+against\s+you\b"),
    re.compile(
        r"(?i)\bupon\s+your\s+failure\s+to\s+(?:appear|answer|defend)\b"
    ),
    re.compile(
        r"(?i)\bin\s+case\s+of\s+your\s+failure\s+to\s+"
        r"(?:appear|answer|defend)\b"
    ),
    re.compile(
        r"(?i)\bfailure\s+to\s+(?:appear|answer|defend)\s+(?:or\s+appear\s+)?"
        r"(?:will|shall)\b"
    ),
    re.compile(
        r"(?i)\bthis\s+(?:document|filing|pleading)\s+(?:was|has\s+been)\s+"
        r"electronically\s+(?:filed|uploaded)\b"
    ),
    re.compile(
        r"(?i)\belectronically\s+filed\s+(?:and\s+served\s+)?"
        r"(?:through|via|using|with)\s+nyscef\b"
    ),
    re.compile(r"(?i)^\s*confirmation\s+notice\b.*\bnyscef\b"),
    re.compile(
        r"(?i)\bnyscef\s+(?:case\s+)?(?:processing|upload|administration)\b"
    ),
)

# Responsive anchors that must survive collapsed-page span removal. Collapsed
# caption pages often omit the period before PARTIES / numbered allegations;
# sentence-only ends would otherwise eat party names and role labels.
# Do not treat bare "complaint" as an anchor — summons prose says "answer the
# complaint" and must not truncate there.
_PARTY_ROLE_BOILERPLATE_SPAN_ANCHOR = (
    r"PARTIES\b|WHEREFORE\b|SUPREME\s+COURT\b|"
    r"\d{1,4}\s+of\s+\d{1,4}\b|"
    r"\d{1,3}\.\s+(?:Plaintiffs?|Defendants?)\b"
)
_PARTY_ROLE_BOILERPLATE_SPAN_BODY = (
    r"(?:(?!\b(?:PARTIES|WHEREFORE|SUPREME\s+COURT)\b)[^.])"
)
_PARTY_ROLE_BOILERPLATE_SPAN_END = (
    r"(?:\.|(?=\s*(?:" + _PARTY_ROLE_BOILERPLATE_SPAN_ANCHOR + r"))|$)"
)


def _party_role_boilerplate_span_re(core, max_body):
    """Compile a span removal that stops at '.' or a responsive caption anchor."""
    return re.compile(
        r"(?i)"
        + core
        + _PARTY_ROLE_BOILERPLATE_SPAN_BODY
        + r"{0,"
        + str(int(max_body))
        + r"}"
        + _PARTY_ROLE_BOILERPLATE_SPAN_END
    )


# Narrow span removals for collapsed (single-line) page text / linkage labels.
_PARTY_ROLE_PROCEDURAL_BOILERPLATE_SPAN_RES = (
    re.compile(
        r"(?i)\bFILED\s*:\s*.{0,160}?(?=\s*(?:INDEX\s+NO\.?|NYSCEF\s+DOC\.?|"
        r"RECEIVED\s+NYSCEF|SUPREME\s+COURT|PARTIES|COMPLAINT|SUMMONS)\b|$)"
    ),
    re.compile(r"(?i)\bINDEX\s+NO\.?\s*[:#]?\s*[\dA-Z/\-]+"),
    re.compile(r"(?i)\bNYSCEF\s+DOC\.?\s*NO\.?\s*[:#]?\s*\d+"),
    re.compile(r"(?i)\bRECEIVED\s+NYSCEF\s*:\s*\d{1,2}/\d{1,2}/\d{2,4}"),
    # Standalone "N of N" page footers embedded in collapsed text.
    re.compile(r"(?i)(?<!\d)\b\d{1,4}\s+of\s+\d{1,4}\b(?!\d)"),
    # Residual summons heading token — require a heading-like neighbor so ordinary
    # "service of this summons" prose is preserved (reject "this/the/a summons").
    # Also strip when followed by recognized venue-basis / default-warning spans.
    re.compile(
        r"(?i)(?<![A-Za-z])(?<!this )(?<!the )(?<!a )SUMMONS(?![A-Za-z])\s*:?"
        r"(?=\s*(?:$|\d{1,4}\s+of\s+\d{1,4}|COMPLAINT|PARTIES|YOU\s+ARE|"
        r"TO\s+THE\s+ABOVE|THE\s+BASIS\s+FOR\s+VENUE|"
        r"IN\s+CASE\s+OF\s+YOUR\s+FAILURE))"
    ),
    re.compile(
        r"(?i)\bTO\s+THE\s+ABOVE\s+NAMED\s+DEFENDANTS?\s*:?\s*\.?"
    ),
    _party_role_boilerplate_span_re(
        r"\bYOU\s+ARE\s+HEREBY\s+SUMMONED\b", 400
    ),
    _party_role_boilerplate_span_re(
        r"\bwithin\s+(?:twenty|thirty|20|30)\s*"
        r"(?:\([^)]*\))?\s*days?\s+after\s+(?:the\s+)?service\b",
        200,
    ),
    _party_role_boilerplate_span_re(
        r"\bserve\s+a\s+copy\s+of\s+(?:your|the)\s+answer\b", 200
    ),
    _party_role_boilerplate_span_re(
        r"\b(?:must|shall)\s+appear\s+(?:and|or)\s+(?:answer|defend)\b", 160
    ),
    _party_role_boilerplate_span_re(
        r"\byou\s+(?:are\s+)?(?:hereby\s+)?required\s+to\s+(?:appear|answer)\b",
        160,
    ),
    _party_role_boilerplate_span_re(
        r"\bthe\s+place\s+of\s+trial\s+(?:is|shall\s+be)\s+(?:hereby\s+)?"
        r"designated\b",
        120,
    ),
    # Prefer the longer "basis for venue is …" form before the shorter
    # "venue is designated/laid" core so collapsed summons text is not orphaned.
    # Body budget matches default-warning spans so adjacent collapsed clauses
    # without an intervening period still clear through to PARTIES.
    _party_role_boilerplate_span_re(
        r"\bthe\s+basis\s+for\s+venue\s+is\b",
        200,
    ),
    _party_role_boilerplate_span_re(
        r"\b(?:venue|place\s+of\s+trial)\s+(?:is|shall\s+be)\s+"
        r"(?:hereby\s+)?(?:designated|laid)\b",
        120,
    ),
    _party_role_boilerplate_span_re(
        r"\b(?:upon\s+your\s+failure\s+to\s+(?:appear|answer|defend)\b|"
        r"in\s+case\s+of\s+your\s+failure\s+to\s+"
        r"(?:appear|answer|defend)\b|"
        r"judgment\s+will\s+be\s+taken\s+against\s+you\s+by\s+default\b|"
        r"default\s+will\s+be\s+taken\s+against\s+you\b|"
        r"failure\s+to\s+(?:appear|answer|defend)\s+(?:or\s+appear\s+)?"
        r"(?:will|shall)\b)",
        200,
    ),
    _party_role_boilerplate_span_re(
        r"\bthis\s+(?:document|filing|pleading)\s+(?:was|has\s+been)\s+"
        r"electronically\s+(?:filed|uploaded)\b",
        160,
    ),
    _party_role_boilerplate_span_re(
        r"\belectronically\s+filed\s+(?:and\s+served\s+)?"
        r"(?:through|via|using|with)\s+nyscef\b",
        160,
    ),
    _party_role_boilerplate_span_re(
        r"\bconfirmation\s+notice\b[^.]{0,120}\bnyscef\b", 80
    ),
    _party_role_boilerplate_span_re(
        r"\bnyscef\s+(?:case\s+)?(?:processing|upload|administration)\b", 120
    ),
)

_PARTY_ROLE_BOILERPLATE_MIXED_CONTENT_RE = re.compile(
    r"(?i)\b(?:supreme\s+court|parties|plaintiffs?|defendants?|"
    r"petitioners?|respondents?|against|complaint|limited\s+liability|"
    r"domestic\s+corporation|principal\s+place|notice\s+defendant)\b"
)


def _strip_party_role_procedural_boilerplate_spans(text):
    """Remove known boilerplate spans while preserving surrounding role prose."""
    out = str(text or "")
    if not out:
        return ""
    for pattern in _PARTY_ROLE_PROCEDURAL_BOILERPLATE_SPAN_RES:
        out = pattern.sub(" ", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def _is_party_role_procedural_boilerplate_line(line):
    """True when a single line is filing/summons/admin boilerplate."""
    stripped = str(line or "").strip()
    if not stripped:
        return False
    if not any(pat.search(stripped) for pat in _PARTY_ROLE_PROCEDURAL_BOILERPLATE_LINE_RES):
        return False
    # Mixed caption/PARTIES lines are span-stripped instead of dropped whole.
    if _PARTY_ROLE_BOILERPLATE_MIXED_CONTENT_RE.search(stripped):
        residual = _strip_party_role_procedural_boilerplate_spans(stripped)
        return not residual
    return True


def _filter_party_role_procedural_boilerplate(text):
    """
    Strip line/passage-level procedural boilerplate from party-role excerpt text.

    Removes filing stamps, NYSCEF docket headers/footers, summons instructions,
    default-warning language, and court-upload administration text while
    preserving caption, PARTIES, and other identity/role material in order.
    """
    if text is None:
        return ""
    raw = str(text)
    if not raw:
        return ""
    if "\n" in raw:
        kept = [
            line
            for line in raw.splitlines()
            if not _is_party_role_procedural_boilerplate_line(line)
        ]
        raw = "\n".join(kept)
    return _strip_party_role_procedural_boilerplate_spans(raw)


def _sanitize_party_role_case_map_linkage_label(label):
    """
    Sanitize case_map_linkage.label for party-role evidence packets.

    Removes procedural boilerplate spans/lines. Returns the residual responsive
    text, or an empty string when nothing responsive remains (caller may omit).
    """
    if label is None:
        return ""
    cleaned = _filter_party_role_procedural_boilerplate(str(label))
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip(" \t\r\n-–—,:;")
    return cleaned


def _party_role_evidence_excerpt(
    entry, text, phrase=None, tokens=None, phrases=None, *, intro_continuation=False
):
    """
    Build focused party-role evidence from complete captions and role passages.

    Uses full-page content as the source of truth, then returns focused evidence
    rather than a short query-centered 240-character fragment.

    intro_continuation marks a page that continues a concise opening section
    begun on a prior page (heading already consumed).
    """
    kind = _pleading_kind_for_party_role(entry, text)
    page = entry.get("page") or {}
    page_number = page.get("page_number")
    parts = []
    # Filter only excerpt source text; page-level decisions still use full text.
    filtered = _filter_party_role_procedural_boilerplate(text)

    if (
        _is_operative_pleading_kind(kind)
        and not _is_affirmation_or_service_filing(entry, text)
        and _looks_like_caption_bearing_page(text, page_number, kind)
    ):
        caption = _extract_complete_pleading_caption(filtered)
        if caption:
            parts.append(caption)

    if _is_operative_pleading_kind(kind) or _page_has_role_bearing_language(text):
        passages = _extract_party_role_passages(
            filtered, start_in_intro_section=bool(intro_continuation)
        )
        if passages:
            # Avoid duplicating caption text already captured.
            if parts:
                caption_norm = normalize_retrieval_text(parts[0])
                passage_norm = normalize_retrieval_text(passages)
                if passage_norm == caption_norm:
                    passages = ""
                elif caption_norm:
                    # Drop passage lines that merely repeat the caption block.
                    trimmed = []
                    for line in passages.split("\n"):
                        line_norm = normalize_retrieval_text(line)
                        if not line_norm or line_norm == caption_norm:
                            continue
                        trimmed.append(line)
                    passages = "\n".join(trimmed).strip()
            if passages:
                parts.append(passages)

    if parts:
        combined = _filter_party_role_procedural_boilerplate("\n\n".join(parts))
        return _truncate_at_token_boundary(combined, PARTY_ROLE_COMBINED_EXCERPT_MAX)

    # Fallback: ordinary excerpt with token-safe truncation.
    fallback = _retrieval_excerpt(
        filtered, phrase=phrase, tokens=tokens, phrases=phrases
    )
    fallback = _filter_party_role_procedural_boilerplate(fallback)
    return _truncate_at_token_boundary(fallback, RETRIEVAL_EXCERPT_MAX)


def _iter_document_page_entries(entry_lookup_by_doc):
    for doc_no, entries in sorted(
        entry_lookup_by_doc.items(),
        key=lambda item: (item[0] is None, item[0] if item[0] is not None else 10**9),
    ):
        ordered = sorted(
            entries,
            key=lambda item: (
                (item.get("page") or {}).get("page_number") or 0,
                item.get("page_id") or "",
            ),
        )
        yield doc_no, ordered


def _collect_parties_section_page_ids(page_lookup):
    """
    Contiguous PARTIES-section page ids for initiating/operative pleadings.

    Expansion walks forward from a PARTIES heading, stops at the next clearly
    identified major section, and respects PARTY_ROLE_SECTION_EXPAND_MAX_PAGES.
    """
    by_doc = {}
    for page_id, entry in (page_lookup or {}).items():
        doc_no = entry.get("nyscef_document_number")
        by_doc.setdefault(doc_no, []).append({**entry, "page_id": page_id})

    section_page_ids = []
    for _doc_no, entries in _iter_document_page_entries(by_doc):
        texts = [
            ((entry.get("page") or {}).get("text") or "") for entry in entries
        ]
        kinds = [
            _pleading_kind_for_party_role(entry, texts[idx])
            for idx, entry in enumerate(entries)
        ]
        idx = 0
        while idx < len(entries):
            text = texts[idx]
            kind = kinds[idx]
            if not (
                _is_operative_pleading_kind(kind)
                and not _is_affirmation_or_service_filing(entries[idx], text)
                and _page_has_parties_section_heading(text)
            ):
                idx += 1
                continue
            span = [entries[idx]["page_id"]]
            stop_after_current = _page_has_major_section_after_parties(text)
            j = idx + 1
            while (
                not stop_after_current
                and j < len(entries)
                and len(span) < PARTY_ROLE_SECTION_EXPAND_MAX_PAGES
            ):
                nxt_text = texts[j]
                nxt_kind = kinds[j]
                if not _is_operative_pleading_kind(nxt_kind):
                    break
                if _is_affirmation_or_service_filing(entries[j], nxt_text):
                    break
                if _page_starts_major_pleading_section(nxt_text):
                    break
                span.append(entries[j]["page_id"])
                if _page_has_major_section_after_parties(nxt_text):
                    break
                # Stop when the continuation page no longer carries party-role signal.
                if not (
                    _page_has_role_bearing_language(nxt_text)
                    or PARTY_ROLE_PASSAGE_RE.search(nxt_text or "")
                    or _page_has_parties_section_heading(nxt_text)
                ):
                    break
                j += 1
            section_page_ids.extend(span)
            idx = max(j, idx + 1)
    # Preserve order while uniquifying.
    seen = set()
    ordered = []
    for page_id in section_page_ids:
        if page_id in seen:
            continue
        seen.add(page_id)
        ordered.append(page_id)
    return ordered


def _collect_intro_section_page_ids(page_lookup):
    """
    Contiguous intro / nature / preliminary-statement page ids.

    Walks forward from a retainable opening-section heading on an operative
    pleading until PARTIES, factual background, or another ending boundary.
    Returns (ordered_page_ids, continuation_page_ids) where continuation pages
    lack their own retainable heading and must keep intro scope active.
    """
    by_doc = {}
    for page_id, entry in (page_lookup or {}).items():
        doc_no = entry.get("nyscef_document_number")
        by_doc.setdefault(doc_no, []).append({**entry, "page_id": page_id})

    section_page_ids = []
    continuation_ids = set()
    for _doc_no, entries in _iter_document_page_entries(by_doc):
        texts = [
            ((entry.get("page") or {}).get("text") or "") for entry in entries
        ]
        kinds = [
            _pleading_kind_for_party_role(entry, texts[idx])
            for idx, entry in enumerate(entries)
        ]
        idx = 0
        while idx < len(entries):
            text = texts[idx]
            kind = kinds[idx]
            if not (
                _is_operative_pleading_kind(kind)
                and not _is_affirmation_or_service_filing(entries[idx], text)
                and _page_has_retainable_intro_section_heading(text)
            ):
                idx += 1
                continue
            span = [entries[idx]["page_id"]]
            stop_after_current = _page_has_intro_retention_boundary_after_heading(text)
            j = idx + 1
            while (
                not stop_after_current
                and j < len(entries)
                and len(span) < PARTY_ROLE_SECTION_EXPAND_MAX_PAGES
            ):
                nxt_text = texts[j]
                nxt_kind = kinds[j]
                if not _is_operative_pleading_kind(nxt_kind):
                    break
                if _is_affirmation_or_service_filing(entries[j], nxt_text):
                    break
                if _page_starts_intro_retention_end(nxt_text):
                    break
                if _page_has_retainable_intro_section_heading(nxt_text):
                    # A fresh opening heading starts its own span.
                    break
                span.append(entries[j]["page_id"])
                continuation_ids.add(entries[j]["page_id"])
                if _page_has_intro_retention_boundary_after_heading(nxt_text):
                    # Continuation page may itself contain the ending boundary.
                    # Still include it so excerpt can keep pre-boundary intro text.
                    break
                # Stop when the page no longer looks like opening-section body.
                cleaned = clean_text(nxt_text or "")
                if not cleaned:
                    break
                if not (
                    re.search(r"\b\d{1,4}\.\s+", cleaned)
                    or _page_has_role_bearing_language(nxt_text)
                    or PARTY_ROLE_PASSAGE_RE.search(nxt_text or "")
                ):
                    # Drop this empty/non-responsive page from the span.
                    span.pop()
                    continuation_ids.discard(entries[j]["page_id"])
                    break
                j += 1
            section_page_ids.extend(span)
            idx = max(j, idx + 1)
    seen = set()
    ordered = []
    for page_id in section_page_ids:
        if page_id in seen:
            continue
        seen.add(page_id)
        ordered.append(page_id)
    return ordered, continuation_ids


def _build_party_role_section_candidate(
    entry,
    phrase,
    tokens,
    hints,
    case_map_signals,
    phrases=None,
    *,
    intro_continuation=False,
):
    """Score/build a candidate for a contiguous PARTIES/intro-section page."""
    candidate = _score_page_candidate(
        entry,
        phrase,
        tokens,
        hints,
        case_map_signals,
        phrases=phrases,
    )
    # Section expansion pages remain available even with weak lexical scores.
    if candidate["score"] <= 0:
        priority = _party_role_pleading_priority_score(
            entry, (entry.get("page") or {}).get("text") or "", hints
        )
        if priority:
            bump = _round_retrieval_score(
                max(0.15, priority) * RETRIEVAL_WEIGHTS["party_role_pleading"]
            )
            components = dict(candidate.get("component_scores") or {})
            components["party_role_pleading"] = bump
            candidate["component_scores"] = components
            candidate["score"] = _round_retrieval_score(
                sum(float(v) for v in components.values())
            )
            explanation = list(candidate.get("ranking_explanation") or [])
            explanation.append("contiguous PARTIES-section expansion")
            candidate["ranking_explanation"] = explanation
    # Rebuild excerpt when this page continues an opening section without a heading.
    if intro_continuation or not candidate.get("excerpt"):
        text = (entry.get("page") or {}).get("text") or ""
        candidate["excerpt"] = _party_role_evidence_excerpt(
            entry,
            text,
            phrase=phrase,
            tokens=tokens,
            phrases=list((_phrase_lists(phrases))[2]),
            intro_continuation=intro_continuation,
        )
        candidate.setdefault("page_text", text)
    candidate["party_role_section_expanded"] = True
    return candidate


def _ensure_party_role_section_pages(
    ranked,
    *,
    page_lookup,
    phrase,
    tokens,
    hints,
    case_map_signals,
    phrases=None,
    scored_by_page=None,
):
    """
    Ensure contiguous PARTIES and intro-section pages reach evidence packets.

    Preserves ordinary diversification for pages already selected; injects any
    missing section pages that ranking/top-k would otherwise drop.
    """
    if not (hints or {}).get("party_role_intent"):
        return ranked

    parties_ids = _collect_parties_section_page_ids(page_lookup)
    intro_ids, intro_continuations = _collect_intro_section_page_ids(page_lookup)
    section_ids = []
    seen = set()
    for page_id in list(intro_ids) + list(parties_ids):
        if page_id in seen:
            continue
        seen.add(page_id)
        section_ids.append(page_id)
    if not section_ids:
        return ranked

    selected_ids = {
        item.get("page_id") for item in ranked if isinstance(item, dict)
    }
    scored_by_page = scored_by_page or {}
    injected = list(ranked)
    for page_id in section_ids:
        intro_continuation = page_id in intro_continuations
        if page_id in selected_ids:
            # A section page can have entered through ordinary scoring before
            # expansion.  Give it the same provenance marker as an injected
            # page so downstream packet budgeting can protect the complete
            # contiguous section, not just the pages expansion happened to add.
            for existing_hit in injected:
                if existing_hit.get("page_id") == page_id:
                    existing_hit["party_role_section_expanded"] = True
                    if intro_continuation:
                        entry = page_lookup.get(page_id)
                        if entry:
                            text = (entry.get("page") or {}).get("text") or ""
                            existing_hit["excerpt"] = _party_role_evidence_excerpt(
                                entry,
                                text,
                                phrase=phrase,
                                tokens=tokens,
                                phrases=list((_phrase_lists(phrases))[2]),
                                intro_continuation=True,
                            )
                            existing_hit.setdefault("page_text", text)
                    break
            continue
        entry = page_lookup.get(page_id)
        if not entry:
            continue
        existing = scored_by_page.get(page_id)
        if existing is None:
            existing = _build_party_role_section_candidate(
                entry,
                phrase,
                tokens,
                hints,
                case_map_signals,
                phrases=phrases,
                intro_continuation=intro_continuation,
            )
        else:
            existing = dict(existing)
            existing["party_role_section_expanded"] = True
            text = (entry.get("page") or {}).get("text") or ""
            if intro_continuation or not existing.get("excerpt"):
                existing["excerpt"] = _party_role_evidence_excerpt(
                    entry,
                    text,
                    phrase=phrase,
                    tokens=tokens,
                    phrases=list((_phrase_lists(phrases))[2]),
                    intro_continuation=intro_continuation,
                )
            existing.setdefault("page_text", text)
        if not validate_canonical_result_citation(existing, page_lookup):
            continue
        existing.setdefault(
            "diversity_adjusted_score", existing.get("score") or 0.0
        )
        injected.append(existing)
        selected_ids.add(page_id)

    injected.sort(
        key=lambda item: (
            -item.get("diversity_adjusted_score", item.get("score") or 0.0),
            -(item.get("score") or 0.0),
            item.get("nyscef_document_number") is None,
            item.get("nyscef_document_number")
            if item.get("nyscef_document_number") is not None
            else 10**9,
            item.get("pdf_page") or 0,
            item.get("result_id") or "",
        )
    )
    return injected


def _detect_retrieval_boilerplate(text):
    """
    Classify stamp / service / caption-shell pages.

    Returns (kind, strength) where strength is in [0, 1].
    """
    hay = normalize_retrieval_text(text)
    if not hay:
        return None, 0.0

    hits = []
    for pattern in RETRIEVAL_BOILERPLATE_PATTERNS:
        if re.search(pattern, hay):
            hits.append(pattern)

    if not hits:
        return None, 0.0

    kind = "procedural_boilerplate"
    if any("affidavit of service" in h for h in hits) or "affixing to" in hay:
        kind = "affidavit_of_service"
    elif any("admission of service" in h for h in hits):
        kind = "admission_of_service"
    elif sum(
        1
        for h in hits
        if "nyscef" in h or "county clerk" in h or "received nyscef" in h
    ) >= 2:
        kind = "stamp_only"
    elif "supreme court of the state of new york" in hay and len(hay) < 500:
        kind = "caption_only"

    # Strength scales with marker density but stays conservative (<= 1).
    strength = min(1.0, 0.35 + (0.2 * len(hits)))
    if kind in {"affidavit_of_service", "admission_of_service", "stamp_only"}:
        strength = min(1.0, strength + 0.15)
    return kind, strength


def make_canonical_result_id(nyscef_document_number, page_number, segment_id=None):
    doc_no = coerce_nyscef_document_number(nyscef_document_number)
    if doc_no is None:
        doc_no = UNKNOWN_NYSCEF_DOCUMENT_NUMBER
    base = f"cret-nyscef-{doc_no:03d}-page-{int(page_number):04d}"
    if segment_id:
        return f"{base}-{slugify_case_map_key(segment_id)}"
    return base


def map_retrieval_classifications(
    assertion_kind=None,
    *,
    requires_review=False,
    is_review_candidate=False,
):
    """Map case-map assertion kinds onto explicit retrieval classifications."""
    flags = []
    mapped = ASSERTION_KIND_TO_CLASSIFICATION.get(assertion_kind)
    if mapped:
        flags.append(mapped)
    elif assertion_kind is None:
        flags.append("unknown")
    if requires_review or is_review_candidate:
        if "review_candidate" not in flags:
            flags.append("review_candidate")
    # Stable order matching RETRIEVAL_CLASSIFICATIONS.
    order = {name: index for index, name in enumerate(RETRIEVAL_CLASSIFICATIONS)}
    return sorted(set(flags), key=lambda item: order.get(item, 99))


def _retrieval_excerpt(
    text,
    phrase=None,
    tokens=None,
    phrases=None,
    radius=RETRIEVAL_EXCERPT_RADIUS,
):
    raw = text or ""
    cleaned = clean_text(raw)
    if not cleaned:
        return ""
    hay = normalize_retrieval_text(cleaned)
    anchor = None
    # Prefer multiword legal phrases, then full query, then distinctive tokens.
    for candidate in list(phrases or []) + ([phrase] if phrase else []):
        if candidate and candidate in hay:
            anchor = hay.find(candidate)
            break
    if anchor is None and tokens:
        for token in _distinctive_retrieval_tokens(tokens) + list(tokens):
            pos = hay.find(token)
            if pos >= 0:
                anchor = pos
                break
    if anchor is None:
        return cleaned[:RETRIEVAL_EXCERPT_MAX]
    # Map roughly from normalized hay offset back onto cleaned text length.
    ratio = len(cleaned) / max(len(hay), 1)
    center = int(anchor * ratio)
    start = max(0, center - radius)
    end = min(len(cleaned), center + radius)
    return clean_text(cleaned[start:end])[:RETRIEVAL_EXCERPT_MAX]


def _round_retrieval_score(value):
    return round(float(value), RETRIEVAL_SCORE_PRECISION)


def _detect_query_category_hints(normalized_query, tokens):
    doc_types = set()
    case_map_categories = set()
    joined = normalized_query or ""
    token_set = set(tokens or [])
    for doc_type, hints in CATEGORY_QUERY_HINTS.items():
        for hint in hints:
            if " " in hint:
                if hint in joined:
                    doc_types.add(doc_type)
            elif hint in token_set or hint in joined:
                doc_types.add(doc_type)
    for category, hints in CASE_MAP_CATEGORY_HINTS.items():
        for hint in hints:
            if " " in hint:
                if hint in joined:
                    case_map_categories.add(category)
            elif hint in token_set or hint in joined:
                case_map_categories.add(category)

    provisional = {
        "document_types": sorted(doc_types),
        "case_map_categories": sorted(case_map_categories),
    }
    party_role_intent = _detect_party_role_query_intent(
        joined, token_set, provisional
    )
    if party_role_intent:
        case_map_categories.add("parties")
        # Soft initiating preference without letting bare "answer" dominate.
        if not doc_types.intersection({"complaint", "motion", "order"}):
            doc_types.add("complaint")
        if "answer" in doc_types and not _query_explicitly_targets_answer_pleading(
            joined, token_set
        ):
            doc_types.discard("answer")

    exhibit_labels = set()
    for match in re.finditer(
        r"\bexhibit\s+([a-z0-9]{1,4})\b", joined, flags=re.IGNORECASE
    ):
        exhibit_labels.add(normalize_exhibit_label(match.group(1)))
    return {
        "document_types": sorted(doc_types),
        "case_map_categories": sorted(case_map_categories),
        "exhibit_labels": sorted(label for label in exhibit_labels if label),
        "party_role_intent": bool(party_role_intent),
    }


def _segment_for_page(document, page_id):
    for segment in document.get("exhibit_segments") or []:
        if page_id in (segment.get("page_ids") or []):
            return segment
    return None


def _page_lookup_from_documents(documents):
    by_page_id = {}
    for document in documents or []:
        nyscef = coerce_nyscef_document_number(document.get("nyscef_document_number"))
        filename = document.get("filename") or document.get("title") or ""
        doc_type = document.get("type") or document.get("category") or "other"
        for page in _iter_document_pages(document):
            page_id = page.get("page_id")
            if not page_id:
                continue
            by_page_id[page_id] = {
                "page": page,
                "document": document,
                "nyscef_document_number": nyscef,
                "filename": filename,
                "document_type": doc_type,
                "segment": _segment_for_page(document, page_id),
            }
    return by_page_id


def _review_candidate_ids(case_map):
    ids = set()
    for entry in (case_map or {}).get("review_candidates") or []:
        if isinstance(entry, dict):
            node_id = entry.get("id")
            if node_id:
                ids.add(node_id)
        elif isinstance(entry, str):
            ids.add(entry)
    return ids


def _case_map_signals_by_page(case_map, page_lookup):
    """
    Index case-map nodes/relationships onto underlying page_ids.

    Nodes without resolvable page support are skipped (never returned alone).
    """
    signals = {}
    if not case_map:
        return signals

    review_ids = _review_candidate_ids(case_map)

    def _touch(page_id, payload):
        if page_id not in page_lookup:
            return
        bucket = signals.setdefault(page_id, [])
        bucket.append(payload)

    for collection, node in iter_case_map_nodes(case_map):
        supports = node.get("record_support") or []
        if not supports:
            continue
        page_ids = []
        for support in supports:
            page_ids.extend(support.get("page_ids") or [])
        if not page_ids:
            continue
        payload = {
            "kind": "node",
            "node_id": node.get("id"),
            "node_type": node.get("node_type"),
            "collection": collection,
            "label": node.get("label") or "",
            "assertion_kind": node.get("assertion_kind"),
            "requires_review": bool(node.get("requires_review")),
            "is_review_candidate": node.get("id") in review_ids,
            "conflicts_with": list(node.get("conflicts_with") or []),
            "search_text": normalize_retrieval_text(
                " ".join(
                    [
                        node.get("label") or "",
                        node.get("node_type") or "",
                        collection,
                        " ".join(node.get("extraction_signals") or []),
                    ]
                )
            ),
        }
        for page_id in page_ids:
            _touch(page_id, payload)

    for rel in case_map.get("relationships") or []:
        supports = rel.get("record_support") or []
        page_ids = []
        for support in supports:
            page_ids.extend(support.get("page_ids") or [])
        if not page_ids:
            continue
        payload = {
            "kind": "relationship",
            "relationship_id": rel.get("id"),
            "relation_type": rel.get("relation_type"),
            "source_id": rel.get("source_id"),
            "target_id": rel.get("target_id"),
            "assertion_kind": rel.get("assertion_kind"),
            "requires_review": bool(rel.get("requires_review")),
            "is_review_candidate": rel.get("id") in review_ids,
            "search_text": normalize_retrieval_text(
                " ".join(
                    [
                        rel.get("relation_type") or "",
                        rel.get("source_id") or "",
                        rel.get("target_id") or "",
                        " ".join(rel.get("extraction_signals") or []),
                    ]
                )
            ),
        }
        for page_id in page_ids:
            _touch(page_id, payload)

    return signals


def _lexical_component_scores(text, phrase, tokens, phrases=None):
    hay = normalize_retrieval_text(text)
    known_phrases, support_phrases, all_phrases = _phrase_lists(phrases)
    if not hay or (not phrase and not tokens and not all_phrases):
        return {
            "exact_phrase": 0.0,
            "token_coverage": 0.0,
            "matched_tokens": [],
            "matched_phrases": [],
        }

    exact = 0.0
    matched_phrases = []
    if phrase and phrase in hay:
        # Full normalized query hit: length-normalized (binary, not page-length).
        exact = 1.0
        matched_phrases.append(phrase)
    else:
        # Known legal phrases dominate; incidental support bigrams are weaker.
        # Additional known hits add a small bonus so void ab initio + WHEREFORE
        # outranks a page that only echoes declaratory relief / wherefore clause.
        best = 0.0
        known_hits = 0
        for candidate in known_phrases:
            if candidate and candidate in hay:
                matched_phrases.append(candidate)
                known_hits += 1
                parts = max(1, len(candidate.split()))
                if parts >= 3:
                    local = 0.88
                elif parts == 2:
                    local = 0.70
                else:
                    local = 0.55
                if local > best:
                    best = local
        support_best = 0.0
        for candidate in support_phrases:
            if candidate and candidate in hay:
                matched_phrases.append(candidate)
                parts = max(1, len(candidate.split()))
                local = 0.34 if parts >= 2 else 0.22
                if local > support_best:
                    support_best = local
        if known_hits:
            exact = min(1.0, best + (0.12 * (known_hits - 1)))
        else:
            exact = support_best

    matched = []
    distinctive = _distinctive_retrieval_tokens(tokens)
    # Tokens that only appear inside an unmatched query legal phrase should not
    # earn full scattered coverage (prevents void + initio without the phrase).
    phrase_locked_tokens = set()
    for candidate in known_phrases:
        if candidate and candidate not in hay:
            for part in candidate.split():
                if part in distinctive or len(part) <= 2:
                    phrase_locked_tokens.add(part)

    if distinctive:
        weighted_hits = 0.0
        for token in distinctive:
            if token in hay:
                matched.append(token)
                if token in phrase_locked_tokens:
                    weighted_hits += 0.25
                else:
                    weighted_hits += 1.0
        coverage = weighted_hits / float(len(distinctive))
    elif tokens:
        for token in tokens:
            if token in hay:
                matched.append(token)
        coverage = len(matched) / float(len(tokens))
    else:
        coverage = 0.0

    return {
        "exact_phrase": exact,
        "token_coverage": min(1.0, coverage),
        "matched_tokens": matched,
        "matched_phrases": matched_phrases,
    }


def _excerpt_query_grounding(excerpt, tokens, phrases=None):
    """
    Ratio of distinctive query signal present in the returned excerpt.

    Used to gate case-map / relationship elevation so metadata-only pages do
    not outrank strongly grounded excerpts.
    """
    hay = normalize_retrieval_text(excerpt)
    if not hay:
        return 0.0

    _known, _support, phrase_list = _phrase_lists(phrases)
    distinctive = _distinctive_retrieval_tokens(tokens)
    if not distinctive and not phrase_list:
        return 0.0

    hits = 0.0
    total = 0.0
    for candidate in phrase_list:
        total += 1.5
        if candidate and candidate in hay:
            hits += 1.5
    for token in distinctive:
        total += 1.0
        if token in hay:
            hits += 1.0
    if total <= 0:
        return 0.0
    return min(1.0, hits / total)


def _boilerplate_penalty_component(
    text,
    *,
    normalized_query,
    tokens,
    hints,
):
    """
    Conservative demotion for stamp/service/caption shells on substantive
    queries. Procedural/service/discovery queries keep full retrieval.
    """
    if _query_seeks_procedural_service(normalized_query, tokens, hints):
        return 0.0, None
    if not _query_seeks_substantive_content(normalized_query, tokens, hints):
        return 0.0, None

    kind, strength = _detect_retrieval_boilerplate(text)
    if not kind or strength <= 0:
        return 0.0, None
    return _round_retrieval_score(
        strength * RETRIEVAL_WEIGHTS["boilerplate_penalty"]
    ), kind


def _metadata_component_score(document_type, hints):
    wanted = set(hints.get("document_types") or [])
    if not wanted:
        return 0.0
    if document_type in wanted:
        return 1.0
    # Policy language often lives inside complaints/answers/exhibits.
    if "policy" in wanted and document_type in {"complaint", "answer", "exhibit", "other"}:
        return 0.45
    return 0.0


def _exhibit_component_score(segment, hints, phrase, tokens):
    if not segment:
        return 0.0, None
    label = normalize_exhibit_label(segment.get("exhibit_label"))
    score = 0.0
    if label and label in (hints.get("exhibit_labels") or []):
        score = 1.0
    elif label and tokens and label.lower() in tokens:
        score = 0.7
    elif segment.get("segment_type") == "exhibit" and (
        "exhibit" in (phrase or "") or "exhibit" in (tokens or [])
    ):
        score = 0.35
    return score, segment


def _case_map_component_scores(signals, phrase, tokens, hints):
    if not signals:
        return 0.0, 0.0, None, []

    best_node = None
    best_node_score = 0.0
    rel_score = 0.0
    explanations = []

    wanted_collections = set(hints.get("case_map_categories") or [])

    for signal in signals:
        search_text = signal.get("search_text") or ""
        lex = _lexical_component_scores(search_text, phrase, tokens)
        local = (0.65 * lex["exact_phrase"]) + (0.35 * lex["token_coverage"])
        if signal.get("kind") == "node":
            if signal.get("collection") in wanted_collections:
                local += 0.25
            if local > best_node_score:
                best_node_score = local
                best_node = signal
            if local > 0:
                explanations.append(
                    f"case-map node {signal.get('node_id')} matched ({signal.get('assertion_kind')})"
                )
        elif signal.get("kind") == "relationship":
            local_rel = local
            if local_rel > rel_score:
                rel_score = local_rel
            if local_rel > 0:
                explanations.append(
                    f"case-map relationship {signal.get('relationship_id')} matched"
                )

    return min(best_node_score, 1.0), min(rel_score, 1.0), best_node, explanations


def _passes_canonical_filters(candidate, filters):
    if not filters:
        return True

    filing = filters.get("nyscef_document_number")
    if filing is not None:
        if isinstance(filing, (list, tuple, set)):
            allowed = {
                coerce_nyscef_document_number(item) for item in filing
            }
            if candidate["nyscef_document_number"] not in allowed:
                return False
        else:
            if candidate["nyscef_document_number"] != coerce_nyscef_document_number(
                filing
            ):
                return False

    doc_type = filters.get("document_type") or filters.get("category")
    if doc_type:
        if isinstance(doc_type, (list, tuple, set)):
            if candidate["document_type"] not in set(doc_type):
                return False
        elif candidate["document_type"] != doc_type:
            return False

    case_map_category = filters.get("case_map_category")
    if case_map_category:
        linkage = candidate.get("case_map_linkage") or {}
        collection = linkage.get("collection")
        if isinstance(case_map_category, (list, tuple, set)):
            if collection not in set(case_map_category):
                return False
        elif collection != case_map_category:
            return False

    classification = filters.get("classification")
    if classification:
        flags = set(candidate.get("classifications") or [])
        if isinstance(classification, (list, tuple, set)):
            if not flags.intersection(set(classification)):
                return False
        elif classification not in flags:
            return False

    exhibit = filters.get("exhibit_segment")
    if exhibit is not None and exhibit != "":
        segment = candidate.get("exhibit_segment") or {}
        label = normalize_exhibit_label(segment.get("exhibit_label"))
        segment_id = segment.get("segment_id")
        wanted = str(exhibit).strip()
        wanted_norm = normalize_retrieval_text(wanted)
        if wanted_norm.startswith("exhibit "):
            wanted_label = normalize_exhibit_label(wanted_norm[8:])
        else:
            wanted_label = normalize_exhibit_label(wanted)
        if wanted == segment_id:
            return True
        if wanted_label and label and wanted_label == label:
            return True
        if wanted and label and wanted == label:
            return True
        return False

    return True


def _score_page_candidate(
    entry,
    phrase,
    tokens,
    hints,
    case_map_signals,
    phrases=None,
):
    page = entry["page"]
    text = page.get("text") or ""
    _known_phrases, _support_phrases, phrase_list = _phrase_lists(phrases)
    lex = _lexical_component_scores(text, phrase, tokens, phrases=phrases)
    metadata = _metadata_component_score(entry["document_type"], hints)
    exhibit_score, segment = _exhibit_component_score(
        entry.get("segment"), hints, phrase, tokens
    )
    page_signals = case_map_signals.get(page.get("page_id")) or []
    case_map_score, relationship_score, best_node, map_explanations = (
        _case_map_component_scores(page_signals, phrase, tokens, hints)
    )

    excerpt = _retrieval_excerpt(
        text, phrase, tokens, phrases=phrase_list
    )
    page_text_for_materiality = None
    if (hints or {}).get("party_role_intent"):
        excerpt = _party_role_evidence_excerpt(
            entry,
            text,
            phrase=phrase,
            tokens=tokens,
            phrases=phrase_list,
        )
        # Full canonical page text supports materiality decisions downstream;
        # compact evidence packets continue to use the focused excerpt only.
        page_text_for_materiality = text
    grounding = _excerpt_query_grounding(excerpt, tokens, phrases=phrases)
    # Require meaningful excerpt/query grounding before case-map or relationship
    # boosts can materially elevate a result. Weakly grounded pages keep a
    # residual signal so filters can still surface them, but they cannot outrank
    # strongly grounded pages via metadata linkage alone.
    if grounding >= RETRIEVAL_CASE_MAP_GROUNDING_THRESHOLD:
        case_map_gate = 1.0
        relationship_gate = 1.0
        grounding_note = None
    elif grounding > 0:
        case_map_gate = 0.25 * (grounding / RETRIEVAL_CASE_MAP_GROUNDING_THRESHOLD)
        relationship_gate = case_map_gate
        grounding_note = (
            f"case-map boost gated by weak excerpt grounding ({grounding:.3f})"
        )
    else:
        case_map_gate = 0.05
        relationship_gate = 0.05
        grounding_note = "case-map boost gated: no excerpt query grounding"

    gated_case_map = case_map_score * case_map_gate
    gated_relationship = relationship_score * relationship_gate

    penalty_value, boilerplate_kind = _boilerplate_penalty_component(
        text,
        normalized_query=phrase,
        tokens=tokens,
        hints=hints,
    )
    party_role_priority = _party_role_pleading_priority_score(entry, text, hints)

    components = {
        "exact_phrase": _round_retrieval_score(
            lex["exact_phrase"] * RETRIEVAL_WEIGHTS["exact_phrase"]
        ),
        "token_coverage": _round_retrieval_score(
            lex["token_coverage"] * RETRIEVAL_WEIGHTS["token_coverage"]
        ),
        "metadata": _round_retrieval_score(metadata * RETRIEVAL_WEIGHTS["metadata"]),
        "exhibit": _round_retrieval_score(exhibit_score * RETRIEVAL_WEIGHTS["exhibit"]),
        "case_map": _round_retrieval_score(
            gated_case_map * RETRIEVAL_WEIGHTS["case_map"]
        ),
        "relationship": _round_retrieval_score(
            gated_relationship * RETRIEVAL_WEIGHTS["relationship"]
        ),
        "boilerplate_penalty": _round_retrieval_score(-penalty_value)
        if penalty_value
        else 0.0,
        "party_role_pleading": _round_retrieval_score(
            party_role_priority * RETRIEVAL_WEIGHTS["party_role_pleading"]
        ),
    }
    total = _round_retrieval_score(sum(components.values()))

    explanation = []
    if lex["exact_phrase"]:
        if lex.get("matched_phrases"):
            explanation.append(
                "exact phrase match: "
                + ", ".join(lex["matched_phrases"][:4])
            )
        else:
            explanation.append("exact phrase match on page text")
    if lex["matched_tokens"]:
        explanation.append(
            "token matches: " + ", ".join(lex["matched_tokens"][:12])
        )
    if metadata:
        explanation.append(f"document type boost ({entry['document_type']})")
    if exhibit_score and segment:
        explanation.append(
            f"exhibit segment {segment.get('exhibit_label') or segment.get('segment_id')}"
        )
    explanation.extend(map_explanations[:4])
    if grounding_note and (case_map_score > 0 or relationship_score > 0):
        explanation.append(grounding_note)
    if penalty_value and boilerplate_kind:
        explanation.append(
            f"boilerplate penalty ({boilerplate_kind}: -{penalty_value})"
        )
    if party_role_priority:
        explanation.append(
            f"party-role pleading preference ({party_role_priority:.2f})"
        )

    assertion_kind = None
    requires_review = False
    is_review_candidate = False
    linkage = None
    if best_node and (
        gated_case_map > 0
        or gated_relationship > 0
        or lex["exact_phrase"]
        or lex["token_coverage"]
    ):
        assertion_kind = best_node.get("assertion_kind")
        requires_review = bool(best_node.get("requires_review"))
        is_review_candidate = bool(best_node.get("is_review_candidate"))
        linkage = {
            "node_id": best_node.get("node_id"),
            "node_type": best_node.get("node_type"),
            "collection": best_node.get("collection"),
            "label": best_node.get("label"),
            "assertion_kind": assertion_kind,
            "conflicts_with": list(best_node.get("conflicts_with") or []),
        }
    elif page_signals:
        # Page is cited by case-map but query did not match node text; still
        # surface classification from the strongest review-flagged signal.
        for signal in page_signals:
            if signal.get("kind") != "node":
                continue
            assertion_kind = signal.get("assertion_kind")
            requires_review = bool(signal.get("requires_review"))
            is_review_candidate = bool(signal.get("is_review_candidate"))
            linkage = {
                "node_id": signal.get("node_id"),
                "node_type": signal.get("node_type"),
                "collection": signal.get("collection"),
                "label": signal.get("label"),
                "assertion_kind": assertion_kind,
                "conflicts_with": list(signal.get("conflicts_with") or []),
            }
            break

    classifications = map_retrieval_classifications(
        assertion_kind,
        requires_review=requires_review,
        is_review_candidate=is_review_candidate,
    )

    exhibit_payload = None
    if segment and segment.get("segment_type") == "exhibit":
        exhibit_payload = {
            "segment_id": segment.get("segment_id"),
            "exhibit_label": segment.get("exhibit_label"),
            "exhibit_title": segment.get("exhibit_title"),
            "segment_type": segment.get("segment_type"),
            "start_page": segment.get("start_page"),
            "end_page": segment.get("end_page"),
        }
    elif segment:
        exhibit_payload = {
            "segment_id": segment.get("segment_id"),
            "exhibit_label": segment.get("exhibit_label"),
            "exhibit_title": segment.get("exhibit_title"),
            "segment_type": segment.get("segment_type"),
            "start_page": segment.get("start_page"),
            "end_page": segment.get("end_page"),
        }

    return {
        "result_id": make_canonical_result_id(
            entry["nyscef_document_number"],
            page.get("page_number"),
            (exhibit_payload or {}).get("segment_id")
            if exhibit_payload and exhibit_payload.get("segment_type") == "exhibit"
            else None,
        ),
        "page_id": page.get("page_id"),
        "nyscef_document_number": entry["nyscef_document_number"],
        "pdf_page": page.get("page_number"),
        "source_filename": entry["filename"],
        "document_type": entry["document_type"],
        "exhibit_segment": exhibit_payload,
        "excerpt": excerpt,
        "page_text": page_text_for_materiality,
        "component_scores": components,
        "score": total,
        "ranking_explanation": explanation,
        "case_map_linkage": linkage,
        "classifications": classifications,
        "assertion_kind": assertion_kind or "unknown",
        "matched_tokens": list(lex["matched_tokens"]),
        "excerpt_grounding": _round_retrieval_score(grounding),
    }


def _deduplicate_canonical_hits(candidates):
    """
    Collapse overlapping page/segment hits to one result per page_id.

    Distinct page_ids (including conflicting allegation sources) are retained.
    When merging, keep the higher score and prefer exhibit-segment provenance.
    """
    best_by_page = {}
    duplicate_count = 0
    for candidate in candidates:
        page_id = candidate.get("page_id")
        if not page_id:
            continue
        existing = best_by_page.get(page_id)
        if existing is None:
            best_by_page[page_id] = candidate
            continue
        duplicate_count += 1
        keep = candidate
        drop = existing
        if existing["score"] > candidate["score"]:
            keep = existing
            drop = candidate
        elif existing["score"] == candidate["score"]:
            # Deterministic tie-break: prefer exhibit segment, then result_id.
            keep_exhibit = bool(
                (existing.get("exhibit_segment") or {}).get("segment_type") == "exhibit"
            )
            cand_exhibit = bool(
                (candidate.get("exhibit_segment") or {}).get("segment_type") == "exhibit"
            )
            if cand_exhibit and not keep_exhibit:
                keep = candidate
                drop = existing
            elif keep_exhibit == cand_exhibit:
                if candidate.get("result_id", "") < existing.get("result_id", ""):
                    keep = candidate
                    drop = existing
        # Prefer non-empty exhibit segment metadata from either side.
        if keep is candidate and (drop.get("exhibit_segment") and not keep.get("exhibit_segment")):
            keep = dict(keep)
            keep["exhibit_segment"] = drop["exhibit_segment"]
            keep["result_id"] = make_canonical_result_id(
                keep["nyscef_document_number"],
                keep["pdf_page"],
                (keep["exhibit_segment"] or {}).get("segment_id")
                if (keep["exhibit_segment"] or {}).get("segment_type") == "exhibit"
                else None,
            )
        best_by_page[page_id] = keep

    deduped = list(best_by_page.values())
    deduped.sort(
        key=lambda item: (
            -item["score"],
            item.get("nyscef_document_number") is None,
            item.get("nyscef_document_number") if item.get("nyscef_document_number") is not None else 10**9,
            item.get("pdf_page") or 0,
            item.get("result_id") or "",
        )
    )
    return deduped, duplicate_count


def _diversify_by_filing(results, *, top_k, filing_penalty=0.18, max_per_filing=None):
    """
    Re-rank so a single large filing cannot monopolize the top-k solely by
    page count. Uses a deterministic greedy penalty on repeated filings.
    """
    if top_k <= 0:
        return []

    selected = []
    filing_counts = {}
    remaining = list(results)

    while remaining and len(selected) < top_k:
        best_index = None
        best_adjusted = None
        best_item = None
        for index, item in enumerate(remaining):
            filing = item.get("nyscef_document_number")
            count = filing_counts.get(filing, 0)
            if max_per_filing is not None and count >= max_per_filing:
                continue
            adjusted = _round_retrieval_score(
                item["score"] * (1.0 / (1.0 + (filing_penalty * count)))
            )
            key = (
                adjusted,
                # Prefer previously unseen filings on ties to increase coverage.
                1 if count == 0 else 0,
                -(item.get("nyscef_document_number") or 0)
                if item.get("nyscef_document_number") is not None
                else 0,
                -(item.get("pdf_page") or 0),
                item.get("result_id") or "",
            )
            if best_adjusted is None or key > best_adjusted:
                best_adjusted = key
                best_index = index
                best_item = item
        if best_index is None:
            break
        chosen = dict(best_item)
        chosen["diversity_adjusted_score"] = best_adjusted[0]
        selected.append(chosen)
        filing = chosen.get("nyscef_document_number")
        filing_counts[filing] = filing_counts.get(filing, 0) + 1
        remaining.pop(best_index)

    return selected


def validate_canonical_result_citation(result, page_lookup):
    """Return True when a result cites a known page record."""
    if not isinstance(result, dict):
        return False
    page_id = result.get("page_id")
    if not page_id or page_id not in page_lookup:
        return False
    if result.get("nyscef_document_number") is None:
        return False
    if result.get("pdf_page") is None:
        return False
    expected = page_lookup[page_id]
    if expected["nyscef_document_number"] != result.get("nyscef_document_number"):
        return False
    if expected["page"].get("page_number") != result.get("pdf_page"):
        return False
    return True


def compute_canonical_retrieval_metrics(payload, documents=None):
    """
    Benchmark-friendly diagnostics. Does not claim relevance/recall.

    Metrics:
      - citation_validity
      - unique_filing_coverage
      - duplicate_hit_rate
      - deterministic_ranking (caller may set via compare)
      - unsupported_result_rate
      - top_k_evidence
    """
    results = list((payload or {}).get("results") or [])
    diagnostics = (payload or {}).get("diagnostics") or {}
    page_lookup = _page_lookup_from_documents(documents or [])

    valid = 0
    unsupported = 0
    filings = []
    for result in results:
        ok = validate_canonical_result_citation(result, page_lookup) if page_lookup else bool(
            result.get("page_id") and result.get("nyscef_document_number") is not None
        )
        if ok:
            valid += 1
        else:
            unsupported += 1
        filing = result.get("nyscef_document_number")
        if filing is not None and filing not in filings:
            filings.append(filing)

    total = len(results)
    pre_dedup = diagnostics.get("pre_dedup_count")
    duplicate_count = diagnostics.get("duplicate_count", 0)
    if pre_dedup:
        duplicate_hit_rate = duplicate_count / float(pre_dedup)
    else:
        duplicate_hit_rate = 0.0

    top_k_evidence = [
        {
            "result_id": item.get("result_id"),
            "page_id": item.get("page_id"),
            "nyscef_document_number": item.get("nyscef_document_number"),
            "excerpt": item.get("excerpt"),
            "score": item.get("score"),
            "classifications": item.get("classifications"),
            "case_map_linkage": item.get("case_map_linkage"),
        }
        for item in results
    ]

    return {
        "citation_validity": (valid / float(total)) if total else 1.0,
        "unique_filing_coverage": len(filings),
        "unique_filings": filings,
        "duplicate_hit_rate": _round_retrieval_score(duplicate_hit_rate),
        "unsupported_result_rate": (unsupported / float(total)) if total else 0.0,
        "result_count": total,
        "top_k_evidence": top_k_evidence,
        "notes": (
            "Metrics are structural/diagnostic only; relevance and recall "
            "require gold labels and are not claimed here."
        ),
    }


def ranking_is_deterministic(results_a, results_b):
    """Compare two ranked result lists for identical order and scores."""
    if len(results_a) != len(results_b):
        return False
    for left, right in zip(results_a, results_b):
        if left.get("result_id") != right.get("result_id"):
            return False
        if left.get("score") != right.get("score"):
            return False
        if left.get("page_id") != right.get("page_id"):
            return False
    return True


def prepare_documents_for_canonical_retrieval(documents, *, include_exhibit_segments=True):
    """Normalize documents with exhibit segments for retrieval (opt-in helper)."""
    prepared = []
    for document in documents or []:
        prepared.append(
            normalize_document(
                document,
                include_exhibit_segments=include_exhibit_segments,
            )
        )
    return prepared


def retrieve_canonical_records(
    documents,
    query,
    *,
    case_map=None,
    filters=None,
    top_k=20,
    include_diagnostics=False,
    build_case_map_if_missing=True,
    filing_diversity_penalty=0.18,
    max_per_filing=None,
    min_score=0.0,
):
    """
    Opt-in provenance-preserving retrieval over canonical page records.

    Searches at page and exhibit-segment granularity while retaining parent
    NYSCEF filing provenance. Case-map nodes contribute ranking signals but
    every returned hit is page-backed evidence (not a legal conclusion).
    """
    prepared = prepare_documents_for_canonical_retrieval(documents)
    phrase, tokens = tokenize_retrieval_query(query)
    phrases = _extract_retrieval_phrases(phrase, tokens)
    hints = _detect_query_category_hints(phrase, tokens)

    active_case_map = case_map
    if active_case_map is None and build_case_map_if_missing:
        active_case_map = build_case_map_from_documents(prepared)
    elif active_case_map is None:
        active_case_map = empty_case_map()

    page_lookup = _page_lookup_from_documents(prepared)
    case_map_signals = _case_map_signals_by_page(active_case_map, page_lookup)

    raw_candidates = []
    for page_id, entry in sorted(
        page_lookup.items(),
        key=lambda item: (
            item[1]["nyscef_document_number"] is None,
            item[1]["nyscef_document_number"]
            if item[1]["nyscef_document_number"] is not None
            else 10**9,
            item[1]["page"].get("page_number") or 0,
            item[0],
        ),
    ):
        candidate = _score_page_candidate(
            entry, phrase, tokens, hints, case_map_signals, phrases=phrases
        )
        # Require some evidence signal: lexical and/or case-map/exhibit/metadata.
        if candidate["score"] <= 0 and not candidate["matched_tokens"]:
            # Keep pure metadata-less empty pages out.
            if not (
                candidate["component_scores"]["exact_phrase"]
                or candidate["component_scores"]["token_coverage"]
                or candidate["component_scores"]["exhibit"]
                or candidate["component_scores"]["case_map"]
                or candidate["component_scores"]["relationship"]
            ):
                continue
        if candidate["score"] < min_score:
            continue
        if not _passes_canonical_filters(candidate, filters):
            continue
        # Never emit a case-map assertion without underlying record citation.
        if candidate.get("case_map_linkage") and not candidate.get("page_id"):
            continue
        raw_candidates.append(candidate)

    deduped, duplicate_count = _deduplicate_canonical_hits(raw_candidates)
    ranked = _diversify_by_filing(
        deduped,
        top_k=top_k,
        filing_penalty=filing_diversity_penalty,
        max_per_filing=max_per_filing,
    )

    # Party-role intent: keep contiguous PARTIES-section pages available even
    # when individual page scores fall below the ordinary top-page cutoff.
    if hints.get("party_role_intent"):
        scored_by_page = {
            item.get("page_id"): item
            for item in deduped
            if isinstance(item, dict) and item.get("page_id")
        }
        ranked = _ensure_party_role_section_pages(
            ranked,
            page_lookup=page_lookup,
            phrase=phrase,
            tokens=tokens,
            hints=hints,
            case_map_signals=case_map_signals,
            phrases=phrases,
            scored_by_page=scored_by_page,
        )

    # Final deterministic ordering key after diversity selection.
    ranked.sort(
        key=lambda item: (
            -item.get("diversity_adjusted_score", item["score"]),
            -item["score"],
            item.get("nyscef_document_number") is None,
            item.get("nyscef_document_number")
            if item.get("nyscef_document_number") is not None
            else 10**9,
            item.get("pdf_page") or 0,
            item.get("result_id") or "",
        )
    )

    # Drop any unsupported / invalid citations defensively.
    validated = []
    rejected_invalid = 0
    for item in ranked:
        if validate_canonical_result_citation(item, page_lookup):
            validated.append(item)
        else:
            rejected_invalid += 1

    payload = {
        "query": query,
        "normalized_query": phrase,
        "tokens": tokens,
        "filters": filters or {},
        "results": validated,
        "result_count": len(validated),
    }

    if include_diagnostics:
        diagnostics = {
            "pre_dedup_count": len(raw_candidates),
            "post_dedup_count": len(deduped),
            "duplicate_count": duplicate_count,
            "rejected_invalid_citations": rejected_invalid,
            "query_hints": hints,
            "query_phrases": list((_phrase_lists(phrases))[2]),
            "weights": dict(RETRIEVAL_WEIGHTS),
            "case_map_used": bool(active_case_map and any(
                active_case_map.get(collection)
                for collection in CASE_MAP_NODE_COLLECTIONS
            )),
            "page_corpus_size": len(page_lookup),
            "scoring": (
                "hybrid lexical (exact/multiword phrase + distinctive token "
                "coverage) + metadata/category + exhibit + grounding-gated "
                "case-map/relationship - boilerplate penalty; no vector DB"
            ),
        }
        payload["diagnostics"] = diagnostics
        payload["metrics"] = compute_canonical_retrieval_metrics(payload, prepared)

    return payload


def retrieve_canonical_records_benchmark(
    documents,
    query,
    *,
    case_map=None,
    filters=None,
    top_k=20,
    **kwargs,
):
    """
    Benchmark-friendly API: ranked results plus diagnostics/metrics.

    Does not claim relevance or recall without gold labels.
    """
    primary = retrieve_canonical_records(
        documents,
        query,
        case_map=case_map,
        filters=filters,
        top_k=top_k,
        include_diagnostics=True,
        **kwargs,
    )
    secondary = retrieve_canonical_records(
        documents,
        query,
        case_map=case_map,
        filters=filters,
        top_k=top_k,
        include_diagnostics=False,
        **kwargs,
    )
    primary["metrics"]["deterministic_ranking"] = ranking_is_deterministic(
        primary.get("results") or [],
        secondary.get("results") or [],
    )
    return primary


def build_attorney_work_product(
    summary,
    documents,
    *,
    retrieval_grounded_qa=None,
):
    """
    Attorney work-product container.

    Default shape is unchanged (empty drafting stubs). When a retrieval-grounded
    Q&A payload is supplied (opt-in), it is nested under retrieval_grounded_qa.
    """
    product = {
        "plaintiff_core_arguments": [],
        "defense_core_arguments": [],
        "strongest_authorities": [],
        "weaknesses": [],
        "drafting_strategy": [],
        "recommended_outline": [],
        "draft_generation": {},
        "citation_exhibit_engine": {},
    }
    if retrieval_grounded_qa is not None:
        product["retrieval_grounded_qa"] = retrieval_grounded_qa
    return product


def build_matter_summary(documents):
    selected = selected_case_summary(documents)

    text = combined_text(documents)

    if selected and selected.get("title"):
        case_name = selected["title"]
    else:
        case_name = extract_case_name(text)

    parties = extract_parties(case_name)

    summary = {
        "case_name": case_name,
        "index_number": extract_index_number(text),
        "plaintiff": parties["plaintiff"],
        "defendant": parties["defendant"],
        "motion_posture": detect_motion_posture(documents, text),
        "procedural_posture": detect_procedural_posture(text),
        "strongest_motion_documents": strongest_motion_documents(documents),
        "selected_case": selected,
    }

    summary["issue_packet"] = build_issue_analysis(
        selected,
        documents,
    )

    summary["contradiction_analysis"] = build_contradiction_analysis(documents)

    summary["attorney_work_product"] = build_attorney_work_product(summary, documents)

    return summary


def get_matter(
    selected_case=None,
    documents=None,
    matter_folder=None,
    inventory_path=None,
    *,
    include_exhibit_segments=False,
    include_case_map=False,
    canonical_retrieval_query=None,
    canonical_retrieval_options=None,
    attorney_qa_question=None,
    attorney_qa_options=None,
):
    resolved_folder = resolve_matter_folder(matter_folder)
    folder_documents = read_matter_folder(
        resolved_folder,
        inventory_path=inventory_path,
    )

    selected_case_document = None

    if isinstance(selected_case, dict):
        selected_case_document = selected_case_to_document(selected_case)

    if documents is None:
        documents = []

    all_documents = []

    if selected_case_document:
        all_documents.append(selected_case_document)

    all_documents.extend(folder_documents)
    all_documents.extend(documents)

    # Case-map / canonical retrieval need exhibit segment provenance when pages
    # exist. Opt-in only: default consumers still omit segments and case_map.
    # Attorney Q&A is also opt-in and implies retrieval over the same question
    # when canonical_retrieval_query is not separately provided.
    qa_opt_in = attorney_qa_question is not None
    retrieval_opt_in = canonical_retrieval_query is not None or qa_opt_in
    segment_opt_in = (
        include_exhibit_segments or include_case_map or retrieval_opt_in
    )

    normalized_documents = [
        normalize_document(
            doc,
            include_exhibit_segments=segment_opt_in,
        )
        for doc in all_documents
    ]

    grouped_documents = group_documents(normalized_documents)

    summary = build_matter_summary(normalized_documents)

    result = {
        "matter_name": summary["case_name"],
        "case_name": summary["case_name"],
        "index_number": summary["index_number"],
        "document_count": len(normalized_documents),
        "documents": normalized_documents,
        "groups": grouped_documents,
        "grouped_documents": grouped_documents,
        "folder": str(resolved_folder),
        "summary": summary,
        "selected_case": summary.get("selected_case"),
        "issue_packet": summary.get("issue_packet", {}),
        "contradiction_analysis": summary.get("contradiction_analysis", {}),
        "attorney_work_product": summary.get("attorney_work_product", {}),
        "draft_generation": summary.get("attorney_work_product", {}).get("draft_generation", {}),
        "citation_exhibit_engine": summary.get("attorney_work_product", {}).get("citation_exhibit_engine", {}),
    }

    case_map = None
    if include_case_map or retrieval_opt_in:
        case_map = build_case_map_from_documents(normalized_documents)
        if include_case_map:
            result["case_map"] = case_map

    if retrieval_opt_in:
        retrieval_query = (
            canonical_retrieval_query
            if canonical_retrieval_query is not None
            else attorney_qa_question
        )
        options = dict(canonical_retrieval_options or {})
        options.setdefault("include_diagnostics", True)
        options.setdefault("build_case_map_if_missing", False)
        result["canonical_retrieval"] = retrieve_canonical_records(
            normalized_documents,
            retrieval_query,
            case_map=case_map,
            **options,
        )

    if qa_opt_in:
        qa_options = dict(attorney_qa_options or {})
        model_call = qa_options.pop("model_call", None)
        allowed_sources = qa_options.pop("allowed_sources", None)
        exhibit_context = qa_options.pop("exhibit_context", None)
        qa_payload = build_retrieval_grounded_qa(
            summary,
            normalized_documents,
            question=attorney_qa_question,
            retrieval=result.get("canonical_retrieval"),
            case_map=case_map,
            exhibit_context=exhibit_context,
            allowed_sources=allowed_sources,
            model_call=model_call,
        )
        # Re-seal attorney work product with the opt-in Q&A nested inside.
        work_product = build_attorney_work_product(
            summary,
            normalized_documents,
            retrieval_grounded_qa=qa_payload,
        )
        summary["attorney_work_product"] = work_product
        result["summary"] = summary
        result["attorney_work_product"] = work_product
        result["draft_generation"] = work_product.get("draft_generation", {})
        result["citation_exhibit_engine"] = work_product.get(
            "citation_exhibit_engine", {}
        )
        result["retrieval_grounded_qa"] = qa_payload

    return result
