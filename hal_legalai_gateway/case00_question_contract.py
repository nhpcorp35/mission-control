"""Authoritative MCP contract for ``storage.get_case00_question``.

Loads the versioned JSON Schema artifact, exposes registration metadata, and
validates public input/output fail-closed (no arbitrary source leakage).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

CONTRACT_SCHEMA_RELATIVE = "schemas/storage.get_case00_question.v1.json"
GATEWAY_TOOL = "storage.get_case00_question"
DOWNSTREAM_TOOL = "get_case00_question"
NAMESPACE = "storage"
DOWNSTREAM_SERVICE = "storage"
BENCHMARK_ID = "Case-00-Triborough"
QUESTION_ID_RE = re.compile(r"^Q[1-9]\d*$")
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

_CONTRACT_VIOLATION = "contract_violation"


class Case00QuestionContractError(ValueError):
    """Public contract validation failure (safe to surface as bounded metadata)."""


@dataclass(frozen=True)
class Case00QuestionContract:
    """Resolved contract metadata and schema fragments."""

    schema_id: str
    version: str
    gateway_tool: str
    downstream_tool: str
    namespace: str
    downstream_service: str
    description: str
    benchmark_id: str
    input_schema: dict[str, Any]
    success_output_schema: dict[str, Any]
    not_found_output_schema: dict[str, Any]
    public_output_schema: dict[str, Any]
    document: dict[str, Any]


_contract_cache: Case00QuestionContract | None = None


def contract_schema_path() -> Path:
    return Path(__file__).resolve().parent / CONTRACT_SCHEMA_RELATIVE


def load_contract_document() -> dict[str, Any]:
    path = contract_schema_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"case00 question contract schema missing or unreadable: {path}"
        ) from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"case00 question contract schema is not valid JSON: {path}"
        ) from exc
    if not isinstance(document, dict):
        raise RuntimeError("case00 question contract schema root must be an object")
    return document


def _require_str(document: Mapping[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"case00 question contract schema missing {key}")
    return value.strip()


def _require_def(document: Mapping[str, Any], name: str) -> dict[str, Any]:
    defs = document.get("$defs")
    if not isinstance(defs, dict):
        raise RuntimeError("case00 question contract schema missing $defs")
    schema = defs.get(name)
    if not isinstance(schema, dict):
        raise RuntimeError(
            f"case00 question contract schema missing $defs.{name}"
        )
    return schema


def load_case00_question_contract() -> Case00QuestionContract:
    global _contract_cache
    if _contract_cache is not None:
        return _contract_cache

    document = load_contract_document()
    schema_id = _require_str(document, "$id")
    version = _require_str(document, "x-hal-contract-version")
    gateway_tool = _require_str(document, "x-hal-mcp-tool")
    downstream_tool = _require_str(document, "x-hal-downstream-tool")
    namespace = _require_str(document, "x-hal-namespace")
    downstream_service = _require_str(document, "x-hal-downstream-service")
    benchmark_id = _require_str(document, "x-hal-benchmark-id")
    description = _require_str(document, "description")

    if gateway_tool != GATEWAY_TOOL:
        raise RuntimeError(
            f"contract x-hal-mcp-tool must be {GATEWAY_TOOL!r}, got {gateway_tool!r}"
        )
    if downstream_tool != DOWNSTREAM_TOOL:
        raise RuntimeError(
            f"contract x-hal-downstream-tool must be {DOWNSTREAM_TOOL!r}, "
            f"got {downstream_tool!r}"
        )
    if benchmark_id != BENCHMARK_ID:
        raise RuntimeError(
            f"contract x-hal-benchmark-id must be {BENCHMARK_ID!r}, "
            f"got {benchmark_id!r}"
        )

    _contract_cache = Case00QuestionContract(
        schema_id=schema_id,
        version=version,
        gateway_tool=gateway_tool,
        downstream_tool=downstream_tool,
        namespace=namespace,
        downstream_service=downstream_service,
        description=description,
        benchmark_id=benchmark_id,
        input_schema=_require_def(document, "input"),
        success_output_schema=_require_def(document, "success_output"),
        not_found_output_schema=_require_def(document, "not_found_output"),
        public_output_schema=_require_def(document, "public_output"),
        document=document,
    )
    return _contract_cache


def assert_registry_binding_matches_contract(binding: Mapping[str, Any]) -> None:
    """Fail closed when registry metadata diverges from the canonical schema."""
    contract = load_case00_question_contract()
    tool = binding.get("tool")
    if tool != contract.gateway_tool:
        raise RuntimeError(
            f"registry tool_bindings tool must be {contract.gateway_tool!r}, "
            f"got {tool!r}"
        )
    schema_ref = binding.get("contract_schema")
    if schema_ref != CONTRACT_SCHEMA_RELATIVE:
        raise RuntimeError(
            "registry tool_bindings.contract_schema must be "
            f"{CONTRACT_SCHEMA_RELATIVE!r}, got {schema_ref!r}"
        )
    if binding.get("namespace") != contract.namespace:
        raise RuntimeError("registry namespace does not match contract schema")
    if binding.get("downstream_service") != contract.downstream_service:
        raise RuntimeError(
            "registry downstream_service does not match contract schema"
        )
    if binding.get("downstream_tool") != contract.downstream_tool:
        raise RuntimeError("registry downstream_tool does not match contract schema")
    if "description" in binding:
        raise RuntimeError(
            "registry must not duplicate storage.get_case00_question description; "
            "use contract_schema only"
        )


def validate_contract_schema_document(document: Mapping[str, Any]) -> None:
    """Validate the canonical schema artifact itself (CI gate)."""
    load_case00_question_contract()
    if document.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise Case00QuestionContractError("schema $schema must be draft 2020-12")
    for field in (
        "$id",
        "x-hal-contract-version",
        "x-hal-mcp-tool",
        "x-hal-downstream-tool",
        "description",
    ):
        if not isinstance(document.get(field), str) or not str(document[field]).strip():
            raise Case00QuestionContractError(f"schema missing {field}")


def _validate_question_id(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise Case00QuestionContractError(f"{path} must be a string")
    if not QUESTION_ID_RE.fullmatch(value):
        raise Case00QuestionContractError(
            f"{path} must match ^Q[1-9]\\d*$"
        )
    return value


def _validate_sha256(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise Case00QuestionContractError(f"{path} must be a string")
    if not SHA256_HEX_RE.fullmatch(value):
        raise Case00QuestionContractError(
            f"{path} must be a lowercase 64-character hex SHA-256 digest"
        )
    return value


def _validate_keys_exact(instance: Mapping[str, Any], allowed: frozenset[str], *, label: str) -> None:
    extra = set(instance) - allowed
    if extra:
        raise Case00QuestionContractError(
            f"{label} has undocumented fields: {', '.join(sorted(extra))}"
        )
    missing = allowed - set(instance)
    if missing:
        raise Case00QuestionContractError(
            f"{label} missing required fields: {', '.join(sorted(missing))}"
        )


def validate_public_input(arguments: Mapping[str, Any]) -> dict[str, str]:
    """Validate gateway public input; reject before downstream access."""
    if not isinstance(arguments, Mapping):
        raise Case00QuestionContractError("input must be an object")
    _validate_keys_exact(arguments, frozenset({"question_id"}), label="input")
    question_id = _validate_question_id(arguments.get("question_id"), path="question_id")
    return {"question_id": question_id}


def validate_public_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate downstream/public output against the bounded contract."""
    if not isinstance(payload, Mapping):
        raise Case00QuestionContractError("output must be an object")

    ok = payload.get("ok")
    if ok is True:
        allowed = frozenset(
            {
                "ok",
                "benchmark_id",
                "question_id",
                "question_text",
                "source_object_key",
                "sha256",
            }
        )
        _validate_keys_exact(payload, allowed, label="success output")
        if payload.get("benchmark_id") != BENCHMARK_ID:
            raise Case00QuestionContractError(
                f"benchmark_id must be {BENCHMARK_ID!r}"
            )
        question_id = _validate_question_id(
            payload.get("question_id"), path="question_id"
        )
        question_text = payload.get("question_text")
        if not isinstance(question_text, str) or not question_text.strip():
            raise Case00QuestionContractError("question_text must be a non-empty string")
        source_object_key = payload.get("source_object_key")
        if not isinstance(source_object_key, str) or not source_object_key.strip():
            raise Case00QuestionContractError("source_object_key must be a non-empty string")
        sha256 = _validate_sha256(payload.get("sha256"), path="sha256")
        return {
            "ok": True,
            "benchmark_id": BENCHMARK_ID,
            "question_id": question_id,
            "question_text": question_text,
            "source_object_key": source_object_key,
            "sha256": sha256,
        }

    if ok is False and payload.get("error") == "not_found":
        allowed = frozenset(
            {
                "ok",
                "benchmark_id",
                "question_id",
                "error",
                "source_object_key",
                "sha256",
            }
        )
        _validate_keys_exact(payload, allowed, label="not_found output")
        if payload.get("benchmark_id") != BENCHMARK_ID:
            raise Case00QuestionContractError(
                f"benchmark_id must be {BENCHMARK_ID!r}"
            )
        question_id = _validate_question_id(
            payload.get("question_id"), path="question_id"
        )
        source_object_key = payload.get("source_object_key")
        if not isinstance(source_object_key, str) or not source_object_key.strip():
            raise Case00QuestionContractError("source_object_key must be a non-empty string")
        sha256 = _validate_sha256(payload.get("sha256"), path="sha256")
        return {
            "ok": False,
            "benchmark_id": BENCHMARK_ID,
            "question_id": question_id,
            "error": "not_found",
            "source_object_key": source_object_key,
            "sha256": sha256,
        }

    raise Case00QuestionContractError("output must be success or not_found shape")


def contract_violation_response(
    *,
    question_id: str | None = None,
    stage: str = _CONTRACT_VIOLATION,
) -> dict[str, Any]:
    """Bounded public failure without leaking arbitrary downstream content."""
    payload: dict[str, Any] = {
        "ok": False,
        "error": stage,
    }
    if question_id is not None:
        try:
            payload["question_id"] = _validate_question_id(
                question_id, path="question_id"
            )
        except Case00QuestionContractError:
            payload["question_id"] = str(question_id)[:32]
    return payload


def public_metadata_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Machine-readable evidence fields without full question body."""
    summary: dict[str, Any] = {
        "ok": payload.get("ok"),
        "benchmark_id": payload.get("benchmark_id"),
        "question_id": payload.get("question_id"),
        "source_object_key": payload.get("source_object_key"),
        "sha256": payload.get("sha256"),
        "schema_id": load_case00_question_contract().schema_id,
        "contract_version": load_case00_question_contract().version,
    }
    if "error" in payload:
        summary["error"] = payload.get("error")
    if payload.get("ok") is True and isinstance(payload.get("question_text"), str):
        summary["question_text_chars"] = len(payload["question_text"])
        summary["question_text_prefix"] = payload["question_text"][:40]
    return summary


def resolve_registry_case00_binding(registry_document: Mapping[str, Any]) -> None:
    """Validate registry registration against the canonical contract artifact."""
    bindings = registry_document.get("tool_bindings")
    if not isinstance(bindings, list):
        raise RuntimeError("registry.tool_bindings must be an array")
    matches = [
        item
        for item in bindings
        if isinstance(item, dict) and item.get("tool") == GATEWAY_TOOL
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"registry must contain exactly one binding for {GATEWAY_TOOL}"
        )
    assert_registry_binding_matches_contract(matches[0])
