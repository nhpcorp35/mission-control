"""Validation logic for Mission Specification v1.0 files."""

from dataclasses import dataclass

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


def validate_mission_file(path: str) -> ValidationResult:
    """Load a mission file from disk and validate it."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except FileNotFoundError:
        return ValidationResult(ok=False, error=f"File not found: {path}")
    except OSError as exc:
        return ValidationResult(ok=False, error=f"Cannot read file: {path} ({exc})")
    except yaml.YAMLError as exc:
        return ValidationResult(ok=False, error=f"Invalid YAML: {exc}")

    return validate_mission(data)
