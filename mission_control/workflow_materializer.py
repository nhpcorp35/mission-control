"""Crash-safe materialization of claimed workflow children into RunRegistry.

Claim-to-create protocol (deterministic; SQLite is the correctness boundary):

1. Load claimed step + exact stored ``mission_yaml`` (never regenerate).
2. Resolve durable ``retried_from`` ownership before any reserved create:
   claimed ``parent_run_id`` when present, otherwise ``workflow_id`` for
   HTTP/MCP submits that have no parent run. Reject only when neither is
   a usable identity.
3. Re-run launch policy, permissions, ceiling, and authority gates.
4. ``RunRegistry.create_run(run_id=child_run_id, …)`` with canonical
   ``retried_from`` ownership (created | recovered_idempotently | conflict).
5. ``WorkflowRegistry.mark_step_materialized`` (CAS) after a matching create;
   the mark transaction also persists a unique pending dispatch intent.
6. Claim/lease → idempotent ``RunQueue.enqueue`` (process-local only). Durable
   dispatch ack is **execution-observed**: finalize only when authoritative
   ``RunRegistry`` status is ``running`` or terminal. Enqueue acceptance alone
   must never ack — process death would otherwise lose queue memory while the
   intent is already finalized and no longer redrivable.

Crash windows and recovery:

- before create → retry creates
- after create before mark → ``create_run`` recovers idempotently, then mark
- after mark before enqueue → durable pending intent; retry/redrive handoff
- after enqueue while registry still ``queued`` → release lease to pending
  (no failure backoff / attempt burn); redrive re-enqueues on empty process
  queue after restart; same-process pending/active suppress is idempotent
- worker reaches ``running`` / terminal before bookkeeping → ack exactly once
  without a second execution (registry suppress + execution-observed ack)
- enqueue exception → retryable intent with bounded backoff; poison at ceiling
- mismatch / conflict → poison fail-closed with auditable non-secret reason

Authoritative execution-claim boundary: ``RunRegistry`` status ``running`` or
terminal (``completed`` / ``failed`` / ``timed_out``). There is no separate
durable execution-claim state beyond that SQLite status.

Process-local locks are not a correctness boundary. Feature flag remains
off by default; materialization refuses when disabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import logging
import uuid
from typing import Any, Callable, Mapping, Protocol

import yaml

from mission_control.run_registry import (
    ReservedRunCreateResult,
    ReservedRunOutcome,
    RunRecord,
    RunRegistry,
    is_terminal_status,
    normalize_ownership_id,
    reserved_run_identity_matches,
    resolve_run_registry_ownership,
)
from mission_control.workflow_orchestrator import (
    enforce_launch_policy_gates,
)
from mission_control.workflow_registry import (
    DISPATCH_BACKOFF_BASE_SECONDS,
    DISPATCH_BACKOFF_MAX_SECONDS,
    DISPATCH_LEASE_SECONDS,
    DispatchIntentState,
    StepMaterializationState,
    StepStatus,
    TransitionReason,
    WorkflowRecord,
    WorkflowRegistry,
    WorkflowState,
    WorkflowStepRecord,
    is_terminal_workflow_state,
    is_workflow_orchestration_enabled,
)

logger = logging.getLogger(__name__)

POISON_CAS_MAX_ATTEMPTS = 5


class MaterializeOutcome(str, Enum):
    """Deterministic materialization outcomes (secret-free)."""

    CREATED = "created"
    RECOVERED_IDEMPOTENTLY = "recovered_idempotently"
    ALREADY_MATERIALIZED = "already_materialized"
    REDRIVEN = "redriven"
    FEATURE_DISABLED = "feature_disabled"
    MISSING_PARENT_BINDING = "missing_parent_binding"
    POLICY_DENIED = "policy_denied"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    NOT_CLAIMED = "not_claimed"
    INVALID_MISSION = "invalid_mission"
    WORKFLOW_TERMINAL = "workflow_terminal"
    DISPATCH_DEFERRED = "dispatch_deferred"
    DISPATCH_POISONED = "dispatch_poisoned"
    POISON_CAS_FAILED = "poison_cas_failed"


@dataclass(frozen=True)
class MaterializeResult:
    """Result of claim→registry materialization. Never log mission YAML."""

    outcome: MaterializeOutcome
    child_run_id: str | None = None
    step_id: str | None = None
    enqueued: bool = False
    reason: str | None = None
    policy_audit: dict[str, Any] | None = None
    create_outcome: str | None = None
    dispatch_state: str | None = None


@dataclass(frozen=True)
class PoisonResult:
    """Outcome of fail-closed poison CAS handling."""

    ok: bool
    reason: str | None = None
    attempts: int = 0


@dataclass
class MaterializeCrashHooks:
    """Optional crash-injection points for deterministic recovery tests."""

    before_create: Callable[[], None] | None = None
    after_create: Callable[[], None] | None = None
    before_mark: Callable[[], None] | None = None
    after_mark: Callable[[], None] | None = None
    before_enqueue: Callable[[], None] | None = None
    after_enqueue: Callable[[], None] | None = None
    before_ack: Callable[[], None] | None = None
    after_ack: Callable[[], None] | None = None


class SupportsEnqueue(Protocol):
    def enqueue(self, run_id: str, mission: dict, registry: Any) -> Any: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _budget_ceiling_reason(
    workflow: WorkflowRecord, *, now: datetime | None = None
) -> str | None:
    """Return a ceiling denial when wall-clock / credit ceilings are hit.

    Child-run count was already reserved at claim time; re-check inclusive
    ceilings that can still trip between claim and materialize without
    double-counting the reserved child.
    """
    policy = workflow.policy_snapshot
    if workflow.child_run_count > policy.max_child_runs:
        return "budget_ceiling_children"
    if workflow.credit_units_used > policy.max_credit_units:
        return "budget_ceiling_credit"
    if (
        workflow.credit_usage_actual is not None
        and float(workflow.credit_usage_actual) >= float(policy.max_credit_units)
    ):
        return "budget_ceiling_actual"
    started = workflow.started_at or workflow.created_at
    now = now or _utc_now()
    elapsed = (now - started).total_seconds()
    if elapsed >= policy.max_wall_clock_seconds:
        return "budget_ceiling_wall_clock"
    return None


def _strip_followup_context_trailer(mission_yaml: str) -> str:
    """Return YAML authority text with opaque follow-up trailer removed.

    The trailer is not YAML and must not participate in ``yaml.safe_load``;
    ``create_run`` still receives the exact stored ``mission_yaml``.
    """
    begin_marker = "<<<MC_FOLLOWUP_CONTEXT_V1>>>"
    text = mission_yaml or ""
    begin = text.find(begin_marker)
    if begin == -1:
        return text
    return text[:begin].rstrip() + "\n"


def _parse_exact_mission(mission_yaml: str) -> dict[str, Any] | None:
    """Parse stored YAML into a mission mapping without regenerating text."""
    # Opaque follow-up trailers are appended after the template; strip them
    # for structure parse only. Exact stored text is still used for create.
    parse_text = _strip_followup_context_trailer(mission_yaml)
    try:
        data = yaml.safe_load(parse_text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _poison(
    workflow_registry: WorkflowRegistry,
    *,
    workflow: WorkflowRecord,
    step: WorkflowStepRecord,
    reason: str,
    detail: Mapping[str, Any] | None = None,
    max_attempts: int = POISON_CAS_MAX_ATTEMPTS,
) -> PoisonResult:
    """Fail closed with an auditable, non-secret reason.

    Checks CAS results. On stale version, refreshes and retries a bounded
    number of times; otherwise returns an explicit fail-closed outcome.
    Never silently no-ops a required poison when the workflow is still
    non-terminal.
    """
    attempts = 0
    current = workflow
    for _ in range(max(1, int(max_attempts))):
        attempts += 1
        latest = workflow_registry.get_workflow(current.workflow_id)
        if latest is None:
            return PoisonResult(
                ok=False, reason="workflow_not_found", attempts=attempts
            )
        current = latest
        if is_terminal_workflow_state(current.state):
            return PoisonResult(
                ok=True, reason="already_terminal", attempts=attempts
            )
        payload = {
            "cause": reason,
            "step_id": step.step_id,
            "child_run_id": step.child_run_id,
            **dict(detail or {}),
        }
        # Never attach mission YAML, secrets, or review findings.
        result = workflow_registry.apply_cas_transition(
            workflow_id=current.workflow_id,
            expected_version=current.version,
            to_state=WorkflowState.BLOCKED,
            reason=TransitionReason.ERROR,
            detail=payload,
            step_id=step.step_id,
            child_run_id=step.child_run_id,
            workflow_updates={
                "error": reason,
                "last_decision": {
                    "action": "materialize_poison",
                    "cause": reason,
                },
            },
            step_updates={"error": reason},
        )
        if result.ok:
            logger.info(
                (
                    "workflow event=child_materialize_poisoned workflow_id=%s "
                    "step_id=%s child_run_id=%s reason=%s"
                ),
                current.workflow_id,
                step.step_id,
                step.child_run_id,
                reason,
            )
            return PoisonResult(ok=True, reason=reason, attempts=attempts)
        if result.error == "workflow_terminal":
            return PoisonResult(
                ok=True, reason="already_terminal", attempts=attempts
            )
        if result.conflict or result.error == "version_conflict":
            continue
        logger.warning(
            (
                "workflow event=child_materialize_poison_failed "
                "workflow_id=%s step_id=%s reason=%s cas_error=%s"
            ),
            current.workflow_id,
            step.step_id,
            reason,
            result.error,
        )
        return PoisonResult(
            ok=False,
            reason=result.error or "poison_cas_failed",
            attempts=attempts,
        )
    logger.warning(
        (
            "workflow event=child_materialize_poison_cas_exhausted "
            "workflow_id=%s step_id=%s reason=%s attempts=%s"
        ),
        current.workflow_id,
        step.step_id,
        reason,
        attempts,
    )
    return PoisonResult(
        ok=False, reason="poison_cas_exhausted", attempts=attempts
    )


def _resolve_step(
    workflow_registry: WorkflowRegistry,
    *,
    workflow_id: str,
    step_id: str | None,
    child_run_id: str | None,
) -> WorkflowStepRecord | None:
    if step_id:
        step = workflow_registry.get_step(step_id)
        if step is None or step.workflow_id != workflow_id:
            return None
        return step
    if child_run_id:
        step = workflow_registry.get_step_by_child_run(child_run_id)
        if step is None or step.workflow_id != workflow_id:
            return None
        return step
    workflow = workflow_registry.get_workflow(workflow_id)
    if workflow is None or workflow.current_step_id is None:
        return None
    return workflow_registry.get_step(workflow.current_step_id)


def _ownership_for_step(step: WorkflowStepRecord) -> str | None:
    """Canonical RunRegistry ownership for reserved child create.

    Prefer the claimed parent binding. HTTP/MCP workflows omit
    ``parent_run_id``; ``workflow_id`` is then the durable ``retried_from``
    identity so reserved creates stay idempotent across retries.
    """
    parent = normalize_ownership_id(step.parent_run_id)
    fallback = (
        None if parent is not None else normalize_ownership_id(step.workflow_id)
    )
    try:
        return resolve_run_registry_ownership(
            parent_run_id=parent,
            retried_from=fallback,
        )
    except ValueError:
        return None


def _execution_observed(record: RunRecord | None) -> bool:
    """True when authoritative RunRegistry proves execution started or ended.

    Durable dispatch ack is allowed only for ``running`` or terminal statuses.
    ``queued`` alone is not sufficient — process-local queue memory can die.
    """
    if record is None:
        return False
    if is_terminal_status(record.status):
        return True
    status = record.status
    value = status.value if hasattr(status, "value") else str(status)
    return value == "running"


def _verify_existing_run(
    existing: RunRecord,
    *,
    mission_yaml: str,
    ownership: str,
) -> str | None:
    """Return conflict class when an existing row does not match bindings."""
    if reserved_run_identity_matches(
        existing,
        mission_yaml=mission_yaml,
        retried_from=ownership,
    ):
        return None
    from mission_control.run_registry import classify_reserved_run_conflict

    return classify_reserved_run_conflict(
        existing,
        mission_yaml=mission_yaml,
        retried_from=ownership,
    )


def _poison_or_fail(
    workflow_registry: WorkflowRegistry,
    *,
    workflow: WorkflowRecord,
    step: WorkflowStepRecord,
    reason: str,
    detail: Mapping[str, Any] | None = None,
    child_run_id: str | None = None,
    step_id: str | None = None,
    create_outcome: str | None = None,
    policy_audit: dict[str, Any] | None = None,
) -> MaterializeResult:
    poison = _poison(
        workflow_registry,
        workflow=workflow,
        step=step,
        reason=reason,
        detail=detail,
    )
    if not poison.ok:
        return MaterializeResult(
            outcome=MaterializeOutcome.POISON_CAS_FAILED,
            child_run_id=child_run_id or step.child_run_id,
            step_id=step_id or step.step_id,
            reason=poison.reason or "poison_cas_failed",
            create_outcome=create_outcome,
            policy_audit=policy_audit,
        )
    return MaterializeResult(
        outcome=MaterializeOutcome.CONFLICT,
        child_run_id=child_run_id or step.child_run_id,
        step_id=step_id or step.step_id,
        reason=reason,
        create_outcome=create_outcome,
        policy_audit=policy_audit,
    )


def _handoff_dispatch(
    *,
    workflow_registry: WorkflowRegistry,
    run_registry: RunRegistry,
    run_queue: SupportsEnqueue,
    workflow: WorkflowRecord,
    step: WorkflowStepRecord,
    mission: dict[str, Any],
    hooks: MaterializeCrashHooks | None = None,
    owner: str | None = None,
    lease_seconds: float = DISPATCH_LEASE_SECONDS,
    backoff_base_seconds: float = DISPATCH_BACKOFF_BASE_SECONDS,
    backoff_max_seconds: float = DISPATCH_BACKOFF_MAX_SECONDS,
) -> MaterializeResult:
    """Claim → enqueue → execution-observed ack for one dispatch intent."""
    assert step.child_run_id is not None
    child_run_id = step.child_run_id
    lease_owner = owner or f"materialize:{uuid.uuid4()}"

    workflow_registry.ensure_dispatch_intent(
        child_run_id=child_run_id,
        workflow_id=workflow.workflow_id,
        step_id=step.step_id,
    )
    intent = workflow_registry.get_dispatch_intent(child_run_id)
    if intent is not None and intent.state is DispatchIntentState.ACKED:
        return MaterializeResult(
            outcome=MaterializeOutcome.ALREADY_MATERIALIZED,
            child_run_id=child_run_id,
            step_id=step.step_id,
            enqueued=False,
            reason="dispatch_acked",
            dispatch_state=DispatchIntentState.ACKED.value,
        )
    if intent is not None and intent.state is DispatchIntentState.POISONED:
        return MaterializeResult(
            outcome=MaterializeOutcome.DISPATCH_POISONED,
            child_run_id=child_run_id,
            step_id=step.step_id,
            enqueued=False,
            reason=intent.last_error or "dispatch_poisoned",
            dispatch_state=DispatchIntentState.POISONED.value,
        )

    existing = run_registry.get_run(child_run_id)
    if _execution_observed(existing):
        # Running/terminal with stale intent: ack once; never re-execute.
        observed = (
            "run_terminal"
            if existing is not None and is_terminal_status(existing.status)
            else "run_running"
        )
        claim = workflow_registry.claim_dispatch_intent(
            child_run_id=child_run_id,
            owner=lease_owner,
            lease_seconds=lease_seconds,
        )
        if claim.ok:
            if not workflow_registry.ack_dispatch_intent(
                child_run_id=child_run_id, owner=lease_owner
            ):
                return MaterializeResult(
                    outcome=MaterializeOutcome.DISPATCH_DEFERRED,
                    child_run_id=child_run_id,
                    step_id=step.step_id,
                    enqueued=False,
                    reason="ack_failed",
                    dispatch_state=DispatchIntentState.LEASED.value,
                )
        elif claim.error == "already_acked":
            pass
        else:
            return MaterializeResult(
                outcome=MaterializeOutcome.DISPATCH_DEFERRED,
                child_run_id=child_run_id,
                step_id=step.step_id,
                enqueued=False,
                reason=claim.error or "dispatch_deferred",
                dispatch_state=(
                    claim.intent.state.value if claim.intent is not None else None
                ),
            )
        return MaterializeResult(
            outcome=MaterializeOutcome.ALREADY_MATERIALIZED,
            child_run_id=child_run_id,
            step_id=step.step_id,
            enqueued=False,
            reason=observed,
            dispatch_state=DispatchIntentState.ACKED.value,
        )

    claim = workflow_registry.claim_dispatch_intent(
        child_run_id=child_run_id,
        owner=lease_owner,
        lease_seconds=lease_seconds,
    )
    if claim.poisoned:
        poison = _poison(
            workflow_registry,
            workflow=workflow,
            step=step,
            reason=claim.error or "dispatch_attempt_ceiling",
            detail={"dispatch": True},
        )
        if not poison.ok:
            return MaterializeResult(
                outcome=MaterializeOutcome.POISON_CAS_FAILED,
                child_run_id=child_run_id,
                step_id=step.step_id,
                reason=poison.reason,
                dispatch_state=DispatchIntentState.POISONED.value,
            )
        return MaterializeResult(
            outcome=MaterializeOutcome.DISPATCH_POISONED,
            child_run_id=child_run_id,
            step_id=step.step_id,
            reason=claim.error or "dispatch_attempt_ceiling",
            dispatch_state=DispatchIntentState.POISONED.value,
        )
    if not claim.ok:
        return MaterializeResult(
            outcome=MaterializeOutcome.DISPATCH_DEFERRED,
            child_run_id=child_run_id,
            step_id=step.step_id,
            enqueued=False,
            reason=claim.error or "dispatch_deferred",
            dispatch_state=(
                claim.intent.state.value if claim.intent is not None else None
            ),
        )

    # Re-check after winning the lease: worker may have started already.
    existing = run_registry.get_run(child_run_id)
    if _execution_observed(existing):
        return _finalize_execution_observed_ack(
            workflow_registry=workflow_registry,
            child_run_id=child_run_id,
            step_id=step.step_id,
            lease_owner=lease_owner,
            existing=existing,
            newly_enqueued=False,
            hooks=hooks,
        )

    if hooks and hooks.before_enqueue:
        hooks.before_enqueue()

    newly_enqueued = False
    try:
        result = run_queue.enqueue(child_run_id, mission, run_registry)
        newly_enqueued = bool(result) if result is not None else True
    except Exception as exc:
        failed = workflow_registry.fail_dispatch_intent(
            child_run_id=child_run_id,
            owner=lease_owner,
            error=type(exc).__name__,
            backoff_base_seconds=backoff_base_seconds,
            backoff_max_seconds=backoff_max_seconds,
        )
        logger.warning(
            (
                "workflow event=child_dispatch_enqueue_failed "
                "workflow_id=%s step_id=%s child_run_id=%s error=%s "
                "dispatch_state=%s"
            ),
            workflow.workflow_id,
            step.step_id,
            child_run_id,
            type(exc).__name__,
            failed.state.value if failed is not None else None,
        )
        if failed is not None and failed.state is DispatchIntentState.POISONED:
            poison = _poison(
                workflow_registry,
                workflow=workflow,
                step=step,
                reason="dispatch_enqueue_poisoned",
                detail={"enqueue_error": type(exc).__name__},
            )
            if not poison.ok:
                return MaterializeResult(
                    outcome=MaterializeOutcome.POISON_CAS_FAILED,
                    child_run_id=child_run_id,
                    step_id=step.step_id,
                    reason=poison.reason,
                    dispatch_state=DispatchIntentState.POISONED.value,
                )
            return MaterializeResult(
                outcome=MaterializeOutcome.DISPATCH_POISONED,
                child_run_id=child_run_id,
                step_id=step.step_id,
                reason="dispatch_enqueue_poisoned",
                dispatch_state=DispatchIntentState.POISONED.value,
            )
        return MaterializeResult(
            outcome=MaterializeOutcome.DISPATCH_DEFERRED,
            child_run_id=child_run_id,
            step_id=step.step_id,
            enqueued=False,
            reason="enqueue_exception",
            dispatch_state=(
                failed.state.value if failed is not None else "pending"
            ),
        )

    if hooks and hooks.after_enqueue:
        hooks.after_enqueue()

    # Separate enqueue acknowledgment from durable execution acknowledgment.
    post = run_registry.get_run(child_run_id)
    if _execution_observed(post):
        return _finalize_execution_observed_ack(
            workflow_registry=workflow_registry,
            child_run_id=child_run_id,
            step_id=step.step_id,
            lease_owner=lease_owner,
            existing=post,
            newly_enqueued=newly_enqueued,
            hooks=hooks,
        )

    released = workflow_registry.release_dispatch_lease_for_queued_handoff(
        child_run_id=child_run_id, owner=lease_owner
    )
    logger.info(
        (
            "workflow event=child_dispatch_enqueued_awaiting_execution "
            "workflow_id=%s step_id=%s child_run_id=%s newly_enqueued=%s "
            "dispatch_state=%s"
        ),
        workflow.workflow_id,
        step.step_id,
        child_run_id,
        int(newly_enqueued),
        released.state.value if released is not None else None,
    )
    return MaterializeResult(
        outcome=MaterializeOutcome.REDRIVEN,
        child_run_id=child_run_id,
        step_id=step.step_id,
        enqueued=newly_enqueued,
        reason=(
            "awaiting_execution"
            if newly_enqueued
            else "queue_already_pending"
        ),
        dispatch_state=(
            released.state.value
            if released is not None
            else DispatchIntentState.PENDING.value
        ),
    )


def _finalize_execution_observed_ack(
    *,
    workflow_registry: WorkflowRegistry,
    child_run_id: str,
    step_id: str,
    lease_owner: str,
    existing: RunRecord | None,
    newly_enqueued: bool,
    hooks: MaterializeCrashHooks | None = None,
) -> MaterializeResult:
    """Ack exactly once after RunRegistry proves running or terminal."""
    observed = (
        "run_terminal"
        if existing is not None and is_terminal_status(existing.status)
        else "run_running"
    )
    if hooks and hooks.before_ack:
        hooks.before_ack()
    acked = workflow_registry.ack_dispatch_intent(
        child_run_id=child_run_id, owner=lease_owner
    )
    if not acked:
        logger.warning(
            (
                "workflow event=child_dispatch_ack_failed child_run_id=%s "
                "step_id=%s reason=%s"
            ),
            child_run_id,
            step_id,
            observed,
        )
        return MaterializeResult(
            outcome=MaterializeOutcome.DISPATCH_DEFERRED,
            child_run_id=child_run_id,
            step_id=step_id,
            enqueued=newly_enqueued,
            reason="ack_failed",
            dispatch_state=DispatchIntentState.LEASED.value,
        )
    if hooks and hooks.after_ack:
        hooks.after_ack()
    return MaterializeResult(
        outcome=MaterializeOutcome.REDRIVEN,
        child_run_id=child_run_id,
        step_id=step_id,
        enqueued=newly_enqueued,
        reason=observed if not newly_enqueued else None,
        dispatch_state=DispatchIntentState.ACKED.value,
    )


def redrive_materialized_dispatch(
    *,
    workflow_registry: WorkflowRegistry,
    run_registry: RunRegistry,
    run_queue: SupportsEnqueue,
    workflow_id: str,
    step_id: str | None = None,
    child_run_id: str | None = None,
    hooks: MaterializeCrashHooks | None = None,
    environ: Mapping[str, str] | None = None,
    owner: str | None = None,
    lease_seconds: float = DISPATCH_LEASE_SECONDS,
    backoff_base_seconds: float = DISPATCH_BACKOFF_BASE_SECONDS,
    backoff_max_seconds: float = DISPATCH_BACKOFF_MAX_SECONDS,
) -> MaterializeResult:
    """Durable redrive primitive for pending/leased dispatch intents.

    Intended for the lifespan reconciler follow-up and for MATERIALIZED
    retry paths. Does not start a continuous reconciler here.
    """
    if not is_workflow_orchestration_enabled(
        dict(environ) if environ is not None else None
    ):
        return MaterializeResult(
            outcome=MaterializeOutcome.FEATURE_DISABLED,
            reason="workflow_orchestration_disabled",
        )

    workflow = workflow_registry.get_workflow(workflow_id)
    if workflow is None:
        return MaterializeResult(
            outcome=MaterializeOutcome.NOT_FOUND,
            reason="workflow_not_found",
        )
    if is_terminal_workflow_state(workflow.state):
        # Still allow terminal-run stale-intent ack via child lookup below.
        pass

    step = _resolve_step(
        workflow_registry,
        workflow_id=workflow_id,
        step_id=step_id,
        child_run_id=child_run_id,
    )
    if step is None or not step.child_run_id:
        return MaterializeResult(
            outcome=MaterializeOutcome.NOT_FOUND,
            reason="step_not_found",
        )
    if step.materialization_state is not StepMaterializationState.MATERIALIZED:
        return MaterializeResult(
            outcome=MaterializeOutcome.NOT_CLAIMED,
            child_run_id=step.child_run_id,
            step_id=step.step_id,
            reason="not_materialized",
        )

    mission_yaml = step.mission_yaml
    if mission_yaml is None or str(mission_yaml).strip() == "":
        return _poison_or_fail(
            workflow_registry,
            workflow=workflow,
            step=step,
            reason="missing_mission_yaml",
        )
    mission = _parse_exact_mission(mission_yaml)
    if mission is None:
        return _poison_or_fail(
            workflow_registry,
            workflow=workflow,
            step=step,
            reason="invalid_mission_yaml",
        )

    return _handoff_dispatch(
        workflow_registry=workflow_registry,
        run_registry=run_registry,
        run_queue=run_queue,
        workflow=workflow,
        step=step,
        mission=mission,
        hooks=hooks,
        owner=owner,
        lease_seconds=lease_seconds,
        backoff_base_seconds=backoff_base_seconds,
        backoff_max_seconds=backoff_max_seconds,
    )


def materialize_claimed_child(
    *,
    workflow_registry: WorkflowRegistry,
    run_registry: RunRegistry,
    run_queue: SupportsEnqueue,
    workflow_id: str,
    step_id: str | None = None,
    child_run_id: str | None = None,
    hooks: MaterializeCrashHooks | None = None,
    environ: Mapping[str, str] | None = None,
    lease_seconds: float = DISPATCH_LEASE_SECONDS,
    backoff_base_seconds: float = DISPATCH_BACKOFF_BASE_SECONDS,
    backoff_max_seconds: float = DISPATCH_BACKOFF_MAX_SECONDS,
) -> MaterializeResult:
    """Materialize one claimed workflow child into RunRegistry + queue.

    Uses the exact stored step ``mission_yaml`` and caller-reserved
    ``child_run_id``. Persists a unique dispatch intent with the
    materialization mark, then performs leased idempotent handoff.
    """
    if not is_workflow_orchestration_enabled(
        dict(environ) if environ is not None else None
    ):
        return MaterializeResult(
            outcome=MaterializeOutcome.FEATURE_DISABLED,
            reason="workflow_orchestration_disabled",
        )

    workflow = workflow_registry.get_workflow(workflow_id)
    if workflow is None:
        return MaterializeResult(
            outcome=MaterializeOutcome.NOT_FOUND,
            reason="workflow_not_found",
        )
    if is_terminal_workflow_state(workflow.state):
        return MaterializeResult(
            outcome=MaterializeOutcome.WORKFLOW_TERMINAL,
            reason="workflow_terminal",
        )

    step = _resolve_step(
        workflow_registry,
        workflow_id=workflow_id,
        step_id=step_id,
        child_run_id=child_run_id,
    )
    if step is None:
        return MaterializeResult(
            outcome=MaterializeOutcome.NOT_FOUND,
            reason="step_not_found",
        )
    if not step.child_run_id:
        return MaterializeResult(
            outcome=MaterializeOutcome.NOT_CLAIMED,
            step_id=step.step_id,
            reason="missing_child_run_id",
        )

    # Exact stored YAML only — never interpolate or rebuild.
    mission_yaml = step.mission_yaml
    if mission_yaml is None or str(mission_yaml).strip() == "":
        return _poison_or_fail(
            workflow_registry,
            workflow=workflow,
            step=step,
            reason="missing_mission_yaml",
        )

    ownership = _ownership_for_step(step)
    if ownership is None or normalize_ownership_id(ownership) is None:
        logger.info(
            (
                "workflow event=child_materialize_rejected workflow_id=%s "
                "step_id=%s child_run_id=%s reason=missing_parent_binding"
            ),
            workflow_id,
            step.step_id,
            step.child_run_id,
        )
        return MaterializeResult(
            outcome=MaterializeOutcome.MISSING_PARENT_BINDING,
            child_run_id=step.child_run_id,
            step_id=step.step_id,
            reason="missing_parent_binding",
        )

    # Already materialized → verify identity; redrive pending dispatch.
    if (
        step.materialization_state is StepMaterializationState.MATERIALIZED
        or step.status
        in {StepStatus.QUEUED, StepStatus.RUNNING, StepStatus.COMPLETED}
    ):
        existing = run_registry.get_run(step.child_run_id)
        if existing is None:
            workflow = workflow_registry.get_workflow(workflow_id) or workflow
            return _poison_or_fail(
                workflow_registry,
                workflow=workflow,
                step=step,
                reason="materialized_without_run",
            )
        mismatch = _verify_existing_run(
            existing,
            mission_yaml=mission_yaml,
            ownership=ownership,
        )
        if mismatch:
            workflow = workflow_registry.get_workflow(workflow_id) or workflow
            return _poison_or_fail(
                workflow_registry,
                workflow=workflow,
                step=step,
                reason=mismatch,
                detail={"conflict_class": mismatch},
            )
        mission = _parse_exact_mission(mission_yaml)
        if mission is None:
            workflow = workflow_registry.get_workflow(workflow_id) or workflow
            return _poison_or_fail(
                workflow_registry,
                workflow=workflow,
                step=step,
                reason="invalid_mission_yaml",
            )
        handoff = _handoff_dispatch(
            workflow_registry=workflow_registry,
            run_registry=run_registry,
            run_queue=run_queue,
            workflow=workflow,
            step=step,
            mission=mission,
            hooks=hooks,
            lease_seconds=lease_seconds,
            backoff_base_seconds=backoff_base_seconds,
            backoff_max_seconds=backoff_max_seconds,
        )
        if handoff.outcome is MaterializeOutcome.REDRIVEN:
            logger.info(
                (
                    "workflow event=child_materialize_redriven workflow_id=%s "
                    "step_id=%s child_run_id=%s enqueued=%s"
                ),
                workflow_id,
                step.step_id,
                step.child_run_id,
                int(handoff.enqueued),
            )
            return MaterializeResult(
                outcome=MaterializeOutcome.REDRIVEN
                if handoff.enqueued
                else MaterializeOutcome.ALREADY_MATERIALIZED,
                child_run_id=step.child_run_id,
                step_id=step.step_id,
                enqueued=handoff.enqueued,
                reason=handoff.reason,
                create_outcome=ReservedRunOutcome.RECOVERED_IDEMPOTENTLY.value,
                dispatch_state=handoff.dispatch_state,
            )
        if handoff.outcome is MaterializeOutcome.ALREADY_MATERIALIZED:
            logger.info(
                (
                    "workflow event=child_materialize_idempotent "
                    "workflow_id=%s step_id=%s child_run_id=%s"
                ),
                workflow_id,
                step.step_id,
                step.child_run_id,
            )
            return MaterializeResult(
                outcome=MaterializeOutcome.ALREADY_MATERIALIZED,
                child_run_id=step.child_run_id,
                step_id=step.step_id,
                enqueued=False,
                reason=handoff.reason,
                create_outcome=ReservedRunOutcome.RECOVERED_IDEMPOTENTLY.value,
                dispatch_state=handoff.dispatch_state,
            )
        return handoff

    if step.status is not StepStatus.CLAIMED or (
        step.materialization_state is not StepMaterializationState.CLAIMED
    ):
        return MaterializeResult(
            outcome=MaterializeOutcome.NOT_CLAIMED,
            child_run_id=step.child_run_id,
            step_id=step.step_id,
            reason=f"status={step.status.value}",
        )

    # Final policy / authority / ceiling gates immediately before create.
    denial, policy_audit = enforce_launch_policy_gates(
        policy=workflow.policy_snapshot,
        step_type=step.step_type,
        mission_yaml=mission_yaml,
    )
    if denial:
        workflow = workflow_registry.get_workflow(workflow_id) or workflow
        poisoned = _poison_or_fail(
            workflow_registry,
            workflow=workflow,
            step=step,
            reason=denial,
            detail={"policy_audit": policy_audit},
            policy_audit=policy_audit,
        )
        if poisoned.outcome is MaterializeOutcome.CONFLICT:
            return MaterializeResult(
                outcome=MaterializeOutcome.POLICY_DENIED,
                child_run_id=step.child_run_id,
                step_id=step.step_id,
                reason=denial,
                policy_audit=policy_audit,
            )
        return poisoned

    ceiling = _budget_ceiling_reason(workflow)
    if ceiling:
        if not is_terminal_workflow_state(workflow.state):
            workflow_registry.apply_cas_transition(
                workflow_id=workflow_id,
                expected_version=workflow.version,
                to_state=WorkflowState.BUDGET_EXHAUSTED,
                reason=TransitionReason.BUDGET,
                detail={"cause": ceiling, "phase": "materialize"},
                step_id=step.step_id,
                child_run_id=step.child_run_id,
                workflow_updates={
                    "error": ceiling,
                    "last_decision": {
                        "action": "budget_exhausted",
                        "cause": ceiling,
                    },
                },
            )
        return MaterializeResult(
            outcome=MaterializeOutcome.BUDGET_EXHAUSTED,
            child_run_id=step.child_run_id,
            step_id=step.step_id,
            reason=ceiling,
            policy_audit=policy_audit,
        )

    mission = _parse_exact_mission(mission_yaml)
    if mission is None:
        workflow = workflow_registry.get_workflow(workflow_id) or workflow
        return _poison_or_fail(
            workflow_registry,
            workflow=workflow,
            step=step,
            reason="invalid_mission_yaml",
            policy_audit=policy_audit,
        )

    if hooks and hooks.before_create:
        hooks.before_create()

    create_result = run_registry.create_run(
        run_id=step.child_run_id,
        mission_yaml=mission_yaml,
        retried_from=ownership,
    )
    assert isinstance(create_result, ReservedRunCreateResult)

    if hooks and hooks.after_create:
        hooks.after_create()

    if create_result.outcome is ReservedRunOutcome.CONFLICT:
        conflict = create_result.conflict_class or "existing_run_collision"
        workflow = workflow_registry.get_workflow(workflow_id) or workflow
        return _poison_or_fail(
            workflow_registry,
            workflow=workflow,
            step=step,
            reason=conflict,
            detail={"conflict_class": conflict},
            create_outcome=ReservedRunOutcome.CONFLICT.value,
            policy_audit=policy_audit,
        )

    # Refresh version after any concurrent workflow writes.
    workflow = workflow_registry.get_workflow(workflow_id)
    if workflow is None:
        return MaterializeResult(
            outcome=MaterializeOutcome.NOT_FOUND,
            child_run_id=step.child_run_id,
            step_id=step.step_id,
            reason="workflow_not_found",
        )

    if hooks and hooks.before_mark:
        hooks.before_mark()

    mark = workflow_registry.mark_step_materialized(
        workflow_id=workflow_id,
        expected_version=workflow.version,
        step_id=step.step_id,
        child_status="queued",
    )

    if hooks and hooks.after_mark:
        hooks.after_mark()

    won_mark = bool(mark.ok)
    if not won_mark:
        # Peer may have materialized; recover idempotently via redrive.
        latest_step = workflow_registry.get_step(step.step_id)
        if (
            latest_step is not None
            and latest_step.materialization_state
            is StepMaterializationState.MATERIALIZED
        ):
            existing = run_registry.get_run(step.child_run_id)
            if existing is not None:
                mismatch = _verify_existing_run(
                    existing,
                    mission_yaml=mission_yaml,
                    ownership=ownership,
                )
                if mismatch is None:
                    handoff = _handoff_dispatch(
                        workflow_registry=workflow_registry,
                        run_registry=run_registry,
                        run_queue=run_queue,
                        workflow=mark.workflow
                        or workflow_registry.get_workflow(workflow_id)
                        or workflow,
                        step=latest_step,
                        mission=mission,
                        hooks=hooks,
                        lease_seconds=lease_seconds,
                        backoff_base_seconds=backoff_base_seconds,
                        backoff_max_seconds=backoff_max_seconds,
                    )
                    logger.info(
                        (
                            "workflow event=child_materialize_recovered "
                            "workflow_id=%s step_id=%s child_run_id=%s "
                            "create_outcome=%s enqueued=%s"
                        ),
                        workflow_id,
                        step.step_id,
                        step.child_run_id,
                        create_result.outcome.value,
                        int(handoff.enqueued),
                    )
                    if handoff.outcome in {
                        MaterializeOutcome.REDRIVEN,
                        MaterializeOutcome.ALREADY_MATERIALIZED,
                        MaterializeOutcome.DISPATCH_DEFERRED,
                    }:
                        return MaterializeResult(
                            outcome=MaterializeOutcome.RECOVERED_IDEMPOTENTLY,
                            child_run_id=step.child_run_id,
                            step_id=step.step_id,
                            enqueued=handoff.enqueued,
                            reason=handoff.reason,
                            create_outcome=create_result.outcome.value,
                            policy_audit=policy_audit,
                            dispatch_state=handoff.dispatch_state,
                        )
                    return handoff
            workflow = workflow_registry.get_workflow(workflow_id) or workflow
            return _poison_or_fail(
                workflow_registry,
                workflow=workflow,
                step=step,
                reason=mismatch or "materialize_mark_race",
                create_outcome=create_result.outcome.value,
            )
        return MaterializeResult(
            outcome=MaterializeOutcome.CONFLICT,
            child_run_id=step.child_run_id,
            step_id=step.step_id,
            reason=mark.error or "version_conflict",
            create_outcome=create_result.outcome.value,
            policy_audit=policy_audit,
        )

    workflow = mark.workflow or workflow_registry.get_workflow(workflow_id) or workflow
    handoff = _handoff_dispatch(
        workflow_registry=workflow_registry,
        run_registry=run_registry,
        run_queue=run_queue,
        workflow=workflow,
        step=step,
        mission=mission,
        hooks=hooks,
        lease_seconds=lease_seconds,
        backoff_base_seconds=backoff_base_seconds,
        backoff_max_seconds=backoff_max_seconds,
    )
    if handoff.outcome not in {
        MaterializeOutcome.REDRIVEN,
        MaterializeOutcome.ALREADY_MATERIALIZED,
    }:
        # Preserve deferred / poisoned / fail-closed outcomes for retry.
        if handoff.outcome is MaterializeOutcome.DISPATCH_DEFERRED:
            outcome = (
                MaterializeOutcome.CREATED
                if create_result.outcome is ReservedRunOutcome.CREATED
                else MaterializeOutcome.RECOVERED_IDEMPOTENTLY
            )
            logger.info(
                (
                    "workflow event=child_materialized workflow_id=%s "
                    "step_id=%s child_run_id=%s outcome=%s "
                    "create_outcome=%s enqueued=0 dispatch_state=%s "
                    "reason=%s"
                ),
                workflow_id,
                step.step_id,
                step.child_run_id,
                outcome.value,
                create_result.outcome.value,
                handoff.dispatch_state,
                handoff.reason,
            )
            return MaterializeResult(
                outcome=MaterializeOutcome.DISPATCH_DEFERRED,
                child_run_id=step.child_run_id,
                step_id=step.step_id,
                enqueued=False,
                reason=handoff.reason,
                create_outcome=create_result.outcome.value,
                policy_audit=policy_audit,
                dispatch_state=handoff.dispatch_state,
            )
        return handoff

    outcome = (
        MaterializeOutcome.CREATED
        if create_result.outcome is ReservedRunOutcome.CREATED
        else MaterializeOutcome.RECOVERED_IDEMPOTENTLY
    )
    logger.info(
        (
            "workflow event=child_materialized workflow_id=%s step_id=%s "
            "child_run_id=%s outcome=%s create_outcome=%s enqueued=%s "
            "dispatch_state=%s"
        ),
        workflow_id,
        step.step_id,
        step.child_run_id,
        outcome.value,
        create_result.outcome.value,
        int(handoff.enqueued),
        handoff.dispatch_state,
    )
    return MaterializeResult(
        outcome=outcome,
        child_run_id=step.child_run_id,
        step_id=step.step_id,
        enqueued=handoff.enqueued,
        reason=handoff.reason,
        create_outcome=create_result.outcome.value,
        policy_audit=policy_audit,
        dispatch_state=handoff.dispatch_state,
    )


__all__ = [
    "MaterializeCrashHooks",
    "MaterializeOutcome",
    "MaterializeResult",
    "POISON_CAS_MAX_ATTEMPTS",
    "PoisonResult",
    "materialize_claimed_child",
    "redrive_materialized_dispatch",
]
