"""Bounded workflow orchestration state machine (v1).

Pure decision logic over durable ``WorkflowRegistry`` records. Does not
depend on an open API request or chat turn. Child launches are reserved
through registry CAS + idempotency keys; callers materialize reserved
``child_run_id`` values into the existing run registry/queue.

V1 intentionally stops at ``needs_approval`` after MERGE-READY unless the
immutable policy snapshot explicitly authorizes auto-merge/deploy (still
not wired to GitHub/Railway in this module).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import logging
import re
from typing import Any, Mapping

from mission_control.workflow_registry import (
    ACTIONABLE_WORKFLOW_ALERT_STATES,
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

_MERGE_READY_RE = re.compile(
    r"(?im)^\s*MERGE-READY\b|^\s*verdict\s*:\s*MERGE-READY\b"
)
_BLOCKED_RE = re.compile(
    r"(?im)^\s*BLOCKED\b|^\s*verdict\s*:\s*BLOCKED\b"
)
_FINDING_LINE_RE = re.compile(
    r"(?im)^\s*(?:[-*]|\d+\.)\s*(?:\[(?:P\d|actionable)\])?\s*(.+)$"
)


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


def redact_secrets(text: str) -> str:
    """Redact secret-ish tokens from interpolated prior-run output."""
    return _SECRETISH_RE.sub("[redacted]", text)


def truncate_prior_output(text: str, max_chars: int = _PRIOR_OUTPUT_MAX_CHARS) -> str:
    cleaned = redact_secrets(text)
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3] + "..."


def fingerprint_findings(findings: tuple[str, ...] | list[str]) -> str:
    normalized = "\n".join(
        " ".join(str(f).lower().split()) for f in findings if str(f).strip()
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def parse_review_verdict(text: str) -> ReviewVerdict:
    """Parse a machine-oriented review verdict from child output.

    Requires an unambiguous MERGE-READY or BLOCKED marker. BLOCKED must
    include at least one actionable finding line.
    """
    body = text or ""
    excerpt = truncate_prior_output(body, max_chars=800)
    merge = bool(_MERGE_READY_RE.search(body))
    blocked = bool(_BLOCKED_RE.search(body))
    if merge and blocked:
        return ReviewVerdict(
            kind=ReviewVerdictKind.AMBIGUOUS,
            raw_excerpt=excerpt,
        )
    if merge:
        return ReviewVerdict(
            kind=ReviewVerdictKind.MERGE_READY,
            raw_excerpt=excerpt,
        )
    if blocked:
        findings = tuple(
            m.group(1).strip()
            for m in _FINDING_LINE_RE.finditer(body)
            if m.group(1).strip()
        )
        # Prefer explicit FINDINGS sections when present.
        findings_block = re.search(
            r"(?is)findings?\s*:\s*(.+?)(?:\n\s*\n|\Z)",
            body,
        )
        if findings_block:
            block_findings = tuple(
                m.group(1).strip()
                for m in _FINDING_LINE_RE.finditer(findings_block.group(1))
                if m.group(1).strip()
            )
            if block_findings:
                findings = block_findings
        if not findings:
            return ReviewVerdict(
                kind=ReviewVerdictKind.MALFORMED,
                raw_excerpt=excerpt,
            )
        fp = fingerprint_findings(findings)
        return ReviewVerdict(
            kind=ReviewVerdictKind.BLOCKED,
            findings=findings,
            fingerprint=fp,
            raw_excerpt=excerpt,
        )
    return ReviewVerdict(
        kind=ReviewVerdictKind.MALFORMED,
        raw_excerpt=excerpt,
    )


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
            StepStatus.QUEUED,
            StepStatus.RUNNING,
        }:
            return step
    return None


def _latest_step_of_type(
    steps: list[WorkflowStepRecord], step_type: StepType
) -> WorkflowStepRecord | None:
    for step in reversed(steps):
        if step.step_type is step_type:
            return step
    return None


def _budget_violation(
    workflow: WorkflowRecord, *, now: datetime | None = None
) -> WorkflowState | None:
    policy = workflow.policy_snapshot
    if workflow.child_run_count >= policy.max_child_runs:
        # Equality means next launch would exceed; checked before launch.
        pass
    if workflow.credit_units_used >= policy.max_credit_units:
        return WorkflowState.BUDGET_EXHAUSTED
    started = workflow.started_at or workflow.created_at
    now = now or _utc_now()
    elapsed = (now - started).total_seconds()
    if elapsed > policy.max_wall_clock_seconds:
        return WorkflowState.BUDGET_EXHAUSTED
    return None


def _would_exceed_child_budget(workflow: WorkflowRecord) -> bool:
    policy = workflow.policy_snapshot
    next_children = workflow.child_run_count + 1
    next_credits = (
        workflow.credit_units_used + int(policy.credit_unit_per_child_run)
    )
    if next_children > policy.max_child_runs:
        return True
    if next_credits > policy.max_credit_units:
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
    findings_block = "\n".join(f"- {f}" for f in findings) or "- (none)"
    prior = truncate_prior_output(prior_output)
    return (
        f"{spec.mission_yaml.rstrip()}\n\n"
        "## Targeted fix context (platform-authored)\n"
        "Address only the actionable findings below. Do not expand scope,\n"
        "change repository/branch, merge, deploy, or alter secrets.\n\n"
        f"Findings:\n{findings_block}\n\n"
        f"Prior review excerpt (redacted/truncated):\n{prior}\n"
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
        # All steps terminal but workflow not — treat as failed intervention.
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

    # Reserved but unknown to run registry yet → wait (materialize elsewhere).
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
) -> list[OrchestratorDecision]:
    wid = workflow.workflow_id
    decisions: list[OrchestratorDecision] = [
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
    ]
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
) -> list[OrchestratorDecision]:
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

    if verdict.kind in {
        ReviewVerdictKind.MALFORMED,
        ReviewVerdictKind.AMBIGUOUS,
    }:
        return [
            base_mark,
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
            return [
                base_mark,
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
        return [
            base_mark,
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
        return [
            base_mark,
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

    next_cycle = workflow.fix_cycle_count + 1
    if next_cycle > workflow.policy_snapshot.max_fix_cycles:
        return [
            base_mark,
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
        return [
            base_mark,
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
        return [
            base_mark,
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

    # Mark step + bump fix cycle, then launch fix. Apply path handles
    # fix_cycle_count via workflow_updates on the launch's preceding mark.
    key = make_idempotency_key(wid, StepType.FIX, next_cycle, 1)
    return [
        OrchestratorDecision(
            action=DecisionAction.MARK_STEP,
            workflow_id=wid,
            expected_version=version,
            reason=TransitionReason.VERDICT.value,
            detail={
                "verdict": verdict.kind.value,
                "fingerprint": verdict.fingerprint,
                "next_fix_cycle": next_cycle,
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
                },
            },
        ),
        OrchestratorDecision(
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
        ),
    ]


def _after_fix_success(
    *,
    workflow: WorkflowRecord,
    active: WorkflowStepRecord,
    child: ChildRunView,
    version: int,
) -> list[OrchestratorDecision]:
    wid = workflow.workflow_id
    decisions: list[OrchestratorDecision] = [
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
    ]
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
            claim = self._registry.claim_child_launch(
                workflow_id=decision.workflow_id,
                expected_version=workflow.version,
                step_type=decision.step_type,
                mission_yaml=decision.mission_yaml,
                cycle=decision.cycle,
                attempt=decision.attempt,
                parent_run_id=decision.parent_run_id,
                idempotency_key=decision.idempotency_key,
                decision=decision.detail,
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


__all__ = [
    "ChildRunView",
    "DecisionAction",
    "OrchestratorDecision",
    "ReviewVerdict",
    "ReviewVerdictKind",
    "WorkflowOrchestrator",
    "assert_review_step_read_only",
    "decide_reconcile",
    "fingerprint_findings",
    "is_workflow_orchestration_enabled",
    "parse_review_verdict",
    "redact_secrets",
    "should_emit_workflow_alert",
    "should_suppress_child_terminal_alert",
    "truncate_prior_output",
    "validate_followup_against_policy",
]
