"""Deterministic evidence roles for the LegalAI litigation framework."""
from __future__ import annotations

from typing import Any

try:
    from .verified_case_search import search_index_jsonl
except ImportError:
    from verified_case_search import search_index_jsonl

EVIDENCE_CATEGORIES: tuple[dict[str, str], ...] = (
    {"name": "expert_opinion", "record_role": "opinion", "rule": "Expert analysis, including a proportion or 1/4 methodology, is evidence and is not governing law.", "query": "expert engineer preliminary opinion methodology riparian"},
    {"name": "regulatory_record", "record_role": "regulatory_record", "rule": "Agency permits, approvals, inspections, and records must be evaluated as records, not assumed to resolve private boundary law.", "query": "DEC permit approval inspection certificate occupancy violation"},
    {"name": "drawings_and_space", "record_role": "physical_record", "rule": "Survey, drawing, measurement, depth, and maneuvering-space proof bears on factual feasibility.", "query": "survey drawing site plan measurement depth maneuvering space"},
    {"name": "cited_authority", "record_role": "legal_authority", "rule": "Cited cases and statutes must be separately identified before treating a proposition as governing law.", "query": "court case decision N.Y. A.D. CPLR ECL riparian navigation"},
)

def framework_evidence_categories(raw: bytes, limit: int = 20) -> dict[str, dict[str, Any]]:
    """Return citation-backed framework inputs without a model or legal conclusion."""
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    return {
        spec["name"]: {
            "record_role": spec["record_role"], "rule": spec["rule"],
            "citation_requirement": "Each later proposition must retain the returned filename and page_number.",
            "results": search_index_jsonl(raw, spec["query"], limit),
        }
        for spec in EVIDENCE_CATEGORIES
    }

def framework_evidence_check(raw: bytes, limit: int = 20) -> dict[str, Any]:
    """Produce a deterministic attorney-analysis input map from verified pages."""
    categories = framework_evidence_categories(raw, limit)
    return {
        "ok": True, "model_called": False, "categories": categories,
        "missing_categories": [name for name, category in categories.items() if not category["results"]],
        "analysis_guardrails": [
            "Do not treat expert opinion as governing law.",
            "Do not infer regulatory approval, spatial feasibility, or legal authority from an absent category.",
            "Use only returned record citations for subsequent analysis.",
            "Before any later analysis, keep facts, party positions, expert opinion, regulatory records, physical proof, and legal authority in separate cited propositions.",
        ],
    }
