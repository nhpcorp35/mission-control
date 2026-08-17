#!/usr/bin/env python3
"""One-command Case-00 generate-then-evaluate workflow.

Generates a candidate for a specified question and immediately runs the
Case-00 attorney evaluator, writing candidate artifacts plus evaluation JSON
and human summary into the same output directory.

Works in:
  - normal local git checkouts (``.git`` metadata)
  - Railway runtimes (``RAILWAY_GIT_COMMIT_SHA`` + related provenance env vars)

Does not require temporary branches, temporary clones, git update-ref, inline
Python, or manual path discovery.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from case00_attorney_eval.cli import (  # noqa: E402
    EvaluatorCLIError,
    load_candidates_from_directory,
)
from case00_attorney_eval.evaluate import (  # noqa: E402
    evaluate_case00,
    format_human_summary,
    write_evaluation_outputs,
)
from case00_attorney_eval.review_packet import (  # noqa: E402
    PACKET_FILENAME,
    write_attorney_review_packet,
)


def _load_generation_cli():
    path = REPO_ROOT / "scripts" / "generate_attorney_feedback_candidate.py"
    spec = importlib.util.spec_from_file_location(
        "generate_attorney_feedback_candidate", path
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


GEN = _load_generation_cli()


class WorkflowError(Exception):
    """Machine-readable workflow failure."""

    def __init__(self, message: str, *, code: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_case00_generate_and_evaluate",
        description=(
            "Generate one Case-00 attorney-feedback candidate and evaluate it "
            "in a single command. Candidate artifacts and evaluation outputs "
            "are written to the same run directory."
        ),
    )
    p.add_argument(
        "--case-root",
        type=Path,
        required=True,
        help="Case corpus root containing derived page/exhibit/case-map inputs.",
    )
    p.add_argument(
        "--question-id",
        required=True,
        help="Question identifier (for example Q1).",
    )
    p.add_argument(
        "--required-commit",
        required=True,
        help=(
            "Repository commit that must match checkout provenance "
            "(.git HEAD/origin/main, or Railway RAILWAY_GIT_* metadata)."
        ),
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help=(
            "Parent directory for this run. Generation creates a timestamped "
            "candidate subdirectory; evaluation artifacts are written there too."
        ),
    )
    p.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help=(
            "Alternate parent directory for the run (same behavior as "
            "--output-dir when you want an explicit path)."
        ),
    )
    p.add_argument(
        "--authorize-private-evidence-transmission",
        required=True,
        dest="authorization_acknowledgement",
        help=(
            "Explicit acknowledgement required before private evidence may be "
            f"sent to a model provider. Must equal: {GEN.AUTHORIZATION_ACK}"
        ),
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root for commit preflight (default: inferred).",
    )
    p.add_argument(
        "--inventory-path",
        type=Path,
        default=None,
        help="Optional explicit NYSCEF inventory path.",
    )
    p.add_argument(
        "--skip-commit-check",
        action="store_true",
        help=argparse.SUPPRESS,  # test-only escape hatch; not documented for ops use
    )
    return p


def run_workflow(
    *,
    case_root: Path,
    question_id: str,
    required_commit: str,
    output_dir: Path,
    authorization_acknowledgement: str,
    run_dir: Optional[Path] = None,
    repo_root: Optional[Path] = None,
    inventory_path: Optional[Path] = None,
    skip_commit_check: bool = False,
    model_call=None,
) -> dict[str, Any]:
    """Generate then evaluate; return machine-readable combined result."""
    # Generation always creates a timestamped subdirectory under
    # candidate_output_root. When --run-dir is supplied, use it as that root
    # and let generation create one child folder inside it; evaluation writes
    # into the same child folder.
    if run_dir is not None:
        candidate_output_root = Path(run_dir)
    else:
        candidate_output_root = Path(output_dir)
    candidate_output_root.mkdir(parents=True, exist_ok=True)

    try:
        gen_result = GEN.run_generation(
            case_root=Path(case_root),
            question_id=question_id,
            required_commit=required_commit,
            candidate_output_root=candidate_output_root,
            authorization_acknowledgement=authorization_acknowledgement,
            generation_only=True,
            repo_root=repo_root,
            inventory_path=inventory_path,
            skip_commit_check=skip_commit_check,
            model_call=model_call,
        )
    except GEN.GenerationError as exc:
        raise WorkflowError(
            exc.blocker,
            code="GENERATION_FAILED",
            **exc.details,
        ) from exc

    candidate_dir = Path(
        gen_result.get("candidate_directory")
        or candidate_output_root
    )
    files = gen_result.get("files") or {}
    if not candidate_dir.is_dir():
        for path_text in files.values():
            parent = Path(path_text).parent
            if parent.is_dir():
                candidate_dir = parent
                break

    try:
        candidates = load_candidates_from_directory(candidate_dir)
    except EvaluatorCLIError as exc:
        raise WorkflowError(exc.message, code=exc.code, **exc.details) from exc

    try:
        eval_result = evaluate_case00(
            Path(case_root),
            candidate_answers=candidates,
            question_ids=[question_id],
        )
        eval_paths = write_evaluation_outputs(
            eval_result,
            candidate_dir,
            json_path=candidate_dir / "case00_attorney_feedback_eval.json",
            summary_path=candidate_dir / "case00_attorney_feedback_eval_summary.txt",
        )
        candidate_json_path = next(
            (
                Path(path_text)
                for name, path_text in files.items()
                if name.endswith("_candidate_answer.json")
            ),
            None,
        )
        if candidate_json_path is None:
            raise WorkflowError(
                "Candidate JSON path missing from generation result",
                code="CANDIDATE_ARTIFACT_MISSING",
                generation=gen_result,
            )
        review_packet_path = write_attorney_review_packet(
            candidate_json_path,
            eval_result,
            output_path=candidate_dir / PACKET_FILENAME,
            generation=gen_result,
        )
    except WorkflowError:
        raise
    except FileNotFoundError as exc:
        raise WorkflowError(
            str(exc),
            code="CASE00_ARTIFACTS_MISSING",
            generation=gen_result,
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise WorkflowError(
            f"{type(exc).__name__}: {exc}",
            code="EVALUATION_FAILED",
            generation=gen_result,
        ) from exc

    workflow_files = dict(files)
    workflow_files[PACKET_FILENAME] = str(review_packet_path)
    gen_result = dict(gen_result)
    gen_result["files"] = workflow_files

    return {
        "ok": True,
        "question_id": question_id,
        "required_commit": required_commit,
        "run_dir": str(candidate_dir.resolve()),
        "attorney_review_packet": str(review_packet_path),
        "files": workflow_files,
        "generation": gen_result,
        "evaluation": {
            "json": str(eval_paths["json"]),
            "summary": str(eval_paths["summary"]),
            "review_packet": str(review_packet_path),
            "summary_text": format_human_summary(eval_result),
            "result": eval_result,
        },
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run_workflow(
            case_root=args.case_root,
            question_id=args.question_id,
            required_commit=args.required_commit,
            output_dir=args.output_dir,
            authorization_acknowledgement=args.authorization_acknowledgement,
            run_dir=args.run_dir,
            repo_root=args.repo_root,
            inventory_path=args.inventory_path,
            skip_commit_check=bool(args.skip_commit_check),
        )
    except WorkflowError as exc:
        payload = {
            "ok": False,
            "error": exc.message,
            "code": exc.code,
            **exc.details,
        }
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        return 2
    except Exception as exc:  # noqa: BLE001
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "code": "WORKFLOW_UNEXPECTED",
        }
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        return 1

    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
