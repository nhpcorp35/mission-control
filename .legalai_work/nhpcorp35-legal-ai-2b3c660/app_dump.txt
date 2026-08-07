import csv
import html
import math
import re
from pathlib import Path

from flask import Flask, render_template, request, url_for

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
CSV_PATH = BASE_DIR / "output_clean.csv"
PDF_DIR = BASE_DIR / "static" / "pdfs"
MIN_PDF_SIZE = 1000
PER_PAGE = 10


# =========================
# Phase 4 Ranking Weights
# =========================

EXACT_FULL_TEXT_PHRASE_BOOST = 1200
EXACT_SECTION_PHRASE_BOOST = 900
EXACT_METADATA_PHRASE_BOOST = 120

LEGAL_TERM_BOOST = 200
CAUSE_OF_ACTION_BOOST = 240

PARTIAL_FULL_TEXT_BOOST = 40
PARTIAL_SECTION_BOOST = 25
PARTIAL_METADATA_BOOST = 8


LEGAL_PHRASES = {
    "motion to dismiss",
    "summary judgment",
    "breach of contract",
    "tortious interference",
    "fraud",
    "negligence",
    "breach of fiduciary duty",
    "unjust enrichment",
    "deceptive trade practices",
    "conversion",
    "assault",
    "battery",
    "trespass",
    "defamation",
    "promissory estoppel",
    "injunction",
    "writ of mandamus",
}

CAUSES_OF_ACTION = {
    "breach of contract",
    "tortious interference",
    "conversion",
    "fraud",
    "deceptive and unlawful trade practices",
    "breach of fiduciary duty",
    "negligence",
    "assault",
    "battery",
    "trespass to land",
    "defamation",
    "unjust enrichment",
    "extortion",
    "invasion of privacy",
    "intentional infliction of emotional distress",
    "labor law 200",
    "labor law 240",
    "labor law 241",
    "foreclosure on a mortgage",
    "foreclosure on a lien",
    "civil rights interference",
}


# =========================
# Utilities
# =========================

def normalize_space(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_text(value):
    text = normalize_space(value).lower()
    text = text.replace("\u2019", "'")
    return text


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def tokenize_query(query):
    return [t for t in re.findall(r"[A-Za-z0-9\-]+", normalize_text(query)) if t]


def tokenize_text(text):
    return set(re.findall(r"[A-Za-z0-9\-]+", normalize_text(text)))


def split_judges(text):
    raw = normalize_space(text)
    if not raw:
        return []

    parts = re.split(r"\s*(?:,|;|/| and |\band\b|&|\|)\s*", raw, flags=re.IGNORECASE)
    cleaned = []

    for part in parts:
        part = normalize_space(part)
        if part:
            cleaned.append(part)

    seen = set()
    result = []
    for item in cleaned:
        key = normalize_text(item)
        if key not in seen:
            seen.add(key)
            result.append(item)

    return result


def judge_key(name):
    return normalize_text(name)


def overlap_count(a, b):
    return len(set(a) & set(b))


def html_highlight(text, terms):
    escaped = html.escape(str(text or ""))
    if not escaped or not terms:
        return escaped

    unique_terms = []
    seen = set()

    for term in sorted(terms, key=len, reverse=True):
        key = normalize_text(term)
        if key and key not in seen:
            seen.add(key)
            unique_terms.append(re.escape(term))

    if not unique_terms:
        return escaped

    pattern = re.compile(r"(" + "|".join(unique_terms) + r")", re.IGNORECASE)
    return pattern.sub(r"<mark>\1</mark>", escaped)


def first_nonempty(row, candidates, default=""):
    for key in candidates:
        if key in row:
            value = normalize_space(row.get(key, ""))
            if value:
                return value
    return default


def normalize_outcome(value):
    text = normalize_text(value)
    if "affirm" in text:
        return "affirmed"
    if "reverse" in text:
        return "reversed"
    if "modify" in text:
        return "modified"
    if "vacat" in text:
        return "vacated"
    if "dismiss" in text:
        return "dismissed"
    return normalize_space(value)


# =========================
# PDF indexing / matching
# =========================

def build_pdf_index():
    pdf_index = {}
    if not PDF_DIR.exists():
        return pdf_index

    for pdf_path in sorted(PDF_DIR.glob("*.pdf")):
        if not pdf_path.is_file():
            continue
        if pdf_path.stat().st_size < MIN_PDF_SIZE:
            continue

        stem = pdf_path.stem
        case_number = stem.split("__", 1)[0].strip()

        if case_number and case_number not in pdf_index:
            pdf_index[case_number] = pdf_path

    return pdf_index


def find_pdf_for_case(case_number, pdf_index):
    case_number = normalize_space(case_number)
    if not case_number:
        return None

    if case_number in pdf_index:
        return pdf_index[case_number]

    prefix = f"{case_number}__"
    for pdf_path in pdf_index.values():
        if pdf_path.name.startswith(prefix) or pdf_path.name == f"{case_number}.pdf":
            return pdf_path

    return None


# =========================
# Data loading + validation
# =========================

def load_rows():
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"CSV not found: {CSV_PATH}")

    pdf_index = build_pdf_index()
    rows = []

    with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        for idx, raw in enumerate(reader, start=1):
            row = {k.strip(): normalize_space(v) for k, v in raw.items() if k is not None}

            case_number = first_nonempty(row, [
                "case_number", "case no", "case_no", "docket", "docket_number", "index_number"
            ])
            title = first_nonempty(row, [
                "case_name", "title", "caption", "name", "parties"
            ])
            court = first_nonempty(row, [
                "court", "court_name"
            ])
            outcome = normalize_outcome(first_nonempty(row, [
                "outcome", "result", "disposition", "decision"
            ]))
            judges_text = first_nonempty(row, [
                "judges", "judge", "panel", "author", "justice", "justices"
            ])
            date = first_nonempty(row, [
                "date", "decision_date", "filed_date"
            ])
            summary = first_nonempty(row, [
                "summary", "description", "text", "excerpt", "reporters", "slip_op"
            ])

            pdf_path = find_pdf_for_case(case_number, pdf_index)
            pdf_valid = bool(pdf_path and pdf_path.exists() and pdf_path.stat().st_size >= MIN_PDF_SIZE)

            if not pdf_valid:
                continue

            judges = split_judges(judges_text)
            judge_keys = [judge_key(j) for j in judges]

            search_blob = " | ".join([
                case_number,
                title,
                court,
                outcome,
                judges_text,
                date,
                summary,
                row.get("facts_excerpt", ""),
                row.get("procedure_text", ""),
                row.get("claims_text", ""),
                row.get("relief_text", ""),
                row.get("full_text", ""),
                row.get("text_excerpt", ""),
            ])

            rows.append({
                "id": idx,
                "raw": row,
                "case_number": case_number,
                "title": title,
                "court": court,
                "outcome": outcome,
                "judges_text": judges_text,
                "judges": judges,
                "judge_keys": judge_keys,
                "date": date,
                "summary": summary,
                "pdf_filename": pdf_path.name,
                "pdf_valid": True,
                "search_blob": search_blob,
                "search_blob_norm": normalize_text(search_blob),
                "full_text": row.get("full_text", ""),
                "facts_excerpt": row.get("facts_excerpt", ""),
                "procedure_text": row.get("procedure_text", ""),
                "claims_text": row.get("claims_text", ""),
                "relief_text": row.get("relief_text", ""),
                "title_tokens": tokenize_text(title),
            })

    return rows


# =========================
# Search
# =========================

def score_row(row, query, terms):
    if not query:
        return 0

    q_norm = normalize_text(query)
    score = 0

    case_number = normalize_text(row["case_number"])
    title = normalize_text(row["title"])
    court = normalize_text(row["court"])
    outcome = normalize_text(row["outcome"])
    judges_text = normalize_text(row["judges_text"])
    summary = normalize_text(row["summary"])

    full_text = normalize_text(row.get("full_text", ""))
    facts_excerpt = normalize_text(row.get("facts_excerpt", ""))
    procedure_text = normalize_text(row.get("procedure_text", ""))
    claims_text = normalize_text(row.get("claims_text", ""))
    relief_text = normalize_text(row.get("relief_text", ""))

    section_fields = [
        facts_excerpt,
        procedure_text,
        claims_text,
        relief_text,
    ]

    metadata_text = normalize_text(" ".join([
        row["case_number"],
        row["title"],
        row["court"],
        row["outcome"],
        row["judges_text"],
        row["summary"],
    ]))

    blob = row["search_blob_norm"]

    if q_norm == case_number:
        score += 10000

    if q_norm and q_norm in full_text:
        score += EXACT_FULL_TEXT_PHRASE_BOOST

    for field in section_fields:
        if q_norm and q_norm in field:
            score += EXACT_SECTION_PHRASE_BOOST
            break

    if q_norm and q_norm in metadata_text:
        score += EXACT_METADATA_PHRASE_BOOST

    if q_norm and q_norm in title:
        score += 250
    if q_norm and q_norm in case_number:
        score += 220
    if q_norm and q_norm in court:
        score += 120
    if q_norm and q_norm in judges_text:
        score += 110
    if q_norm and q_norm in outcome:
        score += 100
    if q_norm and q_norm in summary:
        score += 60

    if q_norm in LEGAL_PHRASES:
        score += LEGAL_TERM_BOOST

    if q_norm in CAUSES_OF_ACTION:
        score += CAUSE_OF_ACTION_BOOST

    if terms:
        full_hits = sum(1 for t in terms if t in full_text)
        section_hits = sum(1 for t in terms if any(t in f for f in section_fields))
        metadata_hits = sum(1 for t in terms if t in metadata_text)

        score += full_hits * PARTIAL_FULL_TEXT_BOOST
        score += section_hits * PARTIAL_SECTION_BOOST
        score += metadata_hits * PARTIAL_METADATA_BOOST

        if all(t in full_text for t in terms):
            score += 100

        if any(all(t in f for t in terms) for f in section_fields):
            score += 60

    if q_norm and q_norm in blob:
        score += 20

    if q_norm and title.startswith(q_norm):
        score += 45

    if q_norm and case_number.startswith(q_norm):
        score += 60

    return score


def search_rows(rows, query):
    query = normalize_space(query)
    if not query:
        return list(rows)

    terms = tokenize_query(query)
    scored = []

    for row in rows:
        score = score_row(row, query, terms)
        if score > 0:
            item = dict(row)
            item["_score"] = score
            scored.append(item)

    scored.sort(
        key=lambda r: (
            -r["_score"],
            normalize_text(r["case_number"]),
            normalize_text(r["title"]),
        )
    )
    return scored


# =========================
# Similar cases
# =========================

def similar_case_score(base_row, candidate):
    if base_row["id"] == candidate["id"]:
        return 0

    score = 0

    if normalize_text(base_row["court"]) and normalize_text(base_row["court"]) == normalize_text(candidate["court"]):
        score += 50

    if normalize_text(base_row["outcome"]) and normalize_text(base_row["outcome"]) == normalize_text(candidate["outcome"]):
        score += 35

    judge_overlap = overlap_count(base_row["judge_keys"], candidate["judge_keys"])
    score += judge_overlap * 40

    title_overlap = len(base_row["title_tokens"] & candidate["title_tokens"])
    score += min(title_overlap, 3) * 5

    return score


def attach_similar_cases(rows):
    all_rows = APP_STATE["rows"]

    for row in rows:
        candidates = []

        for candidate in all_rows:
            score = similar_case_score(row, candidate)
            if score <= 0:
                continue
            candidates.append((score, candidate))

        candidates.sort(
            key=lambda x: (
                -x[0],
                normalize_text(x[1]["case_number"]),
                normalize_text(x[1]["title"]),
            )
        )

        similar = []
        base_judges = set(row["judge_keys"])

        for score, candidate in candidates[:3]:
            reason_parts = []

            if normalize_text(row["court"]) == normalize_text(candidate["court"]) and row["court"]:
                reason_parts.append("same court")

            if normalize_text(row["outcome"]) == normalize_text(candidate["outcome"]) and row["outcome"]:
                reason_parts.append("same outcome")

            shared_judges = [
                j for j in candidate["judges"]
                if judge_key(j) in base_judges
            ]
            if shared_judges:
                reason_parts.append("shared judge" if len(shared_judges) == 1 else "shared judges")

            similar.append({
                "case_number": candidate["case_number"],
                "title": candidate["title"],
                "court": candidate["court"],
                "outcome": candidate["outcome"],
                "judges_text": candidate["judges_text"],
                "date": candidate["date"],
                "pdf_filename": candidate["pdf_filename"],
                "reason": ", ".join(reason_parts),
            })

        row["similar_cases"] = similar


# =========================
# Pagination
# =========================

def paginate(items, page, per_page):
    total = len(items)
    total_pages = max(1, math.ceil(total / per_page)) if total else 1

    page = max(1, page)
    page = min(page, total_pages)

    start = (page - 1) * per_page
    end = start + per_page
    page_items = items[start:end]

    return {
        "items": page_items,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_page": page - 1,
        "next_page": page + 1,
        "start_index": start + 1 if total else 0,
        "end_index": min(end, total),
    }


# =========================
# App state
# =========================

APP_STATE = {
    "rows": [],
}


def refresh_state():
    APP_STATE["rows"] = load_rows()


refresh_state()


# =========================
# Routes
# =========================

@app.route("/", methods=["GET"])
def index():
    query = normalize_space(request.args.get("q", ""))
    page = safe_int(request.args.get("page", "1"), 1)

    rows = APP_STATE["rows"]

    # 🔧 FIX: reload if empty
    if not rows:
        refresh_state()
        rows = APP_STATE["rows"]

    filtered = search_rows(rows, query)
    attach_similar_cases(filtered)

    pager = paginate(filtered, page, PER_PAGE)
    terms = tokenize_query(query)

    display_rows = []
    for row in pager["items"]:
        display_rows.append({
            **row,
            "pdf_url": url_for("static", filename=f"pdfs/{row['pdf_filename']}"),
            "title_html": html_highlight(row["title"], terms),
            "case_number_html": html_highlight(row["case_number"], terms),
            "court_html": html_highlight(row["court"], terms),
            "outcome_html": html_highlight(row["outcome"], terms),
            "judges_html": html_highlight(row["judges_text"], terms),
            "summary_html": html_highlight(row["summary"], terms),
            "similar_cases": [
                {
                    **sim,
                    "pdf_url": url_for("static", filename=f"pdfs/{sim['pdf_filename']}")
                }
                for sim in row.get("similar_cases", [])
            ],
        })

    return render_template(
        "index.html",
        query=query,
        results=display_rows,
        pager=pager,
        total_loaded=len(rows),
    )


if __name__ == "__main__":
    app.run(debug=True)