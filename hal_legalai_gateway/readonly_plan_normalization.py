"""Fail-closed normalization of safe read-only plan missions to execute.

Mission Control's asynchronous ``/runs`` path requires ``execution.mode=execute``.
Callers sometimes submit substantively read-only review missions with
``execution.mode=plan``. This adapter runs at the Unified gateway boundary
immediately before forwarding raw Mission Spec YAML so safe read-only plan
missions become eligible without weakening Mission Control eligibility.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Mutation / Git agent flags that must be present and exactly false.
_REQUIRED_FALSE_PERMISSIONS: tuple[str, ...] = (
    "create_files",
    "modify_files",
    "delete_files",
    "stage_changes",
    "commit",
    "push",
)


@dataclass(frozen=True)
class NormalizationResult:
    """Outcome of attempting read-only plan→execute normalization."""

    mission_yaml: str
    normalized: bool
    reason: str


def _is_exact_false(value: Any) -> bool:
    """Return True only for the boolean ``False`` (not 0, ``"false"``, None)."""
    return isinstance(value, bool) and value is False


def _mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    return None


def _gates_allow_normalize(mission: dict[str, Any]) -> str | None:
    """Return None when safe to normalize; otherwise a secret-free reason code."""
    execution = _mapping(mission.get("execution"))
    if execution is None:
        return "execution_not_mapping"

    mode = execution.get("mode")
    if not isinstance(mode, str):
        return "mode_not_string"
    if mode != "plan":
        return "mode_not_plan"

    permissions = _mapping(mission.get("permissions"))
    if permissions is None:
        return "permissions_not_mapping"

    for key in _REQUIRED_FALSE_PERMISSIONS:
        if key not in permissions:
            return f"permission_missing_{key}"
        if not _is_exact_false(permissions[key]):
            return f"permission_not_exact_false_{key}"

    persistence = _mapping(mission.get("persistence"))
    if persistence is None:
        return "persistence_missing_or_not_mapping"

    if "mode" not in persistence:
        return "persistence_mode_missing"

    persistence_mode = persistence.get("mode")
    if not isinstance(persistence_mode, str):
        return "persistence_mode_not_string"
    if persistence_mode != "none":
        return "persistence_mode_not_none"

    return None


def normalize_readonly_plan_mission_yaml(
    mission_yaml: str,
    *,
    gateway_tool: str = "mission.submit",
) -> NormalizationResult:
    """Convert ``plan``→``execute`` only for demonstrably non-mutating missions.

    Fail closed: missing, malformed, or ambiguous gates return the original YAML
    unchanged. Never normalizes ``ask``, ``execute``, or unknown modes. Only
    ``execution.mode`` is changed when normalization applies; serialization uses
    ``yaml.safe_dump`` (no regex rewriting).
    """
    if not isinstance(mission_yaml, str):
        return NormalizationResult(
            mission_yaml="",
            normalized=False,
            reason="input_not_string",
        )

    if not mission_yaml.strip():
        return NormalizationResult(
            mission_yaml=mission_yaml,
            normalized=False,
            reason="empty_input",
        )

    try:
        data = yaml.safe_load(mission_yaml)
    except yaml.YAMLError:
        return NormalizationResult(
            mission_yaml=mission_yaml,
            normalized=False,
            reason="yaml_parse_error",
        )

    if not isinstance(data, dict):
        return NormalizationResult(
            mission_yaml=mission_yaml,
            normalized=False,
            reason="not_a_mapping",
        )

    execution = _mapping(data.get("execution"))
    if execution is None:
        return NormalizationResult(
            mission_yaml=mission_yaml,
            normalized=False,
            reason="execution_not_mapping",
        )

    mode = execution.get("mode")
    if mode == "execute":
        return NormalizationResult(
            mission_yaml=mission_yaml,
            normalized=False,
            reason="already_execute",
        )
    if mode == "ask":
        return NormalizationResult(
            mission_yaml=mission_yaml,
            normalized=False,
            reason="ask_unchanged",
        )
    if not isinstance(mode, str) or mode != "plan":
        return NormalizationResult(
            mission_yaml=mission_yaml,
            normalized=False,
            reason="mode_not_plan",
        )

    gate_reason = _gates_allow_normalize(data)
    if gate_reason is not None:
        return NormalizationResult(
            mission_yaml=mission_yaml,
            normalized=False,
            reason=gate_reason,
        )

    # Preserve semantics except the single mode field.
    execution["mode"] = "execute"
    normalized_yaml = yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )

    # Structured, secret-free observability (no instructions / credentials).
    logger.info(
        "readonly_plan_normalized gateway_tool=%s from_mode=plan "
        "to_mode=execute reason=safe_readonly_gates",
        gateway_tool,
    )

    return NormalizationResult(
        mission_yaml=normalized_yaml,
        normalized=True,
        reason="safe_readonly_plan_to_execute",
    )
