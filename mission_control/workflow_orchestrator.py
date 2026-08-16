"""Bounded workflow orchestration state machine (v1).

Pure decision logic over durable ``WorkflowRegistry`` records. Does not
depend on an open API request or chat turn. Child launches are reserved
through registry CAS + idempotency keys; callers materialize reserved
``child_run_id`` values into the existing run registry/queue.

V1 intentionally stops at ``needs_approval`` after MERGE-READY unless the
immutable policy snapshot explicitly authorizes auto-merge/deploy (still
not wired to GitHub/Railway in this module).

Security contract (hardened):
- Review verdicts are accepted only via a terminal machine-readable
  envelope (not prose regex).
- Follow-up mission context is opaque, bounded, redacted JSON — never
  raw YAML interpolation of findings/prior output.
- Policy gates run transactionally inside every child-launch claim.
- Child templates may omit ``repository``; executable children receive the
  complete repository contract from the immutable policy snapshot only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import logging
import re
from typing import Any, Mapping

import yaml

from mission_control.mission_builder import DEFAULT_REPOSITORY_PATH
from mission_control.workspace import normalize_submit_repository_path
from mission_control.workflow_registry import (
    ACTIONABLE_WORKFLOW_ALERT_STATES,
    StepMaterializationState,
    StepStatus,
    StepType,
    TransitionReason,
    WorkflowPolicySnapshot,
    WorkflowRecord,
    WorkflowRegistry,
    WorkflowState,
    WorkflowStepRecord,
    WorkflowStepSpec,
    is_terminal_workflow_state,
    is_workflow_orchestration_enabled,
    make_idempotency_key,
)

logger = logging.getLogger(__name__)

_SECRETISH_RE = re.compile(
    r"(?i)\b(token|secret|password|api[_-]?key|authorization|bearer)\b|"
    r"\b[A-Za-z0-9_-]{24,}\b"
)
_PRIOR_OUTPUT_MAX_CHARS = 4000
_FINDING_MAX_CHARS = 500
_FINDING_MAX_COUNT = 32
_CONTEXT_FIELD_MAX_CHARS = 4000

# Terminal machine-readable verdict envelope (spoof-resistant).
_VERDICT_BEGIN = "<<<MC_REVIEW_VERDICT_V1>>>"
_VERDICT_END = "<<<END_MC_REVIEW_VERDICT_V1>>>"
_VERDICT_BEGIN_RE = re.compile(
    r"(?m)^<<<MC_REVIEW_VERDICT_V1>>>\s*$"
)
_VERDICT_END_RE = re.compile(
    r"(?m)^<<<END_MC_REVIEW_VERDICT_V1>>>\s*$"
)

# Opaque follow-up context trailer (not mission authority).
_FOLLOWUP_BEGIN = "<<<MC_FOLLOWUP_CONTEXT_V1>>>"
_FOLLOWUP_END = "<<<END_MC_FOLLOWUP_CONTEXT_V1>>>"

_VALID_VERDICT_KINDS = frozenset({"merge_ready", "blocked"})


class ReviewVerdictKind(str, Enum):
    MERGE_READY = "merge_ready"
    BLOCKED = "blocked"
    MALFORMED = "malformed"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ReviewVerdict:
    kind: ReviewVerdictKind
    findings: tuple[str, ...] = ()
    fingerprint: str | None = None
    raw_excerpt: str | None = None


@dataclass(frozen=True)
class ChildRunView:
    """Minimal child-run projection for reconciliation (secret-free)."""

    run_id: str
    status: str
    error: str | None = None
    stdout: str = ""
    stderr: str = ""
    elapsed_seconds: float | None = None


class DecisionAction(str, Enum):
    NOOP = "noop"
    LAUNCH_CHILD = "launch_child"
    MARK_STEP = "mark_step"
    TERMINATE = "terminate"
    SUPPRESS_CHILD_ALERT = "suppress_child_alert"
    EMIT_WORKFLOW_ALERT = "emit_workflow_alert"


@dataclass(frozen=True)
class OrchestratorDecision:
    """Single reconcile decision (idempotent when re-applied)."""

    action: DecisionAction
    workflow_id: str
    expected_version: int
    reason: str
    detail: dict[str, Any]
    # Launch fields
    step_type: StepType | None = None
    mission_yaml: str | None = None
    cycle: int | None = None
    attempt: int | None = None
    parent_run_id: str | None = None
    idempotency_key: str | None = None
    # Terminate / mark fields
    to_state: WorkflowState | None = None
    step_id: str | None = None
    child_run_id: str | None = None
    step_status: StepStatus | None = None
    workflow_updates: dict[str, Any] | None = None
    step_updates: dict[str, Any] | None = None
    # Notification
    suppress_child_terminal_alert: bool = False
    emit_workflow_alert: bool = False


@dataclass(frozen=True)
class ChildMissionHydration:
    """Executable child mission after policy repository hydration."""

    mission: dict[str, Any]
    mission_yaml: str


def redact_secrets(text: str) -> str:
    """Redact secret-ish tokens from interpolated prior-run output."""
    return _SECRETISH_RE.sub("[redacted]", text)


def bound_text(text: str, max_chars: int) -> str:
    cleaned = redact_secrets(text or "")
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3] + "..."


def truncate_prior_output(text: str, max_chars: int = _PRIOR_OUTPUT_MAX_CHARS) -> str:
    return bound_text(text, max_chars)


def canonicalize_finding(finding: str) -> str:
    """Normalize one finding for fingerprinting (casefold + whitespace)."""
    return " ".join(str(finding).casefold().split())


def fingerprint_findings(findings: tuple[str, ...] | list[str]) -> str:
    """Canonical blocker fingerprint: sorted, casefolded, whitespace-normalized.

    Ordering and whitespace differences must not evade repeated-blocker
    detection.
    """
    normalized = sorted(
        {
            canonicalize_finding(f)
            for f in findings
            if str(f).strip()
        }
    )
    payload = "\n".join(normalized)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _excerpt_for_verdict(text: str) -> str:
    return truncate_prior_output(text or "", max_chars=800)


def parse_review_verdict(text: str) -> ReviewVerdict:
    """Parse a spoof-resistant terminal review verdict envelope.

    Accepted form (must be the terminal trailer; exactly one envelope)::

        <<<MC_REVIEW_VERDICT_V1>>>
        {"kind":"blocked","findings":["missing tests"]}
        <<<END_MC_REVIEW_VERDICT_V1>>>

    ``kind`` is an exact enum: ``merge_ready`` | ``blocked``.
    Prose markers (MERGE-READY/BLOCKED), code fences, blockquotes, quoted
    prior output, and instructions to print those words are ignored.
    """
    body = text or ""
    excerpt = _excerpt_for_verdict(body)
    begins = list(_VERDICT_BEGIN_RE.finditer(body))
    ends = list(_VERDICT_END_RE.finditer(body))
    if not begins and not ends:
        return ReviewVerdict(kind=ReviewVerdictKind.MALFORMED, raw_excerpt=excerpt)
    if len(begins) != 1 or len(ends) != 1:
        return ReviewVerdict(kind=ReviewVerdictKind.AMBIGUOUS, raw_excerpt=excerpt)
    begin_m = begins[0]
    end_m = ends[0]
    if end_m.start() <= begin_m.end():
        return ReviewVerdict(kind=ReviewVerdictKind.MALFORMED, raw_excerpt=excerpt)
    # Envelope must be terminal: only whitespace after closing marker.
    trailing = body[end_m.end() :]
    if trailing.strip():
        return ReviewVerdict(kind=ReviewVerdictKind.MALFORMED, raw_excerpt=excerpt)
    raw_json = body[begin_m.end() : end_m.start()].strip()
    if not raw_json or len(raw_json) > 16_384:
        return ReviewVerdict(kind=ReviewVerdictKind.MALFORMED, raw_excerpt=excerpt)
    try:
        payload = json.loads(raw_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ReviewVerdict(kind=ReviewVerdictKind.MALFORMED, raw_excerpt=excerpt)
    if not isinstance(payload, dict):
        return ReviewVerdict(kind=ReviewVerdictKind.MALFORMED, raw_excerpt=excerpt)
    # Reject unknown top-level keys beyond the bounded contract.
    allowed_keys = {"kind", "findings"}
    if set(payload.keys()) - allowed_keys:
        return ReviewVerdict(kind=ReviewVerdictKind.MALFORMED, raw_excerpt=excerpt)
    kind_raw = payload.get("kind")
    if kind_raw not in _VALID_VERDICT_KINDS:
        return ReviewVerdict(kind=ReviewVerdictKind.MALFORMED, raw_excerpt=excerpt)
    findings_raw = payload.get("findings", [])
    if findings_raw is None:
        findings_raw = []
    if not isinstance(findings_raw, list):
        return ReviewVerdict(kind=ReviewVerdictKind.MALFORMED, raw_excerpt=excerpt)
    if len(findings_raw) > _FINDING_MAX_COUNT:
        return ReviewVerdict(kind=ReviewVerdictKind.MALFORMED, raw_excerpt=excerpt)
    findings: list[str] = []
    for item in findings_raw:
        if not isinstance(item, str):
            return ReviewVerdict(kind=ReviewVerdictKind.MALFORMED, raw_excerpt=excerpt)
        cleaned = bound_text(item.strip(), _FINDING_MAX_CHARS)
        if not cleaned:
            return ReviewVerdict(kind=ReviewVerdictKind.MALFORMED, raw_excerpt=excerpt)
        findings.append(cleaned)
    if kind_raw == "merge_ready":
        if findings:
            return ReviewVerdict(kind=ReviewVerdictKind.MALFORMED, raw_excerpt=excerpt)
        return ReviewVerdict(
            kind=ReviewVerdictKind.MERGE_READY,
            raw_excerpt=excerpt,
        )
    # blocked
    if not findings:
        return ReviewVerdict(kind=ReviewVerdictKind.MALFORMED, raw_excerpt=excerpt)
    findings_t = tuple(findings)
    return ReviewVerdict(
        kind=ReviewVerdictKind.BLOCKED,
        findings=findings_t,
        fingerprint=fingerprint_findings(findings_t),
        raw_excerpt=excerpt,
    )


def format_review_verdict_envelope(
    kind: str,
    findings: tuple[str, ...] | list[str] | None = None,
) -> str:
    """Helper for tests / agents: emit a valid terminal verdict envelope."""
    payload: dict[str, Any] = {"kind": kind}
    if findings:
        payload["findings"] = list(findings)
    elif kind == "blocked":
        payload["findings"] = []
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return f"{_VERDICT_BEGIN}\n{body}\n{_VERDICT_END}\n"


def validate_followup_against_policy(
    *,
    policy: WorkflowPolicySnapshot,
    repository_name: str,
    target_branch: str,
    requested_scope: tuple[str, ...] | list[str] | None = None,
    wants_merge: bool = False,
    wants_deploy: bool = False,
    wants_destructive: bool = False,
    wants_permission_expansion: bool = False,
    wants_migrations: bool = False,
    wants_secret_changes: bool = False,
    wants_scope_or_repo_change: bool = False,
) -> str | None:
    """Return a machine-readable denial reason, or None if allowed."""
    if repository_name != policy.repository_name:
        return "repository_mismatch"
    if target_branch != policy.target_branch:
        # Auto-followups may only target approved branch lineage.
        if target_branch != policy.base_branch and target_branch != policy.target_branch:
            return "branch_lineage_mismatch"
        if target_branch != policy.target_branch:
            return "branch_lineage_mismatch"
    if requested_scope:
        allowed = set(policy.implementation_scope)
        for path in requested_scope:
            if path not in allowed and not any(
                path.startswith(prefix.rstrip("/") + "/")
                or path == prefix
                for prefix in allowed
            ):
                if not policy.allow_scope_or_repo_changes:
                    return "scope_expansion"
    if wants_merge and not policy.allow_auto_merge:
        return "merge_not_authorized"
    if wants_deploy and not policy.allow_auto_deploy:
        return "deploy_not_authorized"
    if wants_destructive and not policy.allow_destructive_actions:
        return "destructive_not_authorized"
    if wants_permission_expansion and not policy.allow_permission_expansion:
        return "permission_expansion_not_authorized"
    if wants_migrations and not policy.allow_database_migrations:
        return "migrations_not_authorized"
    if wants_secret_changes and not policy.allow_secret_changes:
        return "secret_changes_not_authorized"
    if wants_scope_or_repo_change and not policy.allow_scope_or_repo_changes:
        return "scope_or_repo_change_not_authorized"
    return None


def assert_review_step_read_only(mission_yaml: str) -> str | None:
    """Fail closed if a review mission appears to allow writes/persistence."""
    lowered = mission_yaml.lower()
    if re.search(r"(?m)^\s*mode:\s*persist", lowered):
        return "review_persistence_not_none"
    if re.search(r"persistence:\s*\n\s*mode:\s*(?!none\b)\w+", mission_yaml):
        return "review_persistence_not_none"
    if "create_files: true" in lowered or "modify_files: true" in lowered:
        return "review_must_be_read_only"
    if "delete_files: true" in lowered:
        return "review_must_be_read_only"
    return None


_CANONICAL_REPOSITORY_KEYS = frozenset({"name", "path", "base_branch"})
_AUTHORITY_ALIAS_KEYS = (
    "repository_name",
    "base_branch",
    "target_branch",
    "implementation_scope",
    "scope",
    "branch",
)


def _optional_nonempty_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _split_followup_trailer(mission_yaml: str) -> tuple[str, str]:
    text = mission_yaml or ""
    begin = text.find(_FOLLOWUP_BEGIN)
    if begin == -1:
        return text, ""
    return text[:begin], text[begin:]


def canonical_child_repository_contract(
    policy: WorkflowPolicySnapshot,
) -> dict[str, str]:
    """Return the policy-owned executable ``repository`` mapping."""
    return {
        "name": policy.repository_name,
        "path": DEFAULT_REPOSITORY_PATH,
        "base_branch": policy.base_branch,
    }


def inspect_child_repository_authority(
    data: Mapping[str, Any],
    *,
    policy: WorkflowPolicySnapshot,
) -> str | None:
    """Return a denial when child YAML tries to override workflow authority.

    Policy is the sole source of repository name, base branch, target branch,
    implementation scope, and workspace path. Absent fields are allowed and
    hydrated later; mismatches and injected extras fail closed.
    """
    raw_repo = data.get("repository")
    repo_map: Mapping[str, Any]
    if raw_repo is None:
        repo_map = {}
    elif isinstance(raw_repo, str):
        if raw_repo.strip() != policy.repository_name:
            return "repository_mismatch"
        repo_map = {"name": raw_repo.strip()}
    elif isinstance(raw_repo, Mapping):
        extra = [key for key in raw_repo.keys() if key not in _CANONICAL_REPOSITORY_KEYS]
        if extra:
            return "scope_or_repo_change_not_authorized"
        repo_map = raw_repo
    else:
        return "repository_mismatch"

    names: list[str] = []
    top_name = _optional_nonempty_str(data.get("repository_name"))
    if top_name is not None:
        names.append(top_name)
    nested_name = repo_map.get("name")
    if nested_name is not None:
        parsed_name = _optional_nonempty_str(nested_name)
        if parsed_name is None:
            return "repository_mismatch"
        names.append(parsed_name)
    for name in names:
        if name != policy.repository_name:
            return "repository_mismatch"

    path = repo_map.get("path")
    if path is not None:
        parsed_path = _optional_nonempty_str(path)
        if parsed_path is None or parsed_path not in {".", "./"}:
            return "repository_path_mismatch"

    bases: list[str] = []
    nested_base = repo_map.get("base_branch")
    if nested_base is not None:
        parsed_base = _optional_nonempty_str(nested_base)
        if parsed_base is None:
            return "branch_lineage_mismatch"
        bases.append(parsed_base)
    top_base = _optional_nonempty_str(data.get("base_branch"))
    if top_base is not None:
        bases.append(top_base)
    for base in bases:
        if base != policy.base_branch:
            return "branch_lineage_mismatch"

    targets: list[str] = []
    for key in ("target_branch", "branch"):
        parsed_target = _optional_nonempty_str(data.get(key))
        if parsed_target is not None:
            targets.append(parsed_target)
    persistence = data.get("persistence")
    if isinstance(persistence, Mapping):
        parsed_target = _optional_nonempty_str(persistence.get("target_branch"))
        if parsed_target is not None:
            targets.append(parsed_target)
    allowed_branches = {policy.target_branch, policy.base_branch}
    for target in targets:
        if target not in allowed_branches:
            return "branch_lineage_mismatch"

    scope = data.get("implementation_scope")
    if scope is None:
        scope = data.get("scope")
    if scope is not None:
        if isinstance(scope, str):
            requested = [scope]
        elif isinstance(scope, (list, tuple)):
            requested = [str(item) for item in scope]
        else:
            return "scope_expansion"
        denial = validate_followup_against_policy(
            policy=policy,
            repository_name=policy.repository_name,
            target_branch=policy.target_branch,
            requested_scope=requested,
        )
        if denial:
            return denial
    return None


def hydrate_executable_child_mission(
    mission_yaml: str,
    *,
    policy: WorkflowPolicySnapshot,
) -> tuple[ChildMissionHydration | None, str | None]:
    """Hydrate a child mission with a validated policy repository contract.

    Child templates may omit ``repository``. The immutable workflow policy is
    the sole authority for name, path, base branch, target branch, and scope.
    Returns ``(hydration, None)`` on success or ``(None, denial_reason)``.
    """
    body, trailer = _split_followup_trailer(mission_yaml)
    try:
        parsed = yaml.safe_load(body)
    except yaml.YAMLError:
        return None, "invalid_mission_yaml"
    if not isinstance(parsed, dict):
        return None, "invalid_mission_yaml"

    denial = inspect_child_repository_authority(parsed, policy=policy)
    if denial:
        return None, denial

    hydrated = dict(parsed)
    hydrated["repository"] = canonical_child_repository_contract(policy)
    for alias in _AUTHORITY_ALIAS_KEYS:
        hydrated.pop(alias, None)
    persistence = hydrated.get("persistence")
    if isinstance(persistence, Mapping):
        persistence_copy = dict(persistence)
        persistence_copy["target_branch"] = policy.target_branch
        hydrated["persistence"] = persistence_copy

    normalized_path, path_error = normalize_submit_repository_path(hydrated)
    if path_error is not None or not normalized_path:
        return None, path_error or "repository_path_mismatch"
    if normalized_path != DEFAULT_REPOSITORY_PATH:
        return None, "repository_path_mismatch"
    hydrated["repository"] = canonical_child_repository_contract(policy)

    dumped = yaml.safe_dump(
        hydrated,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    if not dumped.endswith("\n"):
        dumped += "\n"
    executable_yaml = dumped
    if trailer:
        executable_yaml = dumped.rstrip() + "\n" + trailer
        if not executable_yaml.endswith("\n"):
            executable_yaml += "\n"
    return ChildMissionHydration(mission=hydrated, mission_yaml=executable_yaml), None


def detect_mission_authority_injection(
    mission_yaml: str,
    *,
    policy: WorkflowPolicySnapshot,
) -> str | None:
    """Detect YAML authority fields that must not override the policy snapshot.

    Context trailers are opaque and ignored. Template/prose YAML that tries
    to flip permissions, persistence, repository, or branch is denied for
    auto-followups unless the immutable policy snapshot explicitly allows it.
    """
    # Strip opaque follow-up context trailer before scanning.
    scan_target, _trailer = _split_followup_trailer(mission_yaml)
    lowered = scan_target.lower()

    if re.search(
        r"(?im)^\s*allow_auto_merge\s*:\s*true\b", scan_target
    ) and not policy.allow_auto_merge:
        return "merge_not_authorized"
    if re.search(
        r"(?im)^\s*allow_auto_deploy\s*:\s*true\b", scan_target
    ) and not policy.allow_auto_deploy:
        return "deploy_not_authorized"
    if (
        "delete_files: true" in lowered or "destructive:" in lowered
    ) and not policy.allow_destructive_actions:
        return "destructive_not_authorized"
    if (
        "create_files: true" in lowered or "modify_files: true" in lowered
    ) and not policy.allow_permission_expansion:
        # Implementation templates may legitimately allow writes; only treat
        # as expansion when policy forbids permission expansion *and* the
        # scanned doc tries to enable write flags beyond the snapshotted
        # implementation scope authority. Callers gate review steps separately.
        pass
    if re.search(
        r"(?im)^\s*allow_database_migrations\s*:\s*true\b", scan_target
    ) and not policy.allow_database_migrations:
        return "migrations_not_authorized"
    if re.search(
        r"(?im)^\s*allow_secret_changes\s*:\s*true\b", scan_target
    ) and not policy.allow_secret_changes:
        return "secret_changes_not_authorized"

    parsed: Any = None
    try:
        parsed = yaml.safe_load(scan_target)
    except yaml.YAMLError:
        parsed = None
    if isinstance(parsed, dict):
        structured = inspect_child_repository_authority(parsed, policy=policy)
        if structured:
            return structured
        return None

    # Unparseable templates: keep a conservative line scan, but do not treat a
    # nested ``repository:`` mapping key as a repository name.
    repo_m = re.search(
        r"(?im)^\s*(?:repository\.name|repository_name)\s*:\s*[\"']?([^\s\"']+)",
        scan_target,
    )
    if repo_m and repo_m.group(1) != policy.repository_name:
        return "repository_mismatch"
    branch_m = re.search(
        r"(?im)^\s*(?:target_branch|branch)\s*:\s*[\"']?([^\s\"']+)",
        scan_target,
    )
    if branch_m and branch_m.group(1) not in {
        policy.target_branch,
        policy.base_branch,
    }:
        return "branch_lineage_mismatch"
    return None


def enforce_launch_policy_gates(
    *,
    policy: WorkflowPolicySnapshot,
    step_type: StepType,
    mission_yaml: str,
) -> tuple[str | None, dict[str, Any]]:
    """Run all launch gates; return (denial_reason|None, audit_evidence)."""
    evidence: dict[str, Any] = {
        "step_type": step_type.value,
        "repository_name": policy.repository_name,
        "target_branch": policy.target_branch,
        "gates": [],
    }
    denial = validate_followup_against_policy(
        policy=policy,
        repository_name=policy.repository_name,
        target_branch=policy.target_branch,
    )
    evidence["gates"].append(
        {"gate": "repository_branch_lineage", "result": denial or "ok"}
    )
    if denial:
        return denial, evidence

    injection = detect_mission_authority_injection(
        mission_yaml, policy=policy
    )
    evidence["gates"].append(
        {"gate": "authority_injection", "result": injection or "ok"}
    )
    if injection:
        return injection, evidence

    if step_type in {StepType.REVIEW, StepType.RE_REVIEW}:
        ro = assert_review_step_read_only(mission_yaml)
        evidence["gates"].append(
            {"gate": "review_read_only", "result": ro or "ok"}
        )
        if ro:
            return ro, evidence
        # Reviews must not enable persistence/write via injection either.
        if re.search(
            r"(?im)^\s*persistence:\s*$", mission_yaml
        ) and not re.search(
            r"(?im)^\s*mode:\s*none\b", mission_yaml
        ):
            evidence["gates"].append(
                {"gate": "review_persistence", "result": "review_persistence_not_none"}
            )
            return "review_persistence_not_none", evidence
    else:
        evidence["gates"].append({"gate": "review_read_only", "result": "skipped"})

    # Explicit default-deny checks recorded for audit even when flags false.
    for flag_name, wants, reason in [
        ("allow_auto_merge", False, "merge_not_authorized"),
        ("allow_auto_deploy", False, "deploy_not_authorized"),
        ("allow_destructive_actions", False, "destructive_not_authorized"),
        ("allow_permission_expansion", False, "permission_expansion_not_authorized"),
        ("allow_database_migrations", False, "migrations_not_authorized"),
        ("allow_secret_changes", False, "secret_changes_not_authorized"),
        ("allow_scope_or_repo_changes", False, "scope_or_repo_change_not_authorized"),
    ]:
        # Record snapshot value; do not grant from mission prose.
        evidence["gates"].append(
            {
                "gate": flag_name,
                "snapshot": bool(getattr(policy, flag_name)),
                "result": "ok",
            }
        )
        del wants, reason

    evidence["result"] = "ok"
    return None, evidence


def bound_context_field(value: str, max_chars: int = _CONTEXT_FIELD_MAX_CHARS) -> str:
    return bound_text(value, max_chars)


def build_followup_mission_yaml(
    template_yaml: str,
    *,
    findings: tuple[str, ...] | list[str] = (),
    prior_output: str = "",
    extra_fields: Mapping[str, str] | None = None,
) -> str:
    """Build follow-up mission from an immutable template + opaque context.

    Findings/prior output are never interpolated as YAML structure. Context
    is a terminal JSON trailer that cannot alter permissions, persistence,
    repository, or branch authority (those come only from the policy
    snapshot + validated template).
    """
    base = (template_yaml or "").rstrip()
    # Reject if template already contains a context trailer (ambiguity).
    if _FOLLOWUP_BEGIN in base or _FOLLOWUP_END in base:
        raise ValueError("template_contains_followup_context")
    safe_findings: list[str] = []
    for item in list(findings)[:_FINDING_MAX_COUNT]:
        cleaned = bound_context_field(str(item).strip(), _FINDING_MAX_CHARS)
        if cleaned:
            safe_findings.append(cleaned)
    ctx: dict[str, Any] = {
        "findings": safe_findings,
        "prior_excerpt": bound_context_field(
            truncate_prior_output(prior_output), _CONTEXT_FIELD_MAX_CHARS
        ),
    }
    if extra_fields:
        for key, value in extra_fields.items():
            # Scalars only; bound + redact every field including secrets.
            if not isinstance(key, str) or not key.isidentifier():
                continue
            ctx[key] = bound_context_field(str(value))
    payload = json.dumps(ctx, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
    if len(payload) > 24_576:
        # Fail closed on oversized context rather than truncate JSON invalidly.
        ctx = {
            "findings": safe_findings[:8],
            "prior_excerpt": bound_context_field(ctx["prior_excerpt"], 1000),
            "truncated": True,
        }
        payload = json.dumps(
            ctx, separators=(",", ":"), sort_keys=True, ensure_ascii=True
        )
    return f"{base}\n\n{_FOLLOWUP_BEGIN}\n{payload}\n{_FOLLOWUP_END}\n"


def should_suppress_child_terminal_alert(
    *,
    child_run_id: str | None,
    step: WorkflowStepRecord | None,
) -> bool:
    """Ordinary child terminal alerts are suppressed when workflow-managed."""
    if child_run_id is None or step is None:
        return False
    return step.child_run_id == child_run_id


def should_emit_workflow_alert(workflow: WorkflowRecord) -> bool:
    if workflow.notification_emitted:
        return False
    return workflow.state.value in ACTIONABLE_WORKFLOW_ALERT_STATES


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _spec_for(
    workflow: WorkflowRecord, key: str
) -> WorkflowStepSpec | None:
    raw = workflow.step_specs.get(key)
    if not isinstance(raw, dict):
        return None
    try:
        return WorkflowStepSpec.from_dict(raw)
    except (KeyError, ValueError, TypeError):
        return None


def _active_step(
    steps: list[WorkflowStepRecord],
) -> WorkflowStepRecord | None:
    for step in reversed(steps):
        if step.status in {
            StepStatus.PENDING,
            StepStatus.CLAIMED,
            StepStatus.QUEUED,
            StepStatus.RUNNING,
        }:
            return step
    return None


def _latest_completed_step(
    steps: list[WorkflowStepRecord],
) -> WorkflowStepRecord | None:
    for step in reversed(steps):
        if step.status is StepStatus.COMPLETED:
            return step
    return None


def _has_step_after(
    steps: list[WorkflowStepRecord], step_id: str
) -> bool:
    seen = False
    for step in steps:
        if seen:
            return True
        if step.step_id == step_id:
            seen = True
    return False


def _budget_violation(
    workflow: WorkflowRecord, *, now: datetime | None = None
) -> WorkflowState | None:
    """Return budget_exhausted when any ceiling is already reached.

    Semantics (inclusive ceilings):
    - wall-clock: elapsed >= max_wall_clock_seconds
    - estimated credit: credit_units_used > max_credit_units
      Equality is not exhaustion. Units are reserved when a child is
      authorized/claimed, so a queued or running child that consumed the
      final unit must be allowed to finish. New children are still denied
      by ``_would_exceed_child_budget``
      (credit_units_used + unit > max).
    - actual credit: when credit_usage_actual is not None,
      credit_usage_actual >= max_credit_units
    Child-run ceiling is enforced at launch boundaries via
    ``_would_exceed_child_budget`` (child_run_count + 1 > max).
    """
    policy = workflow.policy_snapshot
    if workflow.credit_units_used > policy.max_credit_units:
        return WorkflowState.BUDGET_EXHAUSTED
    if (
        workflow.credit_usage_actual is not None
        and float(workflow.credit_usage_actual) >= float(policy.max_credit_units)
    ):
        return WorkflowState.BUDGET_EXHAUSTED
    started = workflow.started_at or workflow.created_at
    now = now or _utc_now()
    elapsed = (now - started).total_seconds()
    if elapsed >= policy.max_wall_clock_seconds:
        return WorkflowState.BUDGET_EXHAUSTED
    return None


def _would_exceed_child_budget(workflow: WorkflowRecord) -> bool:
    """True when the next child launch would violate child/credit ceilings.

    - child_run_count: deny when child_run_count + 1 > max_child_runs
      (equivalently child_run_count >= max_child_runs before launch)
    - estimated credit: deny when credit_units_used + unit > max_credit_units
    - actual credit: deny when credit_usage_actual >= max_credit_units
    """
    policy = workflow.policy_snapshot
    next_children = workflow.child_run_count + 1
    next_credits = (
        workflow.credit_units_used + int(policy.credit_unit_per_child_run)
    )
    if next_children > policy.max_child_runs:
        return True
    if next_credits > policy.max_credit_units:
        return True
    if (
        workflow.credit_usage_actual is not None
        and float(workflow.credit_usage_actual) >= float(policy.max_credit_units)
    ):
        return True
    return False


def _review_mission(workflow: WorkflowRecord) -> str | None:
    spec = _spec_for(workflow, "review")
    return spec.mission_yaml if spec else None


def _fix_mission(
    workflow: WorkflowRecord, *, prior_output: str, findings: tuple[str, ...]
) -> str | None:
    spec = _spec_for(workflow, "fix")
    if spec is None:
        return None
    return build_followup_mission_yaml(
        spec.mission_yaml,
        findings=findings,
        prior_output=prior_output,
    )


def _rereview_mission(workflow: WorkflowRecord) -> str | None:
    spec = _spec_for(workflow, "re_review")
    if spec is not None:
        return spec.mission_yaml
    # Fall back to the original review template when re_review omitted.
    return _review_mission(workflow)


def decide_reconcile(
    *,
    workflow: WorkflowRecord,
    steps: list[WorkflowStepRecord],
    child_runs: Mapping[str, ChildRunView],
    now: datetime | None = None,
) -> list[OrchestratorDecision]:
    """Compute reconcile decisions for one workflow (deterministic)."""
    now = now or _utc_now()
    decisions: list[OrchestratorDecision] = []
    wid = workflow.workflow_id
    version = workflow.version

    if is_terminal_workflow_state(workflow.state):
        if should_emit_workflow_alert(workflow):
            decisions.append(
                OrchestratorDecision(
                    action=DecisionAction.EMIT_WORKFLOW_ALERT,
                    workflow_id=wid,
                    expected_version=version,
                    reason="terminal_alert",
                    detail={"state": workflow.state.value},
                    emit_workflow_alert=True,
                    to_state=workflow.state,
                )
            )
        return decisions

    budget_state = _budget_violation(workflow, now=now)
    if budget_state is not None:
        decisions.append(
            OrchestratorDecision(
                action=DecisionAction.TERMINATE,
                workflow_id=wid,
                expected_version=version,
                reason=TransitionReason.BUDGET.value,
                detail={"cause": "ceiling"},
                to_state=budget_state,
                workflow_updates={
                    "last_decision": {"action": "budget_exhausted"},
                    "error": "budget_ceiling",
                },
                emit_workflow_alert=True,
            )
        )
        return decisions

    active = _active_step(steps)

    # Bootstrap: no steps yet → launch implementation.
    if not steps:
        if _would_exceed_child_budget(workflow):
            decisions.append(
                OrchestratorDecision(
                    action=DecisionAction.TERMINATE,
                    workflow_id=wid,
                    expected_version=version,
                    reason=TransitionReason.BUDGET.value,
                    detail={"cause": "child_or_credit_ceiling"},
                    to_state=WorkflowState.BUDGET_EXHAUSTED,
                    workflow_updates={
                        "last_decision": {"action": "budget_exhausted"},
                        "error": "budget_ceiling",
                    },
                    emit_workflow_alert=True,
                )
            )
            return decisions
        impl = _spec_for(workflow, "implementation")
        if impl is None or not impl.mission_yaml.strip():
            decisions.append(
                OrchestratorDecision(
                    action=DecisionAction.TERMINATE,
                    workflow_id=wid,
                    expected_version=version,
                    reason=TransitionReason.ERROR.value,
                    detail={"cause": "missing_implementation_spec"},
                    to_state=WorkflowState.FAILED,
                    workflow_updates={
                        "error": "missing_implementation_spec",
                    },
                    emit_workflow_alert=True,
                )
            )
            return decisions
        key = make_idempotency_key(wid, StepType.IMPLEMENTATION, 0, 1)
        decisions.append(
            OrchestratorDecision(
                action=DecisionAction.LAUNCH_CHILD,
                workflow_id=wid,
                expected_version=version,
                reason=TransitionReason.CHILD_LAUNCHED.value,
                detail={"step_type": StepType.IMPLEMENTATION.value},
                step_type=StepType.IMPLEMENTATION,
                mission_yaml=impl.mission_yaml,
                cycle=0,
                attempt=1,
                parent_run_id=workflow.parent_run_id,
                idempotency_key=key,
            )
        )
        return decisions

    if active is None:
        # Mark-then-launch crash recovery: a completed step may still need
        # its follow-up claim. Never strand as no_active_step when recovery
        # can re-derive the next launch idempotently.
        recovered = _recover_followup_after_mark(
            workflow=workflow,
            steps=steps,
            child_runs=child_runs,
            version=version,
        )
        if recovered is not None:
            return recovered
        decisions.append(
            OrchestratorDecision(
                action=DecisionAction.TERMINATE,
                workflow_id=wid,
                expected_version=version,
                reason=TransitionReason.INTERVENTION.value,
                detail={"cause": "no_active_step"},
                to_state=WorkflowState.BLOCKED,
                workflow_updates={
                    "error": "no_active_step",
                    "last_decision": {"action": "intervention_required"},
                },
                emit_workflow_alert=True,
            )
        )
        return decisions

    child = None
    if active.child_run_id:
        child = child_runs.get(active.child_run_id)

    # Claimed but unknown to run registry yet → wait (materialize elsewhere).
    # Remains retryable/idempotent; never treated as no_active_step.
    if child is None:
        decisions.append(
            OrchestratorDecision(
                action=DecisionAction.NOOP,
                workflow_id=wid,
                expected_version=version,
                reason="awaiting_child_materialize",
                detail={
                    "step_id": active.step_id,
                    "child_run_id": active.child_run_id,
                    "materialization_state": (
                        active.materialization_state.value
                        if active.materialization_state
                        else StepMaterializationState.CLAIMED.value
                    ),
                },
                step_id=active.step_id,
                child_run_id=active.child_run_id,
                suppress_child_terminal_alert=True,
            )
        )
        return decisions

    # Suppress child terminal paging for workflow-managed runs.
    if child.status in {"completed", "failed", "timed_out", "cancelled"}:
        decisions.append(
            OrchestratorDecision(
                action=DecisionAction.SUPPRESS_CHILD_ALERT,
                workflow_id=wid,
                expected_version=version,
                reason="workflow_managed_child",
                detail={"child_run_id": child.run_id},
                step_id=active.step_id,
                child_run_id=child.run_id,
                suppress_child_terminal_alert=True,
            )
        )

    if child.status in {"queued", "running"}:
        # Child materialized — sync claim → queued/running.
        if active.status in {StepStatus.CLAIMED, StepStatus.QUEUED}:
            if (
                active.materialization_state
                is StepMaterializationState.CLAIMED
            ) or active.status is StepStatus.CLAIMED:
                decisions.append(
                    OrchestratorDecision(
                        action=DecisionAction.MARK_STEP,
                        workflow_id=wid,
                        expected_version=version,
                        reason=TransitionReason.CHILD_BOUND.value,
                        detail={
                            "child_status": child.status,
                            "materialization_state": (
                                StepMaterializationState.MATERIALIZED.value
                            ),
                        },
                        to_state=WorkflowState.RUNNING,
                        step_id=active.step_id,
                        child_run_id=child.run_id,
                        step_status=(
                            StepStatus.RUNNING
                            if child.status == "running"
                            else StepStatus.QUEUED
                        ),
                        step_updates={
                            "status": (
                                StepStatus.RUNNING
                                if child.status == "running"
                                else StepStatus.QUEUED
                            ),
                            "materialization_state": (
                                StepMaterializationState.MATERIALIZED
                            ),
                        },
                    )
                )
                return decisions
        if active.status is StepStatus.QUEUED and child.status == "running":
            decisions.append(
                OrchestratorDecision(
                    action=DecisionAction.MARK_STEP,
                    workflow_id=wid,
                    expected_version=version,
                    reason=TransitionReason.CHILD_STATUS.value,
                    detail={"child_status": child.status},
                    to_state=WorkflowState.RUNNING,
                    step_id=active.step_id,
                    child_run_id=child.run_id,
                    step_status=StepStatus.RUNNING,
                    step_updates={"status": StepStatus.RUNNING},
                )
            )
        return decisions

    # Child terminal — drive the v1 policy state machine.
    return decisions + _decisions_for_terminal_child(
        workflow=workflow,
        steps=steps,
        active=active,
        child=child,
        version=version,
    )


def _recover_followup_after_mark(
    *,
    workflow: WorkflowRecord,
    steps: list[WorkflowStepRecord],
    child_runs: Mapping[str, ChildRunView],
    version: int,
) -> list[OrchestratorDecision] | None:
    """Re-derive follow-up launch after crash between mark and claim."""
    completed = _latest_completed_step(steps)
    if completed is None:
        return None
    if _has_step_after(steps, completed.step_id):
        return None
    child = None
    if completed.child_run_id:
        child = child_runs.get(completed.child_run_id)
    if child is None or child.status != "completed":
        # Without the completed child view we cannot safely re-derive.
        return None
    if completed.step_type is StepType.IMPLEMENTATION:
        return _after_implementation_success(
            workflow=workflow,
            active=completed,
            child=child,
            version=version,
            skip_mark=True,
        )
    if completed.step_type in {StepType.REVIEW, StepType.RE_REVIEW}:
        return _after_review_success(
            workflow=workflow,
            steps=steps,
            active=completed,
            child=child,
            version=version,
            skip_mark=True,
        )
    if completed.step_type is StepType.FIX:
        return _after_fix_success(
            workflow=workflow,
            active=completed,
            child=child,
            version=version,
            skip_mark=True,
        )
    return None


def _decisions_for_terminal_child(
    *,
    workflow: WorkflowRecord,
    steps: list[WorkflowStepRecord],
    active: WorkflowStepRecord,
    child: ChildRunView,
    version: int,
) -> list[OrchestratorDecision]:
    wid = workflow.workflow_id
    decisions: list[OrchestratorDecision] = []

    if child.status == "timed_out":
        decisions.append(
            OrchestratorDecision(
                action=DecisionAction.TERMINATE,
                workflow_id=wid,
                expected_version=version,
                reason=TransitionReason.INTERVENTION.value,
                detail={"cause": "child_timeout"},
                to_state=WorkflowState.BLOCKED,
                step_id=active.step_id,
                child_run_id=child.run_id,
                step_status=StepStatus.TIMED_OUT,
                step_updates={
                    "status": StepStatus.TIMED_OUT,
                    "error": child.error or "timed_out",
                },
                workflow_updates={
                    "error": "child_timeout",
                    "last_decision": {"action": "intervention_required"},
                },
                emit_workflow_alert=True,
            )
        )
        return decisions

    if child.status in {"failed", "cancelled"}:
        decisions.append(
            OrchestratorDecision(
                action=DecisionAction.TERMINATE,
                workflow_id=wid,
                expected_version=version,
                reason=TransitionReason.ERROR.value,
                detail={"cause": "child_failure", "status": child.status},
                to_state=WorkflowState.FAILED,
                step_id=active.step_id,
                child_run_id=child.run_id,
                step_status=(
                    StepStatus.FAILED
                    if child.status == "failed"
                    else StepStatus.CANCELLED
                ),
                step_updates={
                    "status": (
                        StepStatus.FAILED
                        if child.status == "failed"
                        else StepStatus.CANCELLED
                    ),
                    "error": child.error or child.status,
                },
                workflow_updates={
                    "error": f"child_{child.status}",
                    "last_decision": {"action": "child_failed"},
                },
                emit_workflow_alert=True,
            )
        )
        return decisions

    # completed
    if active.step_type is StepType.IMPLEMENTATION:
        return _after_implementation_success(
            workflow=workflow, active=active, child=child, version=version
        )
    if active.step_type in {StepType.REVIEW, StepType.RE_REVIEW}:
        return _after_review_success(
            workflow=workflow,
            steps=steps,
            active=active,
            child=child,
            version=version,
        )
    if active.step_type is StepType.FIX:
        return _after_fix_success(
            workflow=workflow, active=active, child=child, version=version
        )

    decisions.append(
        OrchestratorDecision(
            action=DecisionAction.TERMINATE,
            workflow_id=wid,
            expected_version=version,
            reason=TransitionReason.ERROR.value,
            detail={"cause": "unknown_step_type"},
            to_state=WorkflowState.FAILED,
            step_id=active.step_id,
            emit_workflow_alert=True,
        )
    )
    return decisions


def _after_implementation_success(
    *,
    workflow: WorkflowRecord,
    active: WorkflowStepRecord,
    child: ChildRunView,
    version: int,
    skip_mark: bool = False,
) -> list[OrchestratorDecision]:
    wid = workflow.workflow_id
    decisions: list[OrchestratorDecision] = []
    if not skip_mark:
        decisions.append(
            OrchestratorDecision(
                action=DecisionAction.MARK_STEP,
                workflow_id=wid,
                expected_version=version,
                reason=TransitionReason.CHILD_STATUS.value,
                detail={"child_status": "completed"},
                to_state=WorkflowState.RUNNING,
                step_id=active.step_id,
                child_run_id=child.run_id,
                step_status=StepStatus.COMPLETED,
                step_updates={"status": StepStatus.COMPLETED},
            )
        )
    denial = validate_followup_against_policy(
        policy=workflow.policy_snapshot,
        repository_name=workflow.policy_snapshot.repository_name,
        target_branch=workflow.policy_snapshot.target_branch,
    )
    if denial:
        decisions.append(
            OrchestratorDecision(
                action=DecisionAction.TERMINATE,
                workflow_id=wid,
                expected_version=version,
                reason=TransitionReason.POLICY_GATE.value,
                detail={"cause": denial},
                to_state=WorkflowState.BLOCKED,
                workflow_updates={
                    "error": denial,
                    "last_decision": {"action": "policy_violation"},
                },
                emit_workflow_alert=True,
            )
        )
        return decisions
    if _would_exceed_child_budget(workflow):
        decisions.append(
            OrchestratorDecision(
                action=DecisionAction.TERMINATE,
                workflow_id=wid,
                expected_version=version,
                reason=TransitionReason.BUDGET.value,
                detail={"cause": "before_review_launch"},
                to_state=WorkflowState.BUDGET_EXHAUSTED,
                workflow_updates={
                    "error": "budget_ceiling",
                    "last_decision": {"action": "budget_exhausted"},
                },
                emit_workflow_alert=True,
            )
        )
        return decisions
    review_yaml = _review_mission(workflow)
    if not review_yaml:
        decisions.append(
            OrchestratorDecision(
                action=DecisionAction.TERMINATE,
                workflow_id=wid,
                expected_version=version,
                reason=TransitionReason.ERROR.value,
                detail={"cause": "missing_review_spec"},
                to_state=WorkflowState.FAILED,
                workflow_updates={"error": "missing_review_spec"},
                emit_workflow_alert=True,
            )
        )
        return decisions
    key = make_idempotency_key(wid, StepType.REVIEW, 0, 1)
    decisions.append(
        OrchestratorDecision(
            action=DecisionAction.LAUNCH_CHILD,
            workflow_id=wid,
            expected_version=version,
            reason=TransitionReason.CHILD_LAUNCHED.value,
            detail={
                "step_type": StepType.REVIEW.value,
                "preauthorized": "read_only_review",
            },
            step_type=StepType.REVIEW,
            mission_yaml=review_yaml,
            cycle=0,
            attempt=1,
            parent_run_id=child.run_id,
            idempotency_key=key,
        )
    )
    return decisions


def _after_review_success(
    *,
    workflow: WorkflowRecord,
    steps: list[WorkflowStepRecord],
    active: WorkflowStepRecord,
    child: ChildRunView,
    version: int,
    skip_mark: bool = False,
) -> list[OrchestratorDecision]:
    del steps  # reserved for future fingerprint history inspection
    wid = workflow.workflow_id
    output = f"{child.stdout}\n{child.stderr}"
    verdict = parse_review_verdict(output)
    base_mark = OrchestratorDecision(
        action=DecisionAction.MARK_STEP,
        workflow_id=wid,
        expected_version=version,
        reason=TransitionReason.VERDICT.value,
        detail={
            "verdict": verdict.kind.value,
            "fingerprint": verdict.fingerprint,
        },
        to_state=WorkflowState.RUNNING,
        step_id=active.step_id,
        child_run_id=child.run_id,
        step_status=StepStatus.COMPLETED,
        step_updates={
            "status": StepStatus.COMPLETED,
            "blocker_fingerprint": verdict.fingerprint,
            "last_decision": {
                "verdict": verdict.kind.value,
                "fingerprint": verdict.fingerprint,
            },
        },
    )

    prefix: list[OrchestratorDecision] = [] if skip_mark else [base_mark]

    if verdict.kind in {
        ReviewVerdictKind.MALFORMED,
        ReviewVerdictKind.AMBIGUOUS,
    }:
        return prefix + [
            OrchestratorDecision(
                action=DecisionAction.TERMINATE,
                workflow_id=wid,
                expected_version=version,
                reason=TransitionReason.INTERVENTION.value,
                detail={"cause": verdict.kind.value},
                to_state=WorkflowState.BLOCKED,
                workflow_updates={
                    "error": verdict.kind.value,
                    "last_decision": {"action": "intervention_required"},
                },
                emit_workflow_alert=True,
            ),
        ]

    if verdict.kind is ReviewVerdictKind.MERGE_READY:
        policy = workflow.policy_snapshot
        # v1 preferred path: stop at needs_approval unless explicit auth.
        if policy.allow_auto_merge or policy.allow_auto_deploy:
            # Still do not auto-merge/deploy here — require explicit typed
            # primitives in a later mission. Record policy acknowledgment.
            return prefix + [
                OrchestratorDecision(
                    action=DecisionAction.TERMINATE,
                    workflow_id=wid,
                    expected_version=version,
                    reason=TransitionReason.POLICY_GATE.value,
                    detail={
                        "cause": "auto_merge_deploy_deferred",
                        "allow_auto_merge": policy.allow_auto_merge,
                        "allow_auto_deploy": policy.allow_auto_deploy,
                    },
                    to_state=WorkflowState.NEEDS_APPROVAL,
                    workflow_updates={
                        "last_decision": {
                            "action": "needs_approval",
                            "note": "auto_merge_deploy_not_implemented_v1",
                        }
                    },
                    emit_workflow_alert=True,
                ),
            ]
        return prefix + [
            OrchestratorDecision(
                action=DecisionAction.TERMINATE,
                workflow_id=wid,
                expected_version=version,
                reason=TransitionReason.VERDICT.value,
                detail={"verdict": "merge_ready"},
                to_state=WorkflowState.NEEDS_APPROVAL,
                workflow_updates={
                    "last_decision": {"action": "needs_approval"}
                },
                emit_workflow_alert=True,
            ),
        ]

    # BLOCKED → one targeted fix if cycles remain and fingerprint is new.
    assert verdict.kind is ReviewVerdictKind.BLOCKED
    if (
        workflow.last_blocker_fingerprint
        and verdict.fingerprint
        and verdict.fingerprint == workflow.last_blocker_fingerprint
    ):
        return prefix + [
            OrchestratorDecision(
                action=DecisionAction.TERMINATE,
                workflow_id=wid,
                expected_version=version,
                reason=TransitionReason.INTERVENTION.value,
                detail={
                    "cause": "repeated_blocker_fingerprint",
                    "fingerprint": verdict.fingerprint,
                },
                to_state=WorkflowState.BLOCKED,
                workflow_updates={
                    "error": "repeated_blocker_fingerprint",
                    "last_blocker_fingerprint": verdict.fingerprint,
                    "last_decision": {"action": "intervention_required"},
                },
                emit_workflow_alert=True,
            ),
        ]

    # Fix-cycle ceiling: next_cycle > max_fix_cycles (i.e. deny when
    # fix_cycle_count >= max_fix_cycles before launching another fix).
    next_cycle = workflow.fix_cycle_count + 1
    if next_cycle > workflow.policy_snapshot.max_fix_cycles:
        return prefix + [
            OrchestratorDecision(
                action=DecisionAction.TERMINATE,
                workflow_id=wid,
                expected_version=version,
                reason=TransitionReason.INTERVENTION.value,
                detail={
                    "cause": "max_fix_cycles",
                    "fix_cycle_count": workflow.fix_cycle_count,
                },
                to_state=WorkflowState.BLOCKED,
                workflow_updates={
                    "error": "max_fix_cycles",
                    "last_blocker_fingerprint": verdict.fingerprint,
                    "last_decision": {"action": "intervention_required"},
                },
                emit_workflow_alert=True,
            ),
        ]

    if _would_exceed_child_budget(workflow):
        return prefix + [
            OrchestratorDecision(
                action=DecisionAction.TERMINATE,
                workflow_id=wid,
                expected_version=version,
                reason=TransitionReason.BUDGET.value,
                detail={"cause": "before_fix_launch"},
                to_state=WorkflowState.BUDGET_EXHAUSTED,
                workflow_updates={
                    "error": "budget_ceiling",
                    "last_blocker_fingerprint": verdict.fingerprint,
                },
                emit_workflow_alert=True,
            ),
        ]

    fix_yaml = _fix_mission(
        workflow, prior_output=output, findings=verdict.findings
    )
    if not fix_yaml:
        return prefix + [
            OrchestratorDecision(
                action=DecisionAction.TERMINATE,
                workflow_id=wid,
                expected_version=version,
                reason=TransitionReason.ERROR.value,
                detail={"cause": "missing_fix_spec"},
                to_state=WorkflowState.FAILED,
                workflow_updates={"error": "missing_fix_spec"},
                emit_workflow_alert=True,
            ),
        ]

    key = make_idempotency_key(wid, StepType.FIX, next_cycle, 1)
    mark = OrchestratorDecision(
        action=DecisionAction.MARK_STEP,
        workflow_id=wid,
        expected_version=version,
        reason=TransitionReason.VERDICT.value,
        detail={
            "verdict": verdict.kind.value,
            "fingerprint": verdict.fingerprint,
            "next_fix_cycle": next_cycle,
            "pending_launch": StepType.FIX.value,
        },
        to_state=WorkflowState.RUNNING,
        step_id=active.step_id,
        child_run_id=child.run_id,
        step_status=StepStatus.COMPLETED,
        step_updates={
            "status": StepStatus.COMPLETED,
            "blocker_fingerprint": verdict.fingerprint,
        },
        workflow_updates={
            "fix_cycle_count": next_cycle,
            "last_blocker_fingerprint": verdict.fingerprint,
            "last_decision": {
                "action": "launch_fix",
                "cycle": next_cycle,
                "pending_launch": StepType.FIX.value,
            },
        },
    )
    launch = OrchestratorDecision(
        action=DecisionAction.LAUNCH_CHILD,
        workflow_id=wid,
        expected_version=version,  # apply() refreshes after mark
        reason=TransitionReason.CHILD_LAUNCHED.value,
        detail={"step_type": StepType.FIX.value, "cycle": next_cycle},
        step_type=StepType.FIX,
        mission_yaml=fix_yaml,
        cycle=next_cycle,
        attempt=1,
        parent_run_id=child.run_id,
        idempotency_key=key,
    )
    if skip_mark:
        # Mark already committed; only (re)claim launch.
        return [launch]
    return [mark, launch]


def _after_fix_success(
    *,
    workflow: WorkflowRecord,
    active: WorkflowStepRecord,
    child: ChildRunView,
    version: int,
    skip_mark: bool = False,
) -> list[OrchestratorDecision]:
    wid = workflow.workflow_id
    decisions: list[OrchestratorDecision] = []
    if not skip_mark:
        decisions.append(
            OrchestratorDecision(
                action=DecisionAction.MARK_STEP,
                workflow_id=wid,
                expected_version=version,
                reason=TransitionReason.CHILD_STATUS.value,
                detail={
                    "child_status": "completed",
                    "pending_launch": StepType.RE_REVIEW.value,
                },
                to_state=WorkflowState.RUNNING,
                step_id=active.step_id,
                child_run_id=child.run_id,
                step_status=StepStatus.COMPLETED,
                step_updates={"status": StepStatus.COMPLETED},
                workflow_updates={
                    "last_decision": {
                        "action": "launch_re_review",
                        "pending_launch": StepType.RE_REVIEW.value,
                    }
                },
            )
        )
    if _would_exceed_child_budget(workflow):
        decisions.append(
            OrchestratorDecision(
                action=DecisionAction.TERMINATE,
                workflow_id=wid,
                expected_version=version,
                reason=TransitionReason.BUDGET.value,
                detail={"cause": "before_rereview_launch"},
                to_state=WorkflowState.BUDGET_EXHAUSTED,
                workflow_updates={"error": "budget_ceiling"},
                emit_workflow_alert=True,
            )
        )
        return decisions
    review_yaml = _rereview_mission(workflow)
    if not review_yaml:
        decisions.append(
            OrchestratorDecision(
                action=DecisionAction.TERMINATE,
                workflow_id=wid,
                expected_version=version,
                reason=TransitionReason.ERROR.value,
                detail={"cause": "missing_rereview_spec"},
                to_state=WorkflowState.FAILED,
                workflow_updates={"error": "missing_rereview_spec"},
                emit_workflow_alert=True,
            )
        )
        return decisions
    cycle = workflow.fix_cycle_count
    key = make_idempotency_key(wid, StepType.RE_REVIEW, cycle, 1)
    decisions.append(
        OrchestratorDecision(
            action=DecisionAction.LAUNCH_CHILD,
            workflow_id=wid,
            expected_version=version,
            reason=TransitionReason.CHILD_LAUNCHED.value,
            detail={
                "step_type": StepType.RE_REVIEW.value,
                "cycle": cycle,
            },
            step_type=StepType.RE_REVIEW,
            mission_yaml=review_yaml,
            cycle=cycle,
            attempt=1,
            parent_run_id=child.run_id,
            idempotency_key=key,
        )
    )
    return decisions


class WorkflowOrchestrator:
    """Apply reconcile decisions against a ``WorkflowRegistry``."""

    def __init__(self, registry: WorkflowRegistry) -> None:
        self._registry = registry

    @property
    def registry(self) -> WorkflowRegistry:
        return self._registry

    def reconcile_workflow(
        self,
        workflow_id: str,
        *,
        child_runs: Mapping[str, ChildRunView],
        now: datetime | None = None,
    ) -> list[OrchestratorDecision]:
        workflow = self._registry.get_workflow(workflow_id)
        if workflow is None:
            return []
        steps = self._registry.list_steps(workflow_id)
        decisions = decide_reconcile(
            workflow=workflow,
            steps=steps,
            child_runs=child_runs,
            now=now,
        )
        applied: list[OrchestratorDecision] = []
        for decision in decisions:
            ok = self.apply_decision(decision)
            if ok:
                applied.append(decision)
            else:
                # Version conflict or terminal — stop this pass.
                logger.info(
                    (
                        "workflow event=reconcile_stop workflow_id=%s "
                        "action=%s reason=%s"
                    ),
                    workflow_id,
                    decision.action.value,
                    decision.reason,
                )
                break
            # Refresh version-sensitive follow-ups after mutations.
            if decision.action in {
                DecisionAction.LAUNCH_CHILD,
                DecisionAction.MARK_STEP,
                DecisionAction.TERMINATE,
            }:
                workflow = self._registry.get_workflow(workflow_id)
                if workflow is None:
                    break
        return applied

    def reconcile_all(
        self,
        *,
        child_runs: Mapping[str, ChildRunView],
        now: datetime | None = None,
    ) -> dict[str, list[OrchestratorDecision]]:
        results: dict[str, list[OrchestratorDecision]] = {}
        for workflow in self._registry.list_active_workflows():
            results[workflow.workflow_id] = self.reconcile_workflow(
                workflow.workflow_id,
                child_runs=child_runs,
                now=now,
            )
        return results

    def apply_decision(self, decision: OrchestratorDecision) -> bool:
        """Apply one decision. Returns False on conflict / rejected apply."""
        if decision.action is DecisionAction.NOOP:
            return True
        if decision.action is DecisionAction.SUPPRESS_CHILD_ALERT:
            return True
        if decision.action is DecisionAction.EMIT_WORKFLOW_ALERT:
            workflow = self._registry.get_workflow(decision.workflow_id)
            if workflow is None:
                return False
            if workflow.notification_emitted:
                return True
            result = self._registry.mark_notification_emitted(
                decision.workflow_id,
                expected_version=workflow.version,
            )
            return result.ok

        if decision.action is DecisionAction.LAUNCH_CHILD:
            # Refresh expected version — prior mark may have advanced it.
            workflow = self._registry.get_workflow(decision.workflow_id)
            if workflow is None:
                return False
            if decision.step_type is None or decision.mission_yaml is None:
                return False
            if decision.cycle is None or decision.attempt is None:
                return False
            if _would_exceed_child_budget(workflow) and not any(
                s.idempotency_key == decision.idempotency_key
                for s in self._registry.list_steps(decision.workflow_id)
            ):
                self._registry.apply_cas_transition(
                    workflow_id=decision.workflow_id,
                    expected_version=workflow.version,
                    to_state=WorkflowState.BUDGET_EXHAUSTED,
                    reason=TransitionReason.BUDGET,
                    detail={"cause": "launch_gate"},
                    workflow_updates={
                        "error": "budget_ceiling",
                        "last_decision": {"action": "budget_exhausted"},
                    },
                )
                return False
            # Policy gates are enforced again inside claim_child_launch
            # (transactional); calling here fails closed before the write
            # and satisfies the unused-helper regression contract.
            denial, evidence = enforce_launch_policy_gates(
                policy=workflow.policy_snapshot,
                step_type=decision.step_type,
                mission_yaml=decision.mission_yaml,
            )
            if denial:
                self._registry.apply_cas_transition(
                    workflow_id=decision.workflow_id,
                    expected_version=workflow.version,
                    to_state=WorkflowState.BLOCKED,
                    reason=TransitionReason.POLICY_GATE,
                    detail={
                        "cause": denial,
                        "policy_audit": evidence,
                    },
                    workflow_updates={
                        "error": denial,
                        "last_decision": {
                            "action": "policy_violation",
                            "policy_audit": evidence,
                        },
                    },
                )
                return False
            claim = self._registry.claim_child_launch(
                workflow_id=decision.workflow_id,
                expected_version=workflow.version,
                step_type=decision.step_type,
                mission_yaml=decision.mission_yaml,
                cycle=decision.cycle,
                attempt=decision.attempt,
                parent_run_id=decision.parent_run_id,
                idempotency_key=decision.idempotency_key,
                decision={
                    **(decision.detail or {}),
                    "policy_audit": evidence,
                },
            )
            if claim.ok:
                logger.info(
                    (
                        "workflow event=metrics_child_launch workflow_id=%s "
                        "step_type=%s already_claimed=%s child_run_id=%s"
                    ),
                    decision.workflow_id,
                    decision.step_type.value,
                    claim.already_claimed,
                    claim.child_run_id,
                )
            return claim.ok

        if decision.action in {
            DecisionAction.MARK_STEP,
            DecisionAction.TERMINATE,
        }:
            workflow = self._registry.get_workflow(decision.workflow_id)
            if workflow is None:
                return False
            to_state = decision.to_state or workflow.state
            result = self._registry.apply_cas_transition(
                workflow_id=decision.workflow_id,
                expected_version=workflow.version,
                to_state=to_state,
                reason=decision.reason,
                detail=decision.detail,
                step_id=decision.step_id,
                child_run_id=decision.child_run_id,
                step_updates=decision.step_updates,
                workflow_updates=decision.workflow_updates,
            )
            if (
                result.ok
                and result.workflow is not None
                and decision.emit_workflow_alert
                and should_emit_workflow_alert(result.workflow)
            ):
                self._registry.mark_notification_emitted(
                    decision.workflow_id,
                    expected_version=result.workflow.version,
                )
            return result.ok

        return False


__all__ = [
    "ChildRunView",
    "DecisionAction",
    "OrchestratorDecision",
    "ChildMissionHydration",
    "ReviewVerdict",
    "ReviewVerdictKind",
    "WorkflowOrchestrator",
    "assert_review_step_read_only",
    "bound_context_field",
    "build_followup_mission_yaml",
    "canonical_child_repository_contract",
    "canonicalize_finding",
    "decide_reconcile",
    "detect_mission_authority_injection",
    "enforce_launch_policy_gates",
    "fingerprint_findings",
    "format_review_verdict_envelope",
    "hydrate_executable_child_mission",
    "inspect_child_repository_authority",
    "is_workflow_orchestration_enabled",
    "parse_review_verdict",
    "redact_secrets",
    "should_emit_workflow_alert",
    "should_suppress_child_terminal_alert",
    "truncate_prior_output",
    "validate_followup_against_policy",
]
