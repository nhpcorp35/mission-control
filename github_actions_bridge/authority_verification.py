"""No-model authority status extraction for immutable verified case records.

This module deliberately does not decide what a case or statute holds. A citation
found in a filing is a *party-cited candidate* until a separately curated primary
authority record supplies the official source, identity, and proposition.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

_PATTERNS = (
    re.compile(r"\b20\d{2}\s+N\.?Y\.?\s+Slip\s+Op\.?\s+\d{5}\b", re.IGNORECASE),
    re.compile(r"\b\d{1,3}\s+(?:A\.?\s*D\.?\s*(?:2d|3d)|N\.?\s*Y\.?\s*(?:2d|3d))\s+\d{1,4}\b", re.IGNORECASE),
    re.compile(r"\b(?:N\.?\s*Y\.?\s*)?(?:Navigation|Environmental\s+Conservation)\s+Law\s*§\s*\d+[\w().-]*", re.IGNORECASE),
    re.compile(r"\bC\.?P\.?L\.?R\.?\s*§?\s*\d+[\w().-]*\b", re.IGNORECASE),
)


def _normalized_citation(value: str) -> str:
    return " ".join(value.casefold().replace("§", " section ").split())


def extract_party_cited_authorities(raw: bytes, limit: int = 50) -> list[dict[str, Any]]:
    """Extract cited authority candidates with page citations and no legal inference."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    try:
        records = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("verified page index is invalid") from exc
    matches: dict[str, dict[str, Any]] = {}
    pages: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        filename, page_number, text = record.get("filename"), record.get("page_number"), record.get("text")
        if not isinstance(filename, str) or not filename or not isinstance(page_number, int) or page_number < 1 or not isinstance(text, str):
            continue
        for pattern in _PATTERNS:
            for match in pattern.finditer(text):
                citation = match.group(0).strip().rstrip(".,;:")
                key = _normalized_citation(citation)
                if key not in matches:
                    matches[key] = {"citation": citation, "status": "party_cited_unverified", "verification_requirement": "Do not treat this citation as governing law until a separately reviewed primary-authority record identifies an official source, court or legislature, and bounded proposition.", "record_citations": pages[key]}
                location = {"filename": filename, "page_number": page_number}
                if location not in pages[key]:
                    pages[key].append(location)
    candidates = sorted(matches.values(), key=lambda item: (item["citation"].casefold(), item["record_citations"][0]["filename"], item["record_citations"][0]["page_number"]))
    return candidates[:limit]


def authority_verification_check(raw: bytes, limit: int = 50) -> dict[str, Any]:
    """Return authority status only; never resolve holdings or call a model."""
    return {"ok": True, "model_called": False, "candidates": extract_party_cited_authorities(raw, limit=limit), "verified_primary_authorities": [], "analysis_guardrail": "Party-filed citations are not verified authority. No proposition, holding, or governing-law conclusion is produced without a separately verified primary source."}
