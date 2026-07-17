"""Validation logic for Mission Specification v1.0 files."""

from dataclasses import dataclass
from pathlib import Path

import yaml

SUPPORTED_VERSION = "1.0"

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

RUN_FALSE_PERMISSIONS = (
    "create_files",
    "modify_files",
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
    """Normalize a version value to a string for comparison.

    Accepts both `version: 1.0` (parsed by YAML as a float) and
    `version: "1.0"` (parsed as a string).
    """
    return str(value)


def validate_mission(data: object) -> ValidationResult:
    """Validate a parsed mission object against Mission Specification v1.0."""
    if not isinstance(data, dict):
        return ValidationResult(
            ok=False,
            error="Mission must be a YAML mapping at the top level",
        )

    missing_keys = [key for key in REQUIRED_TOP_LEVEL_KEYS if key not in data]
    if missing_keys:
        return ValidationResult(
            ok=False,
            error="Missing required keys: " + ", ".join(missing_keys),
        )

    version = _normalized_version(data["version"])
    if version != SUPPORTED_VERSION:
        return ValidationResult(
            ok=False,
            error=f"Unsupported version: {data['version']} (expected {SUPPORTED_VERSION})",
        )

    return ValidationResult(ok=True)


def load_mission_yaml(yaml_text: str) -> tuple[ValidationResult, dict | None]:
    """Load mission YAML text and return structural validation plus parsed data."""
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return ValidationResult(ok=False, error=f"Invalid YAML: {exc}"), None

    result = validate_mission(data)
    if not result.ok:
        return result, None
    return result, data


def load_mission_file(path: str) -> tuple[ValidationResult, dict | None]:
    """Load a mission file and return structural validation plus parsed data."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            yaml_text = handle.read()
    except FileNotFoundError:
        return ValidationResult(ok=False, error=f"File not found: {path}"), None
    except OSError as exc:
        return ValidationResult(ok=False, error=f"Cannot read file: {path} ({exc})"), None

    return load_mission_yaml(yaml_text)


def validate_mission_file(path: str) -> ValidationResult:
    """Load a mission file from disk and validate it."""
    result, _ = load_mission_file(path)
    return result


def _mapping_value(data: dict, section: str) -> dict | None:
    value = data.get(section)
    if not isinstance(value, dict):
        return None
    return value


def validate_mission_for_run(data: dict) -> ValidationResult:
    """Validate that a mission is eligible for Phase 2 read-only execution."""
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
