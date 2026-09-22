"""Deterministic candidate-conflict map for verified LegalAI records."""
from __future__ import annotations
from typing import Any
from verified_case_search import search_index_jsonl

CONFLICT_LANES: tuple[dict[str, str], ...] = (
    {"name": "opinion_vs_authority", "left_role": "expert_opinion", "left_query": "expert preliminary opinion rule methodology", "right_role": "legal_authority", "right_query": "court case decision statute law riparian", "guardrail": "An expert's methodology or allocation is evidence, not governing law."},
    {"name": "regulatory_record_vs_claim", "left_role": "regulatory_record", "left_query": "DEC permit approval inspection certificate occupancy", "right_role": "party_position", "right_query": "deny dispute violation unpermitted approval", "guardrail": "Agency proof and a party's characterization of that proof must remain separately cited."},
    {"name": "space_proof_vs_access_claim", "left_role": "physical_record", "left_query": "survey drawing measurement depth maneuvering space", "right_role": "party_position", "right_query": "access obstruct block boat vessel dock corridor", "guardrail": "Physical feasibility requires drawings or measurements; a claimed obstruction alone does not resolve it."},
)

def framework_conflict_map(raw: bytes, limit: int = 10) -> dict[str, Any]:
    """Return candidate tensions from exact pages without resolving any dispute."""
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    lanes: list[dict[str, Any]] = []
    for spec in CONFLICT_LANES:
        left = search_index_jsonl(raw, spec["left_query"], limit)
        right = search_index_jsonl(raw, spec["right_query"], limit)
        lanes.append({
            "name": spec["name"], "status": "candidate_tension" if left and right else "insufficient_record",
            "left": {"record_role": spec["left_role"], "results": left},
            "right": {"record_role": spec["right_role"], "results": right},
            "citation_requirement": "Each side must retain its returned filename and page_number; do not convert candidate tension into a conclusion.",
            "guardrail": spec["guardrail"],
        })
    return {"ok": True, "model_called": False, "conflicts": lanes, "analysis_guardrail": "Candidate tension is not a finding of contradiction, liability, or governing law."}
