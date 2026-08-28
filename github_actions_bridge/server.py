Warning: truncated output (original token count: 33386)
Total output lines: 3224

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import pathlib
import re
import tempfile
import time
import uuid
import zipfile
from typing import Any, Callable, Iterable, Literal, NoReturn
from urllib.parse import urlparse

import boto3
import httpx
from cryptography.fernet import Fernet
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from starlette.requests import Request
from starlette.responses import JSONResponse

from botocore.exceptions import ClientError
from pypdf import PdfReader

from storage_policy import (
    ACCEPTANCE_CONTRACT_PREFIX,
    MAX_ACCEPTANCE_CONTRACT_BYTES,
    archive_create_only_put_params,
    assert_archive_objects_absent,
    assert_canonical_legalai_bucket,
    build_acceptance_contract_archive,
    build_acceptance_contract_template,
    build_attorney_review_archive,
    build_review_packet_archive,
    inventory_prefix,
    map_archive_put_precondition_failure,
    resolve_acceptance_contract_retrieval_key,
    validate_acceptance_contract_object_key,
    validate_sha256_hex,
    verify_retrieved_acceptance_contract,
)
from case_intake import (
    MAX_BUNDLE_BYTES,
    MAX_MANIFEST_BYTES,
    decode_base64_upload,
    intake_keys,
    verify_object as verify_case_intake_object,
)
from verified_case_reader import canonical_source_prefix, extract_pdf_pages, read_verified_manifest, validate_page_request
from service_auth import (
    BRIDGE_SERVICE_TOKEN_ENV,
    CANONICAL_GATEWAY_DISPLAY_NAME,
    DEFAULT_PUBLIC_MCP_PATH,
    DEFAULT_SERVICE_MCP_PATH,
    build_service_auth_provider,
    compose_dual_mcp_http_app,
    is_service_access_token,
    normalize_bearer_token,
    plugin_refresh_mcp_url,
    stamp_canonical_protected_resource_identity,
)

# Exact immutable LegalAI commit SHA (lowercase hex only — no abbreviated / mixed case).
_FULL_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Structured Case-00 ref / dispatch failures (safe for Gateway envelopes).
ERROR_REF_INVALID = "ref_invalid"
ERROR_REF_NOT_IN_REPOSITORY = "ref_not_in_repository"
ERROR_REF_RESOLUTION_FAILED = "ref_resolution_failed"
ERROR_DISPATCH_FAILED = "dispatch_failed"
ERROR_UNSUPPORTED_BENCHMARK_QUESTION = "unsupported_benchmark_question"

# Fail-closed Case-00 submit identity: exact benchmark + Q[1-9]\d* only.
CASE00_BENCHMARK_ID = "Case-00-Triborough"
_CASE00_QUESTION_ID_RE = re.compile(r"^Q[1-9]\d*$")
_CASE00_QUESTION_TOKEN_RE = re.compile(r"^q[1-9]\d*$")

# Canonical Case-00 benchmark source document (single verified attorney packet).
# Retrieval is intentionally bounded to this key and verified before parsing.
CANONICAL_CASE00_ATTORNEY_PACKET_KEY = (
    "Benchmarks/Case-00-Triborough/derived/attorney-feedback-eval/"
    "attorney-reviews/review-20260802-2122f82dafe3/"
    "attorney_review_packet_02-original.md"
)
CANONICAL_CASE00_ATTORNEY_PACKET_SIZE = 57278
CANONICAL_CASE00_ATTORNEY_PACKET_SHA256 = (
    "ce7e3a25b22ec23822aec4dcd317b1df38ce6c85b59f684f45f3bdb811316d86"
)
MAX_CASE00_QUESTION_SECTION_CHARS = 20_000
_CASE00_PACKET_QUESTION_SECTION_RE = re.compile(
    r"^## (Q[1-9]\d*)\.\s*(.*?)(?=^## Q[1-9]\d*\.|\Z)",
    re.MULTILINE | re.DOTALL,
)

# Explicit deployment provenance only — never infer or fabricate a SHA.
DEPLOYED_COMMIT_SHA_ENV = "RAILWAY_GIT_COMMIT_SHA"
UNKNOWN_DEPLOYED_COMMIT_SHA = "unknown"

# Minimum production tool set. Subset check stays tolerant of harmless additions.
REQUIRED_PRODUCTION_TOOLS = frozenset(
    {
        "submit_run",
        "get_run",
        "cancel_run",
        "get_artifacts",
        "submit_case00_q1",
        "get_case00_q1_run",
        "cancel_case00_q1_run",
        "get_case00_q1_artifacts",
        "submit_case00",
        "get_case00_run",
        "cancel_case00_run",
        "get_case00_artifacts",
        "get_case_artifact",
        "list_case00_storage",
        "archive_case00_attorney_feedback",
        "archive_case00_review_packet",
        "archive_acceptance_contract",
        "verify_acceptance_contract",
        "list_acceptance_contracts",
        "get_acceptance_contract_template",
        "get_acceptance_contract",
        "get_case00_question",
        "verify_case_intake",
    }
)

# Namespaced HAL LegalAI Gateway catalog advertised on public /mcp so ChatGPT
# Refresh of the existing bridge-backed plugin recaches case/storage/mission/
# workflow tools without recreating the connector. Local aliases wrap existing
# bridge implementations (no schema/business-logic copy). Mission/workflow
# tools thin-forward to the canonical gateway MCP, falling back to Mission
# Control MCP (the gateway's own downstream) when the gateway URL is unset.
REQUIRED_CANONICAL_CATALOG_TOOLS = frozenset(
    {
        "case.submit",
        "case.status",
        "case.get_artifact",
        "storage.list_inventory",
        "storage.verify_archive",
        "mission.submit",
        "mission.status",
        "workflow.submit",
        "workflow.status",
        "workflow.cancel",
    }
)
CANONICAL_CATALOG_NAMESPACES = frozenset({"case", "storage", "mission", "workflow"})
HAL_LEGALAI_GATEWAY_URL_ENV = "HAL_LEGALAI_GATEWAY_URL"
HAL_LEGALAI_GATEWAY_MCP_PATH_ENV = "HAL_LEGALAI_GATEWAY_MCP_PATH"
MISSION_CONTROL_MCP_URL_ENV = "MISSION_CONTROL_MCP_URL"
DEFAULT_MISSION_CONTROL_MCP_URL = (
    "https://mission-control-mcp-production.up.railway.app"
)
_OUTBOUND_MERGED_HEADER_ALLOWLIST = frozenset(
    {"accept", "x-request-id", "x-correlation-id"}
)
_canonical_forward_test_hooks: dict[str, Any] = {}
_CANONICAL_ALIASES_REGISTERED = False
logger = logging.getLogger(__name__)


def reset_canonical_forward_hooks() -> None:
    """Test helper: clear injected downstream transport overrides."""
    _canonical_forward_test_hooks.clear()


def get_deployed_commit_sha() -> str:
    """Return the explicit deployment commit SHA, or a safe unknown fallback."""
    value = (os.environ.get(DEPLOYED_COMMIT_SHA_ENV) or "").strip()
    return value if value else UNKNOWN_DEPLOYED_COMMIT_SHA


def missing_required_production_tools(registered: Iterable[str]) -> list[str]:
    """Return sorted required tool names absent from the registered set."""
    return sorted(REQUIRED_PRODUCTION_TOOLS - set(registered))


async def list_registered_tool_names() -> list[str]:
    """Exact sorted tool names from the running FastMCP instance (supported API)."""
    tools = await mcp.get_tools()
    return sorted(tools)


def assert_required_production_tools(registered: Iterable[str]) -> None:
    """Fail closed when any required production tool is missing."""
    missing = missing_required_production_tools(registered)
    if missing:
        raise RuntimeError(
            "HAL GitHub Actions Bridge refused to start: required production "
            f"MCP tools are not registered: {', '.join(missing)}"
        )


def missing_required_canonical_catalog_tools(registered: Iterable[str]) -> list[str]:
    """Return sorted canonical catalog names absent from the registered set."""
    return sorted(REQUIRED_CANONICAL_CATALOG_TOOLS - set(registered))


def assert_required_canonical_catalog_tools(registered: Iterable[str]) -> None:
    """Fail closed when ChatGPT Refresh would miss required namespaced tools."""
    missing = missing_required_canonical_catalog_tools(registered)
    if missing:
        raise RuntimeError(
            "HAL GitHub Actions Bridge refused to start: canonical HAL "
            "LegalAI Gateway catalog tools are not registered: "
            f"{', '.join(missing)}"
        )


async def validate_required_production_tools() -> None:
    """Preflight: ensure required production tools are registered before serving."""
    names = await list_registered_tool_names()
    assert_required_production_tools(names)
    assert_required_canonical_catalog_tools(names)


def _filter_outbound_merged_headers(headers: Any) -> dict[str, str]:
    if not headers:
        return {}
    items = headers.items() if hasattr(headers, "items") else ()
    return {
        str(name): str(value)
        for name, value in items
        if str(name).lower() in _OUTBOUND_MERGED_HEADER_ALLOWLIST
    }


def _isolating_httpx_factory(
    inner: Callable[..., httpx.AsyncClient],
) -> Callable[..., httpx.AsyncClient]:
    def factory(**kwargs: Any) -> httpx.AsyncClient:
        if "headers" in kwargs:
            kwargs["headers"] = _filter_outbound_merged_headers(kwargs["headers"])
        return inner(**kwargs)

    return factory


def _canonical_forward_secrets() -> tuple[str, ...]:
    values = []
    token = normalize_bearer_token(os.environ.get(BRIDGE_SERVICE_TOKEN_ENV))
    if token:
        values.append(token)
    raw = os.environ.get(BRIDGE_SERVICE_TOKEN_ENV)
    if raw:
        values.append(raw)
    return tuple(values)


def _redact_forward_text(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    redacted = text or ""
    for secret in extra_secrets + _canonical_forward_secrets():
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "[REDACTED]")
    lowered = redacted.lower()
    for needle in ("bearer ", "authorization", "api_key", "jwt_signing"):
        if needle in lowered and "[redacted]" not in lowered:
            return "downstream call failed"
    return redacted[:500]


def _classify_forward_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        status = getattr(exc.response, "status_code", None)
        if status in {401, 403}:
            return "auth"
        return "http"
    if isinstance(exc, (httpx.ConnectError, httpx.Netw…28386 tokens truncated…actual_size,
        "etag": actual_etag,
        "content_type": content_type,
        "content": content,
    }


# Namespaced aliases of existing local tools (same fn/schema; ChatGPT Refresh
# sees case.* / storage.*). Mission/workflow tools thin-forward.
_LOCAL_CANONICAL_ALIASES: tuple[tuple[str, str], ...] = (
    ("case.submit", "submit_case00"),
    ("case.status", "get_case00_run"),
    ("case.cancel", "cancel_case00_run"),
    ("case.list_artifacts", "get_case00_artifacts"),
    ("case.submit_case00_q1", "submit_case00_q1"),
    ("case.get_case00_q1_run", "get_case00_q1_run"),
    ("case.cancel_case00_q1_run", "cancel_case00_q1_run"),
    ("case.get_case00_q1_artifacts", "get_case00_q1_artifacts"),
    ("case.get_artifact", "get_case_artifact"),
    ("case.get_artifacts", "get_artifacts"),
    ("storage.list_inventory", "list_case00_storage"),
    ("storage.archive_feedback", "archive_case00_attorney_feedback"),
    ("storage.archive_review_packet", "archive_case00_review_packet"),
    ("storage.archive_acceptance_contract", "archive_acceptance_contract"),
    ("storage.verify_acceptance_contract", "verify_acceptance_contract"),
    ("storage.list_acceptance_contracts", "list_acceptance_contracts"),
    ("storage.get_acceptance_contract_template", "get_acceptance_contract_template"),
    ("storage.get_acceptance_contract", "get_acceptance_contract"),
    ("storage.verify_archive", "list_case00_storage"),
)


def register_canonical_catalog_aliases() -> None:
    """Register namespaced aliases of local tools (idempotent)."""
    global _CANONICAL_ALIASES_REGISTERED
    if _CANONICAL_ALIASES_REGISTERED:
        return
    namespace = globals()
    for canonical, local_name in _LOCAL_CANONICAL_ALIASES:
        source = namespace[local_name]
        mcp.add_tool(source.model_copy(update={"name": canonical}))
    _CANONICAL_ALIASES_REGISTERED = True


@mcp.tool(
    name="mission.submit",
    description=(
        "Submit an exact Mission Control YAML document (canonical catalog; "
        "thin forward to HAL LegalAI Gateway / Mission Control submit_run)."
    ),
)
async def canonical_mission_submit(mission_yaml: str) -> dict[str, Any]:
    return await _call_canonical_forward(
        "mission.submit", "submit_run", {"mission_yaml": mission_yaml}
    )


@mcp.tool(
    name="mission.submit_structured",
    description="Submit a mission via structured fields (canonical catalog).",
)
async def canonical_mission_submit_structured(
    mission_id: str,
    title: str,
    instructions: str,
    deliverables: list[str],
    create_files: bool,
    modify_files: bool,
    persistence_mode: str | None = None,
    repository_name: str = "Mission-Control",
    repository_path: str = ".",
    base_branch: str = "main",
    run_commands: bool = True,
    platform_push_approved: bool | None = None,
    allow_automatic_platform_push: bool = False,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "mission_id": mission_id,
        "title": title,
        "instructions": instructions,
        "deliverables": deliverables,
        "create_files": create_files,
        "modify_files": modify_files,
        "repository_name": repository_name,
        "repository_path": repository_path,
        "base_branch": base_branch,
        "run_commands": run_commands,
        "allow_automatic_platform_push": allow_automatic_platform_push,
    }
    if persistence_mode is not None:
        args["persistence_mode"] = persistence_mode
    if platform_push_approved is not None:
        args["platform_push_approved"] = platform_push_approved
    if approval is not None:
        args["approval"] = approval
    return await _call_canonical_forward(
        "mission.submit_structured", "submit_structured_run", args
    )


@mcp.tool(
    name="mission.status",
    description="Retrieve the current state of a Mission Control run.",
)
async def canonical_mission_status(run_id: str) -> dict[str, Any]:
    return await _call_canonical_forward(
        "mission.status", "get_run", {"run_id": run_id}
    )


@mcp.tool(
    name="mission.list_notifications",
    description=(
        "List bounded, redacted Phase 2C durable notifications for a "
        "Mission Control run_id."
    ),
)
async def canonical_mission_list_notifications(
    run_id: str,
    limit: int = 64,
) -> dict[str, Any]:
    return await _call_canonical_forward(
        "mission.list_notifications",
        "list_run_notifications",
        {"run_id": run_id, "limit": limit},
    )


@mcp.tool(
    name="mission.wait",
    description=(
        "Wait for a Mission Control run to reach a terminal status or timeout."
    ),
)
async def canonical_mission_wait(
    run_id: str,
    timeout_seconds: float | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {"run_id": run_id}
    if timeout_seconds is not None:
        args["timeout_seconds"] = timeout_seconds
    if cursor is not None:
        args["cursor"] = cursor
    return await _call_canonical_forward("mission.wait", "wait_for_run", args)


@mcp.tool(
    name="mission.submit_and_wait",
    description="Submit exact mission YAML and wait for a terminal run state.",
)
async def canonical_mission_submit_and_wait(
    mission_yaml: str,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {"mission_yaml": mission_yaml}
    if timeout_seconds is not None:
        args["timeout_seconds"] = timeout_seconds
    return await _call_canonical_forward(
        "mission.submit_and_wait", "submit_and_wait", args
    )


@mcp.tool(
    name="mission.run_repository_command",
    description="Run an allowlisted repository command via Mission Control.",
)
async def canonical_mission_run_repository_command(
    repository: str,
    ref: str,
    argv: list[str],
    working_directory: str | None = None,
    timeout_seconds: float | None = None,
    allowed_env_names: list[str] | None = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "repository": repository,
        "ref": ref,
        "argv": argv,
    }
    if working_directory is not None:
        args["working_directory"] = working_directory
    if timeout_seconds is not None:
        args["timeout_seconds"] = timeout_seconds
    if allowed_env_names is not None:
        args["allowed_env_names"] = allowed_env_names
    return await _call_canonical_forward(
        "mission.run_repository_command", "run_repository_command", args
    )


@mcp.tool(
    name="workflow.submit",
    description=(
        "Submit bounded workflow YAML (canonical catalog; thin forward to "
        "HAL LegalAI Gateway / Mission Control submit_workflow)."
    ),
)
async def canonical_workflow_submit(
    workflow_yaml: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {"workflow_yaml": workflow_yaml}
    if idempotency_key is not None:
        args["idempotency_key"] = idempotency_key
    return await _call_canonical_forward(
        "workflow.submit", "submit_workflow", args
    )


@mcp.tool(
    name="workflow.status",
    description=(
        "Return sanitized durable workflow and child summaries for a "
        "canonical workflow_id."
    ),
)
async def canonical_workflow_status(workflow_id: str) -> dict[str, Any]:
    return await _call_canonical_forward(
        "workflow.status", "get_workflow", {"workflow_id": workflow_id}
    )


@mcp.tool(
    name="workflow.cancel",
    description=(
        "Cancel a durable workflow (canonical catalog; thin forward to "
        "HAL LegalAI Gateway / Mission Control cancel_workflow)."
    ),
)
async def canonical_workflow_cancel(workflow_id: str) -> dict[str, Any]:
    return await _call_canonical_forward(
        "workflow.cancel", "cancel_workflow", {"workflow_id": workflow_id}
    )


register_canonical_catalog_aliases()


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "service": "hal-github-actions-bridge",
            "catalog_identity": CANONICAL_GATEWAY_DISPLAY_NAME,
            "plugin_refresh_mcp_url": plugin_refresh_mcp_url(PUBLIC_URL),
            "deployed_commit_sha": get_deployed_commit_sha(),
            "registered_tools": await list_registered_tool_names(),
            "time": int(time.time()),
        }
    )


def create_http_app(
    *,
    oauth_auth: Any | None = None,
    service_token: str | None = None,
    public_mcp_path: str = DEFAULT_PUBLIC_MCP_PATH,
    service_mcp_path: str = DEFAULT_SERVICE_MCP_PATH,
    json_response: bool = False,
) -> Any:
    """ASGI app: public OAuth ``/mcp`` + service-only ``/mcp/service``.

    ``oauth_auth`` is for tests (inject a fixed verifier). Production uses the
    module GitHub OAuth provider. Service auth uses ``BRIDGE_SERVICE_TOKEN`` via
    a FastMCP 2.x TokenVerifier and fails closed when unset. Public OAuth
    metadata stamps RFC 9728 ``resource_name`` as HAL LegalAI Gateway without
    changing the ``{BRIDGE_PUBLIC_URL}/mcp`` resource URL ChatGPT Refresh uses.
    """
    oauth = stamp_canonical_protected_resource_identity(
        oauth_auth if oauth_auth is not None else oauth_auth_provider
    )
    token = (
        service_token
        if service_token is not None
        else os.environ.get(BRIDGE_SERVICE_TOKEN_ENV)
    )
    service_auth = build_service_auth_provider(token)
    return compose_dual_mcp_http_app(
        mcp,
        oauth_auth=oauth,
        service_auth=service_auth,
        public_mcp_path=public_mcp_path,
        service_mcp_path=service_mcp_path,
        json_response=json_response,
    )


def main() -> None:
    import uvicorn

    asyncio.run(validate_required_production_tools())
    app = create_http_app()
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
