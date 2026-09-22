"""Deterministic evidence roles for the LegalAI litigation framework."""
from __future__ import annotations

from typing import Any

try:
    from .verified_case_search import search_index_jsonl
except ImportError:
    from verified_case_search import search_index_jsonl

EVIDENCE_CATEGORIES: tuple[dict[str, str], ...] = (
    {"name": "party_positions", "record_role": "party_position", "rule": "A party's affidavit, pleading, or brief states a position; it is not proof of the position merely because it is asserted.", "query": "plaintiff defendant alleges claims denies contends asserts obstruction access riparian"},
    {"name": "expert_opinion", "record_role": "opinion", "rule": "Expert analysis, including a proportion or 1/4 methodology, is evidence and is not governing law.", "query": "expert engineer preliminary opinion methodology riparian"},
    {"name": "regulatory_record", "record_role": "regulatory_record", "rule": "Agency permits, approvals, inspections, and records must be evaluated as records, not assumed to resolve private boundary law.", "query": "DEC permit approval inspection certificate occupancy violation"},
    {"name": "drawings_and_space", "record_role": "physical_record", "rule": "Survey, drawing, measurement, depth, and maneuvering-space proof bears on factual feasibility.", "query": "survey drawing site plan measurement depth maneuvering space"},
    {"name": "cited_authority", "record_role": "legal_authority", "rule": "Cited cases and statutes must be separately identified before treating a proposition as governing law.", "query": "court case decision N.Y. A.D. CPLR ECL riparian navigation"},
)

# A deterministic attorney-usefulness contract: this is not a score, model
# call, or legal conclusion. A later drafting/review step must satisfy every
# item before its analysis is treated as attorney-ready.
REASONING_ACCEPTANCE_CHECKS: tuple[dict[str, str], ...] = (
    {"id": "separate_propositions", "requirement": "Keep party positions, expert opinion, regulatory records, physical proof, and legal authority as distinct cited propositions.", "failure": "Do not present an assertion, expert methodology, permit, or drawing as though it alone resolves the legal issue."},
    {"id": "competing_positions", "requirement": "State the strongest record-supported position for each side on the material issue, with citations for each position.", "failure": "Do not merely list filings or evidence without explaining the competing positions they support."},
    {"id": "what_each_proves", "requirement": "For every material evidence category used, state what it establishes and what it does not establish.", "failure": "Do not convert an expert opinion, agency record, or physical measurement into governing law or an ultimate finding."},
    {"id": "authority_application", "requirement": "Identify the cited authority separately and explain the conditional connection between that authority and the verified facts.", "failure": "Do not call party-cited authority governing law until an official primary source and the proposition are independently verified."},
    {"id": "outcome_significance", "requirement": "Explain how the identified conflict or missing proof could affect the requested relief, motion, or litigation position, while labeling uncertainty.", "failure": "Do not state a definitive outcome when the record leaves a material factual or legal gap."},
    {"id": "missing_information", "requirement": "Identify the specific record, measurement, primary authority, or procedural fact still needed before a conclusion can be evaluated.", "failure": "Do not use a generic disclaimer instead of naming the missing item and why it matters."},
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
        "reasoning_acceptance_checks": list(REASONING_ACCEPTANCE_CHECKS),
    }
