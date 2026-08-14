"""Crash-safe materialization of claimed workflow children into RunRegistry.

Claim-to-create protocol (deterministic; SQLite is the correctness boundary):

1. Load claimed step + exact stored ``mission_yaml`` (never regenerate).
2. Reject missing parent / ownership before any reserved create.
3. Re-run launch policy, permissions, ceiling, and authority gates.
4. ``RunRegistry.create_run(run_id=child_run_id, …)`` with canonical
   ``retried_from`` ownership (created | recovered_idempotently | conflict).
5. ``WorkflowRegistry.mark_step_materialized`` (CAS) after a matching create.
6. Enqueue exactly once when **this** attempt wins the mark CAS.

Crash windows and recovery:

- before create → retry creates
- after create before mark → ``create_run`` recovers idempotently, then mark
- after mark before enqueue → retry sees MATERIALIZED; no second enqueue
- mismatch / conflict → poison fail-closed with auditable non-secret reason

Process-local locks are not a correctness boundary. Feature flag remains
off by default; materialization refuses when disabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import logging
from typing import Any, Callable, Mapping, Protocol

import yaml

from mission_control.run_registry import (
    ReservedRunCreateResult,
    ReservedRunOutcome,
    RunRecord,
    RunRegistry,
    normalize_ownership_id,
    reserved_run_identity_matches,
    resolve_run_registry_ownership,
)
from mission_control.workflow_orchestrator import (
    enforce_launch_policy_gates,
)
from mission_control.workflow_registry import (
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


class MaterializeOutcome(str, Enum):
    """Deterministic materialization outcomes (secret-free)."""

    CREATED = "created"
    RECOVERED_IDEMPOTENTLY = "recovered_idempotently"
    ALREADY_MATERIALIZED = "already_materialized"
    FEATURE_DISABLED = "feature_disabled"
    MISSING_PARENT_BINDING = "missing_parent_binding"
    POLICY_DENIED = "policy_denied"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    NOT_CLAIMED = "not_claimed"
    INVALID_MISSION = "invalid_mission"
    WORKFLOW_TERMINAL = "workflow_terminal"


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


@dataclass
class MaterializeCrashHooks:
    """Optional crash-injection points for deterministic recovery tests."""

    before_create: Callable[[], None] | None = None
    after_create: Callable[[], None] | None = None
    before_mark: Callable[[], None] | None = None
    after_mark: Callable[[], None] | None = None


class SupportsEnqueue(Protocol):
    def enqueue(self, run_id: str, mission: dict, registry: Any) -> None: ...


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


def _parse_exact_mission(mission_yaml: str) -> dict[str, Any] | None:
    """Parse stored YAML into a mission mapping without regenerating text."""
    try:
        data = yaml.safe_load(mission_yaml)
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
) -> None:
    """Fail closed with an auditable, non-secret reason."""
    if is_terminal_workflow_state(workflow.state):
        return
    payload = {
        "cause": reason,
        "step_id": step.step_id,
        "child_run_id": step.child_run_id,
        **dict(detail or {}),
    }
    # Never attach mission YAML, secrets, or review findings.
    workflow_registry.apply_cas_transition(
        workflow_id=workflow.workflow_id,
        expected_version=workflow.version,
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
    logger.info(
        (
            "workflow event=child_materialize_poisoned workflow_id=%s "
            "step_id=%s child_run_id=%s reason=%s"
        ),
        workflow.workflow_id,
        step.step_id,
        step.child_run_id,
        reason,
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
    """Canonical RunRegistry ownership from the claimed step parent binding."""
    try:
        return resolve_run_registry_ownership(
            parent_run_id=step.parent_run_id,
        )
    except ValueError:
        return None


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
) -> MaterializeResult:
    """Materialize one claimed workflow child into RunRegistry + queue.

    Uses the exact stored step ``mission_yaml`` and caller-reserved
    ``child_run_id``. Enqueues at most once per logical child when this
    attempt wins the materialization CAS.
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
        _poison(
            workflow_registry,
            workflow=workflow,
            step=step,
            reason="missing_mission_yaml",
        )
        return MaterializeResult(
            outcome=MaterializeOutcome.INVALID_MISSION,
            child_run_id=step.child_run_id,
            step_id=step.step_id,
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

    # Already materialized → verify identity; never re-enqueue.
    if (
        step.materialization_state is StepMaterializationState.MATERIALIZED
        or step.status
        in {StepStatus.QUEUED, StepStatus.RUNNING, StepStatus.COMPLETED}
    ):
        existing = run_registry.get_run(step.child_run_id)
        if existing is None:
            workflow = workflow_registry.get_workflow(workflow_id) or workflow
            _poison(
                workflow_registry,
                workflow=workflow,
                step=step,
                reason="materialized_without_run",
            )
            return MaterializeResult(
                outcome=MaterializeOutcome.CONFLICT,
                child_run_id=step.child_run_id,
                step_id=step.step_id,
                reason="materialized_without_run",
            )
        mismatch = _verify_existing_run(
            existing,
            mission_yaml=mission_yaml,
            ownership=ownership,
        )
        if mismatch:
            workflow = workflow_registry.get_workflow(workflow_id) or workflow
            _poison(
                workflow_registry,
                workflow=workflow,
                step=step,
                reason=mismatch,
                detail={"conflict_class": mismatch},
            )
            return MaterializeResult(
                outcome=MaterializeOutcome.CONFLICT,
                child_run_id=step.child_run_id,
                step_id=step.step_id,
                reason=mismatch,
            )
        logger.info(
            (
                "workflow event=child_materialize_idempotent workflow_id=%s "
                "step_id=%s child_run_id=%s"
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
            create_outcome=ReservedRunOutcome.RECOVERED_IDEMPOTENTLY.value,
        )

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
        _poison(
            workflow_registry,
            workflow=workflow,
            step=step,
            reason=denial,
            detail={"policy_audit": policy_audit},
        )
        return MaterializeResult(
            outcome=MaterializeOutcome.POLICY_DENIED,
            child_run_id=step.child_run_id,
            step_id=step.step_id,
            reason=denial,
            policy_audit=policy_audit,
        )

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
        _poison(
            workflow_registry,
            workflow=workflow,
            step=step,
            reason="invalid_mission_yaml",
        )
        return MaterializeResult(
            outcome=MaterializeOutcome.INVALID_MISSION,
            child_run_id=step.child_run_id,
            step_id=step.step_id,
            reason="invalid_mission_yaml",
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
        _poison(
            workflow_registry,
            workflow=workflow,
            step=step,
            reason=conflict,
            detail={"conflict_class": conflict},
        )
        return MaterializeResult(
            outcome=MaterializeOutcome.CONFLICT,
            child_run_id=step.child_run_id,
            step_id=step.step_id,
            reason=conflict,
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
        # Peer may have materialized; recover idempotently without enqueue.
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
                    logger.info(
                        (
                            "workflow event=child_materialize_recovered "
                            "workflow_id=%s step_id=%s child_run_id=%s "
                            "create_outcome=%s enqueued=0"
                        ),
                        workflow_id,
                        step.step_id,
                        step.child_run_id,
                        create_result.outcome.value,
                    )
                    return MaterializeResult(
                        outcome=MaterializeOutcome.RECOVERED_IDEMPOTENTLY,
                        child_run_id=step.child_run_id,
                        step_id=step.step_id,
                        enqueued=False,
                        create_outcome=create_result.outcome.value,
                        policy_audit=policy_audit,
                    )
            workflow = workflow_registry.get_workflow(workflow_id) or workflow
            _poison(
                workflow_registry,
                workflow=workflow,
                step=step,
                reason=mismatch or "materialize_mark_race",
            )
            return MaterializeResult(
                outcome=MaterializeOutcome.CONFLICT,
                child_run_id=step.child_run_id,
                step_id=step.step_id,
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

    # Exactly one enqueue per logical child: only the CAS mark winner.
    run_queue.enqueue(step.child_run_id, mission, run_registry)
    outcome = (
        MaterializeOutcome.CREATED
        if create_result.outcome is ReservedRunOutcome.CREATED
        else MaterializeOutcome.RECOVERED_IDEMPOTENTLY
    )
    logger.info(
        (
            "workflow event=child_materialized workflow_id=%s step_id=%s "
            "child_run_id=%s outcome=%s create_outcome=%s enqueued=1"
        ),
        workflow_id,
        step.step_id,
        step.child_run_id,
        outcome.value,
        create_result.outcome.value,
    )
    return MaterializeResult(
        outcome=outcome,
        child_run_id=step.child_run_id,
        step_id=step.step_id,
        enqueued=True,
        create_outcome=create_result.outcome.value,
        policy_audit=policy_audit,
    )


__all__ = [
    "MaterializeCrashHooks",
    "MaterializeOutcome",
    "MaterializeResult",
    "materialize_claimed_child",
]
