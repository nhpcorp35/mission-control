"""Authenticated HTTP submit/status/cancel helpers for durable workflow orchestration.

Slice A control surface: parse strict workflow YAML, persist through
``WorkflowRegistry.create_workflow``, and project sanitized status. Child
progression remains the existing orchestrator + materializer + reconciler.
This module does not enable the feature flag, forward through a gateway,
or emit notifications.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
import re
import sqlite3
from typing import Any
import uuid

from pydantic import BaseModel, Field
import yaml

from mission_control.workflow_materializer import (
    _parse_exact_mission,
)
from mission_control.workflow_orchestrator import (
    assert_review_step_read_only,
    bound_text,
    enforce_launch_policy_gates,
    redact_secrets,
)
from mission_control.run_registry import RunRegistry
from mission_control.workflow_registry import (
    StepType,
    WorkflowPolicySnapshot,
    WorkflowRecord,
    WorkflowRegistry,
    WorkflowStepSpec,
    is_workflow_orchestration_enabled,
)

logger = logging.getLogger(__name__)

# Strict document / field bounds (fail closed).
MAX_WORKFLOW_YAML_BYTES = 65_536
MAX_WORKFLOW_YAML_CHARS = 65_536
MAX_STEPS = 4
MAX_DEPENDENCIES_PER_STEP = 4
MAX_STRING_CHARS = 256
MAX_MISSION_YAML_CHARS = 16_384
MAX_SCOPE_ITEMS = 16
MAX_SCOPE_ITEM_CHARS = 256
MAX_ERROR_CHARS = 240
MAX_IDEMPOTENCY_KEY_CHARS = 128
MAX_TREE_NODES = 4_096
MAX_TREE_DEPTH = 24

WORKFLOW_YAML_VERSION = "1.0"
ALLOWED_TOP_LEVEL_KEYS = frozenset({"version", "policy", "steps"})
ALLOWED_STEP_KEYS = frozenset(
    {"id", "type", "mission_yaml", "depends_on", "label"}
)
ALLOWED_POLICY_KEYS = frozenset(
    {
        "repository_name",
        "base_branch",
        "target_branch",
        "implementation_scope",
        "allow_auto_merge",
        "allow_auto_deploy",
        "allow_destructive_actions",
        "allow_permission_expansion",
        "allow_database_migrations",
        "allow_secret_changes",
        "allow_scope_or_repo_changes",
        "max_fix_cycles",
        "max_child_runs",
        "max_wall_clock_seconds",
        "max_credit_units",
        "credit_unit_per_child_run",
    }
)
REQUIRED_POLICY_KEYS = frozenset(
    {
        "repository_name",
        "base_branch",
        "target_branch",
        "implementation_scope",
    }
)
REQUIRED_STEP_TYPES = (StepType.IMPLEMENTATION, StepType.REVIEW)
_CANONICAL_PREDECESSOR: dict[StepType, StepType | None] = {
    StepType.IMPLEMENTATION: None,
    StepType.REVIEW: StepType.IMPLEMENTATION,
    StepType.FIX: StepType.REVIEW,
    StepType.RE_REVIEW: StepType.FIX,
}
_IDEMPOTENCY_NAMESPACE = uuid.UUID("3c8b1d6e-7f21-4f0a-9e4c-2a6b8c0d1e5f")
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._~:-]{1,128}$")

_CHILD_SUMMARY_KEYS = frozenset(
    {
        "run_id",
        "status",
        "error",
        "commit_sha",
        "return_code",
        "elapsed_seconds",
        "completed_at",
    }
)


class WorkflowSubmitError(ValueError):
    """Sanitized submit/status failure with a stable machine code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = sanitize_public_error(message)
        super().__init__(self.message)


class WorkflowConflictError(WorkflowSubmitError):
    """Idempotency key reused with a different payload."""


class WorkflowSubmitRequest(BaseModel):
    """POST /workflows request body."""

    model_config = {"extra": "forbid"}

    workflow_yaml: str = Field(..., min_length=1)


class WorkflowAcceptedResponse(BaseModel):
    """POST /workflows success body."""

    workflow_id: str
    state: str
    idempotent_replay: bool = False


class WorkflowPolicyPublicModel(BaseModel):
    """Immutable policy snapshot fields (no secrets, no mission YAML)."""

    repository_name: str
    base_branch: str
    target_branch: str
    implementation_scope: list[str]
    allow_auto_merge: bool
    allow_auto_deploy: bool
    allow_destructive_actions: bool
    allow_permission_expansion: bool
    allow_database_migrations: bool
    allow_secret_changes: bool
    allow_scope_or_repo_changes: bool
    max_fix_cycles: int
    max_child_runs: int
    max_wall_clock_seconds: int
    max_credit_units: int
    credit_unit_per_child_run: int


class WorkflowStepTemplateModel(BaseModel):
    """Declared step template identity (never includes mission YAML)."""

    step_type: str
    label: str | None = None


class WorkflowChildRunSummaryModel(BaseModel):
    """Sanitized child-run projection (never stdout/stderr/mission YAML)."""

    run_id: str
    status: str
    error: str | None = None
    commit_sha: str | None = None
    return_code: int | None = None
    elapsed_seconds: float | None = None
    completed_at: datetime | None = None


class WorkflowStepStatusModel(BaseModel):
    """Durable step row without mission YAML or policy blobs."""

    step_id: str
    step_type: str
    status: str
    attempt: int
    cycle: int
    child_run_id: str | None = None
    error: str | None = None
    child_run: WorkflowChildRunSummaryModel | None = None


class WorkflowStatusResponse(BaseModel):
    """GET /workflows/{workflow_id} sanitized durable status."""

    workflow_id: str
    state: str
    version: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    parent_run_id: str | None = None
    current_step_id: str | None = None
    fix_cycle_count: int
    child_run_count: int
    credit_units_used: int
    error: str | None = None
    last_blocker_fingerprint: str | None = None
    last_decision_action: str | None = None
    notification_emitted: bool
    policy: WorkflowPolicyPublicModel
    step_templates: list[WorkflowStepTemplateModel]
    steps: list[WorkflowStepStatusModel]


@dataclass(frozen=True)
class ParsedWorkflowSubmit:
    """Canonical result of a successful workflow YAML parse."""

    policy: WorkflowPolicySnapshot
    implementation: WorkflowStepSpec
    review: WorkflowStepSpec
    fix: WorkflowStepSpec | None
    re_review: WorkflowStepSpec | None
    fingerprint: str


def sanitize_public_error(message: str | None) -> str:
    """Bound and redact operator-facing error text."""
    cleaned = redact_secrets(str(message or "invalid_workflow"))
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        cleaned = "invalid_workflow"
    return bound_text(cleaned, MAX_ERROR_CHARS)


def require_workflow_orchestration_enabled(
    environ: dict[str, str] | None = None,
) -> None:
    """Fail closed unless the orchestration feature flag is explicit."""
    if not is_workflow_orchestration_enabled(environ):
        raise WorkflowSubmitError(
            "feature_disabled",
            "Workflow orchestration is disabled",
        )


def parse_idempotency_key(raw: str | None) -> str | None:
    """Return a validated idempotency key, or None when omitted."""
    if raw is None:
        return None
    key = str(raw).strip()
    if not key:
        return None
    if len(key) > MAX_IDEMPOTENCY_KEY_CHARS or not _IDEMPOTENCY_KEY_RE.match(key):
        raise WorkflowSubmitError(
            "invalid_idempotency_key",
            "Idempotency-Key is invalid",
        )
    return key


def workflow_id_for_idempotency_key(key: str) -> str:
    """Derive a stable canonical UUID from an idempotency key."""
    return str(
        uuid.uuid5(
            _IDEMPOTENCY_NAMESPACE,
            f"mc-workflow-v1:{key}",
        )
    )


def parse_workflow_yaml(workflow_yaml: str) -> ParsedWorkflowSubmit:
    """Parse and strictly validate a workflow YAML document."""
    if not isinstance(workflow_yaml, str):
        raise WorkflowSubmitError("invalid_yaml", "workflow_yaml must be a string")
    raw = workflow_yaml
    if len(raw.encode("utf-8")) > MAX_WORKFLOW_YAML_BYTES:
        raise WorkflowSubmitError(
            "yaml_too_large",
            "Workflow YAML exceeds the size limit",
        )
    if len(raw) > MAX_WORKFLOW_YAML_CHARS:
        raise WorkflowSubmitError(
            "yaml_too_large",
            "Workflow YAML exceeds the size limit",
        )
    if not raw.strip():
        raise WorkflowSubmitError("invalid_yaml", "Workflow YAML is empty")

    try:
        loaded = yaml.safe_load(raw)
    except (yaml.YAMLError, RecursionError):
        # In-limit deeply nested flow sequences can overflow the parser
        # stack before the post-load tree-depth check runs.
        raise WorkflowSubmitError(
            "invalid_yaml",
            "Workflow YAML could not be parsed",
        ) from None

    if not isinstance(loaded, dict):
        raise WorkflowSubmitError(
            "invalid_yaml",
            "Workflow YAML must be a mapping",
        )

    _assert_bounded_tree(loaded)
    _reject_unknown_keys(loaded, ALLOWED_TOP_LEVEL_KEYS, "workflow")

    version = _normalize_version(loaded.get("version"))
    if version != WORKFLOW_YAML_VERSION:
        raise WorkflowSubmitError(
            "unsupported_version",
            "Unsupported workflow YAML version",
        )

    policy = _parse_policy(loaded.get("policy"))
    steps = _parse_steps(loaded.get("steps"), policy=policy)
    by_type = {spec.step_type: spec for spec in steps}
    if StepType.IMPLEMENTATION not in by_type or StepType.REVIEW not in by_type:
        raise WorkflowSubmitError(
            "missing_required_step",
            "implementation and review steps are required",
        )
    if StepType.RE_REVIEW in by_type and StepType.FIX not in by_type:
        raise WorkflowSubmitError(
            "missing_required_step",
            "re_review requires a fix step",
        )

    parsed = ParsedWorkflowSubmit(
        policy=policy,
        implementation=by_type[StepType.IMPLEMENTATION],
        review=by_type[StepType.REVIEW],
        fix=by_type.get(StepType.FIX),
        re_review=by_type.get(StepType.RE_REVIEW),
        fingerprint="",
    )
    fingerprint = _fingerprint_parsed(parsed)
    return ParsedWorkflowSubmit(
        policy=parsed.policy,
        implementation=parsed.implementation,
        review=parsed.review,
        fix=parsed.fix,
        re_review=parsed.re_review,
        fingerprint=fingerprint,
    )


def submit_workflow(
    workflow_yaml: str,
    *,
    workflow_registry: WorkflowRegistry,
    idempotency_key: str | None = None,
    environ: dict[str, str] | None = None,
) -> WorkflowAcceptedResponse:
    """Validate YAML and persist a pending workflow (idempotent when keyed)."""
    require_workflow_orchestration_enabled(environ)
    key = parse_idempotency_key(idempotency_key)
    parsed = parse_workflow_yaml(workflow_yaml)
    reserved_id = workflow_id_for_idempotency_key(key) if key else None
    if reserved_id is not None:
        existing = workflow_registry.get_workflow(reserved_id)
        if existing is not None:
            return _replay_or_conflict(existing, parsed)

    try:
        record = workflow_registry.create_workflow(
            policy=parsed.policy,
            implementation=parsed.implementation,
            review=parsed.review,
            fix=parsed.fix,
            re_review=parsed.re_review,
            workflow_id=reserved_id,
        )
    except sqlite3.IntegrityError:
        if reserved_id is None:
            raise WorkflowSubmitError(
                "create_conflict",
                "Workflow could not be created",
            ) from None
        existing = workflow_registry.get_workflow(reserved_id)
        if existing is None:
            raise WorkflowSubmitError(
                "create_conflict",
                "Workflow could not be created",
            ) from None
        return _replay_or_conflict(existing, parsed)
    except ValueError as exc:
        raise WorkflowSubmitError(
            "invalid_workflow",
            sanitize_public_error(str(exc)),
        ) from None

    logger.info(
        "workflow_http event=submitted workflow_id=%s state=%s replay=%s",
        record.workflow_id,
        record.state.value,
        False,
    )
    return WorkflowAcceptedResponse(
        workflow_id=record.workflow_id,
        state=record.state.value,
        idempotent_replay=False,
    )


def build_workflow_status(
    workflow_id: str,
    *,
    workflow_registry: WorkflowRegistry,
    run_registry: RunRegistry,
    environ: dict[str, str] | None = None,
) -> WorkflowStatusResponse | None:
    """Return sanitized durable status, or None when the workflow is unknown."""
    require_workflow_orchestration_enabled(environ)
    workflow = workflow_registry.get_workflow(str(workflow_id))
    if workflow is None:
        return None
    step_rows = workflow_registry.list_steps(workflow.workflow_id)
    steps: list[WorkflowStepStatusModel] = []
    for step in step_rows:
        child_summary = None
        if step.child_run_id:
            child_summary = _child_run_summary(
                run_registry, step.child_run_id
            )
        steps.append(
            WorkflowStepStatusModel(
                step_id=step.step_id,
                step_type=step.step_type.value,
                status=step.status.value,
                attempt=step.attempt,
                cycle=step.cycle,
                child_run_id=step.child_run_id,
                error=_public_optional_text(step.error),
                child_run=child_summary,
            )
        )
    policy = workflow.policy_snapshot
    return WorkflowStatusResponse(
        workflow_id=workflow.workflow_id,
        state=workflow.state.value,
        version=workflow.version,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
        started_at=workflow.started_at,
        completed_at=workflow.completed_at,
        parent_run_id=workflow.parent_run_id,
        current_step_id=workflow.current_step_id,
        fix_cycle_count=workflow.fix_cycle_count,
        child_run_count=workflow.child_run_count,
        credit_units_used=workflow.credit_units_used,
        error=_public_optional_text(workflow.error),
        last_blocker_fingerprint=workflow.last_blocker_fingerprint,
        last_decision_action=_decision_action(workflow.last_decision),
        notification_emitted=bool(workflow.notification_emitted),
        policy=WorkflowPolicyPublicModel(**policy.to_dict()),
        step_templates=_step_templates(workflow.step_specs),
        steps=steps,
    )


_CANCEL_CONFLICT_MESSAGES = {
    "workflow_already_cancelled": "Workflow is already cancelled",
    "workflow_terminal": "Workflow is already terminal",
    "version_conflict": "Workflow version conflict",
}


def cancel_workflow(
    workflow_id: str,
    *,
    workflow_registry: WorkflowRegistry,
    run_registry: RunRegistry,
    environ: dict[str, str] | None = None,
) -> WorkflowStatusResponse | None:
    """Cancel a non-terminal workflow and return sanitized status.

    Returns None when ``workflow_id`` is unknown. Already-cancelled and
    other terminal workflows raise ``WorkflowConflictError`` with stable
    codes. Success uses the same sanitized projection as GET status.
    """
    require_workflow_orchestration_enabled(environ)
    result = workflow_registry.cancel_workflow(str(workflow_id))
    if result.error == "workflow_not_found" or (
        not result.ok and result.workflow is None
    ):
        return None
    if not result.ok:
        code = result.error or "workflow_terminal"
        message = _CANCEL_CONFLICT_MESSAGES.get(
            code, "Workflow could not be cancelled"
        )
        if code in _CANCEL_CONFLICT_MESSAGES:
            raise WorkflowConflictError(code, message)
        raise WorkflowSubmitError(code, message)
    return build_workflow_status(
        workflow_id,
        workflow_registry=workflow_registry,
        run_registry=run_registry,
        environ=environ,
    )


def _replay_or_conflict(
    existing: WorkflowRecord,
    parsed: ParsedWorkflowSubmit,
) -> WorkflowAcceptedResponse:
    existing_fp = _fingerprint_record(existing)
    if existing_fp != parsed.fingerprint:
        raise WorkflowConflictError(
            "idempotency_payload_mismatch",
            "Idempotency-Key was reused with a different workflow",
        )
    logger.info(
        "workflow_http event=idempotent_replay workflow_id=%s state=%s",
        existing.workflow_id,
        existing.state.value,
    )
    return WorkflowAcceptedResponse(
        workflow_id=existing.workflow_id,
        state=existing.state.value,
        idempotent_replay=True,
    )


def _fingerprint_parsed(parsed: ParsedWorkflowSubmit) -> str:
    specs: dict[str, Any] = {
        "implementation": parsed.implementation.to_dict(),
        "review": parsed.review.to_dict(),
    }
    if parsed.fix is not None:
        specs["fix"] = parsed.fix.to_dict()
    if parsed.re_review is not None:
        specs["re_review"] = parsed.re_review.to_dict()
    return _fingerprint(parsed.policy.to_dict(), specs)


def _fingerprint_record(record: WorkflowRecord) -> str:
    return _fingerprint(record.policy_snapshot.to_dict(), record.step_specs)


def _fingerprint(policy: dict[str, Any], specs: dict[str, Any]) -> str:
    payload = json.dumps(
        {"policy": policy, "specs": specs},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_version(value: Any) -> str:
    if value is None:
        raise WorkflowSubmitError("missing_field", "version is required")
    if isinstance(value, bool):
        raise WorkflowSubmitError("unsupported_version", "Invalid version")
    if isinstance(value, int):
        return f"{value}.0"
    if isinstance(value, float):
        if value == 1.0:
            return WORKFLOW_YAML_VERSION
        raise WorkflowSubmitError("unsupported_version", "Invalid version")
    text = str(value).strip()
    if text in {"1", "1.0"}:
        return WORKFLOW_YAML_VERSION
    return text


def _parse_policy(raw: Any) -> WorkflowPolicySnapshot:
    if raw is None:
        raise WorkflowSubmitError("missing_field", "policy is required")
    if not isinstance(raw, dict):
        raise WorkflowSubmitError("invalid_field", "policy must be a mapping")
    _reject_unknown_keys(raw, ALLOWED_POLICY_KEYS, "policy")
    missing = [key for key in sorted(REQUIRED_POLICY_KEYS) if key not in raw]
    if missing:
        raise WorkflowSubmitError(
            "missing_field",
            f"policy missing {missing[0]}",
        )
    scope = _parse_scope(raw.get("implementation_scope"))
    kwargs: dict[str, Any] = {
        "repository_name": _required_name(raw.get("repository_name"), "policy.repository_name"),
        "base_branch": _required_name(raw.get("base_branch"), "policy.base_branch"),
        "target_branch": _required_name(raw.get("target_branch"), "policy.target_branch"),
        "implementation_scope": tuple(scope),
    }
    for bool_field in (
        "allow_auto_merge",
        "allow_auto_deploy",
        "allow_destructive_actions",
        "allow_permission_expansion",
        "allow_database_migrations",
        "allow_secret_changes",
        "allow_scope_or_repo_changes",
    ):
        if bool_field in raw:
            kwargs[bool_field] = _parse_bool(raw[bool_field], f"policy.{bool_field}")
    for int_field, lo, hi in (
        ("max_fix_cycles", 1, 16),
        ("max_child_runs", 1, 32),
        ("max_wall_clock_seconds", 1, 7 * 24 * 60 * 60),
        ("max_credit_units", 1, 64),
        ("credit_unit_per_child_run", 1, 8),
    ):
        if int_field in raw:
            kwargs[int_field] = _parse_bounded_int(
                raw[int_field], f"policy.{int_field}", lo, hi
            )
    return WorkflowPolicySnapshot(**kwargs)


def _parse_scope(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise WorkflowSubmitError(
            "invalid_field",
            "policy.implementation_scope must be a list",
        )
    if not raw:
        raise WorkflowSubmitError(
            "invalid_field",
            "policy.implementation_scope must not be empty",
        )
    if len(raw) > MAX_SCOPE_ITEMS:
        raise WorkflowSubmitError(
            "limit_exceeded",
            "policy.implementation_scope exceeds the item limit",
        )
    scope: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise WorkflowSubmitError(
                "invalid_field",
                "policy.implementation_scope items must be strings",
            )
        text = item.strip()
        if not text:
            raise WorkflowSubmitError(
                "invalid_field",
                "policy.implementation_scope items must be non-empty",
            )
        if len(text) > MAX_SCOPE_ITEM_CHARS:
            raise WorkflowSubmitError(
                "limit_exceeded",
                "policy.implementation_scope item exceeds the length limit",
            )
        scope.append(text)
    return scope


def _parse_steps(
    raw: Any,
    *,
    policy: WorkflowPolicySnapshot,
) -> list[WorkflowStepSpec]:
    if raw is None:
        raise WorkflowSubmitError("missing_field", "steps is required")
    if not isinstance(raw, list):
        raise WorkflowSubmitError("invalid_field", "steps must be a list")
    if not raw:
        raise WorkflowSubmitError("invalid_field", "steps must not be empty")
    if len(raw) > MAX_STEPS:
        raise WorkflowSubmitError(
            "limit_exceeded",
            "steps exceeds the maximum of 4",
        )

    parsed_rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_types: set[StepType] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise WorkflowSubmitError(
                "invalid_field",
                "each step must be a mapping",
            )
        _reject_unknown_keys(item, ALLOWED_STEP_KEYS, f"steps[{index}]")
        if "type" not in item:
            raise WorkflowSubmitError("missing_field", "step type is required")
        step_type = _parse_step_type(item.get("type"))
        if step_type in seen_types:
            raise WorkflowSubmitError(
                "duplicate_step",
                "duplicate step type",
            )
        seen_types.add(step_type)
        step_id = item.get("id", step_type.value)
        step_id_text = _required_name(step_id, "step id")
        if step_id_text in seen_ids:
            raise WorkflowSubmitError("duplicate_step", "duplicate step id")
        seen_ids.add(step_id_text)
        mission_yaml = item.get("mission_yaml")
        if not isinstance(mission_yaml, str) or not mission_yaml.strip():
            raise WorkflowSubmitError(
                "invalid_field",
                "step mission_yaml is required",
            )
        if len(mission_yaml) > MAX_MISSION_YAML_CHARS:
            raise WorkflowSubmitError(
                "limit_exceeded",
                "step mission_yaml exceeds the length limit",
            )
        label = None
        if "label" in item and item["label"] is not None:
            label = _required_name(item["label"], "step label")
        depends_on = _parse_depends_on(item.get("depends_on"), index)
        parsed_rows.append(
            {
                "id": step_id_text,
                "type": step_type,
                "mission_yaml": mission_yaml,
                "depends_on": depends_on,
                "label": label,
                "explicit_depends": "depends_on" in item,
            }
        )

    id_to_type = {row["id"]: row["type"] for row in parsed_rows}
    type_to_id = {row["type"]: row["id"] for row in parsed_rows}
    for row in parsed_rows:
        _validate_dependencies(row, id_to_type=id_to_type, type_to_id=type_to_id)
    _assert_acyclic(parsed_rows)

    specs: list[WorkflowStepSpec] = []
    for row in parsed_rows:
        _validate_step_mission(row["type"], row["mission_yaml"], policy)
        specs.append(
            WorkflowStepSpec(
                step_type=row["type"],
                mission_yaml=row["mission_yaml"],
                label=row["label"],
            )
        )
    return specs


def _parse_depends_on(raw: Any, index: int) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise WorkflowSubmitError(
            "invalid_field",
            "depends_on must be a list",
        )
    if len(raw) > MAX_DEPENDENCIES_PER_STEP:
        raise WorkflowSubmitError(
            "limit_exceeded",
            "depends_on exceeds the per-step dependency limit",
        )
    deps: list[str] = []
    seen: set[str] = set()
    for dep in raw:
        name = _required_name(dep, f"steps[{index}].depends_on")
        if name in seen:
            raise WorkflowSubmitError(
                "duplicate_dependency",
                "duplicate depends_on entry",
            )
        seen.add(name)
        deps.append(name)
    return deps


def _validate_dependencies(
    row: dict[str, Any],
    *,
    id_to_type: dict[str, StepType],
    type_to_id: dict[StepType, str],
) -> None:
    step_type: StepType = row["type"]
    predecessor = _CANONICAL_PREDECESSOR[step_type]
    expected: list[str] = []
    if predecessor is not None and predecessor in type_to_id:
        expected = [type_to_id[predecessor]]
    deps: list[str] = list(row["depends_on"])
    if not row["explicit_depends"]:
        row["depends_on"] = expected
        deps = expected
    for dep in deps:
        if dep not in id_to_type:
            raise WorkflowSubmitError(
                "unknown_dependency",
                "depends_on refers to an unknown step",
            )
        if dep == row["id"]:
            raise WorkflowSubmitError(
                "invalid_dependency",
                "a step cannot depend on itself",
            )
    if deps != expected:
        raise WorkflowSubmitError(
            "invalid_dependency",
            "depends_on must match the v1 workflow graph",
        )


def _assert_acyclic(rows: list[dict[str, Any]]) -> None:
    incoming: dict[str, int] = {row["id"]: 0 for row in rows}
    edges: dict[str, list[str]] = {row["id"]: [] for row in rows}
    for row in rows:
        for dep in row["depends_on"]:
            if dep not in edges:
                continue
            edges[dep].append(row["id"])
            incoming[row["id"]] += 1
    queue = [step_id for step_id, count in incoming.items() if count == 0]
    seen = 0
    while queue:
        current = queue.pop()
        seen += 1
        for nxt in edges[current]:
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                queue.append(nxt)
    if seen != len(rows):
        raise WorkflowSubmitError(
            "cyclic_dependency",
            "step dependencies contain a cycle",
        )


def _validate_step_mission(
    step_type: StepType,
    mission_yaml: str,
    policy: WorkflowPolicySnapshot,
) -> None:
    parsed = _parse_exact_mission(mission_yaml)
    if parsed is None:
        raise WorkflowSubmitError(
            "invalid_mission_yaml",
            "step mission_yaml must be a YAML mapping",
        )
    denial, _evidence = enforce_launch_policy_gates(
        policy=policy,
        step_type=step_type,
        mission_yaml=mission_yaml,
    )
    if denial:
        raise WorkflowSubmitError("policy_denied", denial)
    if step_type in {StepType.REVIEW, StepType.RE_REVIEW}:
        read_only = assert_review_step_read_only(mission_yaml)
        if read_only:
            raise WorkflowSubmitError("policy_denied", read_only)


def _parse_step_type(raw: Any) -> StepType:
    if not isinstance(raw, str):
        raise WorkflowSubmitError("invalid_field", "step type must be a string")
    try:
        return StepType(raw.strip())
    except ValueError:
        raise WorkflowSubmitError(
            "unknown_step_type",
            "unknown step type",
        ) from None


def _required_name(raw: Any, field: str) -> str:
    if not isinstance(raw, str):
        raise WorkflowSubmitError("invalid_field", f"{field} must be a string")
    text = raw.strip()
    if not text:
        raise WorkflowSubmitError("invalid_field", f"{field} must be non-empty")
    if len(text) > MAX_STRING_CHARS:
        raise WorkflowSubmitError(
            "limit_exceeded",
            f"{field} exceeds the length limit",
        )
    return text


def _parse_bool(raw: Any, field: str) -> bool:
    if not isinstance(raw, bool):
        raise WorkflowSubmitError("invalid_field", f"{field} must be a boolean")
    return raw


def _parse_bounded_int(raw: Any, field: str, lo: int, hi: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise WorkflowSubmitError("invalid_field", f"{field} must be an integer")
    if raw < lo or raw > hi:
        raise WorkflowSubmitError(
            "limit_exceeded",
            f"{field} is out of bounds",
        )
    return raw


def _reject_unknown_keys(
    mapping: dict[str, Any],
    allowed: frozenset[str],
    where: str,
) -> None:
    unknown = sorted(str(key) for key in mapping.keys() if key not in allowed)
    if unknown:
        # Do not echo values; names are bounded identifiers only.
        name = bound_text(str(unknown[0]), MAX_STRING_CHARS)
        raise WorkflowSubmitError(
            "unknown_field",
            f"Unknown field in {where}: {name}",
        )


def _assert_bounded_tree(root: Any) -> None:
    stack: list[tuple[Any, int]] = [(root, 0)]
    nodes = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > MAX_TREE_NODES:
            raise WorkflowSubmitError(
                "limit_exceeded",
                "Workflow YAML exceeds the node limit",
            )
        if depth > MAX_TREE_DEPTH:
            raise WorkflowSubmitError(
                "limit_exceeded",
                "Workflow YAML exceeds the depth limit",
            )
        if isinstance(node, str) and len(node) > MAX_MISSION_YAML_CHARS:
            raise WorkflowSubmitError(
                "limit_exceeded",
                "Workflow YAML contains an oversized string",
            )
        if isinstance(node, dict):
            for key, value in node.items():
                if not isinstance(key, str):
                    raise WorkflowSubmitError(
                        "invalid_yaml",
                        "Workflow YAML keys must be strings",
                    )
                if len(key) > MAX_STRING_CHARS:
                    raise WorkflowSubmitError(
                        "limit_exceeded",
                        "Workflow YAML key exceeds the length limit",
                    )
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            if len(node) > max(MAX_STEPS, MAX_SCOPE_ITEMS, MAX_DEPENDENCIES_PER_STEP, 64):
                raise WorkflowSubmitError(
                    "limit_exceeded",
                    "Workflow YAML list exceeds the length limit",
                )
            for value in node:
                stack.append((value, depth + 1))


def _step_templates(step_specs: dict[str, Any]) -> list[WorkflowStepTemplateModel]:
    templates: list[WorkflowStepTemplateModel] = []
    for key in ("implementation", "review", "fix", "re_review"):
        raw = step_specs.get(key)
        if not isinstance(raw, dict):
            continue
        label = raw.get("label")
        templates.append(
            WorkflowStepTemplateModel(
                step_type=str(raw.get("step_type") or key),
                label=str(label) if label is not None else None,
            )
        )
    return templates


def _decision_action(last_decision: dict[str, Any] | None) -> str | None:
    if not isinstance(last_decision, dict):
        return None
    action = last_decision.get("action")
    if not isinstance(action, str) or not action.strip():
        return None
    return bound_text(action.strip(), MAX_STRING_CHARS)


def _public_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = sanitize_public_error(value)
    return cleaned or None


def _child_run_summary(
    run_registry: RunRegistry,
    child_run_id: str,
) -> WorkflowChildRunSummaryModel | None:
    record = run_registry.get_run(child_run_id)
    if record is None:
        return None
    payload = {
        "run_id": record.run_id,
        "status": record.status.value,
        "error": _public_optional_text(record.error),
        "commit_sha": record.commit_sha,
        "return_code": record.return_code,
        "elapsed_seconds": record.elapsed_seconds,
        "completed_at": record.completed_at,
    }
    # Drop anything outside the sanitized contract (defense in depth).
    safe = {key: payload[key] for key in _CHILD_SUMMARY_KEYS}
    return WorkflowChildRunSummaryModel(**safe)


__all__ = [
    "MAX_DEPENDENCIES_PER_STEP",
    "MAX_IDEMPOTENCY_KEY_CHARS",
    "MAX_STEPS",
    "MAX_WORKFLOW_YAML_BYTES",
    "ParsedWorkflowSubmit",
    "WorkflowAcceptedResponse",
    "WorkflowChildRunSummaryModel",
    "WorkflowConflictError",
    "WorkflowPolicyPublicModel",
    "WorkflowStatusResponse",
    "WorkflowStepStatusModel",
    "WorkflowStepTemplateModel",
    "WorkflowSubmitError",
    "WorkflowSubmitRequest",
    "build_workflow_status",
    "parse_idempotency_key",
    "parse_workflow_yaml",
    "require_workflow_orchestration_enabled",
    "sanitize_public_error",
    "submit_workflow",
    "workflow_id_for_idempotency_key",
]
