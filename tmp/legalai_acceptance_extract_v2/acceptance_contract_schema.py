"""Versioned JSON schema and strict validation for acceptance contracts.

Validation is fail-closed and dependency-free (no jsonschema package required).
Diagnostics never embed contract body contents — only paths and safe codes.
"""

from __future__ import annotations

from typing import Any, Mapping

# Stable schema identity. Unversioned or unknown versions fail closed.
SCHEMA_VERSION = "acceptance_contract.v1"
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION})

# Document-level contract version must be a non-empty string (semver-like or opaque).
_REQUIRED_TOP_LEVEL = (
    "schema_version",
    "contract_id",
    "version",
    "identity",
    "required_criterion_ids",
    "evidence_constraints",
    "semantic_preservation",
    "duplication_rules",
    "object_key",
    "content_sha256",
)


def acceptance_contract_json_schema() -> dict[str, Any]:
    """Return the strict JSON Schema for acceptance_contract.v1 (generic)."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(_REQUIRED_TOP_LEVEL),
        "properties": {
            "schema_version": {
                "type": "string",
                "enum": sorted(SUPPORTED_SCHEMA_VERSIONS),
            },
            "contract_id": {"type": "string", "minLength": 1},
            "version": {"type": "string", "minLength": 1},
            "identity": {
                "type": "object",
                "additionalProperties": False,
                "required": ["benchmark_id", "question_id"],
                "properties": {
                    "benchmark_id": {"type": "string", "minLength": 1},
                    "question_id": {"type": "string", "minLength": 1},
                },
            },
            "required_criterion_ids": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
            "evidence_constraints": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "allowed_source_types",
                    "require_page_citations",
                    "max_excerpts_per_criterion",
                ],
                "properties": {
                    "allowed_source_types": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "require_page_citations": {"type": "boolean"},
                    "max_excerpts_per_criterion": {
                        "type": "integer",
                        "minimum": 1,
                    },
                },
            },
            "semantic_preservation": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "require_same_party_roles",
                    "forbid_material_omissions",
                    "require_preserve_negation",
                ],
                "properties": {
                    "require_same_party_roles": {"type": "boolean"},
                    "forbid_material_omissions": {"type": "boolean"},
                    "require_preserve_negation": {"type": "boolean"},
                },
            },
            "duplication_rules": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "forbid_duplicate_criterion_ids",
                    "forbid_overlapping_evidence_spans",
                    "max_duplicate_phrase_ratio",
                ],
                "properties": {
                    "forbid_duplicate_criterion_ids": {"type": "boolean"},
                    "forbid_overlapping_evidence_spans": {"type": "boolean"},
                    "max_duplicate_phrase_ratio": {"type": "number", "minimum": 0},
                },
            },
            # Optional evaluation payload (phase 2). Values are never logged —
            # only ids/counts/result codes appear in audit/manifest provenance.
            "criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id"],
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "presence_phrases": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "evidence_phrases": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "semantic_required_phrases": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "semantic_forbidden_phrases": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "fallback_text": {"type": "string"},
                        "category": {"type": "string"},
                    },
                },
            },
            "structure_requirements": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "required_kinds",
                    "required_ranges",
                    "required_categories",
                ],
                "properties": {
                    "required_kinds": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                    "required_ranges": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["kind", "start", "end"],
                            "properties": {
                                "kind": {"type": "string", "minLength": 1},
                                "start": {"type": "integer"},
                                "end": {"type": "integer"},
                                "category": {"type": "string"},
                            },
                        },
                    },
                    "required_categories": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
            "object_key": {"type": "string", "minLength": 1},
            "content_sha256": {
                "type": "string",
                "minLength": 64,
                "maxLength": 64,
                "pattern": "^[0-9a-f]{64}$",
            },
        },
    }


def _type_ok(value: Any, type_spec: Any) -> bool:
    if isinstance(type_spec, list):
        return any(_type_ok(value, option) for option in type_spec)
    if type_spec == "object":
        return isinstance(value, dict)
    if type_spec == "array":
        return isinstance(value, list)
    if type_spec == "string":
        return isinstance(value, str)
    if type_spec == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_spec == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_spec == "boolean":
        return isinstance(value, bool)
    if type_spec == "null":
        return value is None
    return False


def _check_string_constraints(
    value: str, schema: Mapping[str, Any], path: str, out: list[str]
) -> None:
    if "minLength" in schema and len(value) < int(schema["minLength"]):
        out.append(f"{path}: string shorter than minLength")
    if "maxLength" in schema and len(value) > int(schema["maxLength"]):
        out.append(f"{path}: string longer than maxLength")
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and pattern == "^[0-9a-f]{64}$":
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            out.append(f"{path}: expected lowercase hex sha-256 digest")


def _validate_against_schema(
    value: Any, schema: Mapping[str, Any], path: str, out: list[str]
) -> None:
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: value not in enum")
        return
    type_spec = schema.get("type")
    if type_spec is not None and not _type_ok(value, type_spec):
        out.append(f"{path}: expected type {type_spec}")
        return

    if type_spec == "string" or (
        isinstance(type_spec, list) and "string" in type_spec and isinstance(value, str)
    ):
        if isinstance(value, str):
            _check_string_constraints(value, schema, path, out)

    if type_spec == "integer" or (
        isinstance(type_spec, list)
        and "integer" in type_spec
        and isinstance(value, int)
        and not isinstance(value, bool)
    ):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path}: integer below minimum")

    if type_spec == "number" or (
        isinstance(type_spec, list)
        and "number" in type_spec
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        if "minimum" in schema and float(value) < float(schema["minimum"]):
            out.append(f"{path}: number below minimum")

    is_object = type_spec == "object" or (
        isinstance(type_spec, list) and "object" in type_spec
    )
    if is_object:
        if not isinstance(value, dict):
            return
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                out.append(f"{path}: missing required property '{key}'")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                out.append(f"{path}: unexpected properties {extra}")
        for key, child in value.items():
            child_schema = props.get(key)
            if child_schema is not None:
                _validate_against_schema(child, child_schema, f"{path}.{key}", out)

    is_array = type_spec == "array" or (
        isinstance(type_spec, list) and "array" in type_spec
    )
    if is_array:
        if not isinstance(value, list):
            return
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            out.append(f"{path}: array shorter than minItems")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_against_schema(
                    item, item_schema, f"{path}[{index}]", out
                )


def validate_acceptance_contract_schema(document: Any) -> list[str]:
    """Strict-validate ``document``; return diagnostics (empty means valid).

    Never includes document values in diagnostics — only JSON paths and codes.
    """
    if not isinstance(document, dict):
        return ["$: expected type object"]
    diagnostics: list[str] = []
    _validate_against_schema(
        document, acceptance_contract_json_schema(), "$", diagnostics
    )
    return diagnostics


def schema_version_of(document: Any) -> str | None:
    """Return schema_version when present as a non-empty string; else None."""
    if not isinstance(document, dict):
        return None
    value = document.get("schema_version")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def contract_version_of(document: Any) -> str | None:
    """Return document ``version`` when present as a non-empty string; else None."""
    if not isinstance(document, dict):
        return None
    value = document.get("version")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
