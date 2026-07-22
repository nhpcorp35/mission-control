"""Validation logic for Mission Specification v1.0 files."""

from dataclasses import dataclass
from pathlib import Path

import yaml

SUPPORTED_VERSION = "1.0"

SUPPORTED_PERSISTENCE_MODES = (
    "none",
    "commit",
    "push",
)

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

EXECUTE_FALSE_PERMISSIONS = (
    "delete_files",
    "stage_changes",
    "commit",
    "push",
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

    return _validate_persistence(data)


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

    repo_path = repository.get("path")

    if not isinstance(repo_path, str) or not repo_path.strip():
        return ValidationResult(
            ok=False,
            error="repository.path must be a non-empty string",
        )

    path = Path(repo_path)

    if not path.exists():
        return ValidationResult(
            ok=False,
            error=f"Repository path does not exist: {repo_path}",
        )

    if not path.is_dir():
        return ValidationResult(
            ok=False,
            error=f"Repository path is not a directory: {repo_path}",
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

    if not create_files and not modify_files:
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

    return _validate_repository_path(data)
