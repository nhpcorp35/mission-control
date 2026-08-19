#!/usr/bin/env python3
"""CLI for Case-00 Triborough attorney-feedback evaluation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from case00_attorney_eval.evaluate import (
    ANSWER_VERSION_CANDIDATE,
    ANSWER_VERSION_ORIGINAL,
    evaluate_case00,
    format_human_summary,
    write_evaluation_outputs,
)
from case00_attorney_eval import paths as pathmod


class EvaluatorCLIError(Exception):
    """Machine-readable evaluator CLI failure."""

    def __init__(self, message: str, *, code: str = "EVALUATOR_CLI_ERROR", **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details


def _load_candidates_from_file(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise EvaluatorCLIError(
            "Candidate answers file must be a JSON object of QID -> text",
            code="INVALID_CANDIDATE_FILE",
            path=str(path),
        )
    # Support either flat QID->text or a single candidate artifact.
    if "proposed_answer" in data and "question_id" in data:
        qid = str(data["question_id"])
        return {qid: str(data.get("proposed_answer") or "")}
    out: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, dict) and "proposed_answer" in value:
            out[str(key)] = str(value.get("proposed_answer") or "")
        else:
            out[str(key)] = str(value)
    return out


def load_candidates_from_directory(directory: Path) -> dict[str, str]:
    """Load candidate answers from a generation output directory."""
    if not directory.is_dir():
        raise EvaluatorCLIError(
            f"Candidate directory not found: {directory}",
            code="CANDIDATE_DIR_MISSING",
            path=str(directory),
        )
    found: dict[str, str] = {}
    patterns = (
        "*_candidate_answer.json",
        "Q*_candidate_answer.json",
    )
    paths: list[Path] = []
    for pattern in patterns:
        paths.extend(directory.glob(pattern))
    # Also search one level of timestamped subdirectories.
    for child in sorted(directory.iterdir()):
        if child.is_dir():
            for pattern in patterns:
                paths.extend(child.glob(pattern))
    for path in sorted(set(paths)):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        qid = data.get("question_id")
        answer = data.get("proposed_answer")
        if qid and isinstance(answer, str):
            found[str(qid)] = answer
            continue
        # Filename fallback: Q1_candidate_answer.json
        stem = path.name
        if stem.endswith("_candidate_answer.json"):
            qid = stem[: -len("_candidate_answer.json")]
            if answer is not None:
                found[qid] = str(answer)
    if not found:
        raise EvaluatorCLIError(
            f"No candidate answer JSON found under {directory}",
            code="CANDIDATE_DIR_EMPTY",
            path=str(directory),
        )
    return found


def _load_candidates(
    *,
    candidate_answers: Path | None,
    candidate_dir: Path | None,
) -> dict[str, str]:
    if candidate_answers is not None and candidate_dir is not None:
        raise EvaluatorCLIError(
            "Pass only one of --candidate-answers or --candidate-dir",
            code="CONFLICTING_CANDIDATE_INPUTS",
        )
    if candidate_answers is not None:
        return _load_candidates_from_file(candidate_answers)
    if candidate_dir is not None:
        return load_candidates_from_directory(candidate_dir)
    return {}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="case00_attorney_eval",
        description=(
            "Run Case-00 Triborough attorney-feedback evaluation against "
            "existing gold-label / packet / provisional artifacts. Emits "
            "deterministic candidate-vs-reference diagnostics without "
            "fabricating numeric scores."
        ),
    )
    p.add_argument(
        "--case-root",
        "--case00-root",
        dest="case_root",
        type=Path,
        default=None,
        help=(
            "Case-00 corpus root (default: $CASE00_TRIBOROUGH_ROOT or "
            "/app/data/case-00-triborough)."
        ),
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Output directory for machine-readable JSON and human summary "
            f"(default: $CASE00_ATTORNEY_EVAL_OUT or "
            f"{pathmod.DEFAULT_VOLUME_ROOT}/derived/attorney-feedback-eval)."
        ),
    )
    p.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Explicit path for the evaluation JSON artifact.",
    )
    p.add_argument(
        "--summary-out",
        type=Path,
        default=None,
        help="Explicit path for the human-readable summary text artifact.",
    )
    p.add_argument(
        "--candidate-answers",
        type=Path,
        default=None,
        help=(
            "Optional JSON object mapping question_id -> new LegalAI answer text, "
            "or a single candidate artifact with proposed_answer. "
            "Original answers remain preserved in the evaluation record."
        ),
    )
    p.add_argument(
        "--candidate-dir",
        "--candidate-directory",
        dest="candidate_dir",
        type=Path,
        default=None,
        help=(
            "Optional directory containing *_candidate_answer.json artifacts "
            "(generation output folder or its parent)."
        ),
    )
    p.add_argument(
        "--question-id",
        action="append",
        dest="question_ids",
        default=None,
        help=(
            "Limit evaluation to one or more question IDs (repeatable). "
            "Default: all questions with original LegalAI answers."
        ),
    )
    p.add_argument(
        "--answer-version",
        choices=(ANSWER_VERSION_ORIGINAL, ANSWER_VERSION_CANDIDATE),
        default=ANSWER_VERSION_ORIGINAL,
        help="Which answer version label to apply when no candidates are supplied.",
    )
    p.add_argument(
        "--stdout-json",
        action="store_true",
        help="Also print the full JSON result to stdout.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        candidates = _load_candidates(
            candidate_answers=args.candidate_answers,
            candidate_dir=args.candidate_dir,
        )
        result = evaluate_case00(
            args.case_root,
            candidate_answers=candidates or None,
            answer_version=args.answer_version,
            question_ids=args.question_ids,
        )
        paths = write_evaluation_outputs(
            result,
            args.out,
            json_path=args.json_out,
            summary_path=args.summary_out,
        )
    except EvaluatorCLIError as exc:
        payload = {
            "ok": False,
            "error": exc.message,
            "code": exc.code,
            **exc.details,
        }
        sys.stderr.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        return 2
    except FileNotFoundError as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "code": "CASE00_ARTIFACTS_MISSING",
        }
        sys.stderr.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        return 2
    except Exception as exc:  # noqa: BLE001
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "code": "EVALUATOR_UNEXPECTED",
        }
        sys.stderr.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        return 1

    summary = format_human_summary(result)
    sys.stdout.write(summary)
    sys.stdout.write(f"\nWrote JSON: {paths['json']}\n")
    sys.stdout.write(f"Wrote summary: {paths['summary']}\n")
    if args.stdout_json:
        sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
