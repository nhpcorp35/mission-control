import os
import re

print("LOADED MATTER BUILDER FROM:", __file__)
print("LOOKING IN:", os.getcwd())


# ============================================================
# Matter Builder — Authority Engine v3.4.3
# Citation Cleanup Upgrade
# ============================================================


def clean_text(value):
    return " ".join(str(value or "").split()).strip()


def safe_lower(value):
    return clean_text(value).lower()


def normalize_text_for_parser(text):
    text = str(text or "")
    text = text.replace("\r", "\n")
    text = re.sub(r"\n+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ============================================================
# Regex
# ============================================================

FULL_CITATION_REGEX = re.compile(
    r'([A-Z][A-Za-z0-9&.,\'"\-\s]+?\s+v\.?\s+[A-Z][A-Za-z0-9&.,\'"\-\s]+?)'
    r'\s*,?\s+'
    r'([0-9]{1,4}\s+(?:AD3d|AD2d|NY3d|NY2d|Misc\s?3d|Misc\s?2d|F3d|F2d|US)\s+[0-9]{1,5})'
    r'\s*\(([^)]*)\)',
    re.IGNORECASE | re.MULTILINE | re.DOTALL
)


CASE_CAPTION_REGEX = re.compile(
    r'([A-Z][A-Za-z0-9&.,\'"\-\s]+?\s+v\.?\s+[A-Z][A-Za-z0-9&.,\'"\-\s]+)',
    re.IGNORECASE | re.MULTILINE
)


# ============================================================
# Terms
# ============================================================

AUTHORITY_TERMS = [
    "held",
    "held that",
    "found",
    "ruled",
    "stated",
    "reasoned",
    "summary judgment",
    "prima facie",
    "motion",
    "burden",
    "standard",
    "dismiss",
    "dismissal",
    "injunction",
    "affirmed",
    "reversed",
    "denied",
    "granted",
]


PLAINTIFF_TERMS = [
    "plaintiff",
    "petitioner",
    "movant",
    "claimant",
]


DEFENDANT_TERMS = [
    "defendant",
    "respondent",
    "opponent",
]


ISSUE_SIGNALS = {
    "summary judgment": ["summary judgment", "prima facie"],
    "motion to dismiss": ["motion to dismiss", "dismissed", "dismissal", "fails to state"],
    "injunctive relief": ["injunction", "injunctive relief", "yellowstone"],
    "motion practice": ["motion", "opposition", "oppose"],
    "burden of proof": ["burden", "prima facie"],
    "pleading sufficiency": ["pleading", "allege facts", "sufficient", "governing elements"],
}


TEXT_FIELDS = [
    "title",
    "case_name",
    "caption",
    "citation",
    "court",
    "date",
    "motion",
    "outcome",
    "rule",
    "holding",
    "reasoning",
    "summary",
    "facts",
    "procedural_history",
    "text",
    "content",
    "body",
]


# ============================================================
# Helpers
# ============================================================

def split_sentences(text):
    text = normalize_text_for_parser(text)
    parts = re.split(r'(?<=[.!?])\s+', text)

    sentences = []

    for part in parts:
        part = clean_text(part)

        if len(part) >= 10:
            sentences.append(part)

    return sentences


def first_nonempty(*values):
    for value in values:
        value = clean_text(value)

        if value:
            return value

    return ""


def get_record_value(data, *keys):
    if not isinstance(data, dict):
        return ""

    for key in keys:
        value = data.get(key)

        if value:
            return clean_text(value)

    return ""


def extract_case_caption_from_text(text):
    text = normalize_text_for_parser(text)

    match = CASE_CAPTION_REGEX.search(text)

    if not match:
        return ""

    candidate = clean_text(match.group(1))

    # ========================================================
    # Remove trailing reporter contamination
    # Example:
    # Smith v Jones, 307 AD2d 234, 237
    # ========================================================

    candidate = re.sub(
        r',?\s+[0-9]{1,4}\s+(AD3d|AD2d|NY3d|NY2d|Misc\s?3d|Misc\s?2d|F3d|F2d|US)\s+[0-9,\s]+$',
        '',
        candidate,
        flags=re.IGNORECASE
    )

    candidate = clean_text(candidate)

    if len(candidate) < 5:
        return ""

    return candidate


def resolve_case_name(data):
    # ========================================================
    # Priority 1:
    # structured metadata
    # ========================================================

    preferred = first_nonempty(
        get_record_value(data, "case_name"),
        get_record_value(data, "caption"),
    )

    if preferred:
        return preferred

    # ========================================================
    # Priority 2:
    # extract from substantive text
    # ========================================================

    extraction_sources = [
        get_record_value(data, "holding"),
        get_record_value(data, "reasoning"),
        get_record_value(data, "summary"),
        get_record_value(data, "procedural_history"),
        get_record_value(data, "text"),
        get_record_value(data, "content"),
        get_record_value(data, "body"),
    ]

    combined = " ".join(extraction_sources)

    extracted = extract_case_caption_from_text(combined)

    if extracted:
        return extracted

    # ========================================================
    # Priority 3:
    # reject generic court labels
    # ========================================================

    title = get_record_value(data, "title")

    generic_titles = [
        "supreme court",
        "appellate division",
        "court of appeals",
        "state of new york",
    ]

    lower = safe_lower(title)

    for phrase in generic_titles:
        if phrase in lower:
            return "Selected Case"

    return title or "Selected Case"


def extract_text_from_dict(data):
    chunks = []

    for field in TEXT_FIELDS:
        value = data.get(field)

        if value:
            chunks.append(f"{field}: {value}")

    for key, value in data.items():
        if key in TEXT_FIELDS:
            continue

        if isinstance(value, str) and len(value.strip()) > 20:
            chunks.append(f"{key}: {value}")

    return "\n\n".join(chunks)


def build_combined_text(input_data):
    if input_data is None:
        return ""

    if isinstance(input_data, dict):
        return extract_text_from_dict(input_data)

    if isinstance(input_data, list):
        chunks = []

        for item in input_data:
            if isinstance(item, dict):
                chunks.append(extract_text_from_dict(item))
            else:
                chunks.append(str(item))

        return "\n\n".join(chunks)

    return str(input_data)


def get_primary_record(input_data):
    if isinstance(input_data, dict):
        return input_data

    if isinstance(input_data, list):
        for item in input_data:
            if isinstance(item, dict):
                return item

    return {}


# ============================================================
# Detection
# ============================================================

def detect_side(sentence):
    lower = safe_lower(sentence)

    for term in PLAINTIFF_TERMS:
        if term in lower:
            return "plaintiff"

    for term in DEFENDANT_TERMS:
        if term in lower:
            return "defendant"

    return "neutral"


def detect_jurisdiction(text, data=None):
    lower = safe_lower(text)

    court_value = safe_lower(get_record_value(data or {}, "court"))
    citation_value = safe_lower(get_record_value(data or {}, "citation"))

    state = "Unknown"
    court = "Unknown"
    department = ""

    combined = f"{lower} {court_value} {citation_value}"

    if (
        "new york" in combined
        or "ny3d" in combined
        or "ny2d" in combined
        or "ad3d" in combined
        or "ad2d" in combined
        or "misc 3d" in combined
        or "misc3d" in combined
    ):
        state = "New York"

    if "court of appeals" in combined or "ny3d" in combined or "ny2d" in combined:
        court = "New York Court of Appeals"
    elif "appellate division" in combined:
        court = "Appellate Division"
    elif "supreme court of the state of new york" in combined:
        court = "Supreme Court"
    elif "supreme court" in combined and state == "New York":
        court = "Supreme Court"

    if "first department" in combined:
        department = "First Department"
        court = "Appellate Division, First Department"

    return {
        "state": state,
        "court": court,
        "department": department,
    }


def detect_issues(text):
    lower = safe_lower(text)
    issues = []

    for issue_name, signals in ISSUE_SIGNALS.items():
        hits = 0

        for signal in signals:
            if signal in lower:
                hits += 1

        if hits > 0:
            issues.append(
                {
                    "issue": issue_name,
                    "hits": hits,
                }
            )

    issues.sort(key=lambda x: x.get("hits", 0), reverse=True)

    return issues


def extract_procedural_posture(text, data=None):
    data = data or {}

    motion = get_record_value(data, "motion")
    outcome = get_record_value(data, "outcome")
    holding = get_record_value(data, "holding")

    source = " ".join([motion, outcome, holding, text])
    lower = safe_lower(source)

    posture = "Procedural posture not detected"

    if "motion to dismiss" in lower:
        posture = "motion to dismiss"
    elif "summary judgment" in lower:
        posture = "summary judgment motion"
    elif "yellowstone injunction" in lower:
        posture = "Yellowstone injunction motion"
    elif "appeal" in lower:
        posture = "appeal"

    disposition = ""

    if "affirmed" in lower:
        disposition = "affirmed"
    elif "reversed" in lower:
        disposition = "reversed"
    elif "granted" in lower and "denied" in lower:
        disposition = "granted in part / denied in part"
    elif "granted" in lower:
        disposition = "granted"
    elif "denied" in lower:
        disposition = "denied"

    return {
        "posture": clean_text(posture),
        "disposition": clean_text(disposition),
    }


# ============================================================
# Classification
# ============================================================

def score_authority(sentence):
    lower = safe_lower(sentence)

    score = 0

    if " v. " in lower:
        score += 20

    if "held that" in lower:
        score += 20

    if "held" in lower:
        score += 12

    if "summary judgment" in lower:
        score += 12

    if "motion to dismiss" in lower:
        score += 12

    if "prima facie" in lower:
        score += 10

    for term in AUTHORITY_TERMS:
        if term in lower:
            score += 3

    return score


def classify_used_for(sentence):
    lower = safe_lower(sentence)

    if "motion to dismiss" in lower:
        return "motion to dismiss"

    if "summary judgment" in lower:
        return "summary judgment"

    if "injunction" in lower:
        return "injunctive relief"

    return "general authority"


def classify_authority_type(auth):
    status = safe_lower(auth.get("verification_status", ""))

    if "synthesized" in status:
        return "fallback synthesized authority"

    return "embedded cited authority"


def rank_strength(auth):
    score = auth.get("relevance_score", 0)

    if score >= 55:
        return "strong"

    if score >= 30:
        return "moderate"

    return "supporting"


# ============================================================
# Extraction
# ============================================================

def clean_case_name(case_name):
    case_name = clean_text(case_name)

    match = re.search(
        r'([A-Z][A-Za-z0-9&.,\'"\-\s]+?\s+v\.?\s+[A-Z][A-Za-z0-9&.,\'"\-\s]+)$',
        case_name
    )

    if match:
        return clean_text(match.group(1))

    return case_name


def build_full_citation(case_name, citation, court_year):
    parts = []

    if case_name:
        parts.append(case_name)

    if citation:
        parts.append(citation)

    full = ", ".join(parts)

    if court_year:
        full = f"{full} ({court_year})"

    return clean_text(full)


def extract_authorities(text):
    normalized = normalize_text_for_parser(text)

    authorities = []

    for match in FULL_CITATION_REGEX.finditer(normalized):
        case_name = clean_case_name(match.group(1))
        citation = clean_text(match.group(2))
        court_year = clean_text(match.group(3))

        context = clean_text(match.group(0))

        relevance_score = score_authority(context)

        authority = {
            "case_name": case_name,
            "citation": citation,
            "court_year": court_year,
            "full_citation": build_full_citation(case_name, citation, court_year),
            "rule": "",
            "holding": "",
            "reasoning": "",
            "context": context,
            "side": detect_side(context),
            "used_for": classify_used_for(context),
            "relevance_score": relevance_score,
            "verification_status": "parser extracted embedded citation; human verification required",
            "authority_rank": "unranked",
            "authority_type": "embedded cited authority",
            "strength": "unranked",
            "procedural_posture": "",
            "disposition": "",
        }

        authorities.append(authority)

    return authorities


# ============================================================
# Fallback synthesis
# ============================================================

def synthesize_selected_case_authority(data, combined_text):
    if not isinstance(data, dict):
        return None

    case_name = resolve_case_name(data)

    citation = get_record_value(data, "citation")
    court = get_record_value(data, "court")
    date = get_record_value(data, "date")

    rule = get_record_value(data, "rule")
    holding = get_record_value(data, "holding")
    reasoning = get_record_value(data, "reasoning")

    procedural = extract_procedural_posture(combined_text, data)

    court_year = " ".join(
        [
            clean_text(court),
            clean_text(date),
        ]
    ).strip()

    score = 35

    if rule:
        score += 15

    if holding:
        score += 15

    if reasoning:
        score += 10

    return {
        "case_name": case_name,
        "citation": citation,
        "court_year": court_year,
        "full_citation": build_full_citation(case_name, citation, court_year),
        "rule": rule,
        "holding": holding,
        "reasoning": reasoning,
        "context": clean_text(" ".join([rule, holding, reasoning])),
        "side": "neutral",
        "used_for": classify_used_for(" ".join([rule, holding, reasoning])),
        "relevance_score": score,
        "verification_status": "synthesized from selected case record; verify before filing",
        "authority_rank": "unranked",
        "authority_type": "selected case fallback authority",
        "strength": "unranked",
        "procedural_posture": procedural.get("posture", ""),
        "disposition": procedural.get("disposition", ""),
    }


def maybe_add_fallback_authority(authorities, data, combined_text):
    fallback = synthesize_selected_case_authority(data, combined_text)

    if not fallback:
        return authorities

    authorities.append(fallback)

    return authorities


# ============================================================
# Ranking
# ============================================================

def sort_authorities(authorities):
    return sorted(
        authorities,
        key=lambda x: (
            x.get("relevance_score", 0),
            x.get("case_name", ""),
        ),
        reverse=True,
    )


def rank_authorities(authorities):
    ranked = []

    for idx, auth in enumerate(authorities, start=1):
        item = dict(auth)

        item["authority_rank"] = f"Rank #{idx}"
        item["authority_type"] = classify_authority_type(item)
        item["strength"] = rank_strength(item)

        ranked.append(item)

    return ranked


# ============================================================
# Engine
# ============================================================

def build_authority_engine(text, data=None):
    text = normalize_text_for_parser(text)
    data = data or {}

    authorities = extract_authorities(text)

    authorities = maybe_add_fallback_authority(
        authorities,
        data,
        text,
    )

    authorities = sort_authorities(authorities)
    authorities = rank_authorities(authorities)

    procedural = extract_procedural_posture(text, data)

    return {
        "version": "Authority Engine v3.4.3",
        "verification_warning": "Draft research aid only. Verify all authority before use.",
        "jurisdiction": detect_jurisdiction(text, data),
        "procedural_posture": procedural,
        "issues_detected": detect_issues(text),
        "authorities": authorities,
        "authority_count": len(authorities),
    }


# ============================================================
# Public API
# ============================================================

def get_matter(documents=None):
    primary_record = get_primary_record(documents)

    combined_text = build_combined_text(documents)
    combined_text = normalize_text_for_parser(combined_text)

    print("TEXT SAMPLE:", combined_text[:500])

    real_authority_layer = build_authority_engine(
        combined_text,
        primary_record,
    )

    authorities = real_authority_layer.get("authorities", [])

    print("AUTHORITIES RETURNED:", len(authorities))

    return {
        "authorities": authorities,
        "real_authority_layer": real_authority_layer,
        "authority_count": len(authorities),
        "document_count": 1 if combined_text else 0,
        "preview": combined_text[:1000],
    }