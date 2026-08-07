#!/usr/bin/env python3
"""Case-00 B2 rebuild + attorney-feedback generation single-shot runner.

Composes existing ``scripts/rebuild_case00_derived.py`` (B2 source mode) and
``scripts/generate_attorney_feedback_candidate.py`` in one process so ephemeral
derived artifacts are consumed immediately after a validated rebuild.

Does not duplicate rebuild or generation logic. Does not weaken authorization
or generation-only gates. Never prints B2 credentials or other secrets.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Same acknowledgement required by generate_attorney_feedback_candidate.py.
AUTHORIZATION_ACK = "I_AUTHORIZE_PRIVATE_EVIDENCE_TRANSMISSION_TO_MODEL_PROVIDER"

_SECRET_DETAIL_KEYS = frozenset(
    {
        "key_id",
        "application_key",
        "secret",
        "b2_key_id",
        "b2_application_key",
        "aws_access_key_id",
        "aws_secret_access_key",
        "authorization_acknowledgement",
    }
)


class RunnerError(Exception):
    """Machine-readable single-shot runner failure."""

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        code: str,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.phase = phase
        self.code = code
        self.details = details


def _load_script_module(module_name: str, filename: str) -> Any:
    path = REPO_ROOT / "scripts" / filename
    if not path.is_file():
        raise RunnerError(
            f"Required companion script missing: {path}",
            phase="bootstrap",
            code="COMPANION_SCRIPT_MISSING",
            path=str(path),
        )
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RunnerError(
            f"Unable to load companion script: {path}",
            phase="bootstrap",
            code="COMPANION_SCRIPT_LOAD_FAILED",
            path=str(path),
        )
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass / circular imports resolve.
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_rebuild_module() -> Any:
    return _load_script_module("rebuild_case00_derived", "rebuild_case00_derived.py")


def load_generation_module() -> Any:
    return _load_script_module(
        "generate_attorney_feedback_candidate",
        "generate_attorney_feedback_candidate.py",
    )


def sanitize_for_log(value: Any) -> Any:
    """Recursively drop secret-bearing keys from machine-readable payloads."""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in _SECRET_DETAIL_KEYS:
                continue
            out[str(key)] = sanitize_for_log(item)
        return out
    if isinstance(value, list):
        return [sanitize_for_log(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_for_log(item) for item in value]
    return value


def _assert_runner_gates(
    *,
    authorization_acknowledgement: str,
    generation_only: bool,
) -> None:
    """Fail closed with the same gates the generator enforces (before rebuild)."""
    if authorization_acknowledgement != AUTHORIZATION_ACK:
        raise RunnerError(
            "Refusing to transmit private evidence without explicit authorization "
            f"acknowledgement ({AUTHORIZATION_ACK})",
            phase="preflight",
            code="AUTHORIZATION_REQUIRED",
            authorization_acknowledgement=authorization_acknowledgement,
        )
    if not generation_only:
        raise RunnerError(
            "CLI is generation-only; pass --generation-only",
            phase="preflight",
            code="GENERATION_ONLY_REQUIRED",
            generation_only=generation_only,
        )


def run_rebuild_phase(
    *,
    case_root: Path,
    b2_prefix: Optional[str] = None,
    inventory_path: Optional[Path] = None,
    rebuild_mod: Optional[Any] = None,
    b2_client: Optional[Any] = None,
    b2_config: Optional[Any] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Rebuild Case-00 derived artifacts from B2; require validation ok."""
    rebuild = rebuild_mod if rebuild_mod is not None else load_rebuild_module()
    prefix = (
        b2_prefix
        if b2_prefix is not None
        else rebuild.DEFAULT_CASE00_B2_PREFIX
    )
    try:
        result = rebuild.rebuild_case00_derived(
            case_root=Path(case_root),
            source_dir=None,
            b2_prefix=prefix,
            inventory_path=inventory_path,
            b2_client=b2_client,
            b2_config=b2_config,
            environ=environ,
            validate=True,
        )
    except rebuild.RebuildError as exc:
        raise RunnerError(
            exc.message,
            phase="rebuild",
            code="REBUILD_FAILED",
            **sanitize_for_log(exc.details or {}),
        ) from exc

    if not isinstance(result, dict) or not result.get("ok"):
        raise RunnerError(
            "Rebuild phase failed or returned non-ok result",
            phase="rebuild",
            code="REBUILD_FAILED",
            result=sanitize_for_log(result if isinstance(result, dict) else {}),
        )

    validation = result.get("validation") or {}
    if not validation.get("ok", True):
        raise RunnerError(
            "Rebuild wrote artifacts but validation failed",
            phase="rebuild",
            code="REBUILD_VALIDATION_FAILED",
            validation=sanitize_for_log(validation),
        )
    return result


def run_generation_phase(
    *,
    case_root: Path,
    question_id: str,
    required_commit: str,
    candidate_output_root: Path,
    authorization_acknowledgement: str,
    generation_only: bool,
    repo_root: Optional[Path] = None,
    inventory_path: Optional[Path] = None,
    gen_mod: Optional[Any] = None,
    model_call: Optional[Any] = None,
    skip_commit_check: bool = False,
) -> dict[str, Any]:
    """Invoke the existing attorney-feedback generator against case-root."""
    gen = gen_mod if gen_mod is not None else load_generation_module()
    try:
        result = gen.run_generation(
            case_root=Path(case_root),
            question_id=question_id,
            required_commit=required_commit,
            candidate_output_root=Path(candidate_output_root),
            authorization_acknowledgement=authorization_acknowledgement,
            generation_only=generation_only,
            repo_root=repo_root,
            inventory_path=inventory_path,
            model_call=model_call,
            skip_commit_check=skip_commit_check,
        )
    except gen.GenerationError as exc:
        raise RunnerError(
            exc.blocker,
            phase="generation",
            code="GENERATION_FAILED",
            **sanitize_for_log(exc.details or {}),
        ) from exc

    if not isinstance(result, dict) or not result.get("ok"):
        raise RunnerError(
            "Generation phase failed or returned non-ok result",
            phase="generation",
            code="GENERATION_FAILED",
            result=sanitize_for_log(result if isinstance(result, dict) else {}),
        )
    return result


def run_case00_b2_q1(
    *,
    case_root: Path,
    question_id: str,
    required_commit: str,
    candidate_output_root: Path,
    authorization_acknowledgement: str,
    generation_only: bool,
    b2_prefix: Optional[str] = None,
    repo_root: Optional[Path] = None,
    inventory_path: Optional[Path] = None,
    rebuild_mod: Optional[Any] = None,
    gen_mod: Optional[Any] = None,
    b2_client: Optional[Any] = None,
    b2_config: Optional[Any] = None,
    environ: Optional[Mapping[str, str]] = None,
    model_call: Optional[Any] = None,
    skip_commit_check: bool = False,
) -> dict[str, Any]:
    """Rebuild from B2, then generate; return a concise phase/result summary."""
    _assert_runner_gates(
        authorization_acknowledgement=authorization_acknowledgement,
        generation_only=generation_only,
    )

    rebuild_result = run_rebuild_phase(
        case_root=case_root,
        b2_prefix=b2_prefix,
        inventory_path=inventory_path,
        rebuild_mod=rebuild_mod,
        b2_client=b2_client,
        b2_config=b2_config,
        environ=environ,
    )

    generation_result = run_generation_phase(
        case_root=case_root,
        question_id=question_id,
        required_commit=required_commit,
        candidate_output_root=candidate_output_root,
        authorization_acknowledgement=authorization_acknowledgement,
        generation_only=generation_only,
        repo_root=repo_root,
        inventory_path=inventory_path,
        gen_mod=gen_mod,
        model_call=model_call,
        skip_commit_check=skip_commit_check,
    )

    return sanitize_for_log(
        {
            "ok": True,
            "source_mode": "b2",
            "question_id": question_id,
            "required_commit": required_commit,
            "case_root": str(Path(case_root).resolve()),
            "phases": [
                {
                    "phase": "rebuild",
                    "ok": True,
                    "result": {
                        "ok": rebuild_result.get("ok"),
                        "document_count": rebuild_result.get("document_count"),
                        "page_count": rebuild_result.get("page_count"),
                        "filing_count": rebuild_result.get("filing_count"),
                        "written": rebuild_result.get("written"),
                        "validation_ok": (rebuild_result.get("validation") or {}).get(
                            "ok"
                        ),
                    },
                },
                {
                    "phase": "generation",
                    "ok": True,
                    "result": {
                        "ok": generation_result.get("ok"),
                        "finalized": generation_result.get("finalized"),
                        "candidate_directory": generation_result.get(
                            "candidate_directory"
                        ),
                        "files": generation_result.get("files"),
                        "reasoner_status": generation_result.get("reasoner_status"),
                        "provider_calls": generation_result.get("provider_calls"),
                    },
                },
            ],
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_case00_b2_q1",
        description=(
            "Rebuild Case-00 derived artifacts from B2, then run attorney-feedback "
            "candidate generation in the same process (generation-only)."
        ),
    )
    parser.add_argument(
        "--case-root",
        type=Path,
        required=True,
        help="Case-00 corpus root (contains derived/ and inventory).",
    )
    parser.add_argument(
        "--question-id",
        required=True,
        help="Question identifier (for example Q1).",
    )
    parser.add_argument(
        "--required-commit",
        required=True,
        help="Repository commit that HEAD and origin/main must equal.",
    )
    parser.add_argument(
        "--candidate-output-root",
        type=Path,
        required=True,
        help="Directory under which a new timestamped candidate folder is created.",
    )
    parser.add_argument(
        "--authorize-private-evidence-transmission",
        required=True,
        dest="authorization_acknowledgement",
        help=(
            "Explicit acknowledgement string required before private evidence may "
            f"be sent to a model provider. Must equal: {AUTHORIZATION_ACK}"
        ),
    )
    parser.add_argument(
        "--generation-only",
        action="store_true",
        required=True,
        help="Required. Restricts the CLI to generation (no evaluation).",
    )
    parser.add_argument(
        "--b2-prefix",
        nargs="?",
        const="",
        default=None,
        help=(
            "Optional B2 object prefix override. If omitted, uses the rebuild "
            "CLI default Case-00 prefix. If flag is present without a value, "
            "also uses that default."
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root for commit preflight (default: inferred).",
    )
    parser.add_argument(
        "--inventory-path",
        type=Path,
        default=None,
        help="Optional explicit NYSCEF inventory path.",
    )
    parser.add_argument(
        "--skip-commit-check",
        action="store_true",
        help=argparse.SUPPRESS,  # test-only escape hatch
    )
    return parser


def _cli_b2_prefix(raw: Optional[str]) -> Optional[str]:
    """Map CLI ``--b2-prefix`` forms to ``run_rebuild_phase`` input.

    ``None`` / empty means ``run_rebuild_phase`` should use the rebuild CLI
    default Case-00 prefix (resolved only after preflight gates pass).
    """
    if raw is None or raw == "":
        return None
    return raw


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        # Do not import companion scripts until after argparse + gate checks
        # inside run_case00_b2_q1 (avoids matter_builder import on auth failure).
        result = run_case00_b2_q1(
            case_root=args.case_root,
            question_id=args.question_id,
            required_commit=args.required_commit,
            candidate_output_root=args.candidate_output_root,
            authorization_acknowledgement=args.authorization_acknowledgement,
            generation_only=bool(args.generation_only),
            b2_prefix=_cli_b2_prefix(args.b2_prefix),
            repo_root=args.repo_root,
            inventory_path=args.inventory_path,
            skip_commit_check=bool(args.skip_commit_check),
        )
    except RunnerError as exc:
        payload = sanitize_for_log(
            {
                "ok": False,
                "failed_phase": exc.phase,
                "error": exc.message,
                "code": exc.code,
                "phases": [
                    {
                        "phase": exc.phase,
                        "ok": False,
                        "error": exc.message,
                        "code": exc.code,
                    }
                ],
                **exc.details,
            }
        )
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return 1
    except Exception as exc:  # noqa: BLE001
        payload = {
            "ok": False,
            "failed_phase": "runner",
            "error": f"{type(exc).__name__}: {exc}",
            "code": "RUNNER_UNEXPECTED",
            "phases": [
                {
                    "phase": "runner",
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "code": "RUNNER_UNEXPECTED",
                }
            ],
        }
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return 1

    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
