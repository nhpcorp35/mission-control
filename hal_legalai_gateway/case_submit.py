"""Authoritative MCP contract and gateway-native direct-GitHub case.submit."""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

CONTRACT_SCHEMA_RELATIVE = "schemas/case.submit.v1.json"
GATEWAY_TOOL = "case.submit"
DOWNSTREAM_TOOL = "case.submit"
NAMESPACE = "case"
DOWNSTREAM_SERVICE = "gateway"
BENCHMARK_ID = "Case-00-Triborough"
ALLOWED_QUESTION_IDS = frozenset({"Q1", "Q2", "Q3"})
FIXED_REPOSITORY = "nhpcorp35/legal-ai"
GITHUB_API = "https://api.github.com"
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._~:-]{1,128}$")
_IDEMPOTENCY_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

CASE00_WORKFLOW = os.environ.get("GITHUB_CASE00_WORKFLOW", "hal-case00-q1.yml")
CASE00_WORKFLOW_BRANCH = os.environ.get("GITHUB_CASE00_WORKFLOW_BRANCH", "main")

LEGACY_PUBLIC_CASE_SUBMISSION_ROUTES = frozenset(
    {
        "case.submit_case00_q1",
    }
)

SUCCESS_KEYS = frozenset(
    {
        "ok",
        "run_id",
        "workflow_run_id",
        "commit_sha",
        "question_id",
        "idempotency_key",
    }
)
FAILURE_KEYS = frozenset({"ok", "error"})

ERROR_INVALID_INPUT = "invalid_input"
ERROR_UNAUTHORIZED = "unauthorized"
ERROR_UNSUPPORTED_BENCHMARK_QUESTION = "unsupported_benchmark_question"
ERROR_COMMIT_NOT_FOUND = "commit_not_found"
ERROR_COMMIT_VERIFICATION_FAILED = "commit_verification_failed"
ERROR_DISPATCH_FAILED = "dispatch_failed"
ERROR_WORKFLOW_RUN_NOT_FOUND = "workflow_run_not_found"
ERROR_CONTRACT_VIOLATION = "contract_violation"

_CONTRACT_VIOLATION = ERROR_CONTRACT_VIOLATION


class CaseSubmitContractError(ValueError):
    """Public contract validation failure (safe to surface as bounded metadata)."""


@dataclass(frozen=True)
class CaseSubmitContract:
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
    failure_output_schema: dict[str, Any]
    public_output_schema: dict[str, Any]
    document: dict[str, Any]


_contract_cache: CaseSubmitContract | None = None


def contract_schema_path() -> Path:
    return Path(__file__).resolve().parent / CONTRACT_SCHEMA_RELATIVE


def load_contract_document() -> dict[str, Any]:
    path = contract_schema_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"case.submit contract schema missing or unreadable: {path}"
        ) from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"case.submit contract schema is not valid JSON: {path}"
        ) from exc
    if not isinstance(document, dict):
        raise RuntimeError("case.submit contract schema root must be an object")
    return document


def _require_str(document: Mapping[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"case.submit contract schema missing {key}")
    return value.strip()


def _require_def(document: Mapping[str, Any], name: str) -> dict[str, Any]:
    defs = document.get("$defs")
    if not isinstance(defs, dict):
        raise RuntimeError("case.submit contract schema missing $defs")
    schema = defs.get(name)
    if not isinstance(schema, dict):
        raise RuntimeError(f"case.submit contract schema missing $defs.{name}")
    return schema


def load_case_submit_contract() -> CaseSubmitContract:
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

    _contract_cache = CaseSubmitContract(
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
        failure_output_schema=_require_def(document, "failure_output"),
        public_output_schema=_require_def(document, "public_output"),
        document=document,
    )
    return _contract_cache


def assert_registry_binding_matches_contract(binding: Mapping[str, Any]) -> None:
    """Fail closed when registry metadata diverges from the canonical schema."""
    contract = load_case_submit_contract()
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
    notes = str(binding.get("notes") or "")
    if "gateway-native" not in notes.lower():
        raise RuntimeError(
            "registry case.submit binding must note gateway-native direct GitHub dispatch"
        )
    if "description" in binding:
        raise RuntimeError(
            "registry must not duplicate case.submit description; use contract_schema only"
        )


def assert_legacy_case_submission_routes_absent(registry_document: Mapping[str, Any]) -> None:
    """Ensure legacy public Case-00 submission routes are not registered."""
    namespaces = registry_document.get("namespaces")
    if not isinstance(namespaces, dict):
        raise RuntimeError("registry.namespaces must be an object")
    case_ns = namespaces.get("case")
    if not isinstance(case_ns, dict):
        raise RuntimeError("registry.namespaces.case must be an object")
    tools = case_ns.get("tools")
    if not isinstance(tools, list):
        raise RuntimeError("registry.namespaces.case.tools must be an array")
    registered = {item for item in tools if isinstance(item, str)}
    overlap = sorted(registered & LEGACY_PUBLIC_CASE_SUBMISSION_ROUTES)
    if overlap:
        raise RuntimeError(
            "legacy public Case-00 submission routes must be absent: "
            + ", ".join(overlap)
        )


def resolve_registry_case_submit_binding(registry_document: Mapping[str, Any]) -> None:
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
    assert_legacy_case_submission_routes_absent(registry_document)


def validate_contract_schema_document(document: Mapping[str, Any]) -> None:
    """Validate the canonical schema artifact itself (CI gate)."""
    load_case_submit_contract()
    if document.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise CaseSubmitContractError("schema $schema must be draft 2020-12")
    for field in (
        "$id",
        "x-hal-contract-version",
        "x-hal-mcp-tool",
        "x-hal-downstream-tool",
        "description",
    ):
        if not isinstance(document.get(field), str) or not str(document[field]).strip():
            raise CaseSubmitContractError(f"schema missing {field}")
    success_props = document.get("$defs", {}).get("success_output", {}).get(
        "properties", {}
    )
    if set(success_props) != set(SUCCESS_KEYS):
        raise CaseSubmitContractError(
            "success_output properties must be exactly "
            + ", ".join(sorted(SUCCESS_KEYS))
        )


def _validate_commit_sha(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise CaseSubmitContractError(f"{path} must be a string")
    if not COMMIT_SHA_RE.fullmatch(value):
        raise CaseSubmitContractError(
            f"{path} must be a lowercase 40-character commit SHA"
        )
    return value


def _validate_question_id(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise CaseSubmitContractError(f"{path} must be a string")
    question_id = value.strip()
    if question_id not in ALLOWED_QUESTION_IDS:
        raise CaseSubmitContractError(
            f"{path} must be one of {', '.join(sorted(ALLOWED_QUESTION_IDS))}"
        )
    return question_id


def _validate_idempotency_key(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise CaseSubmitContractError(f"{path} must be a string")
    key = value.strip()
    if not IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise CaseSubmitContractError(f"{path} is invalid")
    return key


def _validate_keys_exact(
    instance: Mapping[str, Any], allowed: frozenset[str], *, label: str
) -> None:
    extra = set(instance) - allowed
    if extra:
        raise CaseSubmitContractError(
            f"{label} has undocumented fields: {', '.join(sorted(extra))}"
        )
    missing = allowed - set(instance)
    if missing:
        raise CaseSubmitContractError(
            f"{label} missing required fields: {', '.join(sorted(missing))}"
        )


def validate_public_input(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Validate gateway public input; reject before GitHub access."""
    if not isinstance(arguments, Mapping):
        raise CaseSubmitContractError("input must be an object")
    allowed = frozenset(
        {
            "commit_sha",
            "benchmark_id",
            "question_id",
            "idempotency_key",
            "authorization_confirmed",
        }
    )
    _validate_keys_exact(arguments, allowed, label="input")
    if arguments.get("authorization_confirmed") is not True:
        raise CaseSubmitContractError(
            "authorization_confirmed must be true before private evidence is sent"
        )
    benchmark_id = arguments.get("benchmark_id")
    if benchmark_id != BENCHMARK_ID:
        raise CaseSubmitContractError(
            f"benchmark_id must be exactly {BENCHMARK_ID!r}"
        )
    commit_sha = _validate_commit_sha(arguments.get("commit_sha"), path="commit_sha")
    question_id = _validate_question_id(arguments.get("question_id"), path="question_id")
    idempotency_key = _validate_idempotency_key(
        arguments.get("idempotency_key"), path="idempotency_key"
    )
    return {
        "commit_sha": commit_sha,
        "benchmark_id": BENCHMARK_ID,
        "question_id": question_id,
        "idempotency_key": idempotency_key,
        "authorization_confirmed": True,
    }


def validate_public_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate public output against the bounded contract."""
    if not isinstance(payload, Mapping):
        raise CaseSubmitContractError("output must be an object")

    ok = payload.get("ok")
    if ok is True:
        _validate_keys_exact(payload, SUCCESS_KEYS, label="success output")
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            raise CaseSubmitContractError("run_id must be a non-empty string")
        workflow_run_id = payload.get("workflow_run_id")
        if not isinstance(workflow_run_id, int) or workflow_run_id < 1:
            raise CaseSubmitContractError(
                "workflow_run_id must be a positive integer"
            )
        commit_sha = _validate_commit_sha(payload.get("commit_sha"), path="commit_sha")
        question_id = _validate_question_id(payload.get("question_id"), path="question_id")
        idempotency_key = _validate_idempotency_key(
            payload.get("idempotency_key"), path="idempotency_key"
        )
        return {
            "ok": True,
            "run_id": run_id,
            "workflow_run_id": workflow_run_id,
            "commit_sha": commit_sha,
            "question_id": question_id,
            "idempotency_key": idempotency_key,
        }

    if ok is False:
        allowed = frozenset({"ok", "error", "question_id", "idempotency_key"})
        keys = set(payload)
        if not keys <= allowed or "error" not in keys:
            raise CaseSubmitContractError("failure output shape is invalid")
        error = payload.get("error")
        if not isinstance(error, str) or not error.strip():
            raise CaseSubmitContractError("error must be a non-empty string")
        result: dict[str, Any] = {"ok": False, "error": error.strip()}
        if "question_id" in payload:
            result["question_id"] = _validate_question_id(
                payload.get("question_id"), path="question_id"
            )
        if "idempotency_key" in payload:
            raw_key = payload.get("idempotency_key")
            if isinstance(raw_key, str) and raw_key.strip():
                result["idempotency_key"] = raw_key.strip()[:128]
        return result

    raise CaseSubmitContractError("output must be success or failure shape")


def contract_violation_response(
    *,
    question_id: str | None = None,
    idempotency_key: str | None = None,
    stage: str = _CONTRACT_VIOLATION,
) -> dict[str, Any]:
    """Bounded public failure without leaking arbitrary GitHub content."""
    payload: dict[str, Any] = {"ok": False, "error": stage}
    if question_id is not None:
        try:
            payload["question_id"] = _validate_question_id(
                question_id, path="question_id"
            )
        except CaseSubmitContractError:
            payload["question_id"] = str(question_id)[:8]
    if idempotency_key is not None:
        if isinstance(idempotency_key, str) and idempotency_key.strip():
            payload["idempotency_key"] = idempotency_key.strip()[:128]
    return payload


def failure_response(
    *,
    error: str,
    question_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Bounded fail-closed error without arbitrary GitHub content."""
    payload: dict[str, Any] = {"ok": False, "error": error}
    if question_id is not None:
        try:
            payload["question_id"] = _validate_question_id(
                question_id, path="question_id"
            )
        except CaseSubmitContractError:
            payload["question_id"] = str(question_id)[:8]
    if idempotency_key is not None and isinstance(idempotency_key, str):
        payload["idempotency_key"] = idempotency_key.strip()[:128]
    extra = set(payload) - (FAILURE_KEYS | {"question_id", "idempotency_key"})
    if extra:
        raise CaseSubmitContractError("failure output has undocumented fields")
    return payload


def run_id_for_idempotency_key(idempotency_key: str) -> str:
    """Derive a stable correlation UUID from the public idempotency key."""
    return str(uuid.uuid5(_IDEMPOTENCY_NAMESPACE, f"hal-case-submit-v1:{idempotency_key}"))


def case00_question_token(question_id: str) -> str:
    return question_id.strip().lower()


def case00_run_marker(question_id: str, run_id: str) -> str:
    return f"hal-case00-{case00_question_token(question_id)}-{run_id}"


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _github_json(
    method: str,
    path: str,
    *,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
    **kwargs: Any,
) -> tuple[httpx.Response | None, dict[str, Any] | None, str | None]:
    async def _request(client: httpx.AsyncClient) -> tuple[
        httpx.Response | None, dict[str, Any] | None, str | None
    ]:
        try:
            response = await client.request(
                method,
                f"{GITHUB_API}{path}",
                headers=_github_headers(),
                **kwargs,
            )
        except httpx.HTTPError as exc:
            return None, None, exc.__class__.__name__
        try:
            parsed = response.json()
            body = parsed if isinstance(parsed, dict) else None
        except ValueError:
            body = None
        return response, body, None

    if client_factory is not None:
        async with client_factory() as client:
            return await _request(client)
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        return await _request(client)


async def verify_immutable_commit_sha(
    commit_sha: str,
    *,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> tuple[bool, str | None]:
    """Return ``(verified, transport_error)`` for an exact SHA in the fixed repo."""
    path = f"/repos/{FIXED_REPOSITORY}/commits/{commit_sha}"
    response, body, transport_error = await _github_json(
        "GET", path, client_factory=client_factory
    )
    if transport_error is not None:
        return False, transport_error
    if response is None:
        return False, "no_response"
    if response.status_code in (404, 422):
        return False, None
    if response.status_code >= 400:
        return False, f"http_{response.status_code}"
    if not isinstance(body, dict):
        return False, "invalid_body"
    sha = body.get("sha")
    if not isinstance(sha, str) or not COMMIT_SHA_RE.fullmatch(sha.lower()):
        return False, "invalid_body"
    return sha.lower() == commit_sha, None


async def find_workflow_run_id(
    *,
    question_id: str,
    run_id: str,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> int | None:
    """Locate a workflow_dispatch run by Case-00 display-title marker."""
    path = f"/repos/{FIXED_REPOSITORY}/actions/workflows/{CASE00_WORKFLOW}/runs"
    response, body, transport_error = await _github_json(
        "GET",
        path,
        client_factory=client_factory,
        params={"event": "workflow_dispatch", "per_page": 50},
    )
    if transport_error is not None or response is None or response.status_code >= 400:
        return None
    if not isinstance(body, dict):
        return None
    marker = case00_run_marker(question_id, run_id).lower()
    for run in body.get("workflow_runs", []):
        if not isinstance(run, dict):
            continue
        title = str(run.get("display_title") or "").lower()
        if marker in title:
            run_id_value = run.get("id")
            if isinstance(run_id_value, int) and run_id_value >= 1:
                return run_id_value
    return None


async def dispatch_case00_workflow(
    *,
    run_id: str,
    commit_sha: str,
    question_id: str,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> tuple[bool, str | None]:
    """Dispatch workflow_dispatch; return ``(accepted, transport_error)``."""
    path = (
        f"/repos/{FIXED_REPOSITORY}/actions/workflows/{CASE00_WORKFLOW}/dispatches"
    )
    response, _body, transport_error = await _github_json(
        "POST",
        path,
        client_factory=client_factory,
        json={
            "ref": CASE00_WORKFLOW_BRANCH,
            "inputs": {
                "mission_id": run_id,
                "legalai_ref": commit_sha,
                "authorization_confirmed": "true",
                "benchmark_id": BENCHMARK_ID,
                "question_id": question_id,
            },
        },
    )
    if transport_error is not None:
        return False, transport_error
    if response is None:
        return False, "no_response"
    if response.status_code not in {201, 204}:
        return False, f"http_{response.status_code}"
    return True, None


async def submit_case00_direct(
    arguments: Mapping[str, Any],
    *,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
    poll_attempts: int = 5,
    poll_delay_seconds: float = 0.2,
) -> dict[str, Any]:
    """Gateway-native direct GitHub case.submit (no Mission Control or bridge forward)."""
    try:
        validated = validate_public_input(arguments)
    except CaseSubmitContractError:
        return failure_response(
            error=ERROR_INVALID_INPUT,
            question_id=str(arguments.get("question_id", ""))[:8] or None,
            idempotency_key=str(arguments.get("idempotency_key", ""))[:128] or None,
        )

    commit_sha = validated["commit_sha"]
    question_id = validated["question_id"]
    idempotency_key = validated["idempotency_key"]
    run_id = run_id_for_idempotency_key(idempotency_key)

    existing = await find_workflow_run_id(
        question_id=question_id,
        run_id=run_id,
        client_factory=client_factory,
    )
    if existing is not None:
        payload = {
            "ok": True,
            "run_id": run_id,
            "workflow_run_id": existing,
            "commit_sha": commit_sha,
            "question_id": question_id,
            "idempotency_key": idempotency_key,
        }
        return validate_public_output(payload)

    verified, verify_error = await verify_immutable_commit_sha(
        commit_sha, client_factory=client_factory
    )
    if verify_error is not None:
        return failure_response(
            error=ERROR_COMMIT_VERIFICATION_FAILED,
            question_id=question_id,
            idempotency_key=idempotency_key,
        )
    if not verified:
        return failure_response(
            error=ERROR_COMMIT_NOT_FOUND,
            question_id=question_id,
            idempotency_key=idempotency_key,
        )

    accepted, dispatch_error = await dispatch_case00_workflow(
        run_id=run_id,
        commit_sha=commit_sha,
        question_id=question_id,
        client_factory=client_factory,
    )
    if not accepted:
        return failure_response(
            error=ERROR_DISPATCH_FAILED,
            question_id=question_id,
            idempotency_key=idempotency_key,
        )

    workflow_run_id: int | None = None
    for _ in range(max(poll_attempts, 1)):
        workflow_run_id = await find_workflow_run_id(
            question_id=question_id,
            run_id=run_id,
            client_factory=client_factory,
        )
        if workflow_run_id is not None:
            break
        await asyncio.sleep(poll_delay_seconds)

    if workflow_run_id is None:
        return failure_response(
            error=ERROR_WORKFLOW_RUN_NOT_FOUND,
            question_id=question_id,
            idempotency_key=idempotency_key,
        )

    payload = {
        "ok": True,
        "run_id": run_id,
        "workflow_run_id": workflow_run_id,
        "commit_sha": commit_sha,
        "question_id": question_id,
        "idempotency_key": idempotency_key,
    }
    return validate_public_output(payload)
