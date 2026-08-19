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
FALLBACK_SKIPPED_UNSUPPORTED = "fallback_skipped_unsupported"
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

# Shared Q2 no-defense semantic evaluation (preflight + final acceptance).
Q2_NO_DEFENSE_CRITERION_ID = "q2-no-defense-or-indemnity"
Q2_NO_DEFENSE_CATEGORY = "no_defense_or_indemnity"
Q2_VALIDATED_CLAIMS_SCHEMA_VERSION = "q2_validated_structured_claims.v1"
Q1_VALIDATED_PARTY_CLAIMS_SCHEMA_VERSION = "q1_validated_party_claims.v1"
Q1_STRUCTURED_CRITERION_IDS = frozenset({
    "Q1_C1_PLAINTIFF_ROLE", "Q1_C2_DEFENDANT_SIDE_PARTIES",
    "Q1_C3_SPECIFIC_DEFENDANT_ROLE_DESIGNATIONS",
    "Q1_C4_LIMITED_SUBSTANTIVE_ROLE_INFORMATION",
    "Q1_C5_DUAL_ROLES_IN_RELATED_ACTION", "Q1_C6_INCOMPLETE_PARTY_ROSTER",
})

_NO_DUTY_RE = re.compile(r"\bno\b(?:\s+\w+){0,3}\s+duty\b", re.IGNORECASE)
_DEFEND_RE = re.compile(r"\bdefend(?:s|ed|ing)?\b", re.IGNORECASE)
_INDEMNIFY_RE = re.compile(
    r"\bindemnif(?:y|ies|ied|ying)\b|\bindemnity\b", re.IGNORECASE
)
_DEFENDANTS_RE = re.compile(r"\bdefendants?\b", re.IGNORECASE)

# Material-omission diagnostics for source-identified pleaded counts.
MATERIAL_OMISSION_COUNT_MISSING = "material_omission_source_count_missing"
MATERIAL_OMISSION_COUNT_REPAIRED = "material_omission_source_count_repaired"
MATERIAL_OMISSION_COUNT_SUBSTANCE_MISSING = (
    "material_omission_source_count_substance_missing"
)
SOURCE_IDENTIFIED_COUNTS_KEY = "source_identified_pleaded_counts"


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


def _safe_phrase_coverage(
    haystack_norm: str, phrases: Sequence[str]
) -> dict[str, Any]:
    """Return phrase-position coverage without exposing contract prose."""
    matched: list[int] = []
    missing: list[int] = []
    for index, phrase in enumerate(phrases, start=1):
        target = matched if _contains_phrase(haystack_norm, phrase) else missing
        target.append(index)
    return {
        "phrase_count": len(phrases),
        "matched_indices": matched,
        "missing_indices": missing,
    }


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
    phrase_coverage: Mapping[str, Any] = field(default_factory=dict)

    def as_safe_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "presence": self.presence,
            "evidence": self.evidence,
            "semantic": self.semantic,
            "result_code": self.result_code,
            "diagnostics": list(self.diagnostics),
            "phrase_coverage": dict(self.phrase_coverage),
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


def source_identified_counts_from_validated(
    validated_claims: Optional[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return structured source-identified pleaded counts (no invented titles).

    Reads optional ``source_identified_pleaded_counts`` on the validated claims
    object. Each row must carry an ordinal/label. Verified source-grounded
    ``title``, ``substantive_excerpt``, ``substance_phrases``, and ``page_id``
    are preserved when present — never nullified.
    """
    if not isinstance(validated_claims, Mapping):
        return []
    raw = validated_claims.get(SOURCE_IDENTIFIED_COUNTS_KEY)
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        ordinal = str(row.get("ordinal") or "").strip().upper()
        label = str(row.get("label") or "").strip()
        if not label and ordinal:
            label = f"Count {ordinal}"
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        title = str(row.get("title") or "").strip() or None
        excerpt = str(row.get("substantive_excerpt") or "").strip() or None
        phrases_raw = row.get("substance_phrases")
        phrases: list[str] = []
        if isinstance(phrases_raw, list):
            for p in phrases_raw:
                cleaned = str(p or "").strip()
                if cleaned and cleaned.lower() not in {x.lower() for x in phrases}:
                    phrases.append(cleaned)
        out.append(
            {
                "ordinal": ordinal or None,
                "label": label,
                "observed_marker": str(row.get("observed_marker") or label),
                "title": title,
                "substantive_excerpt": excerpt,
                "substance_phrases": phrases,
                "page_id": str(row.get("page_id") or "").strip() or None,
            }
        )
    return out


def _safe_count_diag_token(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9 ]+", "", label).strip().replace(" ", "_")


def substance_phrases_for_source_count(row: Mapping[str, Any]) -> list[str]:
    """Return verified substance phrases for a pleaded-count row."""
    phrases: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        text = _WS_RE.sub(" ", str(value or "").strip())
        key = text.lower()
        if len(text) < 4 or key in seen:
            return
        seen.add(key)
        phrases.append(text)

    raw = row.get("substance_phrases")
    if isinstance(raw, list):
        for item in raw:
            _add(item)
    _add(row.get("title"))
    excerpt = _WS_RE.sub(" ", str(row.get("substantive_excerpt") or "").strip())
    if excerpt:
        # Prefer compact leading span so coverage checks stay fail-closed.
        span = excerpt if len(excerpt) <= 96 else excerpt[:96].rsplit(" ", 1)[0]
        _add(span)
    return phrases


def answer_covers_source_count_substance(
    answer_text: str,
    row: Mapping[str, Any],
) -> bool:
    """True when the answer covers verified substance for one pleaded count."""
    phrases = substance_phrases_for_source_count(row)
    if not phrases:
        return False
    norm_answer = _norm(answer_text)
    return _any_phrase_present(norm_answer, phrases)


def missing_source_identified_count_labels(
    answer_text: str,
    source_counts: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Return source count labels absent from the answer (case-insensitive)."""
    norm_answer = _norm(answer_text)
    missing: list[str] = []
    for row in source_counts or []:
        if not isinstance(row, Mapping):
            continue
        label = str(row.get("label") or "").strip()
        if not label:
            continue
        needle = _norm(label)
        # Word-boundary match so ``Count I`` does not hit inside ``Count II``.
        if not re.search(
            rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])",
            norm_answer,
        ):
            missing.append(label)
    return missing


def missing_source_identified_count_substance_labels(
    answer_text: str,
    source_counts: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Return labels present or required whose verified substance is absent."""
    missing: list[str] = []
    for row in source_counts or []:
        if not isinstance(row, Mapping):
            continue
        label = str(row.get("label") or "").strip()
        if not label:
            continue
        if answer_covers_source_count_substance(answer_text, row):
            continue
        missing.append(label)
    return missing


def _verified_repair_clause_for_count(row: Mapping[str, Any]) -> Optional[str]:
    """Build a cited substance repair clause, or None to fail closed."""
    label = str(row.get("label") or "").strip()
    page_id = str(row.get("page_id") or "").strip()
    if not label or not page_id:
        return None
    phrases = substance_phrases_for_source_count(row)
    if not phrases:
        return None
    # Prefer title, else first phrase, else bounded excerpt — all source-grounded.
    title = str(row.get("title") or "").strip()
    excerpt = str(row.get("substantive_excerpt") or "").strip()
    if title:
        substance = title
    elif excerpt:
        substance = excerpt if len(excerpt) <= 160 else excerpt[:160].rsplit(" ", 1)[0]
    else:
        substance = phrases[0]
    substance = _WS_RE.sub(" ", substance).strip()
    if len(substance) < 4:
        return None
    return (
        f" {label} seeks {substance} (page_id {page_id})."
    )


def apply_source_identified_count_omission_repair(
    answer_text: str,
    source_counts: Sequence[Mapping[str, Any]],
) -> tuple[str, list[str]]:
    """
    Bounded repair for omitted source-identified pleaded counts.

    Appends verified source-grounded substance with page_id citation only.
    Bare labels are never sufficient. Returns ``(answer, repaired_labels)``.
    When verified substance or citation is unavailable for a gap, that count is
    left unrepaired (caller fails closed).
    """
    label_missing = set(
        missing_source_identified_count_labels(answer_text, source_counts)
    )
    substance_missing = set(
        missing_source_identified_count_substance_labels(answer_text, source_counts)
    )
    needs = label_missing | substance_missing
    if not needs:
        return answer_text or "", []

    by_label = {
        str(row.get("label") or "").strip(): row
        for row in (source_counts or [])
        if isinstance(row, Mapping) and str(row.get("label") or "").strip()
    }
    clauses: list[str] = []
    repaired: list[str] = []
    for label in [
        str(row.get("label") or "").strip()
        for row in (source_counts or [])
        if isinstance(row, Mapping) and str(row.get("label") or "").strip() in needs
    ]:
        row = by_label.get(label)
        if not isinstance(row, Mapping):
            continue
        clause = _verified_repair_clause_for_count(row)
        if not clause:
            continue
        clauses.append(clause)
        repaired.append(label)
    if not clauses:
        return answer_text or "", []
    base = (answer_text or "").rstrip()
    if base and base[-1] not in ".!?":
        base += "."
    return (base + "".join(clauses)).strip(), repaired


def evaluate_material_omissions_for_source_counts(
    answer_text: str,
    *,
    semantic_preservation: Mapping[str, Any],
    source_counts: Sequence[Mapping[str, Any]],
    apply_repair: bool = True,
) -> tuple[str, bool, list[str]]:
    """
    Enforce ``forbid_material_omissions`` against structured source counts.

    Shared (not Case-00-specific): every source-identified pleaded count must
    appear with verified substantive coverage (title/excerpt/phrases). Bare
    Count I/II labels are insufficient. When gaps exist and ``apply_repair`` is
    true, performs one bounded substance+citation repair; otherwise fails
    closed. Returns ``(answer_text, ok, diagnostics)``.
    """
    diagnostics: list[str] = []
    text = answer_text or ""
    if not bool(semantic_preservation.get("forbid_material_omissions")):
        return text, True, diagnostics
    if not source_counts:
        return text, True, diagnostics

    label_missing = missing_source_identified_count_labels(text, source_counts)
    substance_missing = missing_source_identified_count_substance_labels(
        text, source_counts
    )
    if not label_missing and not substance_missing:
        return text, True, diagnostics

    if apply_repair:
        text, repaired = apply_source_identified_count_omission_repair(
            text, source_counts
        )
        for label in repaired:
            safe = _safe_count_diag_token(label)
            diagnostics.append(f"{MATERIAL_OMISSION_COUNT_REPAIRED}:{safe}")
        label_missing = missing_source_identified_count_labels(text, source_counts)
        substance_missing = missing_source_identified_count_substance_labels(
            text, source_counts
        )
        if not label_missing and not substance_missing:
            return text, True, diagnostics

    for label in label_missing:
        safe = _safe_count_diag_token(label)
        diagnostics.append(f"{MATERIAL_OMISSION_COUNT_MISSING}:{safe}")
    for label in substance_missing:
        if label in label_missing:
            continue
        safe = _safe_count_diag_token(label)
        diagnostics.append(f"{MATERIAL_OMISSION_COUNT_SUBSTANCE_MISSING}:{safe}")
    return text, False, diagnostics


def q2_no_defense_claim_from_validated(
    validated_claims: Optional[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    """Return the exact no_defense claim row from validated claims (no copy).

    Reads ``q2_validated_structured_claims.v1`` only. Does not rebuild or mutate
    claims. Returns ``None`` when the object or category row is absent.
    """
    if not isinstance(validated_claims, Mapping):
        return None
    if str(validated_claims.get("schema_version") or "") != (
        Q2_VALIDATED_CLAIMS_SCHEMA_VERSION
    ):
        return None
    claims = validated_claims.get("claims")
    if not isinstance(claims, list):
        return None
    for row in claims:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("category") or "") == Q2_NO_DEFENSE_CATEGORY:
            return row
    return None


def q2_no_defense_semantic_signals(
    answer_text: str,
    *,
    page_id: str,
) -> dict[str, bool]:
    """Privacy-safe semantic signals for the no-defense safe paraphrase.

    Requires no-duty meaning, defend and/or indemnify meaning, defendants, and
    the supporting page citation. Does not require OCR-derived wording (e.g.
    Count II / indemnification) and does not inspect quote bodies for OCR.
    """
    text = answer_text or ""
    page = (page_id or "").strip()
    return {
        "no_duty": bool(_NO_DUTY_RE.search(text)),
        "defend_or_indemnify": bool(
            _DEFEND_RE.search(text) or _INDEMNIFY_RE.search(text)
        ),
        "defendants": bool(_DEFENDANTS_RE.search(text)),
        "page_citation": bool(page) and f"page_id {page}" in text,
    }


def evaluate_q2_no_defense_or_indemnity(
    answer_text: str,
    validated_claims: Mapping[str, Any],
) -> CriterionResult:
    """Shared semantic evaluator for ``q2-no-defense-or-indemnity``.

    Support authority is the exact immutable ``q2_validated_structured_claims.v1``
    object. Used by production-boundary preflight and final acceptance so both
    paths share one interpretation. Never rebuilds or mutates claims; never
    requires unreadable OCR phrasing.
    """
    claim = q2_no_defense_claim_from_validated(validated_claims)
    if claim is None:
        return CriterionResult(
            criterion_id=Q2_NO_DEFENSE_CRITERION_ID,
            presence=PRESENCE_ABSENT,
            evidence=EVIDENCE_UNSUPPORTED,
            semantic=SEMANTIC_NOT_APPLICABLE,
            result_code=CRIT_FAIL_MISSING,
            diagnostics=["q2_no_defense_claim_missing"],
        )
    if not claim.get("supported"):
        return CriterionResult(
            criterion_id=Q2_NO_DEFENSE_CRITERION_ID,
            presence=PRESENCE_ABSENT,
            evidence=EVIDENCE_UNSUPPORTED,
            semantic=SEMANTIC_NOT_APPLICABLE,
            result_code=CRIT_FAIL_UNSUPPORTED,
            diagnostics=["q2_no_defense_claim_unsupported"],
        )

    page_id = str(claim.get("page_id") or "").strip()
    if not page_id:
        return CriterionResult(
            criterion_id=Q2_NO_DEFENSE_CRITERION_ID,
            presence=PRESENCE_ABSENT,
            evidence=EVIDENCE_UNSUPPORTED,
            semantic=SEMANTIC_NOT_APPLICABLE,
            result_code=CRIT_FAIL_UNSUPPORTED,
            diagnostics=["q2_no_defense_claim_citation_missing"],
        )

    signals = q2_no_defense_semantic_signals(answer_text, page_id=page_id)
    diagnostics: list[str] = []
    if not signals["no_duty"]:
        diagnostics.append("q2_no_defense_missing_no_duty")
    if not signals["defend_or_indemnify"]:
        diagnostics.append("q2_no_defense_missing_defend_or_indemnify")
    if not signals["defendants"]:
        diagnostics.append("q2_no_defense_missing_defendants")
    if not signals["page_citation"]:
        diagnostics.append("q2_no_defense_missing_page_citation")

    if diagnostics:
        # Distinguish total absence of no-defense meaning from partial near-miss.
        presence = (
            PRESENCE_PRESENT
            if (
                signals["no_duty"]
                or signals["defend_or_indemnify"]
                or signals["defendants"]
            )
            else PRESENCE_ABSENT
        )
        if presence == PRESENCE_ABSENT:
            result_code = CRIT_FAIL_MISSING
        elif not signals["page_citation"] and all(
            signals[k]
            for k in ("no_duty", "defend_or_indemnify", "defendants")
        ):
            result_code = CRIT_FAIL_UNSUPPORTED
        else:
            result_code = CRIT_FAIL_SEMANTIC
        return CriterionResult(
            criterion_id=Q2_NO_DEFENSE_CRITERION_ID,
            presence=presence,
            evidence=EVIDENCE_UNSUPPORTED,
            semantic=SEMANTIC_VIOLATED,
            result_code=result_code,
            diagnostics=tuple(diagnostics),
        )

    return CriterionResult(
        criterion_id=Q2_NO_DEFENSE_CRITERION_ID,
        presence=PRESENCE_PRESENT,
        evidence=EVIDENCE_SUPPORTED,
        semantic=SEMANTIC_PRESERVED,
        result_code=CRIT_PASS,
        diagnostics=(),
    )


def _q1_role_values(party: Mapping[str, Any], field: str) -> set[str]:
    raw = party.get(field)
    values = raw if isinstance(raw, list) else [raw]
    return {_norm(value) for value in values if isinstance(value, str) and _norm(value)}


def q1_party_claims_are_valid(claims: Optional[Mapping[str, Any]]) -> bool:
    """Fail closed unless the typed Q1 handoff has the exact bounded shape."""
    if not isinstance(claims, Mapping):
        return False
    if claims.get("schema_version") != Q1_VALIDATED_PARTY_CLAIMS_SCHEMA_VERSION:
        return False
    if set(claims) != {
        "schema_version",
        "parties",
        "roster_completeness",
    }:
        return False
    parties = claims.get("parties")
    if not isinstance(parties, list):
        return False
    if claims.get("roster_completeness") not in {"complete", "incomplete", "not_established"}:
        return False
    allowed_party_fields = {
        "identity",
        "procedural_roles",
        "pleaded_role_basis",
        "substantive_role",
        "entity_type",
        "residence_or_ppb",
        "related_action_roles",
    }
    for party in parties:
        if (
            not isinstance(party, Mapping)
            or not set(party).issubset(allowed_party_fields)
            or not _norm(str(party.get("identity") or ""))
        ):
            return False
        if any(
            not isinstance(party.get(field), list)
            or any(not isinstance(value, str) for value in party.get(field) or [])
            for field in ("procedural_roles", "related_action_roles")
        ):
            return False
        if any(
            not isinstance(party.get(field, ""), str)
            for field in ("pleaded_role_basis", "substantive_role")
        ):
            return False
    return True


def evaluate_q1_structured_criterion(
    spec: CriterionEvalSpec,
    claims: Mapping[str, Any],
    *,
    phrase_coverage: Optional[Mapping[str, Any]] = None,
) -> CriterionResult:
    """Evaluate Case-00 Q1 criteria from typed validated claims, not prose."""
    parties = [p for p in claims.get("parties") or [] if isinstance(p, Mapping)]
    current = [(p, _q1_role_values(p, "procedural_roles")) for p in parties]
    related = {
        _norm(str(p.get("identity") or "")): _q1_role_values(p, "related_action_roles")
        for p in parties
    }
    satisfied = False
    diagnostic = "q1_structured_claim_missing"
    if spec.id == "Q1_C1_PLAINTIFF_ROLE":
        satisfied = any(any("plaintiff" in r for r in roles) for _p, roles in current)
        diagnostic = "q1_structured_plaintiff_role"
    elif spec.id == "Q1_C2_DEFENDANT_SIDE_PARTIES":
        satisfied = any(any("defendant" in r for r in roles) for _p, roles in current)
        diagnostic = "q1_structured_defendant_parties"
    elif spec.id == "Q1_C3_SPECIFIC_DEFENDANT_ROLE_DESIGNATIONS":
        defendants = [
            (p, roles)
            for p, roles in current
            if any("defendant" in r for r in roles)
        ]
        # The criterion asks whether specific evidence-supported
        # designations are reported; caption-only defendants must not make the
        # entire criterion impossible. At least one designated defendant is
        # sufficient, while an all-caption-only set still fails closed.
        satisfied = any(
            bool(_norm(str(p.get("pleaded_role_basis") or "")))
            or len(roles) > 1
            for p, roles in defendants
        )
        diagnostic = "q1_structured_defendant_designations"
    elif spec.id == "Q1_C4_LIMITED_SUBSTANTIVE_ROLE_INFORMATION":
        defendants = [
            p
            for p, roles in current
            if any("defendant" in role for role in roles)
        ]
        satisfied = bool(defendants) and any(
            not _norm(str(p.get("substantive_role") or ""))
            for p in defendants
        )
        diagnostic = "q1_structured_substantive_role_limitation"
    elif spec.id == "Q1_C5_DUAL_ROLES_IN_RELATED_ACTION":
        satisfied = any(
            bool(
                related.get(_norm(str(p.get("identity") or "")), set())
                - roles
            )
            for p, roles in current
            if roles
        )
        diagnostic = "q1_structured_related_action_roles"
    elif spec.id == "Q1_C6_INCOMPLETE_PARTY_ROSTER":
        satisfied = claims.get("roster_completeness") in {"incomplete", "not_established"}
        diagnostic = "q1_structured_roster_completeness"
    coverage = dict(phrase_coverage or {})
    return CriterionResult(
        criterion_id=spec.id,
        presence=PRESENCE_PRESENT if satisfied else PRESENCE_ABSENT,
        evidence=EVIDENCE_SUPPORTED if satisfied else EVIDENCE_UNSUPPORTED,
        semantic=SEMANTIC_PRESERVED if satisfied else SEMANTIC_NOT_APPLICABLE,
        result_code=CRIT_PASS if satisfied else CRIT_FAIL_MISSING,
        diagnostics=(diagnostic,),
        phrase_coverage=coverage,
    )


def evaluate_criterion(
    answer_text: str,
    spec: CriterionEvalSpec,
    *,
    semantic_preservation: Mapping[str, Any],
    validated_claims: Optional[Mapping[str, Any]] = None,
    validated_evidence_text: Optional[str] = None,
) -> CriterionResult:
    # Single shared path for Q2 no-defense when validated claims are the
    # support authority — do not fall back to OCR-derived phrase matching.
    if (
        spec.id == Q2_NO_DEFENSE_CRITERION_ID
        and q2_no_defense_claim_from_validated(validated_claims) is not None
    ):
        assert validated_claims is not None
        return evaluate_q2_no_defense_or_indemnity(answer_text, validated_claims)

    norm = _norm(answer_text)
    evidence_norm = _norm(
        answer_text
        if validated_evidence_text is None
        else validated_evidence_text
    )
    phrase_coverage = {
        "presence": _safe_phrase_coverage(norm, spec.presence_phrases),
        "evidence": _safe_phrase_coverage(
            evidence_norm, spec.evidence_phrases
        ),
        "semantic_required": _safe_phrase_coverage(
            norm, spec.semantic_required_phrases
        ),
        "semantic_forbidden": _safe_phrase_coverage(
            norm, spec.semantic_forbidden_phrases
        ),
    }
    if spec.id in Q1_STRUCTURED_CRITERION_IDS and q1_party_claims_are_valid(validated_claims):
        assert validated_claims is not None
        return evaluate_q1_structured_criterion(
            spec, validated_claims, phrase_coverage=phrase_coverage
        )
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
            phrase_coverage=phrase_coverage,
            diagnostics=["criterion_absent"],
        )

    evidence_ok = True
    if spec.evidence_phrases:
        evidence_ok = _all_phrases_present(
            evidence_norm, spec.evidence_phrases
        )
    evidence = EVIDENCE_SUPPORTED if evidence_ok else EVIDENCE_UNSUPPORTED
    if not evidence_ok:
        return CriterionResult(
            criterion_id=spec.id,
            presence=presence,
            evidence=evidence,
            semantic=SEMANTIC_NOT_APPLICABLE,
            result_code=CRIT_FAIL_UNSUPPORTED,
            phrase_coverage=phrase_coverage,
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
                phrase_coverage=phrase_coverage,
            )

    return CriterionResult(
        criterion_id=spec.id,
        presence=presence,
        evidence=evidence,
        semantic=semantic,
        result_code=CRIT_PASS,
        diagnostics=(),
        phrase_coverage=phrase_coverage,
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


def criterion_evidence_already_supported(
    answer_text: str,
    spec: CriterionEvalSpec,
    *,
    validated_claims: Optional[Mapping[str, Any]] = None,
    validated_evidence_text: Optional[str] = None,
) -> bool:
    """True when the answer already contains every required evidence phrase.

    Empty ``evidence_phrases`` means no evidence constraint. Fallback must not
    invent evidence linkage; it may only proceed when support is already present.

    When validated claims authorize Q2 no-defense, semantic evidence support
    replaces OCR-derived evidence phrase matching.
    """
    if (
        spec.id == Q2_NO_DEFENSE_CRITERION_ID
        and q2_no_defense_claim_from_validated(validated_claims) is not None
    ):
        assert validated_claims is not None
        return (
            evaluate_q2_no_defense_or_indemnity(
                answer_text, validated_claims
            ).result_code
            == CRIT_PASS
        )
    if not spec.evidence_phrases:
        return True
    evidence_text = (
        answer_text
        if validated_evidence_text is None
        else validated_evidence_text
    )
    return _all_phrases_present(
        _norm(evidence_text), spec.evidence_phrases
    )


def apply_idempotent_contract_fallback(
    answer_text: str,
    view: ContractEvaluationView,
    *,
    missing_ids: Optional[Sequence[str]] = None,
    validated_claims: Optional[Mapping[str, Any]] = None,
    validated_evidence_text: Optional[str] = None,
) -> tuple[str, dict[str, str]]:
    """Append genuinely missing fallback content at most once per criterion.

    Skips when equivalent content is already present. Never duplicates.

    Fail-closed for unsupported claims: when a criterion requires evidence
    phrases that are not already present in the answer, fallback is skipped
    (``fallback_skipped_unsupported``) rather than inserting prose that would
    manufacture legal/factual assertions without cited support.
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
        # Evidence authority is checked before equivalence so answer prose
        # cannot bypass an explicit fail-closed evidence channel.
        if not criterion_evidence_already_supported(
            out,
            spec,
            validated_claims=validated_claims,
            validated_evidence_text=validated_evidence_text,
        ):
            actions[cid] = FALLBACK_SKIPPED_UNSUPPORTED
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


def _primary_validated_identity(
    sentence: str,
    validated_identities: Sequence[str],
) -> str:
    """Return the longest validated identity contained in one sentence."""
    normalized = _norm(sentence)
    matches = [
        _norm(identity)
        for identity in validated_identities
        if _norm(identity) and _norm(identity) in normalized
    ]
    return max(matches, key=len) if matches else ""


def _sentences_name_distinct_validated_identities(
    first: str,
    second: str,
    validated_identities: Sequence[str],
) -> bool:
    first_identity = _primary_validated_identity(first, validated_identities)
    second_identity = _primary_validated_identity(second, validated_identities)
    return bool(
        first_identity
        and second_identity
        and first_identity != second_identity
    )


def _adds_validated_identity(
    sentence: str,
    retained_sentences: Sequence[str],
    validated_identities: Sequence[str],
) -> bool:
    """True when dropping ``sentence`` would erase a validated identity.

    This is an exact normalized-string coverage guard, independent of the
    semantic/overlap heuristic used to classify duplicate prose.
    """
    # Typed-claim retention is punctuation-sensitive: legal identities that
    # differ only by commas or suffix punctuation must not collapse here.
    def normalize_exact(value: str) -> str:
        return " ".join(str(value or "").split()).lower()

    normalized_sentence = normalize_exact(sentence)
    normalized_retained = normalize_exact(" ".join(retained_sentences))
    return any(
        identity_norm in normalized_sentence
        and identity_norm not in normalized_retained
        for identity_norm in (
            normalize_exact(identity) for identity in validated_identities
        )
        if identity_norm
    )


def apply_duplication_gate(
    answer_text: str,
    duplication_rules: Mapping[str, Any],
    *,
    repair: bool = True,
    validated_identities: Sequence[str] = (),
) -> tuple[str, str, list[str]]:
    """Remove materially duplicative prose or fail closed.

    Uses ``max_duplicate_phrase_ratio`` from contract duplication_rules.
    """
    max_ratio = float(duplication_rules.get("max_duplicate_phrase_ratio") or 0.25)
    protected_text = answer_text or ""
    period_sentinel = "\ue000"
    for identity in sorted(validated_identities, key=lambda value: -len(str(value))):
        identity_text = str(identity or "")
        if "." not in identity_text:
            continue
        pattern = re.compile(re.escape(identity_text), re.IGNORECASE)
        protected_text = pattern.sub(
            lambda matched: matched.group(0).replace(".", period_sentinel),
            protected_text,
        )
    sentences = [
        s.strip().replace(period_sentinel, ".")
        for s in _SENTENCE_SPLIT_RE.split(protected_text)
        if s and s.strip()
    ]
    if len(sentences) < 2:
        return answer_text or "", DUP_OK, []

    keep: list[str] = []
    removed = 0
    for sentence in sentences:
        dup = False
        for prior in keep:
            equivalent = texts_are_equivalent(sentence, prior)
            overlap = _phrase_overlap_ratio(sentence, prior) >= max(
                max_ratio, 0.85
            )
            distinct_identities = (
                _sentences_name_distinct_validated_identities(
                    sentence,
                    prior,
                    validated_identities,
                )
            )
            if (equivalent or overlap) and not distinct_identities:
                dup = True
                break
        if dup and _adds_validated_identity(
            sentence,
            keep,
            validated_identities,
        ):
            dup = False
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
            equivalent = texts_are_equivalent(a, b)
            overlap = _phrase_overlap_ratio(a, b) >= max(max_ratio, 0.85)
            distinct_identities = (
                _sentences_name_distinct_validated_identities(
                    a,
                    b,
                    validated_identities,
                )
            )
            if (equivalent or overlap) and not distinct_identities:
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
    validated_claims: Optional[Mapping[str, Any]] = None,
    validated_evidence_text: Optional[str] = None,
    source_identified_counts: Optional[Sequence[Mapping[str, Any]]] = None,
) -> AcceptanceValidationResult:
    """Validate fully assembled final answer; optionally fallback + dedupe.

    A configured-contract run cannot pass unless every required criterion passes
    and the duplication gate is ok/repaired.

    When ``validated_claims`` is the immutable ``q2_validated_structured_claims.v1``
    object, ``q2-no-defense-or-indemnity`` is evaluated by the shared semantic
    evaluator (not OCR-derived contract phrase matching).

    When ``forbid_material_omissions`` is set, source-identified pleaded counts
    (from ``source_identified_counts`` or ``validated_claims``) must appear with
    verified substantive coverage; bare labels are insufficient. Missing
    substance triggers one bounded cited repair when source-grounded substance
    and page_id are available, otherwise fails closed.
    """
    text = answer_text or ""
    fallback_actions: dict[str, str] = {}
    diagnostics: list[str] = []

    counts = list(source_identified_counts or [])
    if not counts:
        counts = source_identified_counts_from_validated(validated_claims)
    text, counts_ok, count_diags = evaluate_material_omissions_for_source_counts(
        text,
        semantic_preservation=view.semantic_preservation,
        source_counts=counts,
        apply_repair=apply_fallback,
    )
    diagnostics.extend(count_diags)

    by_id = view.criterion_by_id()
    missing_for_fallback: list[str] = []
    for cid in view.required_criterion_ids:
        spec = by_id.get(cid)
        if spec is None:
            diagnostics.append(f"missing_criterion_spec:{cid}")
            continue
        trial = evaluate_criterion(
            text,
            spec,
            semantic_preservation=view.semantic_preservation,
            validated_claims=validated_claims,
            validated_evidence_text=validated_evidence_text,
        )
        if trial.result_code == CRIT_FAIL_MISSING:
            missing_for_fallback.append(cid)

    if apply_fallback and missing_for_fallback:
        text, fallback_actions = apply_idempotent_contract_fallback(
            text,
            view,
            missing_ids=missing_for_fallback,
            validated_claims=validated_claims,
            validated_evidence_text=validated_evidence_text,
        )
        for cid, action in fallback_actions.items():
            if action == FALLBACK_SKIPPED_UNSUPPORTED:
                diagnostics.append(f"fallback_skipped_unsupported:{cid}")
        # Second pass: ensure idempotence (running again must not duplicate).
        text, second = apply_idempotent_contract_fallback(
            text,
            view,
            missing_ids=missing_for_fallback,
            validated_claims=validated_claims,
            validated_evidence_text=validated_evidence_text,
        )
        for cid, action in second.items():
            if action == FALLBACK_INSERTED:
                diagnostics.append(f"fallback_non_idempotent:{cid}")
            elif cid not in fallback_actions:
                fallback_actions[cid] = action
            elif action == FALLBACK_SKIPPED_EQUIVALENT:
                # First insert then skip is expected idempotent behavior.
                pass

    validated_identities = []
    if (
        isinstance(validated_claims, Mapping)
        and validated_claims.get("schema_version")
        == Q1_VALIDATED_PARTY_CLAIMS_SCHEMA_VERSION
    ):
        validated_identities = [
            str(party.get("identity") or "")
            for party in validated_claims.get("parties") or []
            if isinstance(party, Mapping) and str(party.get("identity") or "")
        ]
    text, dup_result, dup_diags = apply_duplication_gate(
        text,
        view.duplication_rules,
        repair=apply_duplication_repair,
        validated_identities=validated_identities,
    )
    diagnostics.extend(dup_diags)

    # Re-check count completeness after fallback/dedupe (fail closed).
    text, counts_ok_final, count_diags_final = (
        evaluate_material_omissions_for_source_counts(
            text,
            semantic_preservation=view.semantic_preservation,
            source_counts=counts,
            apply_repair=False,
        )
    )
    for d in count_diags_final:
        if d not in diagnostics:
            diagnostics.append(d)
    counts_ok = counts_ok and counts_ok_final

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
            text,
            spec,
            semantic_preservation=view.semantic_preservation,
            validated_claims=validated_claims,
            validated_evidence_text=validated_evidence_text,
        )
        results.append(result)
        if result.result_code != CRIT_PASS:
            all_pass = False

    if dup_result == DUP_FAIL:
        all_pass = False
    if not counts_ok:
        all_pass = False

    return AcceptanceValidationResult(
        ok=all_pass and dup_result != DUP_FAIL and counts_ok,
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
                "phrase_coverage": dict(c.phrase_coverage),
            }
            for c in validation.criterion_results
        ]
        block["duplication_result"] = validation.duplication_result
        block["fallback_actions"] = dict(validation.fallback_actions)
        block["validation_ok"] = bool(validation.ok)
    return record
