from flask import Flask, request, render_template, abort, send_from_directory
import json
import math
import os
import csv
import re
from types import SimpleNamespace

from matter_builder import get_matter

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PREFERRED_JSON_PATHS = [
    os.path.join(BASE_DIR, "data", "output_v4.json"),
    os.path.join(BASE_DIR, "data", "output_v3.json"),
    os.path.join(BASE_DIR, "output_v1.json"),
]

PREFERRED_CSV_PATHS = [
    os.path.join(BASE_DIR, "data", "output_v3.csv"),
]

PER_PAGE = 10


# =========================
# HELPERS
# =========================

def clean_text(value):
    return " ".join(str(value or "").split()).strip()


def normalize_for_search(value):
    value = str(value or "").lower()
    value = value.replace("§", " section ")
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def build_pager(page, per_page, total_count):
    total_pages = max(1, math.ceil(total_count / per_page)) if total_count else 1

    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages

    return SimpleNamespace(
        page=page,
        per_page=per_page,
        total=total_count,
        total_pages=total_pages,
        has_prev=page > 1,
        has_next=page < total_pages,
        prev_num=page - 1 if page > 1 else None,
        next_num=page + 1 if page < total_pages else None,
    )


def flatten_citation(case):
    direct = clean_text(
        case.get("citation")
        or case.get("cite")
        or case.get("reporter_citation")
        or case.get("slip_op")
        or case.get("slip_op_citation")
    )
    if direct:
        return direct

    citations = case.get("citations")
    if isinstance(citations, dict):
        slip_ops = citations.get("slip_op") or []
        reporters = citations.get("reporters") or []

        if isinstance(slip_ops, list) and slip_ops:
            first_slip = clean_text(slip_ops[0])
            if first_slip:
                return first_slip

        if isinstance(reporters, list) and reporters:
            first_reporter = clean_text(reporters[0])
            if first_reporter:
                return first_reporter

    return ""


def looks_like_bad_title(line):
    if not line:
        return True

    low = line.lower().strip()

    junk_phrases = [
        "appellate division",
        "first judicial department",
        "motion no",
        "index no",
        "case no",
        "order,",
        "entered ",
        "entered on",
        "unanimously",
        "appealed from",
        "to the extent appealed",
        "plaintiff-appellant",
        "defendant-appellant",
        "petitioner-respondent",
        "respondent-appellant",
        "plaintiff-respondent",
        "defendant-respondent",
    ]
    if any(p in low for p in junk_phrases):
        return True

    fragment_starts = [
        "to dismiss",
        "against him",
        "against her",
        "against it",
        "motion as sought",
        "motion pursuant",
        "which granted",
        "which denied",
        "which, to the extent",
        "s motion",
        "cross motion",
    ]
    if any(low.startswith(p) for p in fragment_starts):
        return True

    if len(line) < 12:
        return True
    if len(line) > 140:
        return True

    words = line.split()
    if len(words) < 3:
        return True

    lowercase_words = sum(1 for w in words if w[:1].islower())
    if lowercase_words >= max(2, len(words) // 2):
        return True

    return False


def extract_caption_from_text(text):
    if not text:
        return ""

    raw = str(text)
    lines = [clean_text(line) for line in raw.splitlines() if clean_text(line)]
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
            candidate = clean_text(m.group(1))
            if not looks_like_bad_title(candidate):
                return candidate

    for line in lines[:12]:
        if looks_like_bad_title(line):
            continue
        if re.search(r"[A-Za-z]", line):
            return line

    return ""


def build_safe_title(case):
    direct_title = clean_text(case.get("title"))
    if direct_title and direct_title.lower() not in {"untitled case", "case record"}:
        if not looks_like_bad_title(direct_title):
            return direct_title

    caption = extract_caption_from_text(case.get("text", ""))
    if caption:
        return caption

    case_number = clean_text(case.get("case_number") or case.get("docket"))
    court = clean_text(case.get("court"))
    date = clean_text(case.get("date"))

    if case_number and court and date:
        return f"Case {case_number} ({court}, {date})"
    if case_number and court:
        return f"Case {case_number} ({court})"
    if case_number:
        return f"Case {case_number}"

    return "Case Record"


def detect_record_type(case):
    file_name = clean_text(case.get("file")).lower()
    text = normalize_for_search(" ".join([
        case.get("title", ""),
        case.get("summary", ""),
        case.get("snippet", ""),
        case.get("text", "")[:1200],
    ]))

    if "__motion_order__" in file_name:
        return "motion_order"

    if "motion no" in text and "case no" in text and "index no" in text:
        return "motion_order"

    return "decision"


def court_rank(court_name):
    court = clean_text(court_name)
    if court == "Appellate Division, First Department":
        return 100
    if court == "Court of Appeals":
        return 95
    if court == "Appellate Division, Second Department":
        return 90
    if court == "Appellate Division, Third Department":
        return 80
    if court == "Appellate Division, Fourth Department":
        return 80
    if court == "Appellate Division":
        return 70
    if court == "Supreme Court":
        return 50
    if court == "Civil Court":
        return 35
    return 20


def format_case_text(text):
    raw = str(text or "")
    if not raw.strip():
        return ""

    txt = raw.replace("\r\n", "\n").replace("\r", "\n")
    txt = txt.replace("\u00a0", " ")

    txt = re.sub(r"([A-Za-z])-\s+([A-Za-z])", r"\1-\2", txt)
    txt = re.sub(r"\s+", " ", txt).strip()

    start_markers = [
        r"\bOrder, Supreme Court,",
        r"\bJudgment, Supreme Court,",
        r"\bOrder and judgment, Supreme Court,",
        r"\bDecision and order, Supreme Court,",
        r"\bOpinion of the Court\b",
        r"\bPlaintiff appeals from\b",
        r"\bDefendant appeals from\b",
        r"\bPetitioner appeals from\b",
    ]
    for marker in start_markers:
        m = re.search(marker, txt)
        if m:
            txt = txt[m.start():]
            break

    txt = re.sub(r"\s+\d+\s+(?=[A-Z])", " ", txt)

    txt = re.sub(
        r"\s*THIS CONSTITUTES THE DECISION AND ORDER OF THE SUPREME COURT, APPELLATE DIVISION, FIRST DEPARTMENT\.\s*ENTERED:\s*[A-Za-z]+\s+\d{1,2},\s+\d{4}\s*\d*\s*$",
        "",
        txt,
        flags=re.IGNORECASE,
    )

    paragraph_markers = [
        "However,",
        "In opposition,",
        "In light of",
        "On the merits,",
        "On appeal,",
        "Here,",
        "Moreover,",
        "By contrast,",
        "Separately,",
        "Finally,",
        "Supreme Court correctly",
        "Supreme Court should have",
        "Plaintiff failed",
        "Plaintiff established",
        "Defendant failed",
        "Defendants failed",
        "Defendant established",
        "Defendants established",
        "We do not reach",
        "We reject",
        "We agree",
        "We have considered",
    ]

    for marker in paragraph_markers:
        txt = txt.replace(" " + marker, "\n\n" + marker)

    txt = re.sub(
        r"(\bwithout costs\.)\s+(?=[A-Z])",
        r"\1\n\n",
        txt,
        count=1,
    )

    txt = re.sub(r"\.\s+(?=Although\b)", ".\n\n", txt)
    txt = re.sub(r"\.\s+(?=Because\b)", ".\n\n", txt)
    txt = re.sub(r"\.\s+(?=Given\b)", ".\n\n", txt)

    txt = re.sub(r" *\n *", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()

    return txt


# =========================
# RULE EXTRACTION
# =========================

def split_into_sentences(text):
    raw = clean_text(text)
    if not raw:
        return []

    protected = raw
    protected = re.sub(r"\bNo\.\s", "No<dot> ", protected)
    protected = re.sub(r"\bNos\.\s", "Nos<dot> ", protected)
    protected = re.sub(r"\bv\.\s", "v<dot> ", protected)
    protected = re.sub(r"\bDept\.\s", "Dept<dot> ", protected)
    protected = re.sub(r"\bInc\.\s", "Inc<dot> ", protected)
    protected = re.sub(r"\bCo\.\s", "Co<dot> ", protected)
    protected = re.sub(r"\bCorp\.\s", "Corp<dot> ", protected)
    protected = re.sub(r"\bLLC\.\s", "LLC<dot> ", protected)
    protected = re.sub(r"\bJ\.\s", "J<dot> ", protected)

    parts = re.split(r"(?<=[\.\?!])\s+(?=[A-Z])", protected)

    return [
        clean_text(p.replace("<dot>", "."))
        for p in parts
        if clean_text(p)
    ]


def is_bad_rule_sentence(sentence):
    low = clean_text(sentence).lower()
    if not low:
        return True

    if len(low) < 45:
        return True

    bad_markers = [
        " contend",
        " contends",
        " argue",
        " argues",
        " assert",
        " asserts",
        " maintain",
        " maintains",
        " unpersuasive",
        " persuasive",
        " according to ",
        " plaintiff argues",
        " plaintiff contends",
        " plaintiff asserts",
        " plaintiff maintains",
        " defendants argue",
        " defendants contend",
        " defendants assert",
        " defendants maintain",
        " defendant argues",
        " defendant contends",
        " defendant asserts",
        " defendant maintains",
        " appellant argues",
        " appellant contends",
        " respondent argues",
        " respondent contends",
    ]
    if any(marker in f" {low} " for marker in bad_markers):
        return True

    if low.startswith((
        "plaintiff ",
        "plaintiffs ",
        "defendant ",
        "defendants ",
        "appellant ",
        "respondent ",
        "petitioner ",
    )):
        return True

    return False


def has_legal_reasoning_signal(sentence):
    low = clean_text(sentence).lower()
    if not low:
        return False

    legal_signals = [
        "material",
        "immaterial",
        "fundamental purpose",
        "agreement",
        "contract",
        "breach",
        "constructive trust",
        "unjust enrichment",
        "confidential relationship",
        "fiduciary relationship",
        "promise",
        "transfer in reliance",
        "triable issue",
        "entitled to judgment as a matter of law",
        "summary judgment",
        "dismiss",
        "duplicative",
        "governs the dispute",
        "elements",
        "requires",
        "must show",
        "failed to establish",
        "failed to raise",
        "sufficiently alleged",
        "adequately alleged",
        "cognizable",
        "prima facie",
        "no evidence",
        "absent evidence",
        "therefore",
        "because",
        "since",
        "where",
        "inasmuch as",
        "excused",
        "performance under the contract",
        "clear expressions of intent",
        "tenancy by the entirety",
        "modified by agreement",
        "full force and effect",
        "noncompliance",
        "obligations",
    ]
    return any(signal in low for signal in legal_signals)


def party_name_density(sentence):
    s = clean_text(sentence)
    if not s:
        return 0

    tokens = re.findall(r"\b[A-Z][a-z]+\b", s)
    ignore = {
        "Supreme", "Court", "Appellate", "Division", "First", "Second", "Third", "Fourth",
        "Department", "Labor", "Law", "Order", "Judgment", "Decision", "Contract",
        "Agreement", "Plaintiff", "Defendant", "Defendants", "Petitioner", "Respondent",
    }
    names = [tok for tok in tokens if tok not in ignore]
    return len(names)


def is_fact_specific_key_point(sentence):
    low = clean_text(sentence).lower()
    if not low:
        return False

    fact_markers = [
        "paragraph ",
        "section ",
        "apartment",
        "property",
        "premises",
        "unit ",
        "decedent",
        "predeceased",
        "will ",
        "execute a will",
        "omitted any provisions",
        "title vested",
        "share of the apartment",
        "purchased the apartment",
        "purchased the property",
        "resided at",
        "lived at",
    ]
    hits = sum(1 for marker in fact_markers if marker in low)
    return hits >= 2


def is_bad_key_point_sentence(sentence, holding=""):
    s = clean_text(sentence)
    low = s.lower()
    holding_low = clean_text(holding).lower()

    if not s or len(s) < 55:
        return True

    if is_bad_rule_sentence(s):
        return True

    if any(bad in low for bad in [
        "see generally",
        "see e.g.",
        "see, e.g.",
        "cf.",
        " id. ",
        "(id.",
        " id.",
        "supra",
    ]):
        return True

    if '"' in s or "“" in s or "”" in s:
        return True

    if re.search(r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},\s+\d{4}\b", low):
        return True

    if re.search(r"\bat\s+\d{1,4}\s*-\s*\d{1,4}\b", low):
        return True

    if any(phrase in low for phrase in [
        "now deceased",
        "were married",
        "was married",
        "was born",
        "resided at",
        "lived at",
        "purchased the property",
        "purchased the apartment",
        "title vested",
        "vested entirely",
        "bequeathed her entire estate",
        "bequeathed his entire estate",
        "share of the apartment",
    ]):
        return True

    if low.startswith(("harold ", "joanne ", "harold and joanne ", "joanne and harold ")):
        return True

    if low.startswith(("order, supreme court", "judgment, supreme court", "decision and order,")):
        return True

    if holding_low and clean_text(s).lower() in holding_low:
        return True

    if "this statute" in low or "that statute" in low:
        return True

    if "court of appeals has held" in low:
        return True

    if re.search(r"\b[A-Z][a-z]+ v [A-Z][a-z]+\b", s):
        return True

    if party_name_density(s) >= 2 and not has_legal_reasoning_signal(s):
        return True

    if is_fact_specific_key_point(s) and "material breach" not in low and "excused" not in low:
        return True

    if not has_legal_reasoning_signal(s):
        return True

    return False


def sentence_score(sentence, holding=""):
    s = clean_text(sentence)
    low = s.lower()
    holding_low = normalize_for_search(holding)
    sent_norm = normalize_for_search(s)
    score = 0

    if len(s) < 55:
        score -= 8
    elif len(s) > 420:
        score -= 6
    else:
        score += 4

    strong_phrases = [
        "the court properly",
        "supreme court correctly",
        "supreme court should have",
        "plaintiff established",
        "defendant established",
        "defendants established",
        "plaintiff failed",
        "defendant failed",
        "defendants failed",
        "failed to raise a triable issue",
        "raised a triable issue",
        "entitled to judgment as a matter of law",
        "as a matter of law",
        "triable issues of fact",
        "triable issue of fact",
    ]
    for phrase in strong_phrases:
        if phrase in low:
            score += 8

    reasoning_phrases = [
        "because",
        "where",
        "since",
        "given that",
        "in light of",
        "based on",
        "inasmuch as",
        "therefore",
        "thus",
        "absent evidence",
        "no evidence",
    ]
    for phrase in reasoning_phrases:
        if phrase in low:
            score += 2

    if low.startswith(("however,", "moreover,", "accordingly,", "finally,", "by contrast,", "separately,")):
        score -= 4

    if sent_norm and holding_low and sent_norm == holding_low:
        score -= 10

    if sent_norm and holding_low and sent_norm in holding_low:
        score -= 6

    if any(bad in low for bad in [
        "we have considered",
        "we do not reach",
        "remaining contentions",
        "unpreserved",
    ]):
        score -= 6

    if is_bad_rule_sentence(s):
        score -= 100

    return score


def key_point_score(sentence, holding=""):
    s = clean_text(sentence)
    low = s.lower()
    score = 0

    if is_bad_key_point_sentence(s, holding):
        return -100

    if 80 <= len(s) <= 260:
        score += 8
    elif len(s) > 260:
        score -= 4

    high_value_phrases = [
        "fundamental purpose",
        "material",
        "immaterial",
        "agreement",
        "breach",
        "constructive trust",
        "duplicative",
        "requires",
        "must show",
        "failed to establish",
        "failed to raise",
        "triable issue",
        "entitled to judgment as a matter of law",
        "summary judgment",
        "dismiss",
        "because",
        "since",
        "therefore",
        "where",
        "inasmuch as",
        "excused",
        "performance under the contract",
    ]
    for phrase in high_value_phrases:
        if phrase in low:
            score += 4

    if low.startswith(("however,", "moreover,", "accordingly,", "finally,", "by contrast,", "separately,")):
        score -= 2

    if party_name_density(s) >= 2:
        score -= 8

    if is_fact_specific_key_point(s):
        score -= 8

    if re.search(r"\b[A-Z][a-z]+ and [A-Z][a-z]+\b", s) and not has_legal_reasoning_signal(s):
        score -= 8

    return score


def sentences_too_similar(a, b):
    a_norm = normalize_for_search(a)
    b_norm = normalize_for_search(b)
    if not a_norm or not b_norm:
        return False

    a_tokens = set(a_norm.split())
    b_tokens = set(b_norm.split())
    if not a_tokens or not b_tokens:
        return False

    overlap = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))
    return overlap >= 0.68


def clean_sentence_for_rule(sentence):
    s = clean_text(sentence)
    if not s:
        return ""

    s = re.sub(r"^Supreme Court correctly\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^Supreme Court should have\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^The court properly\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^The court correctly\s+", "", s, flags=re.IGNORECASE)

    s = re.sub(r"\s+as against\s+[A-Z][A-Za-z0-9&.,()' \-]+", "", s)
    s = re.sub(r"\([A-Z][A-Z0-9&.,' \-]{1,30}\)", "", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.;")

    return s


def clean_sentence_for_key_point(sentence):
    s = clean_text(sentence)
    if not s:
        return ""

    s = re.sub(r"\s*\(see[^)]*\)", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*\([^)]*\bid\.[^)]*\)", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*\[[^\]]*\]", "", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.;")

    if len(s) > 240:
        s = s[:240].rsplit(" ", 1)[0].rstrip(" ,.;")

    if s and not s.endswith("."):
        s += "."

    return s


def rewrite_key_point_sentence(sentence, holding=""):
    s = clean_text(sentence)
    low = s.lower()
    holding_low = clean_text(holding).lower()

    if not s:
        return ""

    if (
        "material" in low
        and "excused" in low
        and "performance under the contract" in low
    ):
        return "A material breach excuses the other party's performance under the contract."

    if (
        ("paragraph 7" in low or "execute a will" in low or "full force and effect" in low)
        and ("omitted any provisions" in low or "predeceased" in low or "will" in low)
        and "contract" in holding_low
    ):
        return "Contractual noncompliance with obligations necessary to give full force and effect to the agreement supported a finding of material breach."

    s = re.sub(r"^We find that\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^We agree that\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^Supreme Court correctly\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^The court properly\s+", "", s, flags=re.IGNORECASE)

    s = re.sub(r"\bHarold's\b", "a party's", s)
    s = re.sub(r"\bJoanne's\b", "the other party's", s)
    s = re.sub(r"\bHarold\b", "one party", s)
    s = re.sub(r"\bJoanne\b", "the other party", s)

    s = re.sub(r"\bparagraph\s+\d+\b", "the agreement", s, flags=re.IGNORECASE)

    s = re.sub(r"\s+", " ", s).strip(" ,.;")
    if s and not s.endswith("."):
        s += "."

    return s


def normalize_rule_style(text):
    s = clean_text(text)
    if not s:
        return ""

    s = re.sub(r"\s+", " ", s).strip(" ,.;")
    if not s:
        return ""

    if s[0].islower():
        s = s[0].upper() + s[1:]

    if not s.endswith("."):
        s += "."

    if len(s) > 160:
        s = s[:160].rsplit(" ", 1)[0].rstrip(" ,.;") + "."

    return s


def rule_template_labor_200_control(sentence):
    low = sentence.lower()

    has_labor_200 = ("labor § 200" in low) or ("labor law § 200" in low)
    has_negligence = "common-law negligence" in low or "common law negligence" in low
    has_control = "directed or controlled" in low
    has_injury_work = "injury-producing work" in low
    has_no_evidence = "no evidence" in low or "absent evidence" in low or "triable issue" in low

    if has_labor_200 and has_negligence and has_control and has_injury_work and has_no_evidence:
        return normalize_rule_style(
            "Labor Law § 200 and common-law negligence claims should be dismissed absent evidence raising a triable issue that defendant directed the injury-producing work."
        )

    if has_labor_200 and has_control and has_no_evidence:
        return normalize_rule_style(
            "A Labor Law § 200 claim should be dismissed absent evidence raising a triable issue that defendant directed or controlled the injury-producing work."
        )

    return ""


def rule_template_summary_judgment(sentence):
    low = sentence.lower()

    if "entitled to judgment as a matter of law" in low and "failed to raise a triable issue of fact" in low:
        return normalize_rule_style(
            "Summary judgment is warranted where the movant establishes entitlement to judgment as a matter of law and the opposing party fails to raise a triable issue of fact."
        )

    if "summary judgment" in low and "triable issue of fact" in low and "should be denied" in low:
        return normalize_rule_style(
            "Summary judgment should be denied where the opposing party raises a triable issue of fact."
        )

    if "summary judgment" in low and "triable issue of fact" in low and "should be granted" in low:
        return normalize_rule_style(
            "Summary judgment should be granted where the movant establishes entitlement to judgment as a matter of law and the opposing party fails to raise a triable issue of fact."
        )

    return ""


def rule_template_dismissal(sentence):
    low = sentence.lower()

    if "correctly declined to dismiss" in low and "labor law § 240" in low and "241" in low:
        return normalize_rule_style(
            "Labor Law § 240(1) and § 241(6) claims should not be dismissed where the record presents triable issues as to defendant's statutory responsibility."
        )

    if "should have dismissed" in low and "no evidence" in low:
        cleaned = clean_sentence_for_rule(sentence)
        cleaned = re.sub(r"^dismissed\s+", "", cleaned, flags=re.IGNORECASE)
        return normalize_rule_style(cleaned)

    return ""


def rule_template_contract_constructive_trust(holding):
    low = holding.lower()

    if (
        "breach of contract" in low
        and "constructive trust" in low
        and "summary judgment" in low
    ):
        return normalize_rule_style(
            "A constructive trust claim is unavailable where an enforceable contract governs the dispute and the equitable claim merely duplicates the contract claim."
        )

    return ""


def rule_template_contract(holding):
    low = holding.lower()

    if "breach of contract" in low:
        return normalize_rule_style(
            "A breach of contract claim requires a contract, performance, breach, and resulting damages."
        )

    return ""


def rule_template_holding_summary_judgment(holding):
    low = holding.lower()

    if "failed to raise a triable issue of fact" in low:
        return normalize_rule_style(
            "Summary judgment is warranted where the movant establishes entitlement to judgment as a matter of law and the opposing party fails to raise a triable issue of fact."
        )

    if "triable issue of fact" in low or "triable issues of fact" in low:
        return normalize_rule_style(
            "Summary judgment must be denied where the record presents a triable issue of fact."
        )

    if "entitled to judgment as a matter of law" in low:
        return normalize_rule_style(
            "A party is entitled to summary judgment only upon a prima facie showing of entitlement to judgment as a matter of law."
        )

    return ""


def fallback_rule(sentence):
    s = clean_sentence_for_rule(sentence)
    if not s:
        return ""

    s = s.replace("Labor §", "Labor Law §")

    replacements = [
        (r"\bplaintiff’s\b", "a plaintiff’s"),
        (r"\bplaintiff's\b", "a plaintiff’s"),
        (r"\bdefendant’s\b", "a defendant’s"),
        (r"\bdefendant's\b", "a defendant’s"),
        (r"\bdefendants’\b", "defendants’"),
        (r"\bdefendants'\b", "defendants’"),
    ]
    for pattern, repl in replacements:
        s = re.sub(pattern, repl, s, flags=re.IGNORECASE)

    if "as there is no evidence" in s:
        s = s.replace("as there is no evidence", "absent evidence")
    elif "because there is no evidence" in s:
        s = s.replace("because there is no evidence", "absent evidence")
    elif "because" in s:
        s = s.replace("because", "where", 1)

    return normalize_rule_style(s)


def fallback_rule_from_holding(holding):
    low = holding.lower()

    if "constructive trust" in low:
        return normalize_rule_style(
            "A constructive trust claim requires a confidential or fiduciary relationship, a promise, a transfer in reliance, and unjust enrichment."
        )

    if "breach of contract" in low:
        return normalize_rule_style(
            "A breach of contract claim requires a contract, performance, breach, and resulting damages."
        )

    if "motion to dismiss" in low or "dismissing" in low:
        return normalize_rule_style(
            "A claim should be dismissed where the pleading fails to allege facts sufficient to satisfy the governing elements."
        )

    if "summary judgment" in low:
        return normalize_rule_style(
            "Summary judgment is appropriate only where the movant establishes entitlement to judgment as a matter of law."
        )

    return ""


def generate_rule(holding, best_sentence="", backup_sentence=""):
    for fn in [
        rule_template_contract_constructive_trust,
        rule_template_contract,
        rule_template_holding_summary_judgment,
    ]:
        rule = fn(holding)
        if rule:
            return rule

    for candidate in [best_sentence, backup_sentence]:
        if not candidate or is_bad_rule_sentence(candidate):
            continue

        for fn in [
            rule_template_labor_200_control,
            rule_template_summary_judgment,
            rule_template_dismissal,
        ]:
            rule = fn(candidate)
            if rule:
                return rule

    holding_fallback = fallback_rule_from_holding(holding)
    if holding_fallback:
        return holding_fallback

    for candidate in [best_sentence, backup_sentence]:
        if not candidate or is_bad_rule_sentence(candidate):
            continue
        rule = fallback_rule(candidate)
        if rule:
            return rule

    return ""


def extract_holding_and_key_points(formatted_text):
    text = str(formatted_text or "").strip()
    if not text:
        return "", [], ""

    paragraphs = [clean_text(p) for p in text.split("\n\n") if clean_text(p)]
    if not paragraphs:
        return "", [], ""

    holding = paragraphs[0]
    if len(holding) > 700:
        holding = holding[:700].rsplit(" ", 1)[0] + "..."

    sentences = []
    key_point_candidates = []

    for para in paragraphs[1:7]:
        para_sentences = split_into_sentences(para)
        for sent in para_sentences:
            if len(sent) >= 45 and not is_bad_rule_sentence(sent):
                sentences.append(sent)

            if len(sent) >= 55 and not is_bad_key_point_sentence(sent, holding):
                cleaned_key_point = clean_sentence_for_key_point(sent)
                rewritten_key_point = rewrite_key_point_sentence(cleaned_key_point, holding)
                if rewritten_key_point and not is_bad_key_point_sentence(rewritten_key_point, holding):
                    key_point_candidates.append(rewritten_key_point)

    rule = generate_rule(holding, "", "")
    if sentences:
        ranked = sorted(
            [(sentence_score(s, holding), s) for s in sentences],
            key=lambda x: x[0],
            reverse=True,
        )

        best_sentences = []
        for _, s in ranked:
            if any(sentences_too_similar(s, existing) for existing in best_sentences):
                continue
            best_sentences.append(s)
            if len(best_sentences) >= 2:
                break

        rule = generate_rule(
            holding,
            best_sentences[0] if best_sentences else "",
            best_sentences[1] if len(best_sentences) > 1 else "",
        )
    else:
        best_sentences = []

    ranked_key_points = sorted(
        [(key_point_score(s, holding), s) for s in key_point_candidates],
        key=lambda x: x[0],
        reverse=True,
    )

    final_key_points = []
    for score, s in ranked_key_points:
        if score < 0:
            continue
        if any(sentences_too_similar(s, existing) for existing in final_key_points):
            continue
        final_key_points.append(s)
        if len(final_key_points) >= 2:
            break

    return holding, final_key_points, rule


# =========================
# LOADERS
# =========================

def load_json_cases(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ["cases", "results", "data", "records"]:
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def load_csv_cases(path):
    rows = []
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(dict(row))
    return rows


def load_cases():
    for path in PREFERRED_JSON_PATHS:
        if os.path.exists(path):
            rows = load_json_cases(path)
            if rows:
                print(f"✅ Loaded {len(rows)} cases from {path}")
                return [normalize_case(r) for r in rows]

    for path in PREFERRED_CSV_PATHS:
        if os.path.exists(path):
            rows = load_csv_cases(path)
            if rows:
                print(f"⚠️ Loaded CSV fallback {len(rows)} rows from {path}")
                return [normalize_case(r) for r in rows]

    print("⚠️ No data found")
    return []


# =========================
# PHRASE ALIASES / DETECTION
# =========================

PHRASE_ALIASES = {
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
        "written agreement",
        "oral agreement",
        "contractual breach",
    ],
    "contract": [
        "breach of contract",
        "contract",
        "agreement",
        "material breach",
        "written agreement",
        "oral agreement",
    ],
    "summary judgment": [
        "summary judgment",
        "partial summary judgment",
    ],
    "motion to dismiss": [
        "motion to dismiss",
        "dismissal",
        "dismiss",
    ],
    "negligence": [
        "negligence",
        "negligent",
        "duty of care",
        "breach of duty",
        "proximate cause",
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
}

STRICT_PHRASE_QUERIES = set(PHRASE_ALIASES.keys())

OUTCOME_ALIASES = {
    "affirmed": ["affirmed", "unanimously affirmed", "affirm"],
    "reversed": ["reversed", "reverse"],
    "granted": ["granted", "grant"],
    "denied": ["denied", "deny"],
    "dismissed": ["dismissed", "dismiss"],
}


def detect_motion(case):
    text = normalize_for_search(" ".join([
        case.get("title", ""),
        case.get("summary", ""),
        case.get("snippet", ""),
        case.get("text", ""),
    ]))

    if not text:
        return ""

    if "partial summary judgment" in text:
        return "partial summary judgment"

    if "summary judgment" in text:
        return "summary judgment"

    dismiss_motion_patterns = [
        "motion to dismiss",
        "motions to dismiss",
        "cross motion to dismiss",
        "cross-motion to dismiss",
        "dismiss the complaint",
        "dismissing the complaint",
        "seeking dismissal",
        "for dismissal of the complaint",
    ]
    if any(p in text for p in dismiss_motion_patterns):
        return "motion to dismiss"

    return ""


def detect_primary_cause(case):
    text = normalize_for_search(" ".join([
        case.get("title", ""),
        case.get("summary", ""),
        case.get("snippet", ""),
        case.get("text", ""),
    ]))

    if any(alias in text for alias in PHRASE_ALIASES["labor law"]):
        return "labor law"
    if any(alias in text for alias in PHRASE_ALIASES["breach of contract"]):
        return "breach of contract"
    if any(alias in text for alias in PHRASE_ALIASES["fraud"]):
        return "fraud"
    if any(alias in text for alias in PHRASE_ALIASES["conversion"]):
        return "conversion"
    if any(alias in text for alias in PHRASE_ALIASES["negligence"]):
        return "negligence"

    return ""


def detect_query_outcome(query):
    q = normalize_for_search(query)
    for outcome, aliases in OUTCOME_ALIASES.items():
        if any(alias in q for alias in aliases):
            return outcome
    return ""


def detect_query_motion(query):
    q = normalize_for_search(query)
    if "partial summary judgment" in q:
        return "partial summary judgment"
    if "summary judgment" in q:
        return "summary judgment"
    if "motion to dismiss" in q:
        return "motion to dismiss"
    return ""


def detect_query_cause(query):
    q = normalize_for_search(query)
    for phrase in sorted(STRICT_PHRASE_QUERIES, key=len, reverse=True):
        aliases = PHRASE_ALIASES.get(phrase, [])
        if phrase == q:
            return phrase
        if any(alias in q for alias in aliases):
            return phrase
    return ""


# =========================
# NORMALIZE
# =========================

def build_case_id(case):
    return clean_text(case.get("case_number") or case.get("file") or case.get("title"))


def normalize_case(case):
    case = dict(case)

    case["court"] = clean_text(case.get("court"))
    case["summary"] = clean_text(case.get("summary"))
    case["snippet"] = clean_text(case.get("snippet"))
    case["outcome"] = clean_text(case.get("outcome")).lower()
    case["citation"] = flatten_citation(case)
    case["docket"] = clean_text(case.get("case_number") or case.get("docket"))
    case["case_number"] = clean_text(case.get("case_number"))
    case["date"] = clean_text(case.get("date"))
    case["text"] = clean_text(case.get("text"))
    case["formatted_text"] = format_case_text(case.get("text"))

    holding, key_points, rule = extract_holding_and_key_points(case["formatted_text"])
    case["holding"] = holding
    case["key_points"] = key_points
    case["rule"] = rule

    case["file"] = clean_text(case.get("file"))
    case["record_type"] = detect_record_type(case)
    case["motion"] = detect_motion(case)
    case["primary_cause"] = detect_primary_cause(case)
    case["title"] = build_safe_title(case)
    case["court_rank"] = court_rank(case["court"])
    case["case_id"] = build_case_id(case)
    case["trust_signals"] = []
    case["similarity_signals"] = []

    return case


# =========================
# SEARCH
# =========================

def text_for_search(case):
    return normalize_for_search(" ".join([
        case.get("title", ""),
        case.get("summary", ""),
        case.get("snippet", ""),
        case.get("text", ""),
        case.get("court", ""),
        case.get("citation", ""),
        case.get("docket", ""),
        case.get("date", ""),
        case.get("outcome", ""),
        case.get("motion", ""),
        case.get("primary_cause", ""),
        case.get("record_type", ""),
    ]))


def best_snippet(case, query):
    snippet = clean_text(case.get("snippet"))
    text = clean_text(case.get("formatted_text") or case.get("text"))

    if not query:
        if snippet:
            return snippet[:900]
        return text[:900]

    hay_query = normalize_for_search(query)
    terms = [t for t in hay_query.split() if t]

    if text:
        paragraphs = [clean_text(p) for p in re.split(r"[\n\r]+", text) if clean_text(p)]
        if not paragraphs:
            paragraphs = [text]

        best_para = ""
        best_score = -1

        for para in paragraphs:
            pl = normalize_for_search(para)
            score = 0

            if hay_query and hay_query in pl:
                score += 12

            for term in terms:
                if term in pl:
                    score += 2

            if score > best_score:
                best_score = score
                best_para = para

        if best_para and best_score > 0:
            return best_para[:900]

    if snippet:
        return snippet[:900]

    return text[:900]


def query_aliases(query):
    q = normalize_for_search(query)
    if q in PHRASE_ALIASES:
        return PHRASE_ALIASES[q]
    return [q] if q else []


def matches_query(case, query):
    if not query:
        return True

    haystack = text_for_search(case)
    q = normalize_for_search(query)

    if not q:
        return True

    aliases = query_aliases(query)

    if q in STRICT_PHRASE_QUERIES:
        return any(alias in haystack for alias in aliases)

    if q in haystack:
        return True

    terms = [t for t in q.split() if t]

    if len(terms) == 1:
        return terms[0] in haystack

    return all(term in haystack for term in terms)


def matches_filters(case, selected_court, selected_outcome):
    if selected_court != "All Courts":
        if case.get("court") != selected_court:
            return False

    if selected_outcome != "All Outcomes":
        if case.get("outcome", "").lower() != selected_outcome.lower():
            return False

    return True


# =========================
# RANKING / TRUST SIGNALS
# =========================

def structured_query_data(query):
    normalized = normalize_for_search(query)
    return {
        "normalized": normalized,
        "aliases": query_aliases(query),
        "strict_phrase": normalized in STRICT_PHRASE_QUERIES,
        "query_cause": detect_query_cause(query),
        "query_motion": detect_query_motion(query),
        "query_outcome": detect_query_outcome(query),
        "terms": [t for t in normalized.split() if t],
    }


def build_trust_signals(case, query, selected_court="All Courts", selected_outcome="All Outcomes"):
    signals = []
    qd = structured_query_data(query)

    if selected_court != "All Courts" and case.get("court") == selected_court:
        signals.append("Same Court")

    if qd["query_motion"]:
        case_motion = case.get("motion", "")
        if qd["query_motion"] == case_motion:
            signals.append("Same Motion")
        elif qd["query_motion"] == "summary judgment" and case_motion == "partial summary judgment":
            signals.append("Same Motion")

    if qd["query_cause"] and case.get("primary_cause") == qd["query_cause"]:
        signals.append("Same Cause")

    if (
        qd["query_outcome"]
        and case.get("outcome") == qd["query_outcome"]
        and selected_outcome == "All Outcomes"
    ):
        signals.append("Same Outcome")

    if selected_outcome != "All Outcomes" and case.get("outcome") == selected_outcome.lower():
        if "Same Outcome" not in signals:
            signals.append("Same Outcome")

    return signals


def score_case(case, query, selected_court="All Courts", selected_outcome="All Outcomes"):
    score = 0
    haystack = text_for_search(case)
    qd = structured_query_data(query)

    score += case.get("court_rank", 0) / 8.0

    if selected_court != "All Courts":
        if case.get("court") == selected_court:
            score += 45
        else:
            score -= 20

    if qd["query_motion"]:
        case_motion = case.get("motion", "")
        if qd["query_motion"] == case_motion:
            score += 38
        elif qd["query_motion"] == "summary judgment" and case_motion == "partial summary judgment":
            score += 32
        elif case_motion:
            score -= 32

    if qd["query_cause"]:
        if case.get("primary_cause") == qd["query_cause"]:
            score += 32
        elif case.get("primary_cause"):
            score -= 24

    if qd["query_outcome"]:
        if case.get("outcome") == qd["query_outcome"]:
            score += 14
        elif case.get("outcome"):
            score -= 8

    if qd["strict_phrase"]:
        alias_hits = sum(1 for alias in qd["aliases"] if alias in haystack)
        if alias_hits:
            score += 12 + (alias_hits * 3)
        else:
            score -= 15
    else:
        if qd["normalized"] and qd["normalized"] in haystack:
            score += 8

        matched_terms = 0
        for term in qd["terms"]:
            if term in haystack:
                matched_terms += 1
                score += 1.0

        if len(qd["terms"]) > 1 and matched_terms == len(qd["terms"]):
            score += 4

    if len(case.get("text", "")) > 5000:
        score += 3
    elif len(case.get("text", "")) > 3000:
        score += 2

    if case.get("record_type") == "motion_order":
        score -= 14

    useful_len = len(case.get("snippet", "")) + len(case.get("text", ""))
    if useful_len < 120:
        score -= 10
    elif useful_len < 300:
        score -= 5

    if looks_like_bad_title(case.get("title", "")):
        score -= 8

    case["trust_signals"] = build_trust_signals(case, query, selected_court, selected_outcome)
    case["score"] = round(score, 2)
    return case["score"]


# =========================
# SIMILAR CASES
# =========================

SIMILAR_STOPWORDS = {
    "the", "and", "for", "that", "with", "from", "this", "into", "their", "there",
    "which", "were", "been", "have", "has", "had", "under", "over", "upon", "without",
    "costs", "order", "entered", "about", "appealed", "limited", "briefs", "branch",
    "motion", "court", "county", "state", "york", "law", "plaintiff", "defendant",
    "defendants", "respondent", "appellant", "appellants", "respondents", "issue",
    "against", "denied", "granted", "affirmed", "reversed", "summary", "judgment",
    "complaint", "claims", "claim", "action", "matter", "extent", "sought", "cross",
    "appeal", "appealed", "appeals", "without", "hearing"
}


def token_set(text):
    raw = normalize_for_search(text).split()
    return {
        tok for tok in raw
        if len(tok) > 2 and tok not in SIMILAR_STOPWORDS and not tok.isdigit()
    }


def substantive_text(case):
    return " ".join([
        case.get("summary", ""),
        case.get("snippet", ""),
        case.get("text", "")[:5000],
        case.get("motion", ""),
        case.get("primary_cause", ""),
    ])


def same_motion_family(a_motion, b_motion):
    if not a_motion or not b_motion:
        return False

    if a_motion == b_motion:
        return True

    family = {"summary judgment", "partial summary judgment"}
    if a_motion in family and b_motion in family:
        return True

    return False


def ordered_unique_signals(signals):
    order = {
        "Same Court": 0,
        "Same Motion": 1,
        "Same Cause": 2,
        "Same Outcome": 3,
    }
    deduped = []
    seen = set()
    for sig in signals:
        if sig and sig not in seen:
            seen.add(sig)
            deduped.append(sig)
    deduped.sort(key=lambda s: order.get(s, 99))
    return deduped


def build_similarity_signals(a, b):
    signals = []

    if a.get("court") and a.get("court") == b.get("court"):
        signals.append("Same Court")

    if same_motion_family(a.get("motion"), b.get("motion")):
        signals.append("Same Motion")

    if a.get("primary_cause") and b.get("primary_cause") and a.get("primary_cause") == b.get("primary_cause"):
        signals.append("Same Cause")

    if a.get("outcome") and b.get("outcome") and a.get("outcome") == b.get("outcome"):
        signals.append("Same Outcome")

    return ordered_unique_signals(signals)


def title_signature(case):
    title = clean_text(case.get("title", ""))
    norm = normalize_for_search(title)
    tokens = [t for t in norm.split() if len(t) > 2 and t not in SIMILAR_STOPWORDS]
    return " ".join(tokens[:8])


def jaccard_similarity(set_a, set_b):
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def similar_cluster_key(case):
    return (
        case.get("court", ""),
        case.get("motion", ""),
        case.get("primary_cause", ""),
        case.get("outcome", ""),
    )


def is_near_duplicate_similar(candidate_case, chosen_cases):
    cand_title_sig = title_signature(candidate_case)
    cand_tokens = token_set(substantive_text(candidate_case))

    for chosen in chosen_cases:
        if cand_title_sig and cand_title_sig == title_signature(chosen):
            return True

        chosen_tokens = token_set(substantive_text(chosen))
        overlap_ratio = jaccard_similarity(cand_tokens, chosen_tokens)
        if overlap_ratio >= 0.82:
            return True

        if (
            candidate_case.get("court") == chosen.get("court")
            and candidate_case.get("motion") == chosen.get("motion")
            and candidate_case.get("primary_cause") == chosen.get("primary_cause")
            and candidate_case.get("outcome") == chosen.get("outcome")
            and overlap_ratio >= 0.62
        ):
            return True

    return False


def similar_score(a, b):
    score = 0

    same_court = a.get("court") and a.get("court") == b.get("court")
    same_cause = a.get("primary_cause") and a.get("primary_cause") == b.get("primary_cause")
    same_motion = same_motion_family(a.get("motion"), b.get("motion"))
    same_outcome = a.get("outcome") and a.get("outcome") == b.get("outcome")

    if same_court:
        score += 36
    else:
        score += min(a.get("court_rank", 0), b.get("court_rank", 0)) / 30.0

    if a.get("primary_cause") and b.get("primary_cause"):
        score += 34 if same_cause else -30

    if a.get("motion") and b.get("motion"):
        if same_motion:
            score += 28 if a.get("motion") == b.get("motion") else 20
        else:
            score -= 24

    if a.get("outcome") and b.get("outcome"):
        score += 10 if same_outcome else -4

    a_tokens = token_set(substantive_text(a))
    b_tokens = token_set(substantive_text(b))
    overlap = len(a_tokens & b_tokens)
    overlap_ratio = jaccard_similarity(a_tokens, b_tokens)

    score += min(overlap, 8)
    score += round(overlap_ratio * 14, 2)

    if overlap < 3:
        score -= 10
    elif overlap < 5:
        score -= 4

    if a.get("record_type") == "motion_order":
        score -= 14

    if looks_like_bad_title(a.get("title", "")):
        score -= 8

    return round(score, 2)


def get_similar_cases(target_case, all_cases, limit=5):
    scored = []

    target_cause = target_case.get("primary_cause", "")
    target_motion = target_case.get("motion", "")
    target_tokens = token_set(substantive_text(target_case))

    for case in all_cases:
        if case is target_case:
            continue

        if case.get("case_id") == target_case.get("case_id"):
            continue

        if target_case.get("record_type") != "motion_order" and case.get("record_type") == "motion_order":
            continue

        case_tokens = token_set(substantive_text(case))
        overlap = len(target_tokens & case_tokens)
        overlap_ratio = jaccard_similarity(target_tokens, case_tokens)

        same_court = target_case.get("court") and case.get("court") == target_case.get("court")
        same_cause = target_cause and case.get("primary_cause") == target_cause
        same_motion = target_motion and same_motion_family(target_motion, case.get("motion"))
        same_outcome = target_case.get("outcome") and case.get("outcome") == target_case.get("outcome")

        structured_hits = sum([
            bool(same_court),
            bool(same_cause),
            bool(same_motion),
            bool(same_outcome),
        ])

        if target_cause and not same_cause:
            continue

        if target_motion and not same_motion:
            if overlap < 10 or overlap_ratio < 0.25:
                continue

        if structured_hits < 2 and overlap_ratio < 0.22:
            continue

        sim_value = similar_score(case, target_case)
        if sim_value <= 18:
            continue

        sim_case = dict(case)
        sim_case["similarity"] = sim_value
        sim_case["similarity_signals"] = build_similarity_signals(target_case, case)
        scored.append(sim_case)

    scored.sort(
        key=lambda x: (
            len(x.get("similarity_signals", [])),
            x.get("similarity", 0),
            x.get("court_rank", 0),
            x.get("date", ""),
            x.get("title", ""),
        ),
        reverse=True,
    )

    final_cases = []
    outcome_counts = {}
    cluster_counts = {}

    for sim_case in scored:
        outcome = sim_case.get("outcome", "") or "unknown"
        cluster = similar_cluster_key(sim_case)

        if outcome_counts.get(outcome, 0) >= 2:
            continue

        if cluster_counts.get(cluster, 0) >= 2:
            continue

        if is_near_duplicate_similar(sim_case, final_cases):
            continue

        final_cases.append(sim_case)
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1

        if len(final_cases) >= limit:
            break

    if len(final_cases) < limit:
        for sim_case in scored:
            if sim_case in final_cases:
                continue
            if is_near_duplicate_similar(sim_case, final_cases):
                continue

            final_cases.append(sim_case)
            if len(final_cases) >= limit:
                break

    return final_cases


# =========================
# DROPDOWNS
# =========================

def build_courts(cases):
    courts = sorted({c.get("court") for c in cases if c.get("court")})
    return ["All Courts"] + courts


def build_outcomes(cases):
    outcomes = sorted({c.get("outcome") for c in cases if c.get("outcome")})
    return ["All Outcomes"] + outcomes


# =========================
# CASE LOOKUP
# =========================

def find_case_by_id(case_id, cases):
    case_id = clean_text(case_id)
    for case in cases:
        if case.get("case_id") == case_id:
            return case
    return None


# =========================
# ROUTES
# =========================

@app.route("/", methods=["GET", "POST"])
def index():
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1

    if request.method == "POST":
        query = clean_text(request.form.get("query", ""))
        court = clean_text(request.form.get("court", "All Courts")) or "All Courts"
        outcome = clean_text(request.form.get("outcome", "All Outcomes")) or "All Outcomes"
        page = 1
    else:
        query = clean_text(request.args.get("query", ""))
        court = clean_text(request.args.get("court", "All Courts")) or "All Courts"
        outcome = clean_text(request.args.get("outcome", "All Outcomes")) or "All Outcomes"

    cases = load_cases()

    for case in cases:
        score_case(case, query, court, outcome)
        case["display_snippet"] = best_snippet(case, query)

    if not query:
        filtered = []
    else:
        filtered = [
            c for c in cases
            if matches_query(c, query) and matches_filters(c, court, outcome)
        ]

        filtered.sort(
            key=lambda x: (
                x.get("score", 0),
                x.get("court_rank", 0),
                x.get("date", ""),
                x.get("title", ""),
            ),
            reverse=True,
        )

    pager = build_pager(page, PER_PAGE, len(filtered))

    start = (pager.page - 1) * PER_PAGE
    end = start + PER_PAGE
    results = filtered[start:end]

    for case in results:
        case["similar_cases"] = get_similar_cases(case, filtered, limit=3)

    return render_template(
        "index.html",
        results=results,
        pager=pager,
        query=query,
        courts=build_courts(cases),
        outcomes=build_outcomes(cases),
        selected_court=court,
        selected_outcome=outcome,
    )


@app.route("/matter", methods=["GET", "POST"])
def matter():
    cases = load_cases()

    case_id = clean_text(request.args.get("case_id", ""))
    selected_case = None

    if case_id:
        selected_case = find_case_by_id(case_id, cases)

    matter_data = get_matter(selected_case)

    return render_template(
        "matter.html",
        matter=matter_data,
    )


@app.route("/pdf/<path:filename>")
def serve_pdf(filename):
    return send_from_directory(os.path.join(BASE_DIR, "data", "pdfs"), filename)


@app.route("/case/<path:case_id>")
def case_detail(case_id):
    cases = load_cases()
    case = find_case_by_id(case_id, cases)
    if not case:
        abort(404)

    score_case(case, "", "All Courts", "All Outcomes")
    case["display_snippet"] = case.get("formatted_text") or case.get("text") or ""
    case["similar_cases"] = get_similar_cases(case, cases, limit=8)

    return render_template("case_detail.html", case=case)


# =========================
# RUN
# =========================

if __name__ == "__main__":
    app.run(debug=True, port=5001)