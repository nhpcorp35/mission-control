#!/usr/bin/env python3
"""Durable attorney-feedback candidate generation CLI.

Thin orchestration over existing production retrieval / evidence-packet /
serialization / drafting / validation / bounded-repair / hashing helpers.
Does not call a live model unless the host process already has provider
credentials and an injectable model_call is not supplied.
Does not load gold, provisional, original answers, attorney feedback,
prior candidate prose, or evaluation artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import matter_builder as mb  # noqa: E402
from engines import drafting_engine as de  # noqa: E402

AUTHORIZATION_ACK = "I_AUTHORIZE_PRIVATE_EVIDENCE_TRANSMISSION_TO_MODEL_PROVIDER"

# Trusted Railway deployment metadata (present when .git is stripped at runtime).
RAILWAY_GIT_COMMIT_SHA = "RAILWAY_GIT_COMMIT_SHA"
RAILWAY_GIT_REPO_OWNER = "RAILWAY_GIT_REPO_OWNER"
RAILWAY_GIT_REPO_NAME = "RAILWAY_GIT_REPO_NAME"
RAILWAY_GIT_BRANCH = "RAILWAY_GIT_BRANCH"
RAILWAY_PROVENANCE_ENV_VARS = (
    RAILWAY_GIT_COMMIT_SHA,
    RAILWAY_GIT_REPO_OWNER,
    RAILWAY_GIT_REPO_NAME,
    RAILWAY_GIT_BRANCH,
)

# Expected repository identity for Railway provenance checks.
EXPECTED_REPO_OWNER = "nhpcorp35"
EXPECTED_REPO_NAME = "legal-ai"
EXPECTED_REPO_BRANCH = "main"

# Path substrings that must never be opened as generation inputs.
_PROTECTED_PATH_MARKERS = (
    "attorney-gold-benchmark",
    "provisional-gold-answers",
    "attorney-approved-gold-answers",
    "attorney_gold_labels",
    "attorney-feedback-eval/",
    "case00_attorney_feedback_eval",
    "candidate-answers/",
    "candidates/eval_",
)

ModelCall = Callable[[str, str], Any]


class GenerationError(Exception):
    """Machine-readable generation failure."""

    def __init__(self, blocker: str, **details: Any) -> None:
        super().__init__(blocker)
        self.blocker = blocker
        self.details = details


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _normalize_ref_names(ref_name: str) -> tuple[str, set[str]]:
    """Return (path under .git/refs/, packed-refs name candidates)."""
    cleaned = ref_name.strip()
    if cleaned.startswith("refs/"):
        under_refs = cleaned[len("refs/") :]
        packed = {cleaned}
    else:
        under_refs = cleaned
        packed = {cleaned, f"refs/{cleaned}"}
    return under_refs, packed


def _read_git_ref(repo_root: Path, ref_name: str) -> Optional[str]:
    """Read a git ref from the filesystem (no git subprocess).

    Loose refs live under ``.git/refs/...`` (never directly under ``.git/``).
    Packed refs are matched by full ``refs/...`` name.
    """
    under_refs, packed_names = _normalize_ref_names(ref_name)
    ref_path = repo_root / ".git" / "refs" / under_refs
    if ref_path.is_file():
        return ref_path.read_text(encoding="utf-8").strip()
    packed = repo_root / ".git" / "packed-refs"
    if not packed.is_file():
        return None
    for line in packed.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("^"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[-1] in packed_names:
            return parts[0].strip()
    return None


def read_checked_out_commit(repo_root: Path) -> Optional[str]:
    head_path = repo_root / ".git" / "HEAD"
    if not head_path.is_file():
        return None
    head = head_path.read_text(encoding="utf-8").strip()
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        # Prefer the full refs/... path; also try without the refs/ prefix.
        return _read_git_ref(repo_root, ref) or (
            _read_git_ref(repo_root, ref[len("refs/") :])
            if ref.startswith("refs/")
            else None
        )
    return head or None


def read_origin_main_commit(repo_root: Path) -> Optional[str]:
    return _read_git_ref(repo_root, "refs/remotes/origin/main") or _read_git_ref(
        repo_root, "remotes/origin/main"
    )


def git_metadata_available(repo_root: Path) -> bool:
    """True when a usable ``.git`` directory is present."""
    git_dir = repo_root / ".git"
    return git_dir.is_dir() or git_dir.is_file()


def read_railway_deployment_provenance() -> dict[str, Optional[str]]:
    """Read trusted Railway git provenance env vars (may be incomplete)."""
    return {
        "commit": (os.environ.get(RAILWAY_GIT_COMMIT_SHA) or "").strip() or None,
        "owner": (os.environ.get(RAILWAY_GIT_REPO_OWNER) or "").strip() or None,
        "name": (os.environ.get(RAILWAY_GIT_REPO_NAME) or "").strip() or None,
        "branch": (os.environ.get(RAILWAY_GIT_BRANCH) or "").strip() or None,
    }


def _normalize_branch_name(branch: str) -> str:
    value = branch.strip()
    if value.startswith("refs/heads/"):
        return value[len("refs/heads/") :]
    return value


def assert_railway_provenance_matches(
    required_commit: str,
    *,
    expected_owner: str = EXPECTED_REPO_OWNER,
    expected_name: str = EXPECTED_REPO_NAME,
    expected_branch: str = EXPECTED_REPO_BRANCH,
) -> dict:
    """Fail-closed validation of Railway deployment metadata."""
    provenance = read_railway_deployment_provenance()
    missing = [
        name
        for name, key in (
            (RAILWAY_GIT_COMMIT_SHA, "commit"),
            (RAILWAY_GIT_REPO_OWNER, "owner"),
            (RAILWAY_GIT_REPO_NAME, "name"),
            (RAILWAY_GIT_BRANCH, "branch"),
        )
        if not provenance.get(key)
    ]
    if missing:
        raise GenerationError(
            "Commit provenance missing: .git unavailable and Railway deployment "
            f"metadata incomplete (missing {', '.join(missing)})",
            required_commit=required_commit,
            railway_provenance=provenance,
            missing_env=missing,
            provenance_source="railway_deployment_metadata",
        )

    commit = provenance["commit"]
    owner = provenance["owner"]
    name = provenance["name"]
    branch = _normalize_branch_name(provenance["branch"] or "")

    if commit != required_commit:
        raise GenerationError(
            "Railway deployment commit does not match required commit "
            f"{required_commit}; RAILWAY_GIT_COMMIT_SHA={commit!r}",
            checkout_commit=commit,
            origin_main_commit=commit,
            required_commit=required_commit,
            railway_provenance=provenance,
            provenance_source="railway_deployment_metadata",
        )
    if (owner or "").lower() != expected_owner.lower():
        raise GenerationError(
            "Railway deployment repository owner mismatch: "
            f"expected {expected_owner!r}, got {owner!r}",
            checkout_commit=commit,
            required_commit=required_commit,
            railway_provenance=provenance,
            expected_owner=expected_owner,
            provenance_source="railway_deployment_metadata",
        )
    if (name or "").lower() != expected_name.lower():
        raise GenerationError(
            "Railway deployment repository name mismatch: "
            f"expected {expected_name!r}, got {name!r}",
            checkout_commit=commit,
            required_commit=required_commit,
            railway_provenance=provenance,
            expected_name=expected_name,
            provenance_source="railway_deployment_metadata",
        )
    if branch != expected_branch:
        raise GenerationError(
            "Railway deployment branch mismatch: "
            f"expected {expected_branch!r}, got {branch!r}",
            checkout_commit=commit,
            required_commit=required_commit,
            railway_provenance=provenance,
            expected_branch=expected_branch,
            provenance_source="railway_deployment_metadata",
        )

    return {
        "checkout_commit": commit,
        "origin_main_commit": commit,
        "required_commit": required_commit,
        "provenance_source": "railway_deployment_metadata",
        "railway_repo_owner": owner,
        "railway_repo_name": name,
        "railway_branch": branch,
    }


def assert_commits_match(repo_root: Path, required_commit: str) -> dict:
    """Verify checkout matches ``required_commit``; fail closed.

    Prefer normal ``.git`` metadata when present. When ``.git`` is absent
    (typical Railway runtime image), validate trusted Railway deployment
    metadata instead. Missing or mismatched provenance always raises.
    """
    if git_metadata_available(repo_root):
        head = read_checked_out_commit(repo_root)
        origin_main = read_origin_main_commit(repo_root)
        if head != required_commit or origin_main != required_commit:
            raise GenerationError(
                "HEAD and origin/main are not exactly the required commit "
                f"{required_commit}; HEAD={head!r} origin/main={origin_main!r}",
                checkout_commit=head,
                origin_main_commit=origin_main,
                required_commit=required_commit,
                provenance_source="git_metadata",
            )
        return {
            "checkout_commit": head,
            "origin_main_commit": origin_main,
            "required_commit": required_commit,
            "provenance_source": "git_metadata",
        }

    provenance = read_railway_deployment_provenance()
    if any(provenance.values()):
        return assert_railway_provenance_matches(required_commit)

    raise GenerationError(
        "Commit provenance missing: no .git metadata and no Railway deployment "
        f"metadata ({', '.join(RAILWAY_PROVENANCE_ENV_VARS)})",
        checkout_commit=None,
        origin_main_commit=None,
        required_commit=required_commit,
        provenance_source=None,
    )


def _ensure_not_protected(path: Path, *, role: str) -> Path:
    resolved = path.resolve()
    text = str(resolved).replace("\\", "/")
    lower = text.lower()
    for marker in _PROTECTED_PATH_MARKERS:
        # Allow writing under candidate-output-root even when nested beneath
        # attorney-feedback-eval; only block using those trees as inputs.
        if role == "input" and marker.lower() in lower:
            raise GenerationError(
                f"Refusing to load protected reference material as {role}: {resolved}",
                path=str(resolved),
                marker=marker,
            )
    return resolved


def _load_json(path: Path, *, role: str = "input") -> Any:
    safe = _ensure_not_protected(path, role=role)
    if not safe.is_file():
        raise GenerationError(f"Required input missing: {safe}")
    return json.loads(safe.read_text(encoding="utf-8"))


def resolve_case_input_paths(case_root: Path) -> dict[str, Path]:
    root = case_root.resolve()
    return {
        "page_records": root
        / "derived"
        / "page-extraction"
        / "canonical_page_records.json",
        "exhibit_map": root
        / "derived"
        / "exhibit-segmentation"
        / "filing_exhibit_map.json",
        "case_map": root / "derived" / "case-map" / "case_map.json",
        "question_packet": root
        / "derived"
        / "attorney-review-packet-02-live"
        / "attorney_review_packet_02.json",
        "question_text_file": root / "derived" / "question-text" / "questions.json",
    }


def load_question_text_only(case_root: Path, question_id: str) -> str:
    """Load only the question text for question_id; discard all other fields."""
    paths = resolve_case_input_paths(case_root)
    # Prefer a questions-only JSON (id -> text or list of {question_id,text}).
    qfile = paths["question_text_file"]
    if qfile.is_file():
        raw = _load_json(qfile, role="input")
        if isinstance(raw, dict) and question_id in raw:
            text = raw[question_id]
            if isinstance(text, dict):
                text = text.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("question_id") == question_id:
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        return text.strip()
        raise GenerationError(
            f"Question {question_id!r} text missing from questions-only file",
            path=str(qfile),
        )

    packet_path = paths["question_packet"]
    data = _load_json(packet_path, role="input")
    text = None
    for question in data.get("questions") or []:
        if not isinstance(question, dict):
            continue
        if question.get("question_id") == question_id:
            text = question.get("text")
            break
    # Drop packet payload immediately; never retain answers/feedback fields.
    del data
    if not isinstance(text, str) or not text.strip():
        raise GenerationError(
            f"Question {question_id!r} text field missing from permitted inputs",
            path=str(packet_path),
        )
    return text.strip()


def load_permitted_case_inputs(
    case_root: Path,
    question_id: str,
    *,
    inventory_path: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> dict[str, Any]:
    paths = resolve_case_input_paths(case_root)
    question_text = load_question_text_only(case_root, question_id)
    page_wrap = _load_json(paths["page_records"], role="input")
    exhibit_map = _load_json(paths["exhibit_map"], role="input")
    case_map_wrap = _load_json(paths["case_map"], role="input")
    case_map = case_map_wrap.get("case_map")
    if not isinstance(case_map, dict):
        # Allow either wrapped {"case_map": {...}} or bare case_map object.
        if isinstance(case_map_wrap, dict) and (
            "parties" in case_map_wrap or "nodes" in case_map_wrap or "filings" in case_map_wrap
        ):
            case_map = case_map_wrap
        else:
            raise GenerationError("case_map.json missing usable case_map object")

    inv_path = inventory_path
    if inv_path is None:
        resolved = mb.resolve_inventory_path(None)
        if resolved is not None:
            inv_path = Path(resolved)
        else:
            root = repo_root or REPO_ROOT
            inv_path = root / "data" / "case-00-triborough" / "nyscef_filing_inventory.json"
    inv_path = _ensure_not_protected(Path(inv_path), role="input")
    inventory = mb.load_nyscef_filing_inventory(inv_path)
    if not inventory:
        raise GenerationError(f"NYSCEF inventory unavailable: {inv_path}")

    return {
        "question_id": question_id,
        "question_text": question_text,
        "page_records": page_wrap,
        "exhibit_map": exhibit_map,
        "case_map": case_map,
        "inventory": inventory,
        "inventory_path": str(inv_path),
        "input_paths": {k: str(v) for k, v in paths.items()},
    }


def _inventory_canonical_filings(inventory: dict) -> list[dict]:
    filings = [
        f for f in inventory.get("filings", []) if f.get("ingest_canonical") is True
    ]
    return sorted(filings, key=lambda f: int(f["nyscef_document_number"]))


def _group_pages_by_filing(pages: list[dict]) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = defaultdict(list)
    for page in pages:
        nyscef = page.get("nyscef_document_number")
        if nyscef is None:
            raise GenerationError(
                "Canonical page record missing nyscef_document_number",
                page_id=page.get("page_id"),
            )
        grouped[int(nyscef)].append(page)
    for doc_no in grouped:
        grouped[doc_no].sort(key=lambda p: int(p["page_number"]))
    return dict(sorted(grouped.items()))


def build_documents_from_permitted_inputs(
    page_wrap: dict,
    inventory: dict,
    exhibit_map: dict,
) -> list[dict]:
    """Assemble retrieval documents from permitted corpus inputs only."""
    pages = page_wrap.get("pages") or []
    grouped = _group_pages_by_filing(pages)
    canonical_filings = _inventory_canonical_filings(inventory)
    exhibit_by_nyscef = {
        int(f["nyscef_document_number"]): f for f in exhibit_map.get("filings") or []
    }
    documents = []
    for entry in canonical_filings:
        doc_no = int(entry["nyscef_document_number"])
        doc_pages = grouped.get(doc_no)
        if not doc_pages:
            raise GenerationError(f"No canonical page records for filing {doc_no}")
        filing_ex = exhibit_by_nyscef.get(doc_no)
        if filing_ex is None:
            raise GenerationError(f"No exhibit-map entry for filing {doc_no}")
        documents.append(
            {
                "filename": entry.get("filename") or doc_pages[0].get("source_filename"),
                "path": doc_pages[0].get("source_path", ""),
                "title": entry.get("filename") or doc_pages[0].get("source_filename"),
                "nyscef_document_number": doc_no,
                "page_count": len(doc_pages),
                "pages": doc_pages,
                "exhibit_segments": list(filing_ex.get("segments") or []),
                "uncertain_exhibit_boundaries": list(
                    filing_ex.get("uncertain_boundaries")
                    or filing_ex.get("uncertain_exhibit_boundaries")
                    or []
                ),
                "sha256": entry.get("sha256"),
                "source": "nyscef_canonical_page_records",
                "include_exhibit_segments": True,
            }
        )
    return documents


def _documents_for_hit_pages(
    merged_hits: list[dict], documents: list[dict]
) -> list[dict]:
    needed_pages: dict[int, set[str]] = defaultdict(set)
    for hit in merged_hits:
        nyscef = hit.get("nyscef_document_number")
        page_id = hit.get("page_id")
        if nyscef is None or not page_id:
            continue
        needed_pages[int(nyscef)].add(page_id)
    subset = []
    for doc in documents:
        nyscef = int(doc["nyscef_document_number"])
        wanted = needed_pages.get(nyscef)
        if not wanted:
            continue
        pages = [p for p in doc.get("pages") or [] if p.get("page_id") in wanted]
        if not pages:
            continue
        subset.append(
            {
                **{k: v for k, v in doc.items() if k != "pages"},
                "pages": pages,
                "page_count": len(pages),
            }
        )
    return subset


def run_production_retrieval(
    documents: list[dict],
    case_map: dict,
    question_text: str,
    *,
    top_k: int = 30,
) -> dict:
    prepared = mb.prepare_documents_for_canonical_retrieval(documents)
    return mb.retrieve_canonical_records(
        prepared,
        question_text,
        case_map=case_map,
        top_k=top_k,
        build_case_map_if_missing=False,
    )


def audit_serialized_model_input(
    question_text: str,
    retrieval: dict,
    *,
    case_map: Optional[dict] = None,
) -> dict:
    """Build/audit exact serialized evidence input via production helpers."""
    evidence_packet = de.build_evidence_packet(
        question_text,
        retrieval,
        case_map=case_map,
        exhibit_context=None,
        allowed_sources=[],
    )
    party_role_intent = de.detect_party_role_question_intent(question_text)
    user_prompt = de.build_user_prompt(
        evidence_packet,
        party_role_completeness=party_role_intent,
    )
    serialized = de._stable_json(evidence_packet)
    hits = list(evidence_packet.get("retrieval_hits") or [])
    per_page_lengths = {
        h.get("page_id"): len(h.get("excerpt") or "") for h in hits if h.get("page_id")
    }
    expected = (
        de.extract_party_role_expected_attributes(evidence_packet)
        if party_role_intent
        else []
    )
    audit = {
        "question": question_text,
        "party_role_intent": bool(party_role_intent),
        "evidence_page_ids": [h.get("page_id") for h in hits],
        "per_page_serialized_excerpt_lengths": per_page_lengths,
        "total_serialized_evidence_characters": sum(per_page_lengths.values()),
        "serialized_evidence_packet_sha256": _sha256_bytes(serialized.encode("utf-8")),
        "serialized_user_prompt_sha256": _sha256_bytes(user_prompt.encode("utf-8")),
        "expected_attribute_count": len(expected),
        "retrieval_hit_count": evidence_packet.get("retrieval_hit_count"),
    }
    return {
        "evidence_packet": evidence_packet,
        "user_prompt": user_prompt,
        "party_role_intent": party_role_intent,
        "expected_attributes": expected,
        "audit": audit,
    }


def candidate_content_sha256(candidate: dict) -> str:
    without = {k: v for k, v in candidate.items() if k != "candidate_sha256"}
    return _sha256_bytes(_canonical_json_bytes(without))


def write_candidate_artifacts(
    out_dir: Path,
    *,
    question_id: str,
    question_text: str,
    required_commit: str,
    reasoner_result: dict,
    model_input_audit: dict,
    commit_info: dict,
    completeness: dict,
) -> dict[str, Path]:
    """Write the four candidate artifacts and verify absolute-path hashes."""
    out_dir.mkdir(parents=True, exist_ok=False)
    generated_at = _utc_now()
    proposed = reasoner_result.get("proposed_answer") or ""
    model_name = (reasoner_result.get("audit") or {}).get("model") or "injected_or_resolved"
    provider = (reasoner_result.get("audit") or {}).get("provider") or "model_call"

    json_name = f"{question_id}_candidate_answer.json"
    md_name = f"{question_id}_candidate_answer.md"
    # Durable CLI keeps the historical Q1 artifact filenames when question_id is Q1.
    if question_id == "Q1":
        json_name = "Q1_candidate_answer.json"
        md_name = "Q1_candidate_answer.md"

    candidate = {
        "artifact_type": "attorney_feedback_candidate_answer",
        "status": "candidate",
        "attorney_approved": False,
        "finalized": True,
        "generation_commit": required_commit,
        "generated_at": generated_at,
        "question_id": question_id,
        "question_text": question_text,
        "model": model_name,
        "provider": provider,
        "candidate_directory": str(out_dir.resolve()),
        "reasoner_status": reasoner_result.get("status"),
        "reasoner_result": reasoner_result,
        "proposed_answer": proposed,
        "confidence": reasoner_result.get("confidence"),
        "propositions": reasoner_result.get("propositions") or [],
        "supporting_evidence": reasoner_result.get("supporting_evidence") or [],
        "contrary_evidence": reasoner_result.get("contrary_evidence") or [],
        "unresolved_questions": reasoner_result.get("unresolved_questions") or [],
        "documents_pages_reviewed": reasoner_result.get("documents_pages_reviewed") or [],
        "attorney_review": reasoner_result.get("attorney_review")
        or {"requires_attorney_review": True},
        "review_scope": reasoner_result.get("review_scope"),
        "audit": reasoner_result.get("audit") or {},
        "completeness_validation": completeness,
        "contamination_protection": {
            "original_answer_loaded": False,
            "provisional_or_gold_answers_loaded": False,
            "gold_labels_loaded": False,
            "attorney_feedback_loaded": False,
            "prior_candidate_answer_prose_loaded": False,
            "evaluation_or_comparison_artifacts_loaded": False,
            "confirmation": (
                "Confirmed prohibited artifacts were not loaded during generation."
            ),
        },
        "permitted_inputs_used": [
            "question text field",
            "canonical page records",
            "filing exhibit map",
            "case map",
            "NYSCEF filing inventory",
            "production retrieval/evidence-packet/serialization/drafting/validation/bounded-repair",
        ],
    }
    candidate_hash = candidate_content_sha256(candidate)
    candidate["candidate_sha256"] = candidate_hash

    md_text = (
        f"# {question_id} Candidate Answer\n\n"
        f"status: `candidate`\n\n"
        f"attorney_approved: `false`\n\n"
        f"generation_commit: `{required_commit}`\n\n"
        f"finalized: `true`\n\n"
        f"generated_at: `{generated_at}`\n\n"
        f"candidate_sha256: `{candidate_hash}`\n\n"
        f"## Question\n\n{question_text}\n\n"
        f"## Proposed answer\n\n{proposed}\n"
    )

    absolute_paths = {
        json_name: str((out_dir / json_name).resolve()),
        md_name: str((out_dir / md_name).resolve()),
        "generation_manifest.json": str((out_dir / "generation_manifest.json").resolve()),
        "model_input_audit.json": str((out_dir / "model_input_audit.json").resolve()),
    }

    manifest = {
        "artifact_type": "attorney_feedback_candidate_generation_manifest",
        "status": "candidate",
        "attorney_approved": False,
        "finalized": True,
        "generation_commit": required_commit,
        "generated_at": generated_at,
        "checkout_commit": commit_info.get("checkout_commit"),
        "origin_main_commit": commit_info.get("origin_main_commit"),
        "candidate_directory": str(out_dir.resolve()),
        "question_id": question_id,
        "generation_only": True,
        "candidate_sha256": candidate_hash,
        "candidate_sha256_method": (
            "sha256(utf-8 of json.dumps(candidate_without_candidate_sha256_field, "
            "sort_keys=True, ensure_ascii=False, separators=(',', ':')))"
        ),
        "files": [
            json_name,
            md_name,
            "generation_manifest.json",
            "model_input_audit.json",
        ],
        "absolute_paths": absolute_paths,
        "completeness_validation": completeness,
        "reasoner_status": reasoner_result.get("status"),
    }

    audit_out = dict(model_input_audit)
    audit_out.update(
        {
            "generated_at": generated_at,
            "generation_commit": required_commit,
            "candidate_sha256": candidate_hash,
            "absolute_paths": absolute_paths,
            "completeness_validation": completeness,
        }
    )

    json_path = out_dir / json_name
    md_path = out_dir / md_name
    manifest_path = out_dir / "generation_manifest.json"
    audit_path = out_dir / "model_input_audit.json"

    json_path.write_text(
        json.dumps(candidate, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    md_path.write_text(md_text, encoding="utf-8")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    audit_path.write_text(
        json.dumps(audit_out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    verified_hash = candidate_content_sha256(loaded)
    file_sha = mb.compute_file_sha256(json_path)
    if verified_hash != candidate_hash or loaded.get("candidate_sha256") != candidate_hash:
        raise GenerationError(
            "Hash verification failed after artifact write",
            recorded=candidate_hash,
            recomputed=verified_hash,
            loaded_field=loaded.get("candidate_sha256"),
        )

    manifest["candidate_answer_json_file_sha256"] = file_sha
    audit_out["candidate_answer_json_file_sha256"] = file_sha
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    audit_path.write_text(
        json.dumps(audit_out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    return {
        json_name: json_path.resolve(),
        md_name: md_path.resolve(),
        "generation_manifest.json": manifest_path.resolve(),
        "model_input_audit.json": audit_path.resolve(),
    }


def run_generation(
    *,
    case_root: Path,
    question_id: str,
    required_commit: str,
    candidate_output_root: Path,
    authorization_acknowledgement: str,
    generation_only: bool,
    repo_root: Optional[Path] = None,
    inventory_path: Optional[Path] = None,
    model_call: Optional[ModelCall] = None,
    skip_commit_check: bool = False,
    top_k: int = 30,
) -> dict:
    """Run generation-only candidate creation. Returns machine-readable result."""
    if authorization_acknowledgement != AUTHORIZATION_ACK:
        raise GenerationError(
            "Refusing to transmit private evidence without explicit authorization "
            f"acknowledgement ({AUTHORIZATION_ACK})",
            authorization_acknowledgement=authorization_acknowledgement,
        )
    if not generation_only:
        raise GenerationError(
            "CLI is generation-only; pass --generation-only",
            generation_only=generation_only,
        )

    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    if skip_commit_check:
        commit_info = {
            "checkout_commit": required_commit,
            "origin_main_commit": required_commit,
            "required_commit": required_commit,
            "provenance_source": "skipped",
            "skipped": True,
        }
    else:
        commit_info = assert_commits_match(root, required_commit)

    inputs = load_permitted_case_inputs(
        Path(case_root),
        question_id,
        inventory_path=inventory_path,
        repo_root=root,
    )
    documents = build_documents_from_permitted_inputs(
        inputs["page_records"],
        inputs["inventory"],
        inputs["exhibit_map"],
    )
    retrieval = run_production_retrieval(
        documents,
        inputs["case_map"],
        inputs["question_text"],
        top_k=top_k,
    )
    inspection = audit_serialized_model_input(
        inputs["question_text"],
        retrieval,
        case_map=inputs["case_map"],
    )

    docs_subset = _documents_for_hit_pages(
        list(retrieval.get("results") or []), documents
    )
    reasoner_result = de.answer_attorney_record_question(
        inputs["question_text"],
        retrieval,
        documents=docs_subset,
        case_map=inputs["case_map"],
        exhibit_context=None,
        allowed_sources=[],
        model_call=model_call,
    )

    audit = reasoner_result.get("audit") or {}
    provider_calls = int(audit.get("party_role_provider_calls") or 0)
    repair_attempted = bool(audit.get("party_role_repair_attempted"))
    completeness_failed = bool(audit.get("party_role_completeness_failed"))
    # Non-party questions: treat a single successful READY call as complete.
    if "party_role_provider_calls" not in audit:
        provider_calls = 1 if reasoner_result.get("status") == de.STATUS_READY else 0

    initial_ok = (not repair_attempted) and reasoner_result.get("status") == de.STATUS_READY
    repair_ok = repair_attempted and reasoner_result.get("status") == de.STATUS_READY
    completeness = {
        "initial_completeness_validation": (
            "PASS" if initial_ok else ("FAIL" if repair_attempted else "PASS")
        ),
        "repair_invoked": repair_attempted,
        "repair_validation": (
            "Not Needed"
            if not repair_attempted
            else ("PASS" if repair_ok else "FAIL")
        ),
        "party_role_provider_calls": provider_calls,
        "party_role_completeness_failed": completeness_failed,
        "missing_party_role_attributes": audit.get("missing_party_role_attributes") or [],
    }

    finalized = (
        reasoner_result.get("status") == de.STATUS_READY and not completeness_failed
    )
    if not finalized:
        raise GenerationError(
            "Production completeness validation failed after at most one bounded "
            "repair; candidate not finalized",
            completeness_validation=completeness,
            reasoner_status=reasoner_result.get("status"),
            reasoner_audit=audit,
            provider_calls=provider_calls,
            finalized=False,
        )

    # Cap: production path already enforces <=1 repair; defend in CLI result.
    if provider_calls > 2:
        raise GenerationError(
            "Provider call budget exceeded (expected at most one initial call "
            "and at most one bounded repair)",
            provider_calls=provider_calls,
        )

    out_root = Path(candidate_output_root)
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = _utc_stamp()
    out_dir = out_root / f"{question_id.lower()}-candidate-{stamp}"
    if out_dir.exists():
        out_dir = out_root / f"{question_id.lower()}-candidate-{stamp}.{_sha256_bytes(stamp.encode())[:8]}"

    written = write_candidate_artifacts(
        out_dir,
        question_id=question_id,
        question_text=inputs["question_text"],
        required_commit=required_commit,
        reasoner_result=reasoner_result,
        model_input_audit=inspection["audit"],
        commit_info=commit_info,
        completeness=completeness,
    )

    return {
        "ok": True,
        "finalized": True,
        "candidate_directory": str(out_dir.resolve()),
        "files": {name: str(path) for name, path in written.items()},
        "completeness_validation": completeness,
        "provider_calls": provider_calls,
        "repair_invoked": repair_attempted,
        "reasoner_status": reasoner_result.get("status"),
        "commit": commit_info,
        "model_input_audit": inspection["audit"],
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Generate an attorney-feedback candidate answer using the production "
            "retrieval/drafting path (generation-only)."
        )
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
        help="Repository commit that HEAD and origin/main must equal.",
    )
    p.add_argument(
        "--candidate-output-root",
        type=Path,
        required=True,
        help="Directory under which a new timestamped candidate folder is created.",
    )
    p.add_argument(
        "--authorize-private-evidence-transmission",
        required=True,
        dest="authorization_acknowledgement",
        help=(
            "Explicit acknowledgement string required before private evidence may "
            f"be sent to a model provider. Must equal: {AUTHORIZATION_ACK}"
        ),
    )
    p.add_argument(
        "--generation-only",
        action="store_true",
        required=True,
        help="Required. Restricts the CLI to generation (no evaluation).",
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
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run_generation(
            case_root=args.case_root,
            question_id=args.question_id,
            required_commit=args.required_commit,
            candidate_output_root=args.candidate_output_root,
            authorization_acknowledgement=args.authorization_acknowledgement,
            generation_only=bool(args.generation_only),
            repo_root=args.repo_root,
            inventory_path=args.inventory_path,
        )
    except GenerationError as exc:
        payload = {
            "ok": False,
            "finalized": False,
            "blocker": exc.blocker,
            **exc.details,
        }
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        return 1
    except Exception as exc:  # noqa: BLE001
        payload = {
            "ok": False,
            "finalized": False,
            "blocker": f"{type(exc).__name__}: {exc}",
        }
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        return 1

    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
