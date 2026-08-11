"""Final-answer acceptance-contract validation, fallback, and duplication gate.

Operates on a loaded contract evaluation view plus the fully assembled final
answer. Emits only safe result codes — never contract body or private criterion
prose. Fail-closed for configured-contract runs when any required criterion
fails or material duplication remains after deterministic repair.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

# ---------------------------------------------------------------------------
# Safe machine-readable result codes (stable; safe to log / audit).
# ---------------------------------------------------------------------------

PRESENCE_PRESENT = "present"
PRESENCE_ABSENT = "absent"

EVIDENCE_SUPPORTED = "evidence_supported"
EVIDENCE_UNSUPPORTED = "evidence_unsupported"

SEMANTIC_PRESERVED = "semantic_preserved"
SEMANTIC_VIOLATED = "semantic_violated"
SEMANTIC_NOT_APPLICABLE = "semantic_not_applicable"

CRIT_PASS = "criterion_pass"
CRIT_FAIL_MISSING = "criterion_fail_missing"
CRIT_FAIL_UNSUPPORTED = "criterion_fail_unsupported"
CRIT_FAIL_SEMANTIC = "criterion_fail_semantic"

FALLBACK_NONE = "fallback_none"
FALLBACK_SKIPPED_EQUIVALENT = "fallback_skipped_equivalent"
FALLBACK_INSERTED = "fallback_inserted"

DUP_OK = "duplication_ok"
DUP_REPAIRED = "duplication_repaired"
DUP_FAIL = "duplication_fail"

LOAD_OK = "load_ok"
LOAD_UNAVAILABLE = "load_unavailable"
LOAD_INVALID = "load_invalid"
LOAD_NOT_CONFIGURED = "load_not_configured"


_WS_RE = re.compile(r"\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip().lower())


def _contains_phrase(haystack_norm: str, phrase: str) -> bool:
    needle = _norm(phrase)
    if not needle:
        return True
    return needle in haystack_norm


def _all_phrases_present(haystack_norm: str, phrases: Sequence[str]) -> bool:
    return all(_contains_phrase(haystack_norm, p) for p in phrases)


def _any_phrase_present(haystack_norm: str, phrases: Sequence[str]) -> bool:
    phrases = [p for p in phrases if _norm(p)]
    if not phrases:
        return False
    return any(_contains_phrase(haystack_norm, p) for p in phrases)


@dataclass(frozen=True)
class CriterionEvalSpec:
    """In-memory evaluation fields for one criterion (not for logging)."""

    id: str
    presence_phrases: tuple[str, ...]
    evidence_phrases: tuple[str, ...]
    semantic_required_phrases: tuple[str, ...]
    semantic_forbidden_phrases: tuple[str, ...]
    fallback_text: str
    category: str = ""

    def __repr__(self) -> str:
        return (
            "CriterionEvalSpec("
            f"id={self.id!r}, "
            f"presence_phrase_count={len(self.presence_phrases)}, "
            f"evidence_phrase_count={len(self.evidence_phrases)}, "
            f"semantic_required_count={len(self.semantic_required_phrases)}, "
            f"semantic_forbidden_count={len(self.semantic_forbidden_phrases)}, "
            f"fallback_chars={len(self.fallback_text)}, "
            f"category={self.category!r})"
        )


@dataclass(frozen=True)
class StructureRangeSpec:
    kind: str
    start: int
    end: int
    category: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
        }
        if self.category:
            out["category"] = self.category
        return out


@dataclass(frozen=True)
class StructureRequirements:
    required_kinds: tuple[str, ...]
    required_ranges: tuple[StructureRangeSpec, ...]
    required_categories: tuple[str, ...]

    def as_safe_dict(self) -> dict[str, Any]:
        return {
            "required_kinds": list(self.required_kinds),
            "required_ranges": [r.as_dict() for r in self.required_ranges],
            "required_categories": list(self.required_categories),
        }


@dataclass(frozen=True)
class ContractEvaluationView:
    """Evaluation payload retained in memory after a successful load.

    Repr and provenance helpers expose only ids/counts — never phrases/prose.
    """

    contract_id: str
    version: str
    schema_version: str
    benchmark_id: str
    question_id: str
    object_key: str
    content_sha256: str
    required_criterion_ids: tuple[str, ...]
    evidence_constraints: Mapping[str, Any]
    semantic_preservation: Mapping[str, Any]
    duplication_rules: Mapping[str, Any]
    criteria: tuple[CriterionEvalSpec, ...]
    structure_requirements: StructureRequirements

    def criterion_by_id(self) -> dict[str, CriterionEvalSpec]:
        return {c.id: c for c in self.criteria}

    def __repr__(self) -> str:
        return (
            "ContractEvaluationView("
            f"contract_id={self.contract_id!r}, "
            f"version={self.version!r}, "
            f"object_key={self.object_key!r}, "
            f"content_sha256={self.content_sha256!r}, "
            f"required_criterion_ids={list(self.required_criterion_ids)!r}, "
            f"criterion_count={len(self.criteria)}, "
            f"structure_range_count={len(self.structure_requirements.required_ranges)})"
        )


@dataclass(frozen=True)
class CriterionResult:
    criterion_id: str
    presence: str
    evidence: str
    semantic: str
    result_code: str
    diagnostics: tuple[str, ...] = ()

    def as_safe_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "presence": self.presence,
            "evidence": self.evidence,
            "semantic": self.semantic,
            "result_code": self.result_code,
            "diagnostics": list(self.diagnostics),
        }


@dataclass
class AcceptanceValidationResult:
    ok: bool
    final_answer: str
    criterion_results: list[CriterionResult] = field(default_factory=list)
    fallback_actions: dict[str, str] = field(default_factory=dict)
    duplication_result: str = DUP_OK
    diagnostics: list[str] = field(default_factory=list)

    def as_safe_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "criterion_results": [c.as_safe_dict() for c in self.criterion_results],
            "fallback_actions": dict(self.fallback_actions),
            "duplication_result": self.duplication_result,
            "diagnostics": list(self.diagnostics),
        }


def parse_criterion_specs(document: Mapping[str, Any]) -> tuple[CriterionEvalSpec, ...]:
    raw = document.get("criteria") or []
    if not isinstance(raw, list):
        return ()
    out: list[CriterionEvalSpec] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or "").strip()
        if not cid:
            continue
        out.append(
            CriterionEvalSpec(
                id=cid,
                presence_phrases=tuple(
                    str(x) for x in (item.get("presence_phrases") or []) if str(x).strip()
                ),
                evidence_phrases=tuple(
                    str(x) for x in (item.get("evidence_phrases") or []) if str(x).strip()
                ),
                semantic_required_phrases=tuple(
                    str(x)
                    for x in (item.get("semantic_required_phrases") or [])
                    if str(x).strip()
                ),
                semantic_forbidden_phrases=tuple(
                    str(x)
                    for x in (item.get("semantic_forbidden_phrases") or [])
                    if str(x).strip()
                ),
                fallback_text=str(item.get("fallback_text") or ""),
                category=str(item.get("category") or ""),
            )
        )
    return tuple(out)


def parse_structure_requirements(document: Mapping[str, Any]) -> StructureRequirements:
    raw = document.get("structure_requirements")
    if not isinstance(raw, dict):
        return StructureRequirements((), (), ())
    kinds = tuple(
        str(x) for x in (raw.get("required_kinds") or []) if str(x).strip()
    )
    categories = tuple(
        str(x) for x in (raw.get("required_categories") or []) if str(x).strip()
    )
    ranges: list[StructureRangeSpec] = []
    for item in raw.get("required_ranges") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        if not kind:
            continue
        try:
            start = int(item["start"])
            end = int(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        ranges.append(
            StructureRangeSpec(
                kind=kind,
                start=start,
                end=end,
                category=str(item.get("category") or ""),
            )
        )
    return StructureRequirements(kinds, tuple(ranges), categories)


def build_evaluation_view_from_document(
    document: Mapping[str, Any],
    *,
    metadata: Any,
) -> ContractEvaluationView:
    """Build an evaluation view from a validated document + safe metadata."""
    return ContractEvaluationView(
        contract_id=str(metadata.contract_id),
        version=str(metadata.version),
        schema_version=str(metadata.schema_version),
        benchmark_id=str(metadata.benchmark_id),
        question_id=str(metadata.question_id),
        object_key=str(metadata.object_key),
        content_sha256=str(metadata.content_sha256),
        required_criterion_ids=tuple(metadata.required_criterion_ids),
        evidence_constraints=dict(metadata.evidence_constraints),
        semantic_preservation=dict(metadata.semantic_preservation),
        duplication_rules=dict(metadata.duplication_rules),
        criteria=parse_criterion_specs(document),
        structure_requirements=parse_structure_requirements(document),
    )


def evaluate_criterion(
    answer_text: str,
    spec: CriterionEvalSpec,
    *,
    semantic_preservation: Mapping[str, Any],
) -> CriterionResult:
    norm = _norm(answer_text)
    present = _all_phrases_present(norm, spec.presence_phrases) if spec.presence_phrases else (
        bool(_norm(spec.fallback_text)) and _contains_phrase(norm, spec.fallback_text)
        if spec.fallback_text
        else False
    )
    # Empty presence_phrases with non-empty fallback: treat fallback presence as present.
    if not spec.presence_phrases and spec.fallback_text:
        present = _contains_phrase(norm, spec.fallback_text)
    if not spec.presence_phrases and not spec.fallback_text:
        present = True

    presence = PRESENCE_PRESENT if present else PRESENCE_ABSENT

    if not present:
        return CriterionResult(
            criterion_id=spec.id,
            presence=presence,
            evidence=EVIDENCE_UNSUPPORTED,
            semantic=SEMANTIC_NOT_APPLICABLE,
            result_code=CRIT_FAIL_MISSING,
            diagnostics=["criterion_absent"],
        )

    evidence_ok = True
    if spec.evidence_phrases:
        evidence_ok = _all_phrases_present(norm, spec.evidence_phrases)
    evidence = EVIDENCE_SUPPORTED if evidence_ok else EVIDENCE_UNSUPPORTED
    if not evidence_ok:
        return CriterionResult(
            criterion_id=spec.id,
            presence=presence,
            evidence=evidence,
            semantic=SEMANTIC_NOT_APPLICABLE,
            result_code=CRIT_FAIL_UNSUPPORTED,
            diagnostics=["evidence_unsupported"],
        )

    forbid_omissions = bool(semantic_preservation.get("forbid_material_omissions"))
    preserve_negation = bool(semantic_preservation.get("require_preserve_negation"))
    require_roles = bool(semantic_preservation.get("require_same_party_roles"))

    semantic = SEMANTIC_NOT_APPLICABLE
    if forbid_omissions or preserve_negation or require_roles or (
        spec.semantic_required_phrases or spec.semantic_forbidden_phrases
    ):
        required_ok = _all_phrases_present(norm, spec.semantic_required_phrases)
        forbidden_hit = _any_phrase_present(norm, spec.semantic_forbidden_phrases)
        if required_ok and not forbidden_hit:
            semantic = SEMANTIC_PRESERVED
        else:
            semantic = SEMANTIC_VIOLATED
            return CriterionResult(
                criterion_id=spec.id,
                presence=presence,
                evidence=evidence,
                semantic=semantic,
                result_code=CRIT_FAIL_SEMANTIC,
                diagnostics=["semantic_preservation_failed"],
            )

    return CriterionResult(
        criterion_id=spec.id,
        presence=presence,
        evidence=evidence,
        semantic=semantic,
        result_code=CRIT_PASS,
        diagnostics=(),
    )


def texts_are_equivalent(a: str, b: str) -> bool:
    """True when normalized texts are equal or one contains the other fully."""
    na, nb = _norm(a), _norm(b)
    if not na and not nb:
        return True
    if not na or not nb:
        return False
    if na == nb:
        return True
    if na in nb or nb in na:
        return True
    return False


def answer_already_contains_equivalent(answer_text: str, fragment: str) -> bool:
    if not _norm(fragment):
        return True
    norm_answer = _norm(answer_text)
    norm_frag = _norm(fragment)
    if norm_frag in norm_answer:
        return True
    # Sentence-level equivalence: any answer sentence equivalent to fragment.
    for sentence in _SENTENCE_SPLIT_RE.split(answer_text or ""):
        if texts_are_equivalent(sentence, fragment):
            return True
    return False


def apply_idempotent_contract_fallback(
    answer_text: str,
    view: ContractEvaluationView,
    *,
    missing_ids: Optional[Sequence[str]] = None,
) -> tuple[str, dict[str, str]]:
    """Append genuinely missing fallback content at most once per criterion.

    Skips when equivalent content is already present. Never duplicates.
    """
    by_id = view.criterion_by_id()
    targets = list(missing_ids) if missing_ids is not None else list(
        view.required_criterion_ids
    )
    actions: dict[str, str] = {}
    out = answer_text or ""
    inserted_for: set[str] = set()

    for cid in targets:
        spec = by_id.get(cid)
        if spec is None:
            actions[cid] = FALLBACK_NONE
            continue
        frag = spec.fallback_text or ""
        if not _norm(frag):
            actions[cid] = FALLBACK_NONE
            continue
        if cid in inserted_for or answer_already_contains_equivalent(out, frag):
            actions[cid] = FALLBACK_SKIPPED_EQUIVALENT
            continue
        # Insert exactly once.
        if out and not out.endswith(("\n", " ")):
            out = out.rstrip() + "\n\n"
        out = out + frag.strip()
        inserted_for.add(cid)
        actions[cid] = FALLBACK_INSERTED
    return out, actions


def _sentence_tokens(sentence: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", _norm(sentence)) if len(t) > 2}


def _phrase_overlap_ratio(a: str, b: str) -> float:
    ta, tb = _sentence_tokens(a), _sentence_tokens(b)
    if not ta or not tb:
        return 1.0 if _norm(a) == _norm(b) and _norm(a) else 0.0
    inter = len(ta & tb)
    return inter / float(max(len(ta), len(tb)))


def apply_duplication_gate(
    answer_text: str,
    duplication_rules: Mapping[str, Any],
    *,
    repair: bool = True,
) -> tuple[str, str, list[str]]:
    """Remove materially duplicative prose or fail closed.

    Uses ``max_duplicate_phrase_ratio`` from contract duplication_rules.
    """
    max_ratio = float(duplication_rules.get("max_duplicate_phrase_ratio") or 0.25)
    sentences = [
        s.strip()
        for s in _SENTENCE_SPLIT_RE.split(answer_text or "")
        if s and s.strip()
    ]
    if len(sentences) < 2:
        return answer_text or "", DUP_OK, []

    keep: list[str] = []
    removed = 0
    for sentence in sentences:
        dup = False
        for prior in keep:
            if texts_are_equivalent(sentence, prior) or _phrase_overlap_ratio(
                sentence, prior
            ) >= max(max_ratio, 0.85):
                dup = True
                break
        if dup:
            removed += 1
            continue
        keep.append(sentence)

    if removed == 0:
        return answer_text or "", DUP_OK, []

    repaired = " ".join(keep).strip()
    # Re-check remaining duplication after repair.
    still = False
    for i, a in enumerate(keep):
        for b in keep[i + 1 :]:
            if texts_are_equivalent(a, b) or _phrase_overlap_ratio(a, b) >= max(
                max_ratio, 0.85
            ):
                still = True
                break
        if still:
            break

    if still or not repair:
        return answer_text or "", DUP_FAIL, ["material_duplication_remaining"]
    return repaired, DUP_REPAIRED, ["duplicate_sentences_removed"]


def validate_final_answer_against_contract(
    answer_text: str,
    view: ContractEvaluationView,
    *,
    apply_fallback: bool = True,
    apply_duplication_repair: bool = True,
) -> AcceptanceValidationResult:
    """Validate fully assembled final answer; optionally fallback + dedupe.

    A configured-contract run cannot pass unless every required criterion passes
    and the duplication gate is ok/repaired.
    """
    text = answer_text or ""
    fallback_actions: dict[str, str] = {}
    diagnostics: list[str] = []

    by_id = view.criterion_by_id()
    missing_for_fallback: list[str] = []
    for cid in view.required_criterion_ids:
        spec = by_id.get(cid)
        if spec is None:
            diagnostics.append(f"missing_criterion_spec:{cid}")
            continue
        trial = evaluate_criterion(
            text, spec, semantic_preservation=view.semantic_preservation
        )
        if trial.result_code == CRIT_FAIL_MISSING:
            missing_for_fallback.append(cid)

    if apply_fallback and missing_for_fallback:
        text, fallback_actions = apply_idempotent_contract_fallback(
            text, view, missing_ids=missing_for_fallback
        )
        # Second pass: ensure idempotence (running again must not duplicate).
        text, second = apply_idempotent_contract_fallback(
            text, view, missing_ids=missing_for_fallback
        )
        for cid, action in second.items():
            if action == FALLBACK_INSERTED:
                diagnostics.append(f"fallback_non_idempotent:{cid}")
            elif cid not in fallback_actions:
                fallback_actions[cid] = action
            elif action == FALLBACK_SKIPPED_EQUIVALENT:
                # First insert then skip is expected idempotent behavior.
                pass

    text, dup_result, dup_diags = apply_duplication_gate(
        text,
        view.duplication_rules,
        repair=apply_duplication_repair,
    )
    diagnostics.extend(dup_diags)

    results: list[CriterionResult] = []
    all_pass = True
    for cid in view.required_criterion_ids:
        spec = by_id.get(cid)
        if spec is None:
            results.append(
                CriterionResult(
                    criterion_id=cid,
                    presence=PRESENCE_ABSENT,
                    evidence=EVIDENCE_UNSUPPORTED,
                    semantic=SEMANTIC_NOT_APPLICABLE,
                    result_code=CRIT_FAIL_MISSING,
                    diagnostics=["criterion_spec_absent"],
                )
            )
            all_pass = False
            continue
        result = evaluate_criterion(
            text, spec, semantic_preservation=view.semantic_preservation
        )
        results.append(result)
        if result.result_code != CRIT_PASS:
            all_pass = False

    if dup_result == DUP_FAIL:
        all_pass = False

    return AcceptanceValidationResult(
        ok=all_pass and dup_result != DUP_FAIL,
        final_answer=text,
        criterion_results=results,
        fallback_actions=fallback_actions,
        duplication_result=dup_result,
        diagnostics=diagnostics,
    )


def safe_provenance_record(
    *,
    load_status: str,
    view: Optional[ContractEvaluationView] = None,
    load_error_code: Optional[str] = None,
    validation: Optional[AcceptanceValidationResult] = None,
    object_key: str = "",
    content_sha256: str = "",
) -> dict[str, Any]:
    """Build audit/manifest-safe acceptance-contract provenance (no body/prose)."""
    record: dict[str, Any] = {
        "acceptance_contract": {
            "load_status": load_status,
            "contract_id": None,
            "version": None,
            "object_key": object_key or None,
            "content_sha256": content_sha256 or None,
            "load_error_code": load_error_code,
            "criterion_results": [],
            "duplication_result": None,
            "fallback_actions": {},
            "validation_ok": None,
        }
    }
    block = record["acceptance_contract"]
    if view is not None:
        block["contract_id"] = view.contract_id
        block["version"] = view.version
        block["object_key"] = view.object_key
        block["content_sha256"] = view.content_sha256
        block["required_criterion_ids"] = list(view.required_criterion_ids)
        block["structure_requirements"] = view.structure_requirements.as_safe_dict()
    if validation is not None:
        block["criterion_results"] = [
            {
                "criterion_id": c.criterion_id,
                "presence": c.presence,
                "evidence": c.evidence,
                "semantic": c.semantic,
                "result_code": c.result_code,
            }
            for c in validation.criterion_results
        ]
        block["duplication_result"] = validation.duplication_result
        block["fallback_actions"] = dict(validation.fallback_actions)
        block["validation_ok"] = bool(validation.ok)
    return record
