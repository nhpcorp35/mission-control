"""Scoring foundation for Case-00 attorney-feedback evaluation.

Extends the existing five-dimension attorney gold rubric. Never fabricates
numeric or qualitative scores. When a dimension cannot be scored reliably,
emit an explicit pending / not-evaluable state.
"""

from __future__ import annotations

from typing import Any, Optional

# Align with attorney_gold_labels_01.json rubric (+ mission wording aliases).
SCORING_DIMENSIONS = (
    "factual_accuracy",
    "completeness",
    "legal_procedural_reasoning",
    "substantive_citation_support",
    "uncertainty_handling",
)

# Mission-facing aliases mapped onto the existing rubric ids.
DIMENSION_ALIASES = {
    "citation_support": "substantive_citation_support",
    "legal_reasoning": "legal_procedural_reasoning",
    "handling_of_uncertainty": "uncertainty_handling",
}

STATUS_PENDING_ATTORNEY_SCORE = "pending_attorney_score"
STATUS_NOT_EVALUABLE = "not_evaluable"
STATUS_SCORED = "scored"


def normalize_dimension_id(dimension_id: str) -> str:
    return DIMENSION_ALIASES.get(dimension_id, dimension_id)


def extract_label_dimension_scores(
    label_record: Optional[dict[str, Any]],
) -> dict[str, Any]:
    if not label_record:
        return {}
    scores = label_record.get("rubric_dimension_scores") or {}
    return scores if isinstance(scores, dict) else {}


def score_dimensions(
    label_record: Optional[dict[str, Any]],
    *,
    has_reference_answer: bool,
    reference_status: str,
) -> dict[str, Any]:
    """Return per-dimension score records without inventing values.

    A dimension is ``scored`` only when the attorney-gold label artifact already
    contains an explicit non-null numeric score. Otherwise the status is
    pending or not-evaluable with ``score: null``.
    """
    label_scores = extract_label_dimension_scores(label_record)
    results: dict[str, Any] = {}
    evaluable: list[str] = []
    missing: list[str] = []

    for dim in SCORING_DIMENSIONS:
        raw = label_scores.get(dim, None)
        if raw is None:
            if not has_reference_answer:
                status = STATUS_NOT_EVALUABLE
                reason = (
                    "No usable reference answer and no attorney-supplied "
                    f"numeric score for {dim}."
                )
                missing.append(
                    f"{dim}: missing reference answer and attorney score"
                )
            elif reference_status == "provisional":
                status = STATUS_PENDING_ATTORNEY_SCORE
                reason = (
                    "Provisional reference present, but attorney numeric score "
                    f"for {dim} was not supplied; automated scoring disabled."
                )
                missing.append(
                    f"{dim}: attorney numeric score not supplied "
                    "(provisional reference must not be treated as approved)"
                )
            elif reference_status == "attorney_approved":
                status = STATUS_PENDING_ATTORNEY_SCORE
                reason = (
                    "Attorney-approved reference present, but dimension score "
                    f"for {dim} was not supplied; no LLM judge / fabricated score."
                )
                missing.append(f"{dim}: attorney numeric score not supplied")
            else:
                status = STATUS_NOT_EVALUABLE
                reason = f"No attorney-supplied numeric score for {dim}."
                missing.append(f"{dim}: score unavailable")
            results[dim] = {
                "status": status,
                "score": None,
                "scale": {"min": 0, "max": 4},
                "reason": reason,
            }
            continue

        # Explicit attorney / adjudicated score present in labels.
        if isinstance(raw, (int, float)):
            results[dim] = {
                "status": STATUS_SCORED,
                "score": raw,
                "scale": {"min": 0, "max": 4},
                "reason": "Score taken from attorney_gold_labels rubric_dimension_scores.",
                "source": "attorney_gold_labels",
            }
            evaluable.append(dim)
        else:
            results[dim] = {
                "status": STATUS_NOT_EVALUABLE,
                "score": None,
                "scale": {"min": 0, "max": 4},
                "reason": (
                    f"Label value for {dim} is non-numeric ({type(raw).__name__}); "
                    "refusing to coerce into a score."
                ),
            }
            missing.append(f"{dim}: non-numeric label value")

    return {
        "dimensions": results,
        "currently_evaluable": evaluable,
        "missing_information": missing,
    }
