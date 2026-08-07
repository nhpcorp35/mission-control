"""Deterministic candidate-vs-reference diagnostics for Case-00.

Rule-based comparison only. Never fabricates numeric scores. Never treats
provisional answers as attorney-approved gold. LLM judge remains disabled.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Procedural roles recognized in free-text party/role assertions.
_ROLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "third-party plaintiff",
        re.compile(r"\bthird[\s-]+party\s+plaintiffs?\b", re.IGNORECASE),
    ),
    (
        "third-party defendant",
        re.compile(r"\bthird[\s-]+party\s+defendants?\b", re.IGNORECASE),
    ),
    ("plaintiff", re.compile(r"\bplaintiffs?\b", re.IGNORECASE)),
    ("defendant", re.compile(r"\bdefendants?\b", re.IGNORECASE)),
    ("petitioner", re.compile(r"\bpetitioners?\b", re.IGNORECASE)),
    ("respondent", re.compile(r"\brespondents?\b", re.IGNORECASE)),
    ("appellant", re.compile(r"\bappellants?\b", re.IGNORECASE)),
    ("appellee", re.compile(r"\bappellees?\b", re.IGNORECASE)),
)

# "Name, role" / "role Name" / "Name is the role"
_PARTY_ROLE_PAIR_RE = re.compile(
    r"(?P<name>(?-i:[A-Z0-9][A-Za-z0-9&.'’\-/]*(?:\s+(?:of|at|the|and|for|in|"
    r"[A-Z0-9][A-Za-z0-9&.'’\-/]*)){0,8}))"
    r"(?:\s*,\s*|\s+is\s+(?:also\s+)?(?:an?\s+|the\s+)?|\s+as\s+(?:an?\s+|the\s+)?)"
    r"(?P<role>"
    r"third[\s-]+party\s+(?:plaintiffs?|defendants?)|"
    r"plaintiffs?|defendants?|petitioners?|respondents?|appellants?|appellees?"
    r")\b",
    re.IGNORECASE,
)

_ROLE_THEN_NAME_RE = re.compile(
    r"\b(?P<role>"
    r"third[\s-]+party\s+(?:plaintiffs?|defendants?)|"
    r"plaintiffs?|defendants?|petitioners?|respondents?|appellants?|appellees?"
    r")\s+"
    r"(?P<name>(?-i:[A-Z0-9][A-Za-z0-9&.'’\-/]*(?:\s+(?:of|at|the|and|for|in|"
    r"[A-Z0-9][A-Za-z0-9&.'’\-/]*)){0,8}))"
    r"(?=\s+(?:is|was|are|were|,|\.|$|;))",
    re.IGNORECASE,
)

_CITATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bNYSCEF\s*(?:Doc(?:ument)?\.?\s*)?(?:No\.?\s*)?#?\s*(\d+)\b", re.I),
    re.compile(r"\bDoc(?:ument)?\.?\s*No\.?\s*(\d+)\b", re.I),
    re.compile(r"\bnyscef[_-](\d+)\b", re.I),
    re.compile(r"\bpage\s+(\d+)\b", re.I),
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9&.'’\-]{1,}")

# Tokens that are too generic to count as material fact anchors alone.
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "by",
        "with",
        "from",
        "that",
        "this",
        "these",
        "those",
        "it",
        "its",
        "at",
        "into",
        "about",
        "than",
        "then",
        "also",
        "not",
        "no",
        "yes",
        "which",
        "who",
        "whom",
        "whose",
        "where",
        "when",
        "while",
        "under",
        "over",
        "after",
        "before",
        "between",
        "among",
        "each",
        "all",
        "any",
        "other",
        "such",
        "per",
        "via",
        "alleged",
        "allegedly",
        "answer",
        "question",
        "record",
        "court",
        "case",
        "matter",
        "herein",
        "thereof",
        "hereof",
    }
)

_NAME_BLOCKLIST = frozenset(
    {
        "plaintiff",
        "plaintiffs",
        "defendant",
        "defendants",
        "petitioner",
        "petitioners",
        "respondent",
        "respondents",
        "appellant",
        "appellants",
        "appellee",
        "appellees",
        "party",
        "parties",
        "the court",
        "this action",
    }
)

# Materiality markers from attorney labels that mean unresolved material error.
_MATERIAL_ERROR_VERDICTS = frozenset(
    {
        "incorrect",
        "partially_correct",
        "incomplete",
        "needs_revision",
        "material_error",
    }
)
_MATERIALITY_POSITIVE = frozenset({"material", "materially_incorrect", "high", "yes"})


def normalize_text(value: str) -> str:
    text = (value or "").replace("\u2019", "'").replace("\u2018", "'")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def normalize_party_name(name: str) -> str:
    cleaned = normalize_text(name)
    cleaned = cleaned.strip(" .,;:\"'")
    cleaned = re.sub(r"\b(llc|llp|inc\.?|corp\.?|co\.?|ltd\.?|pllc|p\.?c\.?)\b", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,;:")
    return cleaned


def normalize_role(role: str) -> str:
    value = normalize_text(role).replace("third party", "third-party")
    value = value.strip(" .,;:")
    for canonical, pattern in _ROLE_PATTERNS:
        if pattern.fullmatch(value) or pattern.search(value):
            # Prefer longest / most specific match already ordered first.
            if canonical.startswith("third-party") or pattern.fullmatch(value):
                return canonical
    for canonical, pattern in _ROLE_PATTERNS:
        if pattern.search(value):
            return canonical
    return value


def _plausible_name(name: str) -> bool:
    cleaned = normalize_party_name(name)
    if not cleaned or len(cleaned) < 3:
        return False
    if cleaned in _NAME_BLOCKLIST:
        return False
    if cleaned.startswith(("is ", "was ", "are ", "were ", "the ")):
        return False
    return True


def extract_party_role_pairs(text: str) -> list[dict[str, str]]:
    """Extract deterministic (party, role) pairs from free text."""
    if not text:
        return []
    found: dict[tuple[str, str], dict[str, str]] = {}
    for pattern in (_PARTY_ROLE_PAIR_RE, _ROLE_THEN_NAME_RE):
        for match in pattern.finditer(text):
            raw_name = (match.group("name") or "").strip(" .,;:")
            raw_role = (match.group("role") or "").strip()
            if not _plausible_name(raw_name):
                continue
            role = normalize_role(raw_role)
            key = (normalize_party_name(raw_name), role)
            if key[0] and role and key not in found:
                found[key] = {
                    "party": raw_name.strip(),
                    "party_normalized": key[0],
                    "role": role,
                    "raw": match.group(0).strip(),
                }
    return sorted(found.values(), key=lambda item: (item["party_normalized"], item["role"]))


def extract_material_fact_sentences(text: str) -> list[str]:
    """Split text into non-trivial factual assertion sentences."""
    if not text:
        return []
    sentences: list[str] = []
    seen: set[str] = set()
    for chunk in _SENTENCE_SPLIT_RE.split(text.strip()):
        sentence = re.sub(r"\s+", " ", chunk).strip(" -\t")
        if len(sentence) < 20:
            continue
        # Skip pure headings / status markers.
        if sentence.startswith("#") or sentence.startswith("status:"):
            continue
        norm = normalize_text(sentence)
        if norm in seen:
            continue
        tokens = [t for t in _WORD_RE.findall(norm) if t not in _STOPWORDS]
        if len(tokens) < 3:
            continue
        seen.add(norm)
        sentences.append(sentence)
    return sentences


def fact_token_set(sentence: str) -> frozenset[str]:
    return frozenset(
        t for t in _WORD_RE.findall(normalize_text(sentence)) if t not in _STOPWORDS and len(t) > 2
    )


def facts_covered(reference_fact: str, candidate_text_norm: str, candidate_facts: list[str]) -> bool:
    """True when a reference fact appears covered by the candidate."""
    ref_tokens = fact_token_set(reference_fact)
    if not ref_tokens:
        return True
    # Direct normalized substring of a substantial phrase.
    ref_norm = normalize_text(reference_fact)
    if len(ref_norm) >= 24 and ref_norm in candidate_text_norm:
        return True
    # Party/role facts: covered when each extracted pair from the reference
    # sentence is also present in the candidate text.
    ref_pairs = extract_party_role_pairs(reference_fact)
    if ref_pairs:
        cand_pairs = extract_party_role_pairs(
            "\n".join(candidate_facts) + "\n" + candidate_text_norm
        )
        cand_index = {(p["party_normalized"], p["role"]) for p in cand_pairs}
        # Soft party key containment match.
        def _party_role_present(party: str, role: str) -> bool:
            if (party, role) in cand_index:
                return True
            return any(
                role == c_role and (party in c_party or c_party in party)
                for c_party, c_role in cand_index
            )

        if all(_party_role_present(p["party_normalized"], p["role"]) for p in ref_pairs):
            # Require most non-role content tokens as well when the sentence is
            # longer than a pure party/role assertion.
            content_tokens = {
                t
                for t in ref_tokens
                if t
                not in {
                    "plaintiff",
                    "plaintiffs",
                    "defendant",
                    "defendants",
                    "petitioner",
                    "petitioners",
                    "respondent",
                    "respondents",
                    "appellant",
                    "appellants",
                    "appellee",
                    "appellees",
                }
            }
            # Drop tokens that are part of the party names themselves.
            for pair in ref_pairs:
                content_tokens -= set(pair["party_normalized"].split())
            # Light contextual tails ("in this insurance dispute") do not block
            # coverage once the party/role assertion itself is present.
            if len(content_tokens) <= 3:
                return True
            present = sum(1 for t in content_tokens if t in candidate_text_norm)
            if (present / float(len(content_tokens))) >= 0.5:
                return True

    # Token overlap against any candidate fact sentence.
    best = 0.0
    for cand in candidate_facts:
        cand_tokens = fact_token_set(cand)
        if not cand_tokens:
            continue
        overlap = len(ref_tokens & cand_tokens) / float(len(ref_tokens))
        if overlap > best:
            best = overlap
    if best >= 0.72:
        return True
    # Fallback: majority of distinctive tokens present anywhere in candidate.
    present = sum(1 for t in ref_tokens if t in candidate_text_norm)
    return (present / float(len(ref_tokens))) >= 0.8


def extract_citations(text: str) -> list[str]:
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for pattern in _CITATION_PATTERNS:
        for match in pattern.finditer(text):
            token = normalize_text(match.group(0))
            if token and token not in seen:
                seen.add(token)
                found.append(match.group(0).strip())
    return found


def attorney_labels_indicate_unresolved_material_errors(
    label: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Report whether attorney labels flag unresolved material errors."""
    if not label:
        return {
            "available": False,
            "unresolved_material_errors": None,
            "reason": "attorney labels unavailable",
        }
    verdict = normalize_text(str(label.get("attorney_verdict") or ""))
    materiality = normalize_text(str(label.get("materiality") or ""))
    categories = label.get("primary_error_categories") or []
    if not isinstance(categories, list):
        categories = []
    safety = label.get("safety_gate") if isinstance(label.get("safety_gate"), dict) else {}
    material_gate = bool(safety.get("material_error_gate"))
    label_status = normalize_text(str(label.get("label_status") or ""))
    locked = bool(label.get("locked"))

    material_flag = (
        materiality in _MATERIALITY_POSITIVE
        or material_gate
        or verdict in _MATERIAL_ERROR_VERDICTS
        or (bool(categories) and verdict != "correct" and materiality not in {"none", "immaterial", "no"})
    )
    unresolved = material_flag and not (
        verdict == "correct" and materiality in {"none", "immaterial", "no"} and locked
    )
    # Final locked correct labels with no materiality are resolved.
    if verdict == "correct" and materiality in {"none", "immaterial", "no", ""} and not categories:
        unresolved = False

    return {
        "available": True,
        "unresolved_material_errors": bool(unresolved),
        "attorney_verdict": label.get("attorney_verdict"),
        "materiality": label.get("materiality"),
        "primary_error_categories": list(categories),
        "label_status": label.get("label_status"),
        "locked": locked,
        "material_error_gate": material_gate,
        "reason": (
            "Attorney labels indicate unresolved material errors on the labeled answer."
            if unresolved
            else "Attorney labels do not indicate unresolved material errors."
            if label_status
            else "Attorney label present but incomplete."
        ),
    }


def compare_candidate_to_reference(
    *,
    candidate_text: Optional[str],
    reference_text: Optional[str],
    reference_status: str,
    reference_usable: bool,
    label_record: Optional[dict[str, Any]] = None,
    candidate_evidence: Optional[list[Any]] = None,
) -> dict[str, Any]:
    """Deterministic diagnostic comparison; no numeric scores."""
    label_diag = attorney_labels_indicate_unresolved_material_errors(label_record)

    if not reference_usable or not reference_text:
        return {
            "comparison_performed": False,
            "method": "deterministic_rule_based",
            "numeric_scores_fabricated": False,
            "llm_judge": "disabled",
            "reference_status": reference_status,
            "reference_usable": bool(reference_usable),
            "provisional_treated_as_approved": False,
            "note": (
                "No usable reference text for comparison "
                f"(status={reference_status})."
            ),
            "missing_material_facts": [],
            "unsupported_or_extra_assertions": [],
            "party_role_mismatches": [],
            "citation_evidence_coverage": {
                "available": False,
                "reference_citations": [],
                "candidate_citations": [],
                "missing_from_candidate": [],
                "extra_in_candidate": [],
                "gaps": ["usable reference unavailable"],
            },
            "attorney_label_material_errors": label_diag,
        }

    cand = candidate_text or ""
    ref = reference_text or ""
    cand_norm = normalize_text(cand)
    ref_facts = extract_material_fact_sentences(ref)
    cand_facts = extract_material_fact_sentences(cand)

    missing_facts = [
        fact for fact in ref_facts if not facts_covered(fact, cand_norm, cand_facts)
    ]
    unsupported = [
        fact for fact in cand_facts if not facts_covered(fact, normalize_text(ref), ref_facts)
    ]

    ref_pairs = extract_party_role_pairs(ref)
    cand_pairs = extract_party_role_pairs(cand)
    ref_by_party = {p["party_normalized"]: p for p in ref_pairs}
    cand_by_party = {p["party_normalized"]: p for p in cand_pairs}

    mismatches: list[dict[str, Any]] = []
    for party_key, ref_pair in sorted(ref_by_party.items()):
        cand_pair = cand_by_party.get(party_key)
        if cand_pair is None:
            # Try fuzzy: any candidate party containing/contained by ref name.
            cand_pair = next(
                (
                    p
                    for p in cand_pairs
                    if party_key in p["party_normalized"]
                    or p["party_normalized"] in party_key
                ),
                None,
            )
        if cand_pair is None:
            mismatches.append(
                {
                    "type": "missing_party",
                    "party": ref_pair["party"],
                    "expected_role": ref_pair["role"],
                    "candidate_role": None,
                }
            )
        elif cand_pair["role"] != ref_pair["role"]:
            mismatches.append(
                {
                    "type": "role_mismatch",
                    "party": ref_pair["party"],
                    "expected_role": ref_pair["role"],
                    "candidate_role": cand_pair["role"],
                }
            )
    for party_key, cand_pair in sorted(cand_by_party.items()):
        if party_key in ref_by_party:
            continue
        if any(
            party_key in rk or rk in party_key for rk in ref_by_party
        ):
            continue
        mismatches.append(
            {
                "type": "extra_party",
                "party": cand_pair["party"],
                "expected_role": None,
                "candidate_role": cand_pair["role"],
            }
        )

    ref_cites = extract_citations(ref)
    cand_cites = extract_citations(cand)
    # Also harvest citation-like tokens from structured candidate evidence when present.
    evidence_cites: list[str] = []
    if candidate_evidence:
        for item in candidate_evidence:
            if isinstance(item, dict):
                blob = " ".join(
                    str(item.get(k) or "")
                    for k in (
                        "nyscef_document_number",
                        "page_id",
                        "source_excerpt",
                        "citation",
                        "doc_no",
                    )
                )
            else:
                blob = str(item)
            evidence_cites.extend(extract_citations(blob))
            # Bare document numbers from structured fields.
            if isinstance(item, dict) and item.get("nyscef_document_number") is not None:
                evidence_cites.append(f"NYSCEF {item['nyscef_document_number']}")

    all_cand_cites = list(dict.fromkeys([*cand_cites, *evidence_cites]))
    ref_cite_norms = {normalize_text(c): c for c in ref_cites}
    cand_cite_norms = {normalize_text(c): c for c in all_cand_cites}
    missing_cites = [
        ref_cite_norms[k] for k in ref_cite_norms if k not in cand_cite_norms
    ]
    extra_cites = [
        cand_cite_norms[k] for k in cand_cite_norms if k not in ref_cite_norms
    ]
    cite_available = bool(ref_cites or all_cand_cites or candidate_evidence is not None)
    gaps: list[str] = []
    if not cite_available:
        gaps.append("no citation or evidence tokens available in candidate or reference")
    elif missing_cites:
        gaps.append("candidate missing one or more reference citations")
    if candidate_evidence is not None and not candidate_evidence and ref_cites:
        gaps.append("candidate evidence list empty while reference cites sources")

    note = (
        "Compared candidate against provisional reference for diagnostics only; "
        "provisional material is not attorney-approved gold."
        if reference_status == "provisional"
        else "Compared candidate against attorney-approved reference."
        if reference_status == "attorney_approved"
        else f"Compared candidate against reference status={reference_status}."
    )

    return {
        "comparison_performed": True,
        "method": "deterministic_rule_based",
        "numeric_scores_fabricated": False,
        "llm_judge": "disabled",
        "reference_status": reference_status,
        "reference_usable": True,
        "provisional_treated_as_approved": False,
        "note": note,
        "missing_material_facts": missing_facts,
        "unsupported_or_extra_assertions": unsupported,
        "party_role_mismatches": mismatches,
        "citation_evidence_coverage": {
            "available": cite_available,
            "reference_citations": ref_cites,
            "candidate_citations": all_cand_cites,
            "missing_from_candidate": missing_cites,
            "extra_in_candidate": extra_cites,
            "gaps": gaps,
        },
        "attorney_label_material_errors": label_diag,
        "counts": {
            "missing_material_facts": len(missing_facts),
            "unsupported_or_extra_assertions": len(unsupported),
            "party_role_mismatches": len(mismatches),
            "citation_gaps": len(gaps),
        },
    }
