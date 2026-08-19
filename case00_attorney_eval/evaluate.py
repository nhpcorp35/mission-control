"""Case-00 attorney-feedback evaluation runner (core logic)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from case00_attorney_eval.adapter import (
    Case00BenchmarkCorpus,
    Case00QuestionBundle,
    load_case00_benchmark,
)
from case00_attorney_eval import paths as pathmod
from case00_attorney_eval.diagnostics import compare_candidate_to_reference
from case00_attorney_eval.scoring import SCORING_DIMENSIONS, score_dimensions

SCHEMA_VERSION = "case00_attorney_feedback_eval.v1"

ANSWER_VERSION_ORIGINAL = "original_legalai"
ANSWER_VERSION_CANDIDATE = "candidate_legalai"

REF_STATUS_NONE = "none"
REF_STATUS_PROVISIONAL = "provisional"
REF_STATUS_ATTORNEY_APPROVED = "attorney_approved"
REF_STATUS_PROVISIONAL_PLACEHOLDER = "provisional_placeholder"


def _feedback_fields(label: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not label:
        return {
            "available": False,
            "error_type": None,
            "primary_error_categories": None,
            "materiality": None,
            "corrective_evidence": None,
            "diagnostic_explanation": None,
            "attorney_verdict": None,
            "label_status": None,
            "locked": None,
            "safety_gate": None,
        }
    return {
        "available": True,
        "error_type": list(label.get("primary_error_categories") or []),
        "primary_error_categories": list(label.get("primary_error_categories") or []),
        "materiality": label.get("materiality"),
        "corrective_evidence": label.get("corrective_evidence_target"),
        "diagnostic_explanation": label.get("attorney_notes_summary"),
        "attorney_verdict": label.get("attorney_verdict"),
        "label_status": label.get("label_status"),
        "locked": label.get("locked"),
        "safety_gate": label.get("safety_gate"),
        "awaiting": label.get("awaiting"),
        "correctness_split": label.get("correctness_split"),
    }


def _has_complete_diagnostic_labels(label: Optional[dict[str, Any]]) -> bool:
    if not label:
        return False
    required = ("attorney_verdict", "materiality", "label_status", "attorney_notes_summary")
    if any(label.get(k) in (None, "") for k in required):
        return False
    # primary_error_categories must be present (may be empty list for correct answers).
    if "primary_error_categories" not in label:
        return False
    if not isinstance(label.get("primary_error_categories"), list):
        return False
    return True


def _reference_answer_status(bundle: Case00QuestionBundle) -> dict[str, Any]:
    """Strict distinction: provisional is never attorney-approved."""
    approved = bundle.attorney_approved
    provisional = bundle.provisional

    if approved is not None:
        return {
            "status": REF_STATUS_ATTORNEY_APPROVED,
            "provisional_treated_as_approved": False,
            "attorney_approved_path": str(approved.path),
            "provisional_path": str(provisional.path) if provisional else None,
            "provisional_status": provisional.status if provisional else None,
            "usable_reference": True,
            "reference_text_source": "attorney_approved",
        }

    if provisional is not None and provisional.is_placeholder:
        return {
            "status": REF_STATUS_PROVISIONAL_PLACEHOLDER,
            "provisional_treated_as_approved": False,
            "attorney_approved_path": None,
            "provisional_path": str(provisional.path),
            "provisional_status": provisional.status,
            "usable_reference": False,
            "reference_text_source": None,
            "note": (
                "Provisional file is a placeholder; not a usable corrected "
                "reference and not attorney-approved."
            ),
        }

    if provisional is not None and provisional.body:
        return {
            "status": REF_STATUS_PROVISIONAL,
            "provisional_treated_as_approved": False,
            "attorney_approved_path": None,
            "provisional_path": str(provisional.path),
            "provisional_status": provisional.status,
            "usable_reference": True,
            "reference_text_source": "provisional_corrected",
            "note": (
                "Provisional corrected answer loaded for evaluation targeting only; "
                "never treated as attorney-approved gold."
            ),
        }

    return {
        "status": REF_STATUS_NONE,
        "provisional_treated_as_approved": False,
        "attorney_approved_path": None,
        "provisional_path": str(provisional.path) if provisional else None,
        "provisional_status": provisional.status if provisional else None,
        "usable_reference": False,
        "reference_text_source": None,
    }


def _requires_attorney_review(
    bundle: Case00QuestionBundle,
    ref_info: dict[str, Any],
    review_status: dict[str, Any],
) -> bool:
    label = bundle.label_record or {}
    if label.get("label_status") != "final" or not label.get("locked"):
        return True
    if ref_info["status"] != REF_STATUS_ATTORNEY_APPROVED:
        return True
    if review_status.get("incomplete_overall_comment"):
        return True
    if review_status.get("packet_approved_by_attorney") is False:
        # Packet-level disapproval means gold answers are not signed off.
        return True
    return False


def _ready_for_automated_evaluation(
    bundle: Case00QuestionBundle,
    ref_info: dict[str, Any],
) -> bool:
    """Structural readiness: original + usable reference + complete labels.

    Does not imply numeric dimension scores are available.
    """
    if not bundle.original_legalai_answer:
        return False
    if not ref_info.get("usable_reference"):
        return False
    if not _has_complete_diagnostic_labels(bundle.label_record):
        return False
    return True


def evaluate_question(
    bundle: Case00QuestionBundle,
    *,
    review_status: dict[str, Any],
    answer_version: str = ANSWER_VERSION_ORIGINAL,
    candidate_answer: Optional[str] = None,
    preserve_original: bool = True,
) -> dict[str, Any]:
    """Build one per-question evaluation record.

    When ``candidate_answer`` is provided, the evaluated text is the candidate
    while the original LegalAI answer remains preserved under
    ``preserved_original_legalai_answer`` for later comparison.
    """
    ref_info = _reference_answer_status(bundle)
    feedback = _feedback_fields(bundle.label_record)

    if answer_version == ANSWER_VERSION_CANDIDATE or candidate_answer is not None:
        evaluated_version = ANSWER_VERSION_CANDIDATE
        evaluated_answer = candidate_answer
    else:
        evaluated_version = ANSWER_VERSION_ORIGINAL
        evaluated_answer = bundle.original_legalai_answer

    scoring = score_dimensions(
        bundle.label_record,
        has_reference_answer=bool(ref_info.get("usable_reference")),
        reference_status=(
            REF_STATUS_ATTORNEY_APPROVED
            if ref_info["status"] == REF_STATUS_ATTORNEY_APPROVED
            else REF_STATUS_PROVISIONAL
            if ref_info["status"] == REF_STATUS_PROVISIONAL
            else REF_STATUS_NONE
        ),
    )

    missing_info = list(scoring["missing_information"])
    if not bundle.original_legalai_answer:
        missing_info.append("original LegalAI answer missing")
    if ref_info["status"] == REF_STATUS_NONE:
        missing_info.append("no provisional or attorney-approved reference answer")
    if ref_info["status"] == REF_STATUS_PROVISIONAL_PLACEHOLDER:
        missing_info.append("provisional reference is placeholder / drafting blocked")
    if not bundle.attorney_approved:
        missing_info.append("final attorney-approved gold answer not present")
    if not _has_complete_diagnostic_labels(bundle.label_record):
        missing_info.append("incomplete diagnostic labels")
    if evaluated_version == ANSWER_VERSION_CANDIDATE and evaluated_answer is None:
        missing_info.append("candidate answer not provided")

    reference_payload = {
        "status": ref_info["status"],
        "provisional_treated_as_approved": False,
        "usable_reference": ref_info.get("usable_reference"),
        "reference_text_source": ref_info.get("reference_text_source"),
        "provisional": None,
        "attorney_approved": None,
    }
    if bundle.provisional is not None:
        reference_payload["provisional"] = {
            "status": bundle.provisional.status,
            "is_placeholder": bundle.provisional.is_placeholder,
            "path": str(bundle.provisional.path),
            "has_body": bool(bundle.provisional.body),
            # Include body only when not a placeholder; still labeled provisional.
            "text": bundle.provisional.body,
        }
    if bundle.attorney_approved is not None:
        reference_payload["attorney_approved"] = {
            "path": str(bundle.attorney_approved.path),
            "approval_marker": bundle.attorney_approved.approval_marker,
            "text": bundle.attorney_approved.body,
        }

    reference_text = None
    if ref_info.get("usable_reference"):
        if (
            ref_info.get("reference_text_source") == "attorney_approved"
            and bundle.attorney_approved is not None
        ):
            reference_text = bundle.attorney_approved.body
        elif bundle.provisional is not None and bundle.provisional.body:
            reference_text = bundle.provisional.body

    candidate_evidence = None
    # Structured evidence is optional; only present when callers attach it later.
    diagnostics = compare_candidate_to_reference(
        candidate_text=evaluated_answer,
        reference_text=reference_text,
        reference_status=ref_info["status"],
        reference_usable=bool(ref_info.get("usable_reference")),
        label_record=bundle.label_record,
        candidate_evidence=candidate_evidence,
    )

    record = {
        "question_id": bundle.question_id,
        "question_text": bundle.question_text,
        "answer_version_evaluated": evaluated_version,
        "evaluated_answer": evaluated_answer,
        "preserved_original_legalai_answer": (
            bundle.original_legalai_answer if preserve_original else None
        ),
        "original_preserved": bool(
            preserve_original and bundle.original_legalai_answer is not None
        ),
        "original_packet_meta": bundle.original_packet_meta,
        "reference_answer_status": reference_payload,
        "feedback_and_labels": feedback,
        "candidate_vs_reference_diagnostics": diagnostics,
        "scoring_dimensions_currently_evaluable": scoring["currently_evaluable"],
        "scoring": scoring["dimensions"],
        "missing_information_preventing_complete_scoring": missing_info,
        "flags": {
            "has_original_answer": bool(bundle.original_legalai_answer),
            "has_provisional_answer": bool(
                bundle.provisional
                and bundle.provisional.body
                and not bundle.provisional.is_placeholder
            ),
            "has_provisional_placeholder": bool(
                bundle.provisional and bundle.provisional.is_placeholder
            ),
            "has_final_attorney_approved_answer": bool(bundle.attorney_approved),
            "has_complete_diagnostic_labels": _has_complete_diagnostic_labels(
                bundle.label_record
            ),
            "ready_for_automated_evaluation": _ready_for_automated_evaluation(
                bundle, ref_info
            ),
            "requires_attorney_review": _requires_attorney_review(
                bundle, ref_info, review_status
            ),
            "provisional_silently_promoted": False,
            "fabricated_scores": False,
            "diagnostics_comparison_performed": bool(
                diagnostics.get("comparison_performed")
            ),
        },
    }
    return record


def _summary_counts(records: list[dict[str, Any]]) -> dict[str, Any]:
    def _ids(pred) -> list[str]:
        return [r["question_id"] for r in records if pred(r)]

    return {
        "total_questions": len(records),
        "questions_with_original_answers": len(
            _ids(lambda r: r["flags"]["has_original_answer"])
        ),
        "questions_with_provisional_answers": len(
            _ids(lambda r: r["flags"]["has_provisional_answer"])
        ),
        "questions_with_final_attorney_approved_answers": len(
            _ids(lambda r: r["flags"]["has_final_attorney_approved_answer"])
        ),
        "questions_with_complete_diagnostic_labels": len(
            _ids(lambda r: r["flags"]["has_complete_diagnostic_labels"])
        ),
        "questions_ready_for_automated_evaluation": len(
            _ids(lambda r: r["flags"]["ready_for_automated_evaluation"])
        ),
        "questions_still_requiring_attorney_review": len(
            _ids(lambda r: r["flags"]["requires_attorney_review"])
        ),
        "question_id_lists": {
            "with_original_answers": _ids(lambda r: r["flags"]["has_original_answer"]),
            "with_provisional_answers": _ids(
                lambda r: r["flags"]["has_provisional_answer"]
            ),
            "with_final_attorney_approved_answers": _ids(
                lambda r: r["flags"]["has_final_attorney_approved_answer"]
            ),
            "with_complete_diagnostic_labels": _ids(
                lambda r: r["flags"]["has_complete_diagnostic_labels"]
            ),
            "ready_for_automated_evaluation": _ids(
                lambda r: r["flags"]["ready_for_automated_evaluation"]
            ),
            "still_requiring_attorney_review": _ids(
                lambda r: r["flags"]["requires_attorney_review"]
            ),
        },
    }


def evaluate_case00(
    case00_root: Path | str | None = None,
    *,
    candidate_answers: Optional[dict[str, str]] = None,
    answer_version: str = ANSWER_VERSION_ORIGINAL,
    question_ids: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Run the Case-00 attorney-feedback evaluation loop."""
    corpus = load_case00_benchmark(case00_root)
    candidate_answers = candidate_answers or {}
    selected = set(question_ids) if question_ids else None

    # Only questions that have an original LegalAI answer (mission requirement).
    bundles = [b for b in corpus.questions if b.original_legalai_answer]
    if selected is not None:
        bundles = [b for b in bundles if b.question_id in selected]
    # Still include packets that list questions with empty answers? Mission:
    # "Loads every Case-00 question having an original LegalAI answer."
    # So filter to those with answers. Track skipped.
    skipped = [
        b.question_id
        for b in corpus.questions
        if not b.original_legalai_answer
    ]
    if selected is not None:
        skipped = [qid for qid in skipped if qid in selected]

    records: list[dict[str, Any]] = []
    for bundle in bundles:
        cand = candidate_answers.get(bundle.question_id)
        version = answer_version
        if cand is not None:
            version = ANSWER_VERSION_CANDIDATE
        records.append(
            evaluate_question(
                bundle,
                review_status=corpus.review_status,
                answer_version=version,
                candidate_answer=cand,
                preserve_original=True,
            )
        )

    summary = _summary_counts(records)
    # Reconcile total_questions with corpus: total evaluated with originals.
    # Also expose corpus-level total from labels/packet.
    summary["corpus_question_count"] = len(corpus.questions)
    summary["questions_skipped_missing_original_answer"] = skipped

    result = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "case00_attorney_feedback_evaluation",
        "corpus_id": corpus.corpus_id,
        "benchmark_id": corpus.benchmark_id,
        "packet_id": corpus.packet_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_paths": {
            "case00_root": str(corpus.case00_root),
            "labels": str(corpus.labels_path),
            "packet": str(corpus.packet_path),
            "provisional_answers": str(
                pathmod.provisional_answers_dir(corpus.case00_root)
            ),
            "attorney_approved_answers": str(
                pathmod.attorney_approved_answers_dir(corpus.case00_root)
            ),
        },
        "review_status": corpus.review_status,
        "rubric": {
            "name": (corpus.rubric or {}).get("name"),
            "dimensions": list(SCORING_DIMENSIONS),
            "scale": (corpus.rubric or {}).get("scale"),
            "scoring_note": (corpus.rubric or {}).get("scoring_note"),
            "packet_mean": (corpus.rubric or {}).get("packet_mean"),
            "packet_mean_status": (corpus.rubric or {}).get("packet_mean_status"),
        },
        "policy": {
            "provisional_never_treated_as_attorney_approved": True,
            "no_fabricated_scores": True,
            "no_fabricated_gold_answers": True,
            "original_answers_preserved_on_rerun": True,
            "llm_judge_disabled": True,
            "deterministic_candidate_reference_diagnostics": True,
        },
        "summary": summary,
        "questions": records,
        "missing_artifacts": corpus.missing_artifacts,
    }
    return result


def format_human_summary(result: dict[str, Any]) -> str:
    s = result["summary"]
    lines = [
        "Case-00 Triborough — Attorney-feedback evaluation summary",
        f"Generated: {result.get('generated_at')}",
        f"Benchmark: {result.get('benchmark_id')} | Packet: {result.get('packet_id')}",
        "",
        f"Total questions evaluated (with original LegalAI answers): "
        f"{s['total_questions']}",
        f"Questions with original answers: {s['questions_with_original_answers']}",
        f"Questions with provisional answers: {s['questions_with_provisional_answers']}",
        f"Questions with final attorney-approved answers: "
        f"{s['questions_with_final_attorney_approved_answers']}",
        f"Questions with complete diagnostic labels: "
        f"{s['questions_with_complete_diagnostic_labels']}",
        f"Questions ready for automated evaluation: "
        f"{s['questions_ready_for_automated_evaluation']}",
        f"Questions still requiring attorney review: "
        f"{s['questions_still_requiring_attorney_review']}",
        "",
        "Policy: provisional material is never treated as attorney-approved; "
        "scores are never fabricated.",
    ]
    ids = s.get("question_id_lists") or {}
    if ids.get("still_requiring_attorney_review"):
        lines.append(
            "Requires attorney review: "
            + ", ".join(ids["still_requiring_attorney_review"])
        )
    if ids.get("ready_for_automated_evaluation"):
        lines.append(
            "Ready for automated evaluation: "
            + ", ".join(ids["ready_for_automated_evaluation"])
        )
    # Per-question one-liners
    lines.append("")
    lines.append("Per-question:")
    for q in result.get("questions") or []:
        ref = (q.get("reference_answer_status") or {}).get("status")
        evaluable = q.get("scoring_dimensions_currently_evaluable") or []
        diag = q.get("candidate_vs_reference_diagnostics") or {}
        counts = diag.get("counts") or {}
        lines.append(
            f"  {q['question_id']}: version={q['answer_version_evaluated']}; "
            f"reference={ref}; "
            f"evaluable_dimensions={evaluable or 'none'}; "
            f"review_needed={q['flags']['requires_attorney_review']}; "
            f"diag_missing_facts={counts.get('missing_material_facts', 'n/a')}; "
            f"diag_party_mismatches={counts.get('party_role_mismatches', 'n/a')}"
        )
    return "\n".join(lines) + "\n"


def write_evaluation_outputs(
    result: dict[str, Any],
    output_dir: Path | str | None = None,
    *,
    json_path: Path | str | None = None,
    summary_path: Path | str | None = None,
) -> dict[str, Path]:
    if json_path is not None or summary_path is not None:
        json_out = Path(json_path) if json_path is not None else None
        summary_out = Path(summary_path) if summary_path is not None else None
        if json_out is None or summary_out is None:
            out = Path(output_dir) if output_dir else pathmod.default_output_dir(
                result["source_paths"]["case00_root"]
            )
            out.mkdir(parents=True, exist_ok=True)
            if json_out is None:
                json_out = out / "case00_attorney_feedback_eval.json"
            if summary_out is None:
                summary_out = out / "case00_attorney_feedback_eval_summary.txt"
        json_out.parent.mkdir(parents=True, exist_ok=True)
        summary_out.parent.mkdir(parents=True, exist_ok=True)
    else:
        out = Path(output_dir) if output_dir else pathmod.default_output_dir(
            result["source_paths"]["case00_root"]
        )
        out.mkdir(parents=True, exist_ok=True)
        json_out = out / "case00_attorney_feedback_eval.json"
        summary_out = out / "case00_attorney_feedback_eval_summary.txt"

    json_out.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    summary_out.write_text(format_human_summary(result), encoding="utf-8")
    return {"json": json_out, "summary": summary_out}
