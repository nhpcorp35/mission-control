"""Deterministic human-readable Case-00 attorney review packet renderer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional

PACKET_FILENAME = "case00_attorney_review_packet.md"
_NONE = "None identified."

_LOCATOR_LABELS = (
    ("nyscef_document_number", "NYSCEF document"),
    ("filing_number", "Filing"),
    ("filing_id", "Filing"),
    ("document_number", "Document"),
    ("doc_no", "Document"),
    ("document_id", "Document"),
    ("source_filename", "Document"),
    ("exhibit", "Exhibit"),
    ("exhibit_id", "Exhibit"),
    ("exhibit_label", "Exhibit"),
    ("pdf_page", "PDF page"),
    ("pdf_page_number", "PDF page"),
    ("page", "Page"),
    ("page_number", "Page"),
    ("page_id", "Page ID"),
    ("source_path", "Source path"),
    ("path", "Source path"),
    ("citation", "Citation"),
)


def _as_items(value: Any) -> list[Any]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _locators(item: Any) -> str:
    if not isinstance(item, Mapping):
        return ""
    found: list[str] = []
    seen: set[tuple[str, str]] = set()
    sources: list[Mapping[str, Any]] = [item]
    for key in ("source", "locator", "citation", "document", "filing", "page"):
        nested = item.get(key)
        if isinstance(nested, Mapping):
            sources.append(nested)
    for source in sources:
        for key, label in _LOCATOR_LABELS:
            value = source.get(key)
            if value in (None, "", [], {}):
                continue
            pair = (label, _scalar(value))
            if pair not in seen:
                seen.add(pair)
                found.append(f"{label}: `{pair[1]}`")
    return "; ".join(found)


def _item_text(item: Any) -> str:
    if not isinstance(item, Mapping):
        return _scalar(item)
    primary = _first(
        item,
        "text",
        "proposition",
        "description",
        "summary",
        "source_excerpt",
        "excerpt",
        "fact",
        "question",
        "issue",
        "note",
        "rationale",
    )
    if primary is not None:
        return _scalar(primary)
    omitted = {key for key, _ in _LOCATOR_LABELS}
    omitted.update({"source", "locator", "document", "filing", "page"})
    remainder = {k: v for k, v in item.items() if k not in omitted}
    if not remainder:
        return "Evidence item"
    return "; ".join(
        f"{str(key).replace('_', ' ')}: {_scalar(value)}"
        for key, value in remainder.items()
    )


def _render_items(value: Any) -> list[str]:
    items = _as_items(value)
    if not items:
        return [_NONE]
    lines: list[str] = []
    for item in items:
        text = _item_text(item)
        locator = _locators(item)
        lines.append(f"- {text}" + (f"  \n  **Source:** {locator}" if locator else ""))
    return lines


def _render_propositions(value: Any) -> list[str]:
    items = _as_items(value)
    if not items:
        return [_NONE]
    lines: list[str] = []
    metadata = (
        ("classification", "Classification"),
        ("confidence", "Proposition confidence"),
        ("rationale", "Rationale"),
        ("polarity", "Polarity"),
        ("source_excerpt", "Source excerpt"),
    )
    for item in items:
        if not isinstance(item, Mapping):
            lines.append(f"- {_scalar(item)}")
            continue
        lines.append(f"- {_item_text(item)}")
        for key, label in metadata:
            value = item.get(key)
            if value not in (None, "", [], {}):
                lines.append(f"  - **{label}:** {_scalar(value)}")
        locator = _locators(item)
        if locator:
            lines.append(f"  - **Source:** {locator}")
    return lines


def _render_mapping(mapping: Any) -> list[str]:
    if not isinstance(mapping, Mapping) or not mapping:
        return [_NONE]
    lines: list[str] = []
    for key, value in mapping.items():
        label = str(key).replace("_", " ").strip().capitalize()
        if isinstance(value, list):
            rendered = ", ".join(_scalar(v) for v in value) if value else _NONE
        elif isinstance(value, Mapping):
            rendered = "; ".join(
                f"{str(k).replace('_', ' ')}: {_scalar(v)}" for k, v in value.items()
            ) or _NONE
        else:
            rendered = _scalar(value) if value not in (None, "") else _NONE
        lines.append(f"- **{label}:** {rendered}")
    return lines


def _question_evaluation(
    evaluation: Mapping[str, Any], question_id: Optional[str]
) -> Mapping[str, Any]:
    for record in _as_items(evaluation.get("questions")):
        if isinstance(record, Mapping) and (
            question_id is None or record.get("question_id") == question_id
        ):
            return record
    return {}


def render_attorney_review_packet(
    candidate: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    *,
    generation: Optional[Mapping[str, Any]] = None,
) -> str:
    """Render a review packet solely from existing candidate/evaluation data."""
    generation = generation or {}
    question_id = candidate.get("question_id")
    evaluated = _question_evaluation(evaluation, str(question_id) if question_id else None)
    diagnostics = evaluated.get("candidate_vs_reference_diagnostics") or {}
    reference = evaluated.get("reference_answer_status") or {}
    flags = evaluated.get("flags") or {}
    confidence = candidate.get("confidence")
    attorney_review = (
        candidate.get("attorney_review")
        if isinstance(candidate.get("attorney_review"), Mapping)
        else {}
    )
    limitations = _first(
        candidate,
        "limitations",
        "review_scope",
    )
    if limitations is None:
        limitations = attorney_review.get("limitations")
    requires_attorney_review = bool(
        attorney_review.get("requires_attorney_review", True)
        or flags.get("requires_attorney_review", True)
    )

    lines = [
        "# Case-00 Attorney Cognition Review Packet v1",
        "",
        "> **Status: CANDIDATE — NOT ATTORNEY-APPROVED.** This packet consolidates",
        "> existing generation and evaluation artifacts for attorney review. It does",
        "> not replace independent record, evidentiary, legal, or procedural review.",
        "",
        "## 1. Case / Question Identity and Generation Provenance",
        "",
        f"- **Case / corpus:** {_scalar(evaluation.get('corpus_id') or 'Case-00')}",
        f"- **Question ID:** {_scalar(question_id or 'Not available')}",
        f"- **Candidate status:** {_scalar(candidate.get('status') or 'candidate')}",
        f"- **Attorney approved:** No",
        f"- **Generation commit:** {_scalar(candidate.get('generation_commit') or generation.get('required_commit') or 'Not available')}",
        f"- **Generated at:** {_scalar(candidate.get('generated_at') or 'Not available')}",
        f"- **Model:** {_scalar(candidate.get('model') or 'Not available')}",
        f"- **Provider:** {_scalar(candidate.get('provider') or 'Not available')}",
        f"- **Candidate SHA-256:** {_scalar(candidate.get('candidate_sha256') or 'Not available')}",
        f"- **Benchmark / packet:** {_scalar(evaluation.get('benchmark_id') or 'Not available')} / {_scalar(evaluation.get('packet_id') or 'Not available')}",
        "",
        "## 2. Question",
        "",
        _scalar(candidate.get("question_text") or evaluated.get("question_text") or _NONE),
        "",
        "## 3. Proposed Answer",
        "",
        _scalar(candidate.get("proposed_answer") or evaluated.get("evaluated_answer") or _NONE),
        "",
        "## 4. Material Propositions",
        "",
        *_render_propositions(candidate.get("propositions")),
        "",
        "## 5. Supporting Evidence",
        "",
        *_render_items(candidate.get("supporting_evidence")),
        "",
        "### Documents / Pages Reviewed",
        "",
        *_render_items(candidate.get("documents_pages_reviewed")),
        "",
        "## 6. Contrary Evidence and Contradictions",
        "",
        *_render_items(candidate.get("contrary_evidence")),
        "",
        "## 7. Unresolved Factual, Evidentiary, or Procedural Questions",
        "",
        *_render_items(candidate.get("unresolved_questions")),
        "",
        "## 8. Candidate-vs-Reference Diagnostics",
        "",
        f"- **Reference status:** {_scalar(reference.get('status') or diagnostics.get('reference_status') or 'Not available')}",
        f"- **Comparison performed:** {_scalar(bool(diagnostics.get('comparison_performed')))}",
        f"- **Method:** {_scalar(diagnostics.get('method') or 'Not available')}",
        f"- **Diagnostic note:** {_scalar(diagnostics.get('note') or _NONE)}",
        "",
        "### Missing material facts",
        "",
        *_render_items(diagnostics.get("missing_material_facts")),
        "",
        "### Unsupported or extra assertions",
        "",
        *_render_items(diagnostics.get("unsupported_or_extra_assertions")),
        "",
        "### Party / role mismatches",
        "",
        *_render_items(diagnostics.get("party_role_mismatches")),
        "",
        "### Citation / evidence coverage",
        "",
        *_render_mapping(diagnostics.get("citation_evidence_coverage")),
        "",
        "## 9. Confidence, Limitations, and Attorney-Review Scope",
        "",
        f"- **Candidate confidence:** {_scalar(confidence) if confidence is not None else 'Not available'}",
        f"- **Requires attorney review:** {_scalar(requires_attorney_review)}",
        f"- **Review scope / limitations:** {_scalar(limitations) if limitations not in (None, '') else _NONE}",
        f"- **Attorney review notes:** {_scalar(attorney_review.get('review_notes')) if attorney_review.get('review_notes') not in (None, '') else _NONE}",
        f"- **Legal conclusions labeled:** {_scalar(attorney_review.get('legal_conclusions_labeled')) if attorney_review.get('legal_conclusions_labeled') is not None else 'Not available'}",
        f"- **Coverage conclusion:** {_scalar(attorney_review.get('coverage_conclusion')) if attorney_review.get('coverage_conclusion') not in (None, '') else _NONE}",
        "- **Boundary:** Provisional reference material, if present, is not attorney-approved.",
        "- **Scope:** Verify the answer against the cited record, contrary evidence, unresolved issues, and applicable procedural posture before relying on it.",
        "",
        "## 10. Attorney Decision Checklist",
        "",
        "- [ ] Accept",
        "- [ ] Revise",
        "- [ ] Reject",
        "- [ ] Investigate further",
        "- **Notes:**",
        "",
        "  ",
        "",
    ]
    return "\n".join(lines)


def write_attorney_review_packet(
    candidate_path: Path | str,
    evaluation: Mapping[str, Any],
    *,
    output_path: Path | str | None = None,
    generation: Optional[Mapping[str, Any]] = None,
) -> Path:
    """Load the candidate JSON and write the consolidated packet."""
    candidate_path = Path(candidate_path)
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    destination = (
        Path(output_path)
        if output_path is not None
        else candidate_path.parent / PACKET_FILENAME
    )
    destination.write_text(
        render_attorney_review_packet(candidate, evaluation, generation=generation),
        encoding="utf-8",
    )
    return destination.resolve()
