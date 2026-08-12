"""Validation logic for Mission Specification v1.0 files."""

from dataclasses import dataclass

import yaml

from mission_control.workspace import (
    normalize_submit_repository_path,
    require_platform_push_approval,
    resolve_persistence_mode,
)

SUPPORTED_VERSION = "1.0"

SUPPORTED_PERSISTENCE_MODES = (
    "none",
    "commit",
    "push",
)

SUPPORTED_DOCUMENTATION_MODES = (
    "none",
    "required",
)

DEFAULT_DOCUMENTATION_MODE = "none"

REQUIRED_TOP_LEVEL_KEYS = (
    "version",
    "mission_id",
    "title",
    "repository",
    "execution",
    "permissions",
    "instructions",
    "deliverables",
    "approval",
)

RUN_AGENT = "cursor"
RUN_MODE = "plan"
EXECUTE_MODE = "execute"

RUN_FALSE_PERMISSIONS = (
    "create_files",
    "modify_files",
    "delete_files",
    "stage_changes",
    "commit",
    "push",
)

# Agent Git permission flags (stage_changes / commit / push) are legacy and
# optional for execute. Platform staging, committing, and pushing are governed
# solely by ``persistence.mode`` (see ``persist_workspace_changes``).
EXECUTE_FALSE_PERMISSIONS = (
    "delete_files",
)

# Exact permission set for genuine read-only execute (inspection / planning)
# missions. ``execution.mode`` remains ``execute``; no separate plan mode.
READ_ONLY_EXECUTE_PERMISSIONS = (
    ("read", True),
    ("create_files", False),
    ("modify_files", False),
    ("delete_files", False),
    ("run_commands", True),
    ("stage_changes", False),
    ("commit", False),
    ("push", False),
)


@dataclass
class ValidationResult:
    ok: bool
    error: str | None = None


def _normalized_version(value: object) -> str:
    return str(value)


def validate_mission(data: object) -> ValidationResult:
    if not isinstance(data, dict):
        return ValidationResult(
            ok=False,
            error="Mission must be a YAML mapping at the top level",
        )

    missing_keys = [
        key
        for key in REQUIRED_TOP_LEVEL_KEYS
        if key not in data
    ]

    if missing_keys:
        return ValidationResult(
            ok=False,
            error="Missing required keys: " + ", ".join(missing_keys),
        )

    version = _normalized_version(data["version"])

    if version != SUPPORTED_VERSION:
        return ValidationResult(
            ok=False,
            error=(
                f"Unsupported version: {data['version']} "
                f"(expected {SUPPORTED_VERSION})"
            ),
        )

    persistence_result = _validate_persistence(data)
    if not persistence_result.ok:
        return persistence_result

    return _validate_documentation(data)


def resolve_documentation_mode(mission: dict) -> str:
    """Return the documentation policy mode for ``mission``.

    When the top-level ``documentation`` block is omitted, or when ``mode`` is
    omitted inside that block (or is null), the mode defaults to ``none`` for
    backward compatibility with existing Mission Specs.
    """
    documentation = mission.get("documentation")
    if not isinstance(documentation, dict):
        return DEFAULT_DOCUMENTATION_MODE
    mode = documentation.get("mode", DEFAULT_DOCUMENTATION_MODE)
    if mode is None:
        return DEFAULT_DOCUMENTATION_MODE
    return str(mode)


def _validate_persistence(data: dict) -> ValidationResult:
    """Validate optional top-level ``persistence`` (platform Git modes)."""
    if "persistence" not in data:
        return ValidationResult(ok=True)

    persistence = data["persistence"]
    if not isinstance(persistence, dict):
        return ValidationResult(
            ok=False,
            error="persistence must be a mapping",
        )

    if "mode" not in persistence or persistence.get("mode") is None:
        return ValidationResult(ok=True)

    mode = persistence.get("mode")
    if mode not in SUPPORTED_PERSISTENCE_MODES:
        return ValidationResult(
            ok=False,
            error=(
                f"Unsupported persistence.mode: {mode} "
                "(expected one of: none, commit, push)"
            ),
        )

    return ValidationResult(ok=True)


def _validate_documentation(data: dict) -> ValidationResult:
    """Validate optional top-level ``documentation`` (docs review policy)."""
    if "documentation" not in data:
        return ValidationResult(ok=True)

    documentation = data["documentation"]
    if not isinstance(documentation, dict):
        return ValidationResult(
            ok=False,
            error="documentation must be a mapping",
        )

    if "mode" not in documentation or documentation.get("mode") is None:
        return ValidationResult(ok=True)

    mode = documentation.get("mode")
    if mode not in SUPPORTED_DOCUMENTATION_MODES:
        return ValidationResult(
            ok=False,
            error=(
                f"Unsupported documentation.mode: {mode} "
                "(expected one of: none, required)"
            ),
        )

    return ValidationResult(ok=True)


def load_mission_yaml(
    yaml_text: str,
) -> tuple[ValidationResult, dict | None]:
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return ValidationResult(
            ok=False,
            error=f"Invalid YAML: {exc}",
        ), None

    result = validate_mission(data)

    if not result.ok:
        return result, None

    return result, data


def load_mission_file(
    path: str,
) -> tuple[ValidationResult, dict | None]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            yaml_text = handle.read()
    except FileNotFoundError:
        return ValidationResult(
            ok=False,
            error=f"File not found: {path}",
        ), None
    except OSError as exc:
        return ValidationResult(
            ok=False,
            error=f"Cannot read file: {path} ({exc})",
        ), None

    return load_mission_yaml(yaml_text)


def validate_mission_file(path: str) -> ValidationResult:
    result, _ = load_mission_file(path)
    return result


def _mapping_value(
    data: dict,
    section: str,
) -> dict | None:
    value = data.get(section)

    if not isinstance(value, dict):
        return None

    return value


def _validate_repository_path(data: dict) -> ValidationResult:
    repository = _mapping_value(data, "repository")

    if repository is None:
        return ValidationResult(
            ok=False,
            error="repository must be a mapping",
        )

    # repository.name is authoritative. When it resolves to a managed clone
    # identity, normalize or ignore stale caller paths (e.g. /workspace/…)
    # instead of failing on a missing guessed host filesystem path.
    normalized, path_error = normalize_submit_repository_path(data)
    if path_error is not None or not normalized:
        return ValidationResult(
            ok=False,
            error=path_error or "repository.path is invalid",
        )

    return ValidationResult(ok=True)


def validate_mission_for_run(data: dict) -> ValidationResult:
    execution = _mapping_value(data, "execution")

    if execution is None:
        return ValidationResult(
            ok=False,
            error="execution must be a mapping",
        )

    agent = execution.get("agent")

    if agent != RUN_AGENT:
        return ValidationResult(
            ok=False,
            error=f"Unsupported agent: {agent} (expected {RUN_AGENT})",
        )

    mode = execution.get("mode")

    if mode != RUN_MODE:
        return ValidationResult(
            ok=False,
            error=f"Unsupported mode: {mode} (expected {RUN_MODE})",
        )

    if execution.get("worktree"):
        return ValidationResult(
            ok=False,
            error="Worktrees are not supported in Phase 2",
        )

    permissions = _mapping_value(data, "permissions")

    if permissions is None:
        return ValidationResult(
            ok=False,
            error="permissions must be a mapping",
        )

    for permission in RUN_FALSE_PERMISSIONS:
        if permissions.get(permission):
            return ValidationResult(
                ok=False,
                error=f"Permission not allowed for run: {permission}",
            )

    return _validate_repository_path(data)


def _is_read_only_execute_permissions(permissions: dict) -> bool:
    """Return True when permissions match a read-only execute mission."""
    for name, expected in READ_ONLY_EXECUTE_PERMISSIONS:
        if bool(permissions.get(name)) is not expected:
            return False
    return True


def validate_mission_for_execute(
    data: dict,
) -> ValidationResult:
    execution = _mapping_value(data, "execution")

    if execution is None:
        return ValidationResult(
            ok=False,
            error="execution must be a mapping",
        )

    agent = execution.get("agent")

    if agent != RUN_AGENT:
        return ValidationResult(
            ok=False,
            error=f"Unsupported agent: {agent} (expected {RUN_AGENT})",
        )

    mode = execution.get("mode")

    if mode != EXECUTE_MODE:
        return ValidationResult(
            ok=False,
            error=f"Unsupported mode: {mode} (expected {EXECUTE_MODE})",
        )

    if execution.get("worktree"):
        return ValidationResult(
            ok=False,
            error="Worktrees are not supported for execute",
        )

    permissions = _mapping_value(data, "permissions")

    if permissions is None:
        return ValidationResult(
            ok=False,
            error="permissions must be a mapping",
        )

    create_files = bool(permissions.get("create_files"))
    modify_files = bool(permissions.get("modify_files"))

    # Execute missions without create_files/modify_files are allowed when:
    # 1. Read-only inspection (exact READ_ONLY_EXECUTE_PERMISSIONS), or
    # 2. Push-only (persistence.mode=push). Platform push authorization is
    #    enforced separately via approval.platform_push_approved (or the
    #    automatic platform-push policy). Agent permissions.push is never
    #    required.
    if not create_files and not modify_files:
        if not _is_read_only_execute_permissions(permissions):
            if resolve_persistence_mode(data) != "push":
                return ValidationResult(
                    ok=False,
                    error=(
                        "Execute requires at least one of: "
                        "create_files or modify_files"
                    ),
                )

    for permission in EXECUTE_FALSE_PERMISSIONS:
        if permissions.get(permission):
            return ValidationResult(
                ok=False,
                error=(
                    "Permission not allowed for execute: "
                    f"{permission}"
                ),
            )

    # Legacy agent Git flags may be present (including truthy) without
    # affecting execute eligibility. They must not contradict persistence
    # mode: platform Git actions follow resolve_persistence_mode only.

    path_result = _validate_repository_path(data)
    if not path_result.ok:
        return path_result

    # Platform persistence.mode=push is privileged and distinct from
    # agent permissions.push. Reject queued runs that lack explicit
    # platform-push approval (or the automatic platform-push policy).
    approval_error = require_platform_push_approval(data)
    if approval_error is not None:
        return ValidationResult(ok=False, error=approval_error)

    return ValidationResult(ok=True)
