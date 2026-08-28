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
from verified_case_search import search_index_jsonl
from verified_case_index import build_page_records

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
from verified_case_reader import canonical_source_prefix, extract_pdf_pages_from_object, read_verified_manifest, validate_page_request
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
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, httpx.TransportError)):
        return "connect"
    name = exc.__class__.__name__
    text = str(exc).lower()
    if "401" in text or "403" in text or "unauthorized" in text:
        return "auth"
    if "ToolError" in name or "tool" in text:
        return "tool"
    if "McpError" in name or "jsonrpc" in text:
        return "protocol"
    return "internal"


def resolved_canonical_forward_target() -> dict[str, Any]:
    """Choose the canonical catalog forward target. Never the local public /mcp.

    Prefer HAL_LEGALAI_GATEWAY_URL + /mcp (namespaced tools, service token).
    If unset or that origin is this bridge, fall back to Mission Control MCP
    with downstream tool names (same backend the gateway already uses).
    """
    hooks = _canonical_forward_test_hooks
    if hooks.get("target") is not None:
        return dict(hooks["target"])
    public_origin = urlparse(PUBLIC_URL).netloc.lower()
    gateway_url = (os.environ.get(HAL_LEGALAI_GATEWAY_URL_ENV) or "").strip().rstrip("/")
    if gateway_url and urlparse(gateway_url).netloc.lower() != public_origin:
        mcp_path = (
            os.environ.get(HAL_LEGALAI_GATEWAY_MCP_PATH_ENV) or DEFAULT_PUBLIC_MCP_PATH
        ).strip() or DEFAULT_PUBLIC_MCP_PATH
        if not mcp_path.startswith("/"):
            mcp_path = f"/{mcp_path}"
        return {
            "kind": "gateway",
            "base_url": gateway_url,
            "mcp_path": mcp_path,
            "use_canonical_names": True,
            "require_authorization": True,
        }
    mc_url = (
        os.environ.get(MISSION_CONTROL_MCP_URL_ENV) or DEFAULT_MISSION_CONTROL_MCP_URL
    ).strip().rstrip("/")
    if not mc_url:
        return {
            "kind": "unconfigured",
            "base_url": None,
            "mcp_path": DEFAULT_PUBLIC_MCP_PATH,
            "use_canonical_names": False,
            "require_authorization": False,
        }
    return {
        "kind": "mission_control",
        "base_url": mc_url,
        "mcp_path": DEFAULT_PUBLIC_MCP_PATH,
        "use_canonical_names": False,
        "require_authorization": False,
    }


async def forward_canonical_catalog_tool(
    gateway_tool: str,
    downstream_tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Thin MCP forwarder for namespaced catalog tools. Fail closed.

    Inbound GitHub OAuth is never forwarded. Gateway calls use BRIDGE_SERVICE_TOKEN
    via StreamableHttpTransport auth=. Downstream failures become ok=false.
    """
    started = time.perf_counter()
    extra_secrets = _canonical_forward_secrets()
    for key in ("mission_yaml", "workflow_yaml", "idempotency_key"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            extra_secrets = extra_secrets + (value,)

    def _finish(
        *,
        ok: bool,
        failure_stage: str | None,
        result: Any = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": ok,
            "gateway_tool": gateway_tool,
            "downstream_tool": downstream_tool,
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "failure_stage": failure_stage,
        }
        if ok:
            payload["result"] = result
        else:
            err = error or {
                "message": "downstream call failed",
                "stage": failure_stage,
            }
            message = err.get("message")
            if isinstance(message, str):
                err = dict(err)
                err["message"] = _redact_forward_text(message, extra_secrets)
            payload["error"] = err
        return payload

    target = resolved_canonical_forward_target()
    base_url = target.get("base_url")
    if not base_url:
        return _finish(
            ok=False,
            failure_stage="unconfigured",
            error={
                "message": (
                    "canonical catalog downstream is not configured: set "
                    f"{HAL_LEGALAI_GATEWAY_URL_ENV} or {MISSION_CONTROL_MCP_URL_ENV}"
                ),
                "stage": "unconfigured",
            },
        )
    tool_name = (
        gateway_tool if target.get("use_canonical_names") else downstream_tool
    )
    authorization = None
    if target.get("require_authorization"):
        authorization = os.environ.get(BRIDGE_SERVICE_TOKEN_ENV)
        if not normalize_bearer_token(authorization):
            return _finish(
                ok=False,
                failure_stage="auth",
                error={
                    "message": (
                        "downstream authorization missing: configure "
                        f"{BRIDGE_SERVICE_TOKEN_ENV} to call the canonical gateway"
                    ),
                    "stage": "auth",
                },
            )

    hooks = _canonical_forward_test_hooks
    client_factory = hooks.get("client_factory")
    httpx_client_factory = hooks.get("httpx_client_factory")
    mcp_path = str(target.get("mcp_path") or DEFAULT_PUBLIC_MCP_PATH)
    url = f"{str(base_url).rstrip('/')}{mcp_path}"
    headers = {"Accept": "application/json, text/event-stream"}
    service_token = normalize_bearer_token(authorization)
    try:
        if client_factory is not None:
            client = client_factory()
        else:
            inner = httpx_client_factory or (
                lambda **kwargs: httpx.AsyncClient(**kwargs)
            )
            transport = StreamableHttpTransport(
                url,
                headers=headers,
                auth=service_token,
                httpx_client_factory=_isolating_httpx_factory(inner),
            )
            client = Client(transport, timeout=30.0)
        async with client:
            raw = await client.call_tool(
                tool_name,
                arguments,
                raise_on_error=False,
            )
            if getattr(raw, "is_error", False):
                message = _redact_forward_text(str(raw), extra_secrets)
                logger.warning(
                    "canonical catalog downstream tool error gateway_tool=%s "
                    "tool=%s",
                    gateway_tool,
                    tool_name,
                )
                return _finish(
                    ok=False,
                    failure_stage="tool",
                    error={"message": message[:500], "stage": "tool"},
                )
            data = getattr(raw, "data", None)
            if data is None:
                data = getattr(raw, "structured_content", None)
            if data is None:
                data = getattr(raw, "content", None)
            return _finish(ok=True, failure_stage=None, result=data)
    except Exception as exc:  # noqa: BLE001 — isolation boundary
        stage = _classify_forward_error(exc)
        logger.warning(
            "canonical catalog forward failed gateway_tool=%s tool=%s stage=%s",
            gateway_tool,
            tool_name,
            stage,
        )
        return _finish(
            ok=False,
            failure_stage=stage,
            error={
                "message": _redact_forward_text(str(exc), extra_secrets),
                "stage": stage,
            },
        )


async def _call_canonical_forward(
    gateway_tool: str,
    downstream_tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    _require_allowed_user()
    return await forward_canonical_catalog_tool(
        gateway_tool, downstream_tool, arguments
    )


REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "nhpcorp35/legal-ai")
WORKFLOW = os.environ.get("GITHUB_WORKFLOW", "hal-bridge-proof.yml")
WORKFLOW_BRANCH = os.environ.get("GITHUB_WORKFLOW_BRANCH", "agent/hal-bridge-proof-workflow")
CASE00_WORKFLOW = os.environ.get("GITHUB_CASE00_WORKFLOW", "hal-case00-q1.yml")
CASE00_WORKFLOW_BRANCH = os.environ.get("GITHUB_CASE00_WORKFLOW_BRANCH", "main")
B2_BUCKET = os.environ.get("B2_BUCKET", "legalai-corpus")
B2_PREFIX = os.environ.get("B2_PROOF_PREFIX", "Benchmarks/Bridge-Proof")
PUBLIC_URL = os.environ.get(
    "BRIDGE_PUBLIC_URL",
    "https://hal-github-actions-bridge-production.up.railway.app",
).rstrip("/")
# Exact origin+path the existing unnumbered ChatGPT plugin recaches on Refresh.
PLUGIN_REFRESH_MCP_URL = plugin_refresh_mcp_url(PUBLIC_URL)
ALLOWED_GITHUB_LOGIN = os.environ.get("ALLOWED_GITHUB_LOGIN", "nhpcorp35")
GITHUB_API = "https://api.github.com"
CASE_ARTIFACT_PREFIX = (
    "Benchmarks/Case-00-Triborough/derived/"
    "attorney-feedback-eval/candidate-answers/"
)
# Shared manifests are valid for every Case-00 question; candidate answers are
# question-scoped (Q<N>_candidate_answer.*) and resolved from Bridge metadata.
CASE_ARTIFACT_SHARED_FILENAMES = frozenset(
    {
        "generation_manifest.json",
        "model_input_audit.json",
        "case00_attorney_review_packet.md",
    }
)
CASE_ARTIFACT_CANDIDATE_JSON_LIMIT = 1_000_000
CASE_ARTIFACT_CANDIDATE_MD_LIMIT = 100_000
CASE_ARTIFACT_SHARED_LIMIT = 100_000
_CASE_ARTIFACT_CANDIDATE_FILENAME_RE = re.compile(
    r"^(Q[1-9]\d*)_candidate_answer\.(json|md)$"
)

# Public /mcp surface: GitHub OAuth only (ChatGPT / operator clients).
# Gateway uses the separate /mcp/service TokenVerifier surface — never composite.
oauth_auth_provider = GitHubProvider(
    client_id=os.environ["GITHUB_OAUTH_CLIENT_ID"],
    client_secret=os.environ["GITHUB_OAUTH_CLIENT_SECRET"],
    base_url=PUBLIC_URL,
    jwt_signing_key=os.environ.get("JWT_SIGNING_KEY"),
    client_storage=FernetEncryptionWrapper(
        key_value=RedisStore(
            host=os.environ["REDIS_HOST"],
            port=int(os.environ.get("REDIS_PORT", "6379")),
        ),
        fernet=Fernet(os.environ["STORAGE_ENCRYPTION_KEY"].encode()),
    ),
)

mcp = FastMCP(
    "HAL GitHub Actions Bridge",
    instructions=(
        "Dispatch and observe the bounded LegalAI proof workflow. Use submit_run, "
        "then get_run. Use cancel_run for a deliberate long run. After a successful "
        "run, use get_artifacts to copy the harmless proof JSON to B2, verify it, "
        "and retrieve the durable object key. The separate Case-00 Q1 tools "
        "dispatch the bounded generation-only workflow, require explicit private-evidence "
        "authorization, and return only B2-verified candidate artifact metadata. "
        "Question-agnostic Case-00 tools (submit_case00, get_case00_run, "
        "cancel_case00_run, get_case00_artifacts) accept benchmark_id exactly "
        "'Case-00-Triborough' with question_id matching ^Q[1-9]\\d*$ "
        "and an immutable 40-character commit SHA only; unsupported combinations "
        "fail closed. Use get_case_artifact to read one allowlisted, "
        "mission-correlated artifact after a successful Case-00 run "
        "(Q<N>_candidate_answer.json|.md for that mission's question, plus "
        "generation_manifest.json, model_input_audit.json, and "
        "case00_attorney_review_packet.md). Case-00 storage tools "
        "expose allowlisted inventory metadata, archive a fixed attorney-feedback "
        "package, and archive one DOCX review packet under canonical B2 prefixes "
        "without accepting bucket or key inputs. Namespaced canonical catalog "
        "tools recached by the unnumbered HAL LegalAI Gateway plugin include "
        "mission.submit, workflow.submit, workflow.cancel, and workflow.status."
    ),
    auth=oauth_auth_provider,
)


def _require_allowed_user() -> str:
    token = get_access_token()
    if is_service_access_token(token):
        claims = token.claims if token is not None else {}
        client_id = (claims or {}).get("client_id") or (
            token.client_id if token is not None else "service"
        )
        return f"service:{client_id}"
    login = token.claims.get("login") if token is not None else None
    if login != ALLOWED_GITHUB_LOGIN:
        raise PermissionError("authenticated GitHub user is not authorized")
    return str(login)


def _github_headers() -> dict[str, str]:
    token = os.environ["GITHUB_TOKEN"]
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _github(method: str, path: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.request(
            method, f"{GITHUB_API}{path}", headers=_github_headers(), **kwargs
        )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        try:
            detail = response.json().get("message", response.text[:500])
        except (ValueError, AttributeError):
            detail = response.text[:500]
        accepted = response.headers.get("x-accepted-github-permissions", "unknown")
        scopes = response.headers.get("x-oauth-scopes", "not-reported")
        raise RuntimeError(
            f"GitHub API {response.status_code} for {method} {path}: {detail}; "
            f"accepted_permissions={accepted}; token_scopes={scopes}"
        ) from exc
    return response


def raise_case00_structured_error(error_code: str, message: str) -> NoReturn:
    """Raise a ToolError whose message is safe JSON for Gateway envelopes."""
    payload = {
        "ok": False,
        "error_code": error_code,
        "message": message,
    }
    raise ToolError(json.dumps(payload, separators=(",", ":")))


def _is_exact_commit_sha(value: str) -> bool:
    return bool(_FULL_COMMIT_SHA_RE.fullmatch(value))


async def _github_json(
    method: str, path: str, **kwargs: Any
) -> tuple[httpx.Response | None, dict[str, Any] | None, str | None]:
    """GitHub JSON helper that never echoes credentials or token scope headers.

    Returns ``(response, json_body, transport_error_message)``. On transport
    failure ``response`` is None. Callers map status codes to safe error_codes.
    """
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.request(
                method, f"{GITHUB_API}{path}", headers=_github_headers(), **kwargs
            )
    except httpx.HTTPError as exc:
        return None, None, exc.__class__.__name__
    try:
        body: dict[str, Any] | None
        parsed = response.json()
        body = parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        body = None
    return response, body, None


async def resolve_case00_legalai_ref(ref: str) -> tuple[str, str]:
    """Accept configured branch alias or exact SHA; resolve against GITHUB_REPOSITORY.

    Returns ``(requested_ref, resolved_sha)``. Raises ``ToolError`` with safe
    JSON ``error_code`` / ``message`` on validation or GitHub failures.
    Does not consult the Bridge deployment/source repository — only
    ``REPOSITORY`` (configured LegalAI workflow repository).
    """
    requested_ref = (ref or "").strip()
    if not requested_ref:
        raise_case00_structured_error(
            ERROR_REF_INVALID,
            "ref must be the configured workflow branch "
            f"({CASE00_WORKFLOW_BRANCH}) or an exact 40-character lowercase commit SHA",
        )

    if requested_ref == CASE00_WORKFLOW_BRANCH:
        path = f"/repos/{REPOSITORY}/commits/{CASE00_WORKFLOW_BRANCH}"
        response, body, transport_error = await _github_json("GET", path)
        if transport_error is not None:
            raise_case00_structured_error(
                ERROR_REF_RESOLUTION_FAILED,
                "failed to resolve configured workflow branch HEAD from "
                f"{REPOSITORY} ({transport_error})",
            )
        assert response is not None
        if response.status_code == 404:
            raise_case00_structured_error(
                ERROR_REF_RESOLUTION_FAILED,
                f"configured workflow branch {CASE00_WORKFLOW_BRANCH!r} was not "
                f"found in {REPOSITORY}",
            )
        if response.status_code >= 400 or not isinstance(body, dict):
            raise_case00_structured_error(
                ERROR_REF_RESOLUTION_FAILED,
                "failed to resolve configured workflow branch HEAD from "
                f"{REPOSITORY} (HTTP {response.status_code})",
            )
        sha = body.get("sha")
        if not isinstance(sha, str) or not _is_exact_commit_sha(sha.lower()):
            raise_case00_structured_error(
                ERROR_REF_RESOLUTION_FAILED,
                "GitHub did not return an exact commit SHA for the configured "
                f"workflow branch in {REPOSITORY}",
            )
        return requested_ref, sha.lower()

    if _is_exact_commit_sha(requested_ref):
        path = f"/repos/{REPOSITORY}/commits/{requested_ref}"
        response, body, transport_error = await _github_json("GET", path)
        if transport_error is not None:
            raise_case00_structured_error(
                ERROR_REF_RESOLUTION_FAILED,
                "failed to verify commit in "
                f"{REPOSITORY} ({transport_error})",
            )
        assert response is not None
        # GitHub returns 404 or 422 for a well-formed SHA that is absent from
        # the repository (or belongs to another repo). Both mean fail-closed
        # before workflow_dispatch — not a transient resolution failure.
        if response.status_code in (404, 422):
            raise_case00_structured_error(
                ERROR_REF_NOT_IN_REPOSITORY,
                f"commit {requested_ref} was not found in {REPOSITORY}",
            )
        if response.status_code >= 400:
            raise_case00_structured_error(
                ERROR_REF_RESOLUTION_FAILED,
                "failed to verify commit in "
                f"{REPOSITORY} (HTTP {response.status_code})",
            )
        sha = (body or {}).get("sha") if isinstance(body, dict) else None
        if isinstance(sha, str) and _is_exact_commit_sha(sha.lower()):
            return requested_ref, sha.lower()
        # Some GitHub responses omit body.sha on success; the requested SHA
        # already passed format + existence (non-404) checks.
        return requested_ref, requested_ref

    raise_case00_structured_error(
        ERROR_REF_INVALID,
        "ref must be the configured workflow branch "
        f"({CASE00_WORKFLOW_BRANCH}) or an exact 40-character lowercase commit SHA; "
        "arbitrary branches, tags, abbreviated SHAs, and uppercase SHAs are rejected",
    )


async def _resolve_run(mission_id: str) -> dict[str, Any] | None:
    response = await _github(
        "GET",
        f"/repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/runs",
        params={"event": "workflow_dispatch", "per_page": 50},
    )
    marker = f"hal-proof-{mission_id}"
    for run in response.json().get("workflow_runs", []):
        if marker in (run.get("display_title") or ""):
            return run
    return None


def case00_question_token(question_id: str) -> str:
    """Lowercase question token used in run markers and artifact names (Q2 → q2)."""
    return question_id.strip().lower()


def case00_run_marker(question_id: str, mission_id: str) -> str:
    """GitHub Actions display-title / artifact marker for a Case-00 question run."""
    return f"hal-case00-{case00_question_token(question_id)}-{mission_id}"


def case00_result_filename(question_id: str) -> str:
    """Workflow result JSON filename inside the Case-00 artifact zip."""
    return f"case00-{case00_question_token(question_id)}-result.json"


def parse_case00_question_token(text: str, mission_id: str) -> str | None:
    """Extract q[1-9]\\d* from a Case-00 run title or artifact name, if present.

    Workflow run titles keep the submitted question_id casing (Q2); artifact
    names are lowercase (q2). Match only the Q/q token slot, then normalize.
    """
    match = re.search(
        rf"hal-case00-([Qq][1-9]\d*)-{re.escape(mission_id)}(?:\b|$)",
        text or "",
    )
    if match is None:
        return None
    token = match.group(1).lower()
    if not _CASE00_QUESTION_TOKEN_RE.fullmatch(token):
        return None
    return token


def case00_question_id_from_token(question_token: str) -> str:
    """Normalize a lowercase question token (q2) to canonical question_id (Q2)."""
    token = (question_token or "").strip().lower()
    if not _CASE00_QUESTION_TOKEN_RE.fullmatch(token):
        raise ValueError("invalid case question token")
    return token.upper()


def allowed_case_artifact_filenames(question_token: str) -> frozenset[str]:
    """Exact basenames permitted for one Case-00 question plus shared manifests."""
    question_id = case00_question_id_from_token(question_token)
    return frozenset(
        {
            f"{question_id}_candidate_answer.json",
            f"{question_id}_candidate_answer.md",
        }
    ) | CASE_ARTIFACT_SHARED_FILENAMES


def case00_durable_objects_complete(
    objects: list[Any], question_token: str
) -> bool:
    """Require exactly one object for every allowlisted durable filename."""
    filenames = [
        item.get("filename") if isinstance(item, dict) else None
        for item in objects
    ]
    required = allowed_case_artifact_filenames(question_token)
    return len(filenames) == len(required) and set(filenames) == required


def assert_safe_case_artifact_basename(filename: str) -> str:
    """Reject path traversal, absolute paths, and non-basename artifact keys."""
    if not isinstance(filename, str) or not filename:
        raise ValueError("filename must be a bare allowlisted basename")
    if (
        filename != filename.strip()
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
        or filename in {".", ".."}
        or ".." in filename
    ):
        raise ValueError("filename must be a bare allowlisted basename")
    return filename


def case_artifact_size_limit(filename: str) -> int | None:
    """Byte cap for an allowlisted case artifact basename, else None."""
    if filename in CASE_ARTIFACT_SHARED_FILENAMES:
        return CASE_ARTIFACT_SHARED_LIMIT
    match = _CASE_ARTIFACT_CANDIDATE_FILENAME_RE.fullmatch(filename)
    if match is None:
        return None
    if match.group(2) == "json":
        return CASE_ARTIFACT_CANDIDATE_JSON_LIMIT
    return CASE_ARTIFACT_CANDIDATE_MD_LIMIT


def question_token_from_verified_objects(objects: list[Any]) -> str | None:
    """Derive q[1-9]\\d* from independently verified durable object filenames."""
    tokens: set[str] = set()
    for item in objects:
        name = item.get("filename") if isinstance(item, dict) else None
        if not isinstance(name, str):
            continue
        match = _CASE_ARTIFACT_CANDIDATE_FILENAME_RE.fullmatch(name)
        if match is None:
            continue
        tokens.add(match.group(1).lower())
    if len(tokens) != 1:
        return None
    token = next(iter(tokens))
    if not _CASE00_QUESTION_TOKEN_RE.fullmatch(token):
        return None
    return token


async def _resolve_case00_run(
    mission_id: str, question_id: str | None = None
) -> dict[str, Any] | None:
    response = await _github(
        "GET",
        f"/repos/{REPOSITORY}/actions/workflows/{CASE00_WORKFLOW}/runs",
        params={"event": "workflow_dispatch", "per_page": 50},
    )
    for run in response.json().get("workflow_runs", []):
        title = run.get("display_title") or ""
        if question_id is not None:
            # Marker is lowercase (q2); workflow display_title may keep Q2.
            marker = case00_run_marker(question_id, mission_id)
            if marker.lower() in title.lower():
                return run
            continue
        if parse_case00_question_token(title, mission_id) is not None:
            return run
    return None


def _run_result(mission_id: str, run: dict[str, Any] | None) -> dict[str, Any]:
    if run is None:
        return {"ok": True, "mission_id": mission_id, "status": "dispatching"}
    return {
        "ok": True,
        "mission_id": mission_id,
        "run_id": run["id"],
        "status": run["status"],
        "conclusion": run.get("conclusion"),
        "head_sha": run.get("head_sha"),
        "html_url": run.get("html_url"),
    }


@mcp.tool()
async def submit_run(
    ref: str = "main", sleep_seconds: int = 0, mission_id: str | None = None
) -> dict[str, Any]:
    """Dispatch the bounded LegalAI proof workflow and return its correlation ID."""
    _require_allowed_user()
    if sleep_seconds < 0 or sleep_seconds > 300:
        raise ValueError("sleep_seconds must be between 0 and 300")
    mission_id = mission_id or str(uuid.uuid4())
    await _github(
        "POST",
        f"/repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/dispatches",
        json={
            "ref": WORKFLOW_BRANCH,
            "inputs": {
                "mission_id": mission_id,
                "legalai_ref": ref,
                "sleep_seconds": str(sleep_seconds),
            },
        },
    )
    return {
        "ok": True,
        "mission_id": mission_id,
        "status": "dispatched",
        "repository": REPOSITORY,
        "requested_ref": ref,
    }


@mcp.tool()
async def get_run(mission_id: str) -> dict[str, Any]:
    """Return GitHub's current status, conclusion, and exact checked-out SHA."""
    _require_allowed_user()
    return _run_result(mission_id, await _resolve_run(mission_id))


@mcp.tool()
async def cancel_run(mission_id: str) -> dict[str, Any]:
    """Cancel the GitHub Actions run correlated with mission_id."""
    _require_allowed_user()
    run = await _resolve_run(mission_id)
    if run is None:
        return {"ok": False, "mission_id": mission_id, "error": "run_not_found"}
    await _github(
        "POST", f"/repos/{REPOSITORY}/actions/runs/{run['id']}/cancel"
    )
    return {"ok": True, "mission_id": mission_id, "run_id": run["id"], "status": "cancellation_requested"}


async def _dispatch_case00_generation(
    *,
    mission_id: str,
    requested_ref: str,
    resolved_ref: str,
    benchmark_id: str | None = None,
    question_id: str | None = None,
) -> dict[str, Any]:
    """Dispatch the existing safe Case-00 generation workflow.

    When ``benchmark_id`` / ``question_id`` are provided (generic submit_case00),
    they are forwarded unchanged as workflow_dispatch inputs. The Q1-specific
    submit path supplies the canonical Case-00/Q1 values because the production
    workflow requires both inputs.
    """
    dispatch_path = (
        f"/repos/{REPOSITORY}/actions/workflows/{CASE00_WORKFLOW}/dispatches"
    )
    inputs: dict[str, str] = {
        "mission_id": mission_id,
        "legalai_ref": resolved_ref,
        "authorization_confirmed": "true",
    }
    if benchmark_id is not None:
        inputs["benchmark_id"] = benchmark_id
    if question_id is not None:
        inputs["question_id"] = question_id
    response, _body, transport_error = await _github_json(
        "POST",
        dispatch_path,
        json={
            "ref": CASE00_WORKFLOW_BRANCH,
            "inputs": inputs,
        },
    )
    if transport_error is not None:
        raise_case00_structured_error(
            ERROR_DISPATCH_FAILED,
            f"workflow dispatch failed for {CASE00_WORKFLOW} ({transport_error})",
        )
    assert response is not None
    # workflow_dispatch accepted → 204 No Content
    if response.status_code not in {201, 204}:
        raise_case00_structured_error(
            ERROR_DISPATCH_FAILED,
            f"workflow dispatch failed for {CASE00_WORKFLOW} "
            f"(HTTP {response.status_code})",
        )
    return {
        "ok": True,
        "mission_id": mission_id,
        "status": "dispatched",
        "repository": REPOSITORY,
        "requested_ref": requested_ref,
        "resolved_ref": resolved_ref,
        "workflow": CASE00_WORKFLOW,
    }


def validate_case00_benchmark_question(benchmark_id: str, question_id: str) -> tuple[str, str]:
    """Return normalized IDs or raise a structured unsupported-combination error.

    Fail closed: only ``Case-00-Triborough`` with ``question_id`` matching
    ``^Q[1-9]\\d*$`` (Q1, Q2, …) are accepted.
    """
    benchmark = (benchmark_id or "").strip()
    question = (question_id or "").strip()
    if benchmark != CASE00_BENCHMARK_ID or not _CASE00_QUESTION_ID_RE.fullmatch(question):
        raise_case00_structured_error(
            ERROR_UNSUPPORTED_BENCHMARK_QUESTION,
            "unsupported benchmark_id/question_id combination "
            f"{benchmark!r}/{question!r}; supported: benchmark_id="
            f"{CASE00_BENCHMARK_ID!r} with question_id matching ^Q[1-9]\\d*$",
        )
    return benchmark, question


async def resolve_case00_immutable_commit_sha(commit_sha: str) -> tuple[str, str]:
    """Require an exact lowercase 40-character SHA; reject mutable refs.

    Unlike ``resolve_case00_legalai_ref``, branch aliases (including ``main``)
    are rejected. Returns ``(requested_sha, verified_sha)``.
    """
    requested = (commit_sha or "").strip()
    if not _is_exact_commit_sha(requested):
        raise_case00_structured_error(
            ERROR_REF_INVALID,
            "commit_sha must be an exact 40-character lowercase commit SHA; "
            "mutable refs (branches, tags, HEAD, abbreviated or uppercase SHAs) "
            "are rejected",
        )
    # Reuse LegalAI repository preflight via the shared resolver.
    return await resolve_case00_legalai_ref(requested)


async def _case00_run_status(mission_id: str) -> dict[str, Any]:
    return _run_result(mission_id, await _resolve_case00_run(mission_id))


async def _case00_cancel_run(mission_id: str) -> dict[str, Any]:
    run = await _resolve_case00_run(mission_id)
    if run is None:
        return {"ok": False, "mission_id": mission_id, "error": "run_not_found"}
    await _github("POST", f"/repos/{REPOSITORY}/actions/runs/{run['id']}/cancel")
    return {
        "ok": True,
        "mission_id": mission_id,
        "run_id": run["id"],
        "status": "cancellation_requested",
    }


@mcp.tool()
async def submit_case00_q1(
    ref: str, authorization_confirmed: bool, mission_id: str | None = None
) -> dict[str, Any]:
    """Dispatch generation-only Case-00 Q1 at an immutable verified commit SHA.

    ``ref`` may be the configured workflow branch (normally ``main``), which is
    resolved to HEAD of the configured ``GITHUB_REPOSITORY`` (LegalAI), or an
    exact lowercase 40-character commit SHA preflight-checked in that same
    repository. GitHub Actions always receives the resolved SHA.
    """
    _require_allowed_user()
    if not authorization_confirmed:
        raise ValueError(
            "authorization_confirmed must be true before private evidence is sent"
        )
    requested_ref, resolved_ref = await resolve_case00_legalai_ref(ref)
    mission_id = mission_id or str(uuid.uuid4())
    return await _dispatch_case00_generation(
        mission_id=mission_id,
        requested_ref=requested_ref,
        resolved_ref=resolved_ref,
        benchmark_id=CASE00_BENCHMARK_ID,
        question_id="Q1",
    )


@mcp.tool()
async def submit_case00(
    commit_sha: str,
    benchmark_id: str,
    question_id: str,
    authorization_confirmed: bool,
    mission_id: str | None = None,
) -> dict[str, Any]:
    """Dispatch a question-agnostic Case-00 run at an immutable commit SHA.

    Requires ``benchmark_id`` exactly ``Case-00-Triborough``, ``question_id``
    matching ``^Q[1-9]\\d*$``, and an exact lowercase 40-character
    ``commit_sha`` (mutable refs rejected). Unsupported combinations fail
    closed. Accepted IDs are forwarded unchanged on workflow_dispatch.
    """
    _require_allowed_user()
    if not authorization_confirmed:
        raise ValueError(
            "authorization_confirmed must be true before private evidence is sent"
        )
    benchmark, question = validate_case00_benchmark_question(benchmark_id, question_id)
    requested_ref, resolved_ref = await resolve_case00_immutable_commit_sha(commit_sha)
    mission_id = mission_id or str(uuid.uuid4())
    result = await _dispatch_case00_generation(
        mission_id=mission_id,
        requested_ref=requested_ref,
        resolved_ref=resolved_ref,
        benchmark_id=benchmark,
        question_id=question,
    )
    result["benchmark_id"] = benchmark
    result["question_id"] = question
    return result


@mcp.tool()
async def get_case00_q1_run(mission_id: str) -> dict[str, Any]:
    """Return the current GitHub status for a Case-00 Q1 run."""
    _require_allowed_user()
    return await _case00_run_status(mission_id)


@mcp.tool()
async def get_case00_run(mission_id: str) -> dict[str, Any]:
    """Return the current GitHub status for a Case-00 mission_id."""
    _require_allowed_user()
    return await _case00_run_status(mission_id)


@mcp.tool()
async def cancel_case00_q1_run(mission_id: str) -> dict[str, Any]:
    """Cancel the Case-00 Q1 GitHub Actions run correlated with mission_id."""
    _require_allowed_user()
    return await _case00_cancel_run(mission_id)


@mcp.tool()
async def cancel_case00_run(mission_id: str) -> dict[str, Any]:
    """Cancel the Case-00 GitHub Actions run correlated with mission_id."""
    _require_allowed_user()
    return await _case00_cancel_run(mission_id)


def _b2_client():
    endpoint = os.environ["B2_ENDPOINT"].rstrip("/")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=os.environ.get("B2_REGION", "us-west-004"),
        aws_access_key_id=os.environ["B2_KEY_ID"],
        aws_secret_access_key=os.environ["B2_APPLICATION_KEY"],
    )


def _parse_case00_question_sections(packet: str) -> dict[str, str]:
    """Extract ``## QN.`` sections from the canonical Case-00 source document."""
    sections: dict[str, str] = {}
    for match in _CASE00_PACKET_QUESTION_SECTION_RE.finditer(packet):
        found_id = match.group(1)
        if found_id in sections:
            raise ValueError(
                "canonical Case-00 attorney packet has duplicate question headings"
            )
        sections[found_id] = match.group(0).strip()
    return sections


def _load_verified_case00_benchmark_source() -> str:
    """Fetch and verify the canonical Case-00 benchmark source from B2."""
    client = _b2_client()
    head = client.head_object(
        Bucket=B2_BUCKET, Key=CANONICAL_CASE00_ATTORNEY_PACKET_KEY
    )
    actual_size = head.get("ContentLength")
    if actual_size != CANONICAL_CASE00_ATTORNEY_PACKET_SIZE:
        raise ValueError("canonical Case-00 attorney packet size mismatch")

    response = client.get_object(
        Bucket=B2_BUCKET, Key=CANONICAL_CASE00_ATTORNEY_PACKET_KEY
    )
    stream = response["Body"]
    try:
        body = stream.read(CANONICAL_CASE00_ATTORNEY_PACKET_SIZE + 1)
    finally:
        stream.close()
    if len(body) != CANONICAL_CASE00_ATTORNEY_PACKET_SIZE:
        raise ValueError("canonical Case-00 attorney packet body size mismatch")
    if hashlib.sha256(body).hexdigest() != CANONICAL_CASE00_ATTORNEY_PACKET_SHA256:
        raise ValueError("canonical Case-00 attorney packet sha256 mismatch")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("canonical Case-00 attorney packet is not UTF-8") from exc


@mcp.tool()
async def get_case00_question(question_id: str) -> dict[str, Any]:
    """Read one verified ``## QN.`` section from the canonical Case-00 source.

    This is a read-only, allowlisted B2 retrieval. It returns only the requested
    question section, never the full attorney packet or an arbitrary object.
    """
    _require_allowed_user()
    qid = str(question_id or "").strip()
    if not _CASE00_QUESTION_ID_RE.fullmatch(qid):
        raise ValueError("question_id must match Q followed by a positive integer")

    packet = _load_verified_case00_benchmark_source()
    sections = _parse_case00_question_sections(packet)
    question_text = sections.get(qid)
    provenance = {
        "benchmark_id": CASE00_BENCHMARK_ID,
        "source_object_key": CANONICAL_CASE00_ATTORNEY_PACKET_KEY,
        "sha256": CANONICAL_CASE00_ATTORNEY_PACKET_SHA256,
    }
    if not question_text:
        return {
            "ok": False,
            "question_id": qid,
            "error": "not_found",
            **provenance,
        }
    if len(question_text) > MAX_CASE00_QUESTION_SECTION_CHARS:
        raise ValueError("canonical Case-00 question section exceeds size limit")
    return {
        "ok": True,
        "question_id": qid,
        "question_text": question_text,
        **provenance,
    }


@mcp.tool()
async def list_case00_storage(
    category: Literal[
        "all",
        "source",
        "questions",
        "candidate_answers",
        "attorney_reviews",
        "attorney_review_packets",
    ] = "all",
    max_keys: int = 200,
) -> dict[str, Any]:
    """List allowlisted Case-00 B2 object metadata under a canonical prefix."""
    _require_allowed_user()
    if max_keys < 1 or max_keys > 200:
        raise ValueError("max_keys must be between 1 and 200")
    prefix = inventory_prefix(category)
    response = _b2_client().list_objects_v2(
        Bucket=B2_BUCKET, Prefix=prefix, MaxKeys=max_keys
    )
    objects = [
        {
            "object_key": item["Key"],
            "size": item["Size"],
            "etag": (item.get("ETag") or "").strip('"'),
            "last_modified": item["LastModified"].isoformat(),
        }
        for item in response.get("Contents", [])
    ]
    return {
        "ok": True,
        "b2_bucket": B2_BUCKET,
        "category": category,
        "prefix": prefix,
        "objects": objects,
        "count": len(objects),
        "truncated": bool(response.get("IsTruncated")),
    }


@mcp.tool()
async def verify_case_intake(
    case_id: str,
    source_filename: str,
    source_bundle_size: int,
    source_bundle_sha256: str,
    manifest_filename: str,
    manifest_size: int,
    manifest_sha256: str,
) -> dict[str, Any]:
    """Verify a pre-uploaded active-case bundle and manifest without reading it out."""
    _require_allowed_user()
    source_key, manifest_key = intake_keys(case_id, source_filename, manifest_filename)
    client = _b2_client()
    source = verify_case_intake_object(
        client,
        bucket=B2_BUCKET,
        object_key=source_key,
        expected_size=source_bundle_size,
        expected_sha256=source_bundle_sha256,
        max_size=MAX_BUNDLE_BYTES,
    )
    manifest = verify_case_intake_object(
        client,
        bucket=B2_BUCKET,
        object_key=manifest_key,
        expected_size=manifest_size,
        expected_sha256=manifest_sha256,
        max_size=MAX_MANIFEST_BYTES,
    )
    verified = bool(source.get("verified")) and bool(manifest.get("verified"))
    return {
        "ok": verified,
        "verified": verified,
        "case_id": case_id,
        "objects": [source, manifest],
    }


RENNICK_CASE_ID = "NY-Nassau-613561-2026-Desousa-v-Rennick"
RENNICK_SOURCE_FILENAME = "Rennick_Case_Source_2026-08-26.zip"
RENNICK_MANIFEST_FILENAME = "Rennick_Case_Intake_Manifest_2026-08-26.json"
RENNICK_SUPPLEMENT_ID = "docket-entries-5-18-19-2026-08-26"
RENNICK_SUPPLEMENT_ARCHIVE_FILENAME = "Rennick_Docket_Supplement_2026-08-26_5_18_19.zip"
RENNICK_SUPPLEMENT_MANIFEST_FILENAME = "Rennick_Docket_Supplement_2026-08-26_5_18_19.manifest.json"
RENNICK_SUPPLEMENT_FILENAMES = frozenset(
    {
        "613561_2026_MICHAEL_DESOUSA_et_al_v_GEORGE_RENNICK_et_al_EXHIBIT_S__5 (1).pdf",
        "613561_2026_MICHAEL_DESOUSA_et_al_v_GEORGE_RENNICK_et_al_RJI__RE__ORDER_TO_S_18.pdf",
        "613561_2026_MICHAEL_DESOUSA_et_al_v_GEORGE_RENNICK_et_al_LETTER___CORRESPOND_19.pdf",
    }
)
RENNICK_DIRECT_UPLOAD_TTL_SECONDS = 300
RENNICK_DIRECT_UPLOAD_PREFIX = f"cases/{RENNICK_CASE_ID}/intake/.pending/"
RENNICK_DIRECT_UPLOAD_ORIGIN_ENV = "HAL_LEGALAI_GATEWAY_URL"
PENDING_INTAKE_ID = "szymczyk-case-2026-08-27"
PENDING_INTAKE_FILENAME = "wetransfer_szymczyk-case_2026-08-27_1952"
PENDING_INTAKE_MAX_BYTES = 1024 * 1024 * 1024
PENDING_INTAKE_TTL_SECONDS = 60 * 60
PENDING_INTAKE_PREFIX = f"pending-intakes/{PENDING_INTAKE_ID}/.pending/"
SZYMCZYK_CASE_ID = "NY-NewYork-158068-2018-Szymczyk-v-Hudson-36-37"
SZYMCZYK_CASE_CAPTION = "Andrzej Szymczyk v. Hudson 36 LLC and Hudson 37 LLC"
SZYMCZYK_CASE_COURT = "Supreme Court of the State of New York, County of New York"
SZYMCZYK_CASE_INDEX_NUMBER = "158068/2018"


def _require_absent_intake_object(client: Any, object_key: str) -> None:
    try:
        client.head_object(Bucket=B2_BUCKET, Key=object_key)
    except ClientError as exc:
        code = str(((exc.response or {}).get("Error") or {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return
        raise
    raise ValueError("intake object already exists; refusing to overwrite")


def _upload_rennick_intake_pair(source: bytes, manifest: bytes) -> dict[str, Any]:
    """Store and HEAD-verify the exact Rennick pair with no overwrites."""
    source_key, manifest_key = intake_keys(
        RENNICK_CASE_ID, RENNICK_SOURCE_FILENAME, RENNICK_MANIFEST_FILENAME
    )
    client = _b2_client()
    _require_absent_intake_object(client, source_key)
    _require_absent_intake_object(client, manifest_key)
    source_sha256 = hashlib.sha256(source).hexdigest()
    manifest_sha256 = hashlib.sha256(manifest).hexdigest()
    client.put_object(
        Bucket=B2_BUCKET,
        Key=source_key,
        Body=source,
        ContentType="application/zip",
        Metadata={"sha256": source_sha256},
    )
    try:
        client.put_object(
            Bucket=B2_BUCKET,
            Key=manifest_key,
            Body=manifest,
            ContentType="application/json",
            Metadata={"sha256": manifest_sha256},
        )
    except Exception:
        raise RuntimeError(
            "manifest upload failed after source upload; retry is blocked to prevent overwrite"
        )
    objects = []
    for key, expected_size, expected_sha256 in (
        (source_key, len(source), source_sha256),
        (manifest_key, len(manifest), manifest_sha256),
    ):
        head = client.head_object(Bucket=B2_BUCKET, Key=key)
        if head.get("ContentLength") != expected_size:
            raise ValueError("B2 intake upload size mismatch")
        if (head.get("Metadata") or {}).get("sha256") != expected_sha256:
            raise ValueError("B2 intake upload SHA-256 metadata mismatch")
        objects.append(
            {
                "object_key": key,
                "size": expected_size,
                "sha256": expected_sha256,
                "etag": (head.get("ETag") or "").strip('"'),
            }
        )
    return {
        "ok": True,
        "uploaded": True,
        "case_id": RENNICK_CASE_ID,
        "objects": objects,
    }


def _normalize_rennick_contents_manifest(raw: bytes) -> bytes:
    """Normalize the existing intake manifest without opening the ZIP or PDFs."""
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Rennick intake manifest is invalid")
    candidates = payload.get("files") or payload.get("documents") or payload.get("entries")
    if not isinstance(candidates, list):
        raise ValueError("Rennick intake manifest has no document list")
    files = []
    for item in candidates:
        if not isinstance(item, dict):
            raise ValueError("Rennick intake manifest document entry is invalid")
        filename = item.get("filename") or item.get("name") or item.get("path")
        if not isinstance(filename, str) or not filename:
            raise ValueError("Rennick intake manifest document filename is invalid")
        files.append(dict(item, filename=filename))
    if not files:
        raise ValueError("Rennick intake manifest contains no documents")
    return json.dumps({"schema_version": "case-contents.v1", "files": files}, sort_keys=True).encode()


def _promote_rennick_intake() -> dict[str, Any]:
    """Copy already-verified Rennick bytes to canonical reader keys, immutably."""
    client = _b2_client()
    source_key, manifest_key = intake_keys(RENNICK_CASE_ID, RENNICK_SOURCE_FILENAME, RENNICK_MANIFEST_FILENAME)
    source_head = client.head_object(Bucket=B2_BUCKET, Key=source_key)
    source_sha256 = str((source_head.get("Metadata") or {}).get("sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("Rennick source is not hash-verified")
    raw_manifest = client.get_object(Bucket=B2_BUCKET, Key=manifest_key)["Body"].read()
    contents = _normalize_rennick_contents_manifest(raw_manifest)
    prefix = f"cases/{RENNICK_CASE_ID}/intake/source/{source_sha256}/"
    identity_key = f"cases/{RENNICK_CASE_ID}/intake/case_identity.json"
    identity = json.dumps({"schema_version": "case-identity.v1", "case_id": RENNICK_CASE_ID, "source_sha256": source_sha256, "source_filename": RENNICK_SOURCE_FILENAME}, sort_keys=True).encode()
    descriptor_key = prefix + "source_descriptor.json"
    descriptor = json.dumps({"schema_version": "verified-case-source-descriptor.v1", "case_id": RENNICK_CASE_ID, "source_sha256": source_sha256, "source_object_key": prefix + RENNICK_SOURCE_FILENAME, "contents_manifest_key": prefix + "contents_manifest.json"}, sort_keys=True).encode()
    copies = ((prefix + RENNICK_SOURCE_FILENAME, source_key), (prefix + "intake_manifest.json", manifest_key))
    for target, original in copies:
        try:
            client.head_object(Bucket=B2_BUCKET, Key=target)
        except ClientError as exc:
            if str(((exc.response or {}).get("Error") or {}).get("Code", "")) not in {"404", "NoSuchKey", "NotFound"}:
                raise
            client.copy_object(Bucket=B2_BUCKET, Key=target, CopySource={"Bucket": B2_BUCKET, "Key": original}, MetadataDirective="COPY")
    for key, body in ((prefix + "contents_manifest.json", contents), (identity_key, identity), (descriptor_key, descriptor)):
        try:
            existing = client.get_object(Bucket=B2_BUCKET, Key=key)["Body"].read()
            if existing != body:
                raise ValueError("canonical Rennick intake object already exists with different contents")
        except ClientError as exc:
            if str(((exc.response or {}).get("Error") or {}).get("Code", "")) not in {"404", "NoSuchKey", "NotFound"}:
                raise
            client.put_object(Bucket=B2_BUCKET, Key=key, Body=body, ContentType="application/json", Metadata={"sha256": hashlib.sha256(body).hexdigest()})
    return {"ok": True, "promoted": True, "case_id": RENNICK_CASE_ID, "source_sha256": source_sha256, "canonical_prefix": prefix, "identity_object_key": identity_key}


def _upload_rennick_docket_supplement(archive: bytes, manifest: bytes) -> dict[str, Any]:
    """Store the fixed three-document public-docket supplement without overwrites."""
    try:
        manifest_payload = json.loads(manifest)
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            names = [name for name in bundle.namelist() if not name.endswith("/")]
            if set(names) != RENNICK_SUPPLEMENT_FILENAMES or len(names) != 3:
                raise ValueError("supplement archive must contain exactly docket documents 5, 18, and 19")
            documents = manifest_payload.get("documents") if isinstance(manifest_payload, dict) else None
            if (
                manifest_payload.get("case_id") != RENNICK_CASE_ID
                or manifest_payload.get("supplement_id") != RENNICK_SUPPLEMENT_ID
                or not isinstance(documents, list)
            ):
                raise ValueError("invalid supplement manifest identity")
            expected = {item.get("filename"): item for item in documents if isinstance(item, dict)}
            if set(expected) != RENNICK_SUPPLEMENT_FILENAMES or len(expected) != 3:
                raise ValueError("supplement manifest must identify exactly docket documents 5, 18, and 19")
            for name in names:
                payload = bundle.read(name)
                item = expected[name]
                if item.get("size") != len(payload) or item.get("sha256") != hashlib.sha256(payload).hexdigest():
                    raise ValueError("supplement manifest document hash mismatch")
    except (json.JSONDecodeError, zipfile.BadZipFile, KeyError, TypeError) as exc:
        raise ValueError("invalid docket supplement archive or manifest") from exc

    prefix = f"cases/{RENNICK_CASE_ID}/intake/supplements/{RENNICK_SUPPLEMENT_ID}/"
    archive_key = prefix + RENNICK_SUPPLEMENT_ARCHIVE_FILENAME
    manifest_key = prefix + RENNICK_SUPPLEMENT_MANIFEST_FILENAME
    client = _b2_client()
    _require_absent_intake_object(client, archive_key)
    _require_absent_intake_object(client, manifest_key)
    archive_sha256 = hashlib.sha256(archive).hexdigest()
    manifest_sha256 = hashlib.sha256(manifest).hexdigest()
    client.put_object(Bucket=B2_BUCKET, Key=archive_key, Body=archive, ContentType="application/zip", Metadata={"sha256": archive_sha256})
    try:
        client.put_object(Bucket=B2_BUCKET, Key=manifest_key, Body=manifest, ContentType="application/json", Metadata={"sha256": manifest_sha256})
    except Exception:
        raise RuntimeError("supplement manifest upload failed after archive upload; retry is blocked to prevent overwrite")
    objects = []
    for key, expected_size, expected_sha256 in ((archive_key, len(archive), archive_sha256), (manifest_key, len(manifest), manifest_sha256)):
        head = client.head_object(Bucket=B2_BUCKET, Key=key)
        if head.get("ContentLength") != expected_size or (head.get("Metadata") or {}).get("sha256") != expected_sha256:
            raise ValueError("B2 supplement upload verification mismatch")
        objects.append({"object_key": key, "size": expected_size, "sha256": expected_sha256, "etag": (head.get("ETag") or "").strip('"')})
    return {"ok": True, "uploaded": True, "case_id": RENNICK_CASE_ID, "supplement_id": RENNICK_SUPPLEMENT_ID, "objects": objects}


def _ensure_rennick_direct_upload_cors(client: Any) -> None:
    """Allow this Gateway origin to PUT only the presigned pending objects.

    B2 evaluates CORS before validating the presigned URL. Preserve all existing
    bucket rules and add one narrowly-scoped rule only when no existing rule
    already permits this origin's ``PUT`` with ``Content-Type``.
    """
    raw_origin = (os.environ.get(RENNICK_DIRECT_UPLOAD_ORIGIN_ENV) or "").strip().rstrip("/")
    if raw_origin and "://" not in raw_origin:
        raw_origin = f"https://{raw_origin}"
    parsed = urlparse(raw_origin)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.path not in {"", "/"}:
        raise RuntimeError(f"{RENNICK_DIRECT_UPLOAD_ORIGIN_ENV} must be an absolute origin")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    try:
        rules = list((client.get_bucket_cors(Bucket=B2_BUCKET).get("CORSRules") or []))
    except ClientError as exc:
        code = str(((exc.response or {}).get("Error") or {}).get("Code", ""))
        if code not in {"NoSuchCORSConfiguration", "NoSuchCorsConfiguration", "404", "NotFound"}:
            raise
        rules = []
    for rule in rules:
        origins = set(rule.get("AllowedOrigins") or [])
        methods = {str(method).upper() for method in (rule.get("AllowedMethods") or [])}
        headers = {str(header).lower() for header in (rule.get("AllowedHeaders") or [])}
        if origin in origins and "PUT" in methods and ("*" in headers or "content-type" in headers):
            return
    rules.append(
        {
            "AllowedOrigins": [origin],
            "AllowedMethods": ["PUT"],
            "AllowedHeaders": ["content-type"],
            "MaxAgeSeconds": RENNICK_DIRECT_UPLOAD_TTL_SECONDS,
        }
    )
    client.put_bucket_cors(Bucket=B2_BUCKET, CORSConfiguration={"CORSRules": rules})


def _prepare_rennick_direct_supplement_upload() -> dict[str, Any]:
    """Create direct-to-B2 PDF upload URLs for the fixed supplement.

    Browser clients receive no B2 credentials and upload only to a unique pending
    prefix. A later server-side completion step validates the two B2 objects and
    promotes their exact bytes to the immutable canonical supplement keys.
    """
    client = _b2_client()
    _ensure_rennick_direct_upload_cors(client)
    upload_id = uuid.uuid4().hex
    prefix = f"{RENNICK_DIRECT_UPLOAD_PREFIX}{RENNICK_SUPPLEMENT_ID}/{upload_id}/"
    uploads = []
    for filename in sorted(RENNICK_SUPPLEMENT_FILENAMES):
        object_key = prefix + "documents/" + filename
        uploads.append(
            {
                "name": filename,
                "object_key": object_key,
                "content_type": "application/pdf",
                "url": client.generate_presigned_url(
                    "put_object",
                    Params={"Bucket": B2_BUCKET, "Key": object_key, "ContentType": "application/pdf"},
                    ExpiresIn=RENNICK_DIRECT_UPLOAD_TTL_SECONDS,
                    HttpMethod="PUT",
                ),
            }
        )
    return {
        "ok": True,
        "upload_id": upload_id,
        "expires_in_seconds": RENNICK_DIRECT_UPLOAD_TTL_SECONDS,
        "uploads": uploads,
    }


def _complete_rennick_direct_supplement_upload(upload_id: str) -> dict[str, Any]:
    """Validate pending direct uploads, then promote exact bytes immutably."""
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise ValueError("invalid direct upload id")
    prefix = f"{RENNICK_DIRECT_UPLOAD_PREFIX}{RENNICK_SUPPLEMENT_ID}/{upload_id}/"
    pending_keys = {name: prefix + "documents/" + name for name in RENNICK_SUPPLEMENT_FILENAMES}
    client = _b2_client()
    heads = {name: client.head_object(Bucket=B2_BUCKET, Key=key) for name, key in pending_keys.items()}
    if any(not 0 < head.get("ContentLength", 0) <= MAX_BUNDLE_BYTES for head in heads.values()):
        raise ValueError("invalid pending supplement document size")
    result: dict[str, Any] | None = None
    cleanup_error = None
    try:
        documents = {name: client.get_object(Bucket=B2_BUCKET, Key=key)["Body"].read() for name, key in pending_keys.items()}
        if any(len(documents[name]) != heads[name]["ContentLength"] for name in documents):
            raise ValueError("pending supplement document read size mismatch")
        archive_stream = io.BytesIO()
        with zipfile.ZipFile(archive_stream, "w", compression=zipfile.ZIP_DEFLATED) as archive_file:
            for name in sorted(documents):
                archive_file.writestr(name, documents[name])
        archive = archive_stream.getvalue()
        manifest = json.dumps({"case_id": RENNICK_CASE_ID, "supplement_id": RENNICK_SUPPLEMENT_ID, "documents": [{"filename": name, "size": len(documents[name]), "sha256": hashlib.sha256(documents[name]).hexdigest()} for name in sorted(documents)]}, sort_keys=True).encode()
        result = _upload_rennick_docket_supplement(archive, manifest)
    finally:
        # Failed validation or an immutable-key conflict must not strand legal
        # document bytes in the pending prefix.
        for key in pending_keys.values():
            try:
                client.delete_object(Bucket=B2_BUCKET, Key=key)
            except Exception as exc:
                cleanup_error = str(exc)
    if cleanup_error and result is not None:
        result["staging_cleanup_warning"] = cleanup_error
    if result is None:
        raise RuntimeError("direct supplement completion did not produce a result")
    return result


def _prepare_szymczyk_direct_intake() -> dict[str, Any]:
    """Issue one short-lived direct B2 URL for the 733 MB provisional intake."""
    client = _b2_client()
    _ensure_rennick_direct_upload_cors(client)
    upload_id = uuid.uuid4().hex
    object_key = f"{PENDING_INTAKE_PREFIX}{upload_id}/{PENDING_INTAKE_FILENAME}"
    return {
        "ok": True,
        "upload_id": upload_id,
        "filename": PENDING_INTAKE_FILENAME,
        "max_bytes": PENDING_INTAKE_MAX_BYTES,
        "expires_in_seconds": PENDING_INTAKE_TTL_SECONDS,
        "content_type": "application/octet-stream",
        "url": client.generate_presigned_url(
            "put_object",
            Params={"Bucket": B2_BUCKET, "Key": object_key, "ContentType": "application/octet-stream"},
            ExpiresIn=PENDING_INTAKE_TTL_SECONDS,
            HttpMethod="PUT",
        ),
    }


def _complete_szymczyk_direct_intake(upload_id: str) -> dict[str, Any]:
    """Stream-hash the pending object, then copy it to an immutable provisional key."""
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise ValueError("invalid direct upload id")
    pending_key = f"{PENDING_INTAKE_PREFIX}{upload_id}/{PENDING_INTAKE_FILENAME}"
    client = _b2_client()
    head = client.head_object(Bucket=B2_BUCKET, Key=pending_key)
    size = int(head.get("ContentLength") or 0)
    if not 0 < size <= PENDING_INTAKE_MAX_BYTES:
        raise ValueError("invalid pending intake size")
    digest = hashlib.sha256()
    try:
        body = client.get_object(Bucket=B2_BUCKET, Key=pending_key)["Body"]
        for chunk in iter(lambda: body.read(1024 * 1024), b""):
            digest.update(chunk)
        sha256 = digest.hexdigest()
        verified_prefix = f"pending-intakes/{PENDING_INTAKE_ID}/verified/{sha256}/"
        source_key = verified_prefix + PENDING_INTAKE_FILENAME
        manifest_key = verified_prefix + "intake_manifest.json"
        _require_absent_intake_object(client, source_key)
        _require_absent_intake_object(client, manifest_key)
        client.copy_object(
            Bucket=B2_BUCKET,
            Key=source_key,
            CopySource={"Bucket": B2_BUCKET, "Key": pending_key},
            MetadataDirective="REPLACE",
            Metadata={"sha256": sha256},
            ContentType="application/octet-stream",
        )
        manifest = json.dumps({"schema_version": "provisional-intake.v1", "intake_id": PENDING_INTAKE_ID, "filename": PENDING_INTAKE_FILENAME, "size_bytes": size, "sha256": sha256}, sort_keys=True).encode()
        client.put_object(Bucket=B2_BUCKET, Key=manifest_key, Body=manifest, ContentType="application/json", Metadata={"sha256": hashlib.sha256(manifest).hexdigest()})
        verified = client.head_object(Bucket=B2_BUCKET, Key=source_key)
        if verified.get("ContentLength") != size or (verified.get("Metadata") or {}).get("sha256") != sha256:
            raise ValueError("B2 provisional intake verification mismatch")
        return {"ok": True, "uploaded": True, "intake_id": PENDING_INTAKE_ID, "objects": [{"object_key": source_key, "size": size, "sha256": sha256}, {"object_key": manifest_key, "size": len(manifest), "sha256": hashlib.sha256(manifest).hexdigest()}]}
    finally:
        client.delete_object(Bucket=B2_BUCKET, Key=pending_key)


def _inspect_szymczyk_intake(sha256: str) -> dict[str, Any]:
    """Read-only ZIP integrity and hash inventory for one verified provisional intake."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("invalid provisional intake SHA-256")
    prefix = f"pending-intakes/{PENDING_INTAKE_ID}/verified/{sha256}/"
    source_key = prefix + PENDING_INTAKE_FILENAME
    manifest_key = prefix + "contents_manifest.json"
    client = _b2_client()
    source_head = client.head_object(Bucket=B2_BUCKET, Key=source_key)
    if (source_head.get("Metadata") or {}).get("sha256") != sha256:
        raise ValueError("provisional intake source hash metadata mismatch")
    try:
        existing = client.head_object(Bucket=B2_BUCKET, Key=manifest_key)
        return {"ok": True, "verified": True, "already_inspected": True, "intake_id": PENDING_INTAKE_ID, "manifest_object_key": manifest_key, "manifest_size": existing.get("ContentLength")}
    except ClientError as exc:
        if str(((exc.response or {}).get("Error") or {}).get("Code", "")) not in {"404", "NoSuchKey", "NotFound"}:
            raise
    with tempfile.NamedTemporaryFile(prefix="legalai-szymczyk-", suffix=".zip") as local:
        body = client.get_object(Bucket=B2_BUCKET, Key=source_key)["Body"]
        for chunk in iter(lambda: body.read(1024 * 1024), b""):
            local.write(chunk)
        local.flush()
        try:
            archive = zipfile.ZipFile(local.name)
        except zipfile.BadZipFile as exc:
            raise ValueError("provisional intake is not a valid ZIP archive") from exc
        with archive:
            files = []
            for info in archive.infolist():
                if info.is_dir():
                    continue
                digest = hashlib.sha256()
                with archive.open(info) as member:
                    for chunk in iter(lambda: member.read(1024 * 1024), b""):
                        digest.update(chunk)
                files.append({"filename": info.filename, "size_bytes": info.file_size, "compressed_size_bytes": info.compress_size, "sha256": digest.hexdigest()})
    if not files:
        raise ValueError("provisional intake ZIP contains no files")
    payload = json.dumps({"schema_version": "provisional-intake-contents.v1", "intake_id": PENDING_INTAKE_ID, "source_sha256": sha256, "source_size_bytes": source_head.get("ContentLength"), "file_count": len(files), "pdf_count": sum(item["filename"].lower().endswith(".pdf") for item in files), "files": files}, sort_keys=True).encode()
    manifest_sha256 = hashlib.sha256(payload).hexdigest()
    _require_absent_intake_object(client, manifest_key)
    client.put_object(Bucket=B2_BUCKET, Key=manifest_key, Body=payload, ContentType="application/json", Metadata={"sha256": manifest_sha256})
    head = client.head_object(Bucket=B2_BUCKET, Key=manifest_key)
    if head.get("ContentLength") != len(payload) or (head.get("Metadata") or {}).get("sha256") != manifest_sha256:
        raise ValueError("contents manifest verification mismatch")
    return {"ok": True, "verified": True, "intake_id": PENDING_INTAKE_ID, "file_count": len(files), "pdf_count": sum(item["filename"].lower().endswith(".pdf") for item in files), "manifest_object_key": manifest_key, "manifest_sha256": manifest_sha256}


_INDEX_RE = re.compile(r"(?i)\bindex\s*(?:no\.?|number)?\s*[:#]?\s*(\d{6,}/\d{4})")
_COURT_RE = re.compile(r"(?is)SUPREME\s+COURT\s+OF\s+THE\s+STATE\s+OF\s+NEW\s+YORK\s+COUNTY\s+OF\s+([A-Z ]{3,40})")
_CAPTION_RE = re.compile(r"(?is)(.{3,220}?)\s*,?\s*(?:Plaintiff|Petitioner)s?\s*,?\s*(?:-against-|v\.?|vs\.?)\s*(.{3,220}?)\s*,?\s*(?:Defendant|Respondent)s?")


def _identify_szymczyk_intake(sha256: str) -> dict[str, Any]:
    """Extract factual case identity only from first pages of verified PDFs."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("invalid provisional intake SHA-256")
    prefix = f"pending-intakes/{PENDING_INTAKE_ID}/verified/{sha256}/"
    source_key = prefix + PENDING_INTAKE_FILENAME
    manifest_key = prefix + "identification_manifest.json"
    client = _b2_client()
    try:
        existing = client.get_object(Bucket=B2_BUCKET, Key=manifest_key)["Body"].read()
        return json.loads(existing)
    except ClientError as exc:
        if str(((exc.response or {}).get("Error") or {}).get("Code", "")) not in {"404", "NoSuchKey", "NotFound"}:
            raise
    with tempfile.NamedTemporaryFile(prefix="legalai-szymczyk-id-", suffix=".zip") as local:
        body = client.get_object(Bucket=B2_BUCKET, Key=source_key)["Body"]
        for chunk in iter(lambda: body.read(1024 * 1024), b""):
            local.write(chunk)
        local.flush()
        try:
            archive = zipfile.ZipFile(local.name)
        except zipfile.BadZipFile as exc:
            raise ValueError("provisional intake is not a valid ZIP archive") from exc
        candidates: list[dict[str, str]] = []
        with archive:
            for info in sorted((item for item in archive.infolist() if not item.is_dir() and item.filename.lower().endswith(".pdf")), key=lambda item: item.filename.lower()):
                try:
                    reader = PdfReader(io.BytesIO(archive.read(info)))
                    text = (reader.pages[0].extract_text() if reader.pages else "") or ""
                except Exception:
                    continue
                index = _INDEX_RE.search(text)
                court = _COURT_RE.search(text)
                caption = _CAPTION_RE.search(" ".join(text.split()))
                if index or court or caption:
                    candidates.append({"filename": info.filename, "index_number": index.group(1) if index else "", "court": ("Supreme Court of the State of New York, County of " + " ".join(court.group(1).split()).title()) if court else "", "caption": (" ".join(caption.group(1).split()) + " v. " + " ".join(caption.group(2).split())) if caption else ""})
    if not candidates:
        raise ValueError("no case identity text found on first PDF pages")
    best = next((item for item in candidates if item["index_number"] and item["court"] and item["caption"]), candidates[0])
    payload = {"ok": True, "identified": bool(best["index_number"] and best["court"] and best["caption"]), "intake_id": PENDING_INTAKE_ID, "case_caption": best["caption"], "court": best["court"], "index_number": best["index_number"], "evidence_filename": best["filename"], "candidates": candidates[:20]}
    raw = json.dumps(payload, sort_keys=True).encode()
    _require_absent_intake_object(client, manifest_key)
    client.put_object(Bucket=B2_BUCKET, Key=manifest_key, Body=raw, ContentType="application/json", Metadata={"sha256": hashlib.sha256(raw).hexdigest()})
    return payload


def _promote_szymczyk_intake(sha256: str) -> dict[str, Any]:
    """Copy the verified Szymczyk source and factual manifests to canonical B2 keys."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("invalid provisional intake SHA-256")
    client = _b2_client()
    provisional_prefix = f"pending-intakes/{PENDING_INTAKE_ID}/verified/{sha256}/"
    canonical_prefix = f"cases/{SZYMCZYK_CASE_ID}/intake/source/{sha256}/"
    source_keys = [
        PENDING_INTAKE_FILENAME,
        "intake_manifest.json",
        "contents_manifest.json",
        "identification_manifest.json",
    ]
    source_head = client.head_object(Bucket=B2_BUCKET, Key=provisional_prefix + PENDING_INTAKE_FILENAME)
    if (source_head.get("Metadata") or {}).get("sha256") != sha256:
        raise ValueError("provisional intake source hash metadata mismatch")
    for name in source_keys[1:]:
        client.head_object(Bucket=B2_BUCKET, Key=provisional_prefix + name)
    identity = {
        "schema_version": "case-identity.v1",
        "case_id": SZYMCZYK_CASE_ID,
        "case_caption": SZYMCZYK_CASE_CAPTION,
        "court": SZYMCZYK_CASE_COURT,
        "index_number": SZYMCZYK_CASE_INDEX_NUMBER,
        "source_intake_id": PENDING_INTAKE_ID,
        "source_sha256": sha256,
        "source_filename": PENDING_INTAKE_FILENAME,
    }
    identity_bytes = json.dumps(identity, sort_keys=True).encode()
    identity_key = f"cases/{SZYMCZYK_CASE_ID}/intake/case_identity.json"
    descriptor = {
        "schema_version": "verified-case-source-descriptor.v1",
        "case_id": SZYMCZYK_CASE_ID,
        "source_sha256": sha256,
        "source_object_key": canonical_prefix + PENDING_INTAKE_FILENAME,
        "contents_manifest_key": canonical_prefix + "contents_manifest.json",
    }
    descriptor_bytes = json.dumps(descriptor, sort_keys=True).encode()
    descriptor_key = canonical_prefix + "source_descriptor.json"
    expected = [(canonical_prefix + name, provisional_prefix + name) for name in source_keys]
    existing = []
    for target, original in expected:
        try:
            target_head = client.head_object(Bucket=B2_BUCKET, Key=target)
            original_head = client.head_object(Bucket=B2_BUCKET, Key=original)
            if target_head.get("ContentLength") != original_head.get("ContentLength"):
                raise ValueError("canonical intake object size mismatch")
            existing.append(target)
        except ClientError as exc:
            if str(((exc.response or {}).get("Error") or {}).get("Code", "")) not in {"404", "NoSuchKey", "NotFound"}:
                raise
            client.copy_object(Bucket=B2_BUCKET, Key=target, CopySource={"Bucket": B2_BUCKET, "Key": original}, MetadataDirective="COPY")
    try:
        existing_identity = client.get_object(Bucket=B2_BUCKET, Key=identity_key)["Body"].read()
        if existing_identity != identity_bytes:
            raise ValueError("canonical case identity already exists with different contents")
        identity_already_present = True
    except ClientError as exc:
        if str(((exc.response or {}).get("Error") or {}).get("Code", "")) not in {"404", "NoSuchKey", "NotFound"}:
            raise
        client.put_object(Bucket=B2_BUCKET, Key=identity_key, Body=identity_bytes, ContentType="application/json", Metadata={"sha256": hashlib.sha256(identity_bytes).hexdigest()})
        identity_already_present = False
    try:
        existing_descriptor = client.get_object(Bucket=B2_BUCKET, Key=descriptor_key)["Body"].read()
        if existing_descriptor != descriptor_bytes:
            raise ValueError("canonical source descriptor already exists with different contents")
    except ClientError as exc:
        if str(((exc.response or {}).get("Error") or {}).get("Code", "")) not in {"404", "NoSuchKey", "NotFound"}:
            raise
        client.put_object(Bucket=B2_BUCKET, Key=descriptor_key, Body=descriptor_bytes, ContentType="application/json", Metadata={"sha256": hashlib.sha256(descriptor_bytes).hexdigest()})
    return {
        "ok": True,
        "promoted": True,
        "already_present": len(existing) == len(expected) and identity_already_present,
        "case_id": SZYMCZYK_CASE_ID,
        "case_caption": SZYMCZYK_CASE_CAPTION,
        "court": SZYMCZYK_CASE_COURT,
        "index_number": SZYMCZYK_CASE_INDEX_NUMBER,
        "source_sha256": sha256,
        "canonical_prefix": canonical_prefix,
        "identity_object_key": identity_key,
        "source_descriptor_key": descriptor_key,
    }


def _szymczyk_filename_category(filename: str) -> str:
    """Return a transparent, filename-only document group (not legal classification)."""
    name = filename.casefold()
    if not name.endswith(".pdf"):
        return "Other files"
    if "affidavit" in name or "affirm" in name:
        return "Affidavits / affirmations"
    if "exhibit" in name:
        return "Exhibits"
    if "notice" in name:
        return "Notices"
    if "order" in name:
        return "Orders"
    if "motion" in name or "osc" in name:
        return "Motions / orders to show cause"
    if "complaint" in name or "summons" in name or "answer" in name:
        return "Pleadings"
    if "letter" in name or "correspond" in name:
        return "Correspondence"
    return "Other PDFs"


def _inventory_szymczyk_intake(sha256: str) -> dict[str, Any]:
    """Create a factual, filename-only inventory from the canonical contents manifest."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("invalid source SHA-256")
    client = _b2_client()
    canonical_prefix = f"cases/{SZYMCZYK_CASE_ID}/intake/source/{sha256}/"
    contents_key = canonical_prefix + "contents_manifest.json"
    inventory_key = f"cases/{SZYMCZYK_CASE_ID}/intake/inventory.json"
    try:
        existing = client.get_object(Bucket=B2_BUCKET, Key=inventory_key)["Body"].read()
        return json.loads(existing)
    except ClientError as exc:
        if str(((exc.response or {}).get("Error") or {}).get("Code", "")) not in {"404", "NoSuchKey", "NotFound"}:
            raise
    contents = json.loads(client.get_object(Bucket=B2_BUCKET, Key=contents_key)["Body"].read())
    files = contents.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("canonical contents manifest has no files")
    grouped: dict[str, int] = {}
    extensions: dict[str, int] = {}
    documents: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise ValueError("canonical contents manifest has an invalid file entry")
        filename = item["filename"]
        category = _szymczyk_filename_category(filename)
        grouped[category] = grouped.get(category, 0) + 1
        suffix = pathlib.PurePosixPath(filename).suffix.casefold() or "[no extension]"
        extensions[suffix] = extensions.get(suffix, 0) + 1
        documents.append({"filename": filename, "size_bytes": item.get("size_bytes"), "category": category})
    payload = {
        "ok": True,
        "schema_version": "case-intake-inventory.v1",
        "classification": "filename-only; no document contents were read",
        "case_id": SZYMCZYK_CASE_ID,
        "source_sha256": sha256,
        "source_contents_manifest_key": contents_key,
        "file_count": len(files),
        "pdf_count": sum(1 for item in files if str(item.get("filename", "")).casefold().endswith(".pdf")),
        "groups": dict(sorted(grouped.items())),
        "extensions": dict(sorted(extensions.items())),
        "documents": sorted(documents, key=lambda item: str(item["filename"]).casefold()),
    }
    raw = json.dumps(payload, sort_keys=True).encode()
    _require_absent_intake_object(client, inventory_key)
    client.put_object(Bucket=B2_BUCKET, Key=inventory_key, Body=raw, ContentType="application/json", Metadata={"sha256": hashlib.sha256(raw).hexdigest()})
    return {
        **{key: value for key, value in payload.items() if key != "documents"},
        "inventory_object_key": inventory_key,
        "inventory_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _process_szymczyk_intake(sha256: str) -> dict[str, Any]:
    """Run the complete factual intake pipeline with one idempotent request."""
    inspection = _inspect_szymczyk_intake(sha256)
    identification = _identify_szymczyk_intake(sha256)
    promotion = _promote_szymczyk_intake(sha256)
    inventory = _inventory_szymczyk_intake(sha256)
    return {
        "ok": True,
        "pipeline": "szymczyk-intake.v1",
        "case_id": promotion["case_id"],
        "case_caption": promotion["case_caption"],
        "court": promotion["court"],
        "index_number": promotion["index_number"],
        "source_sha256": sha256,
        "stages": {
            "inspection": {"ok": inspection.get("ok"), "file_count": inspection.get("file_count"), "pdf_count": inspection.get("pdf_count"), "contents_manifest_key": inspection.get("manifest_object_key")},
            "identification": {"ok": identification.get("ok"), "evidence_filename": identification.get("evidence_filename")},
            "promotion": {"ok": promotion.get("ok"), "canonical_prefix": promotion.get("canonical_prefix")},
            "inventory": {"ok": inventory.get("ok"), "inventory_object_key": inventory.get("inventory_object_key")},
        },
    }


@mcp.tool()
async def upload_rennick_case_intake(
    source_bundle_base64: str,
    manifest_base64: str,
) -> dict[str, Any]:
    """Upload the exact Rennick B2 intake pair, hash it server-side, and refuse overwrites."""
    _require_allowed_user()
    source = decode_base64_upload(
        source_bundle_base64, label="source_bundle_base64", max_size=MAX_BUNDLE_BYTES
    )
    manifest = decode_base64_upload(
        manifest_base64, label="manifest_base64", max_size=MAX_MANIFEST_BYTES
    )
    return _upload_rennick_intake_pair(source, manifest)


@mcp.custom_route("/intake/rennick/promote", methods=["POST"])
async def promote_rennick_intake(request: Request) -> JSONResponse:
    expected = normalize_bearer_token(os.environ.get(BRIDGE_SERVICE_TOKEN_ENV))
    provided = normalize_bearer_token(request.headers.get("authorization"))
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        return JSONResponse(_promote_rennick_intake())
    except (ValueError, ClientError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@mcp.custom_route("/intake/rennick/upload", methods=["POST"])
async def upload_rennick_intake_binary(request: Request) -> JSONResponse:
    """Private Gateway-to-Bridge binary intake route (never browser-public)."""
    expected = normalize_bearer_token(os.environ.get(BRIDGE_SERVICE_TOKEN_ENV))
    provided = normalize_bearer_token(request.headers.get("authorization"))
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        source_size = int(request.headers.get("X-Rennick-Source-Size", "0"))
    except ValueError:
        source_size = 0
    body = await request.body()
    if not 0 < source_size <= MAX_BUNDLE_BYTES or source_size >= len(body):
        return JSONResponse({"ok": False, "error": "invalid_source_size"}, status_code=400)
    source, manifest = body[:source_size], body[source_size:]
    if len(manifest) > MAX_MANIFEST_BYTES:
        return JSONResponse({"ok": False, "error": "invalid_manifest_size"}, status_code=400)
    try:
        return JSONResponse(_upload_rennick_intake_pair(source, manifest))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@mcp.custom_route("/intake/rennick/supplement", methods=["POST"])
async def upload_rennick_docket_supplement_binary(request: Request) -> JSONResponse:
    """Private Gateway-to-Bridge route for the fixed three-document supplement."""
    expected = normalize_bearer_token(os.environ.get(BRIDGE_SERVICE_TOKEN_ENV))
    provided = normalize_bearer_token(request.headers.get("authorization"))
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        archive_size = int(request.query_params.get("archive_size") or request.headers.get("X-Rennick-Supplement-Archive-Size", "0"))
    except ValueError:
        archive_size = 0
    body = await request.body()
    if not 0 < archive_size <= MAX_BUNDLE_BYTES or archive_size >= len(body):
        return JSONResponse({"ok": False, "error": "invalid_supplement_archive_size"}, status_code=400)
    archive, manifest = body[:archive_size], body[archive_size:]
    if len(manifest) > MAX_MANIFEST_BYTES:
        return JSONResponse({"ok": False, "error": "invalid_supplement_manifest_size"}, status_code=400)
    try:
        return JSONResponse(_upload_rennick_docket_supplement(archive, manifest))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@mcp.custom_route("/intake/rennick/supplement/direct/prepare", methods=["POST"])
async def prepare_rennick_direct_supplement_upload(request: Request) -> JSONResponse:
    expected = normalize_bearer_token(os.environ.get(BRIDGE_SERVICE_TOKEN_ENV))
    provided = normalize_bearer_token(request.headers.get("authorization"))
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return JSONResponse(_prepare_rennick_direct_supplement_upload())


@mcp.custom_route("/intake/rennick/supplement/direct/complete", methods=["POST"])
async def complete_rennick_direct_supplement_upload(request: Request) -> JSONResponse:
    expected = normalize_bearer_token(os.environ.get(BRIDGE_SERVICE_TOKEN_ENV))
    provided = normalize_bearer_token(request.headers.get("authorization"))
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
        return JSONResponse(_complete_rennick_direct_supplement_upload(str(payload.get("upload_id", ""))))
    except (ValueError, ClientError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@mcp.custom_route("/cases/verified/read-pages", methods=["POST"])
async def read_verified_case_pages(request: Request) -> JSONResponse:
    """Authenticate and validate a bounded generic case-page request."""
    expected = normalize_bearer_token(os.environ.get(BRIDGE_SERVICE_TOKEN_ENV))
    provided = normalize_bearer_token(request.headers.get("authorization"))
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
        case_id = str(payload.get("case_id", ""))
        source_sha256 = str(payload.get("source_sha256", ""))
        document_name, pages = validate_page_request(str(payload.get("document_name", "")), payload.get("pages"))
        prefix, manifest = read_verified_manifest(_b2_client(), B2_BUCKET, case_id, source_sha256)
        filenames = {str(item.get("filename", "")) for item in manifest["files"] if isinstance(item, dict)}
        if document_name not in filenames:
            raise ValueError("document is not in the verified source manifest")
        descriptor = json.loads(_b2_client().get_object(Bucket=B2_BUCKET, Key=prefix + "source_descriptor.json")["Body"].read())
        source_key = str(descriptor.get("source_object_key", ""))
        if not source_key.startswith(prefix):
            raise ValueError("verified source descriptor is invalid")
        return JSONResponse({"ok": True, "case_id": case_id, "source_sha256": source_sha256, "document_name": document_name, "pages": extract_pdf_pages_from_object(_b2_client(), B2_BUCKET, source_key, document_name, pages)})
    except (TypeError, ValueError, KeyError, ClientError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@mcp.custom_route("/cases/verified/search", methods=["POST"])
async def search_verified_case(request: Request) -> JSONResponse:
    """Search a prebuilt immutable page index and return exact citations only."""
    expected = normalize_bearer_token(os.environ.get(BRIDGE_SERVICE_TOKEN_ENV))
    provided = normalize_bearer_token(request.headers.get("authorization"))
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
        case_id, source_sha256 = str(payload.get("case_id", "")), str(payload.get("source_sha256", ""))
        query, limit = str(payload.get("query", "")), int(payload.get("limit", 20))
        prefix, _ = read_verified_manifest(_b2_client(), B2_BUCKET, case_id, source_sha256)
        raw = _b2_client().get_object(Bucket=B2_BUCKET, Key=prefix + "page_records.jsonl")["Body"].read()
        return JSONResponse({"ok": True, "case_id": case_id, "source_sha256": source_sha256, "results": search_index_jsonl(raw, query, limit)})
    except (TypeError, ValueError, KeyError, ClientError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@mcp.custom_route("/cases/verified/build-index", methods=["POST"])
async def build_verified_case_index(request: Request) -> JSONResponse:
    """Create the immutable page-text index once for a promoted source."""
    expected = normalize_bearer_token(os.environ.get(BRIDGE_SERVICE_TOKEN_ENV))
    provided = normalize_bearer_token(request.headers.get("authorization"))
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
        case_id, source_sha256 = str(payload.get("case_id", "")), str(payload.get("source_sha256", ""))
        return JSONResponse(_build_verified_case_index(case_id, source_sha256))
    except (TypeError, ValueError, KeyError, ClientError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


def _build_verified_case_index(case_id: str, source_sha256: str) -> dict[str, Any]:
    client = _b2_client(); prefix, manifest = read_verified_manifest(client, B2_BUCKET, case_id, source_sha256); index_key = prefix + "page_records.jsonl"
    try:
        existing = client.head_object(Bucket=B2_BUCKET, Key=index_key)
        if int(existing.get("ContentLength", 0)) > 0:
            return {"ok": True, "already_present": True, "index_key": index_key}
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey", "NotFound"}: raise
    descriptor = json.loads(client.get_object(Bucket=B2_BUCKET, Key=prefix + "source_descriptor.json")["Body"].read()); source_key = str(descriptor.get("source_object_key", ""))
    if not source_key.startswith(prefix): raise ValueError("verified source descriptor is invalid")
    body = build_page_records(client, B2_BUCKET, source_key, manifest)
    # B2's S3-compatible endpoint does not accept the conditional PUT header;
    # the HEAD check above preserves the no-overwrite rule for this startup job.
    client.put_object(Bucket=B2_BUCKET, Key=index_key, Body=body, ContentType="application/x-ndjson")
    return {"ok": True, "created": True, "index_key": index_key, "bytes": len(body)}


@mcp.custom_route("/intake/szymczyk/direct/prepare", methods=["POST"])
async def prepare_szymczyk_direct_intake(request: Request) -> JSONResponse:
    expected = normalize_bearer_token(os.environ.get(BRIDGE_SERVICE_TOKEN_ENV))
    provided = normalize_bearer_token(request.headers.get("authorization"))
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return JSONResponse(_prepare_szymczyk_direct_intake())


@mcp.custom_route("/intake/szymczyk/direct/complete", methods=["POST"])
async def complete_szymczyk_direct_intake(request: Request) -> JSONResponse:
    expected = normalize_bearer_token(os.environ.get(BRIDGE_SERVICE_TOKEN_ENV))
    provided = normalize_bearer_token(request.headers.get("authorization"))
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
        return JSONResponse(_complete_szymczyk_direct_intake(str(payload.get("upload_id", ""))))
    except (ValueError, ClientError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@mcp.custom_route("/intake/szymczyk/inspect", methods=["POST"])
async def inspect_szymczyk_intake(request: Request) -> JSONResponse:
    expected = normalize_bearer_token(os.environ.get(BRIDGE_SERVICE_TOKEN_ENV))
    provided = normalize_bearer_token(request.headers.get("authorization"))
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
        return JSONResponse(_inspect_szymczyk_intake(str(payload.get("sha256", ""))))
    except (ValueError, ClientError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@mcp.custom_route("/intake/szymczyk/identify", methods=["POST"])
async def identify_szymczyk_intake(request: Request) -> JSONResponse:
    expected = normalize_bearer_token(os.environ.get(BRIDGE_SERVICE_TOKEN_ENV))
    provided = normalize_bearer_token(request.headers.get("authorization"))
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
        return JSONResponse(_identify_szymczyk_intake(str(payload.get("sha256", ""))))
    except (ValueError, ClientError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@mcp.custom_route("/intake/szymczyk/promote", methods=["POST"])
async def promote_szymczyk_intake(request: Request) -> JSONResponse:
    expected = normalize_bearer_token(os.environ.get(BRIDGE_SERVICE_TOKEN_ENV))
    provided = normalize_bearer_token(request.headers.get("authorization"))
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
        return JSONResponse(_promote_szymczyk_intake(str(payload.get("sha256", ""))))
    except (ValueError, ClientError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@mcp.custom_route("/intake/szymczyk/inventory", methods=["POST"])
async def inventory_szymczyk_intake(request: Request) -> JSONResponse:
    expected = normalize_bearer_token(os.environ.get(BRIDGE_SERVICE_TOKEN_ENV))
    provided = normalize_bearer_token(request.headers.get("authorization"))
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
        return JSONResponse(_inventory_szymczyk_intake(str(payload.get("sha256", ""))))
    except (ValueError, ClientError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@mcp.custom_route("/intake/szymczyk/process", methods=["POST"])
async def process_szymczyk_intake(request: Request) -> JSONResponse:
    expected = normalize_bearer_token(os.environ.get(BRIDGE_SERVICE_TOKEN_ENV))
    provided = normalize_bearer_token(request.headers.get("authorization"))
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
        return JSONResponse(_process_szymczyk_intake(str(payload.get("sha256", ""))))
    except (ValueError, ClientError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@mcp.tool()
async def archive_case00_attorney_feedback(
    evaluation_date: str,
    original_packet_md: str,
    feedback_email_md: str,
    structured_evaluation_json: str,
) -> dict[str, Any]:
    """Archive and HEAD-verify one fixed Case-00 attorney-feedback package in B2."""
    archived_by = _require_allowed_user()
    archive_id, items = build_attorney_review_archive(
        evaluation_date=evaluation_date,
        original_packet_md=original_packet_md,
        feedback_email_md=feedback_email_md,
        structured_evaluation_json=structured_evaluation_json,
        archived_by=archived_by,
    )
    client = _b2_client()
    verified_objects = []
    for item in items:
        client.put_object(
            Bucket=B2_BUCKET,
            Key=item["object_key"],
            Body=item["payload"],
            ContentType=item["content_type"],
            Metadata={"sha256": item["sha256"]},
        )
        head = client.head_object(Bucket=B2_BUCKET, Key=item["object_key"])
        if head.get("ContentLength") != len(item["payload"]):
            raise ValueError(f"B2 size mismatch for {item['object_key']}")
        if (head.get("Metadata") or {}).get("sha256") != item["sha256"]:
            raise ValueError(f"B2 SHA-256 metadata mismatch for {item['object_key']}")
        verified_objects.append(
            {
                "filename": item["filename"],
                "object_key": item["object_key"],
                "size": head["ContentLength"],
                "etag": (head.get("ETag") or "").strip('"'),
                "sha256": item["sha256"],
            }
        )
    return {
        "ok": True,
        "verified": True,
        "archive_id": archive_id,
        "b2_bucket": B2_BUCKET,
        "objects": verified_objects,
    }


def _b2_object_exists(client: Any, object_key: str) -> bool:
    try:
        client.head_object(Bucket=B2_BUCKET, Key=object_key)
    except ClientError as exc:
        code = str((exc.response or {}).get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    return True


def _put_archive_object_create_only(client: Any, item: dict[str, Any]) -> None:
    """Put one archive object with B2-compatible PutObject parameters.

    Callers must run assert_archive_objects_absent first. B2 rejects
    IfNoneMatch on PutObject, so collision fail-closed is the preflight.
    """
    metadata = item.get("b2_metadata")
    if not isinstance(metadata, dict):
        metadata = {"sha256": item["sha256"]}
    try:
        client.put_object(
            Bucket=B2_BUCKET,
            Key=item["object_key"],
            Body=item["payload"],
            ContentType=item["content_type"],
            Metadata=metadata,
            **archive_create_only_put_params(),
        )
    except ClientError as exc:
        response = exc.response or {}
        error = response.get("Error", {})
        mapped = map_archive_put_precondition_failure(
            object_key=item["object_key"],
            error_code=str(error.get("Code", "")),
            http_status_code=response.get("ResponseMetadata", {}).get(
                "HTTPStatusCode"
            ),
        )
        if mapped is not None:
            raise mapped from exc
        raise


@mcp.tool()
async def archive_case00_review_packet(
    docx_base64: str,
    recipient: str,
    question_id: str,
    sent_at: str,
    original_filename: str,
) -> dict[str, Any]:
    """Archive and HEAD-verify one Case-00 attorney review-packet DOCX in B2."""
    archived_by = _require_allowed_user()
    archive_id, items = build_review_packet_archive(
        docx_base64=docx_base64,
        recipient=recipient,
        question_id=question_id,
        sent_at=sent_at,
        original_filename=original_filename,
        archived_by=archived_by,
    )
    client = _b2_client()
    # Fail closed if any canonical target already exists; do not overwrite.
    assert_archive_objects_absent(
        items,
        object_exists=lambda key: _b2_object_exists(client, key),
    )
    verified_objects = []
    # DOCX first, manifest last. Any failure after a successful put leaves a
    # partial archive; reruns fail closed via the existing-object preflight.
    for item in items:
        _put_archive_object_create_only(client, item)
        head = client.head_object(Bucket=B2_BUCKET, Key=item["object_key"])
        if head.get("ContentLength") != len(item["payload"]):
            raise ValueError(
                f"B2 size mismatch for {item['object_key']} "
                "(archive incomplete; rerun rejected until objects are absent)"
            )
        if (head.get("Metadata") or {}).get("sha256") != item["sha256"]:
            raise ValueError(
                f"B2 SHA-256 metadata mismatch for {item['object_key']} "
                "(archive incomplete; rerun rejected until objects are absent)"
            )
        verified_objects.append(
            {
                "filename": item["filename"],
                "object_key": item["object_key"],
                "size": head["ContentLength"],
                "etag": (head.get("ETag") or "").strip('"'),
                "sha256": item["sha256"],
            }
        )
    if len(verified_objects) != len(items):
        raise ValueError("review packet archive incomplete; refusing verified result")
    return {
        "ok": True,
        "verified": True,
        "archive_id": archive_id,
        "b2_bucket": B2_BUCKET,
        "objects": verified_objects,
    }


def _head_acceptance_contract_metadata(
    client: Any, object_key: str
) -> dict[str, Any] | None:
    """Return HEAD metadata for an acceptance-contract object, or None if absent."""
    try:
        head = client.head_object(Bucket=B2_BUCKET, Key=object_key)
    except ClientError as exc:
        code = str((exc.response or {}).get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    meta = head.get("Metadata") or {}
    contract_sha256 = meta.get("contract_sha256")
    object_sha256 = meta.get("object_sha256") or meta.get("sha256")
    return {
        "object_key": object_key,
        "size": head.get("ContentLength"),
        "etag": (head.get("ETag") or "").strip('"'),
        "contract_sha256": contract_sha256,
        "object_sha256": object_sha256,
        # Legacy alias kept for older objects that only stored sha256.
        "sha256": object_sha256,
    }


def _acceptance_contract_integrity_ok(
    *,
    size: object,
    expected_size: int,
    stored_contract_sha256: object,
    expected_contract_sha256: str,
    stored_object_sha256: object,
    expected_object_sha256: str,
) -> tuple[bool, bool, bool, bool]:
    size_ok = size == expected_size
    contract_ok = stored_contract_sha256 == expected_contract_sha256
    object_ok = stored_object_sha256 == expected_object_sha256
    verified = bool(size_ok and contract_ok and object_ok)
    return size_ok, contract_ok, object_ok, verified


@mcp.tool()
async def get_acceptance_contract_template() -> dict[str, Any]:
    """Return the acceptance_contract.v1 schema, hashing rules, and synthetic example.

    Read-only. No B2 writes. Example uses wholly generic synthetic IDs only.
    """
    _require_allowed_user()
    return build_acceptance_contract_template()


@mcp.tool()
async def get_acceptance_contract(
    benchmark_id: str,
    question_id: str,
    contract_id: str,
    version: str,
) -> dict[str, Any]:
    """Fetch and verify one LegalAI acceptance_contract.v1 JSON object from B2.

    Accepts only bounded identity fields. The canonical B2 object key is
    generated server-side under the acceptance-contracts prefix — never accept
    arbitrary object keys, buckets, prefixes, URLs, or filesystem paths.

    Before returning structured contract JSON, verifies canonical key/identity,
    byte size, embedded content_sha256/contract_sha256, and independently
    computed object_sha256 against B2 metadata. Fail closed on any mismatch.
    Contract bodies and credentials are never logged.
    """
    _require_allowed_user()
    assert_canonical_legalai_bucket(B2_BUCKET)
    requested = resolve_acceptance_contract_retrieval_key(
        benchmark_id=benchmark_id,
        question_id=question_id,
        contract_id=contract_id,
        version=version,
    )
    client = _b2_client()
    object_key = requested["object_key"]
    head = _head_acceptance_contract_metadata(client, object_key)
    if (
        head is None
        and requested["benchmark_id"] != requested["benchmark_id"].casefold()
    ):
        response = client.list_objects_v2(
            Bucket=B2_BUCKET, Prefix=ACCEPTANCE_CONTRACT_PREFIX, MaxKeys=200
        )
        if response.get("IsTruncated"):
            raise ValueError(
                "acceptance-contract lookup is ambiguous: listing truncated"
            )
        suffix = (
            f"/{requested['question_id']}/{requested['contract_id']}"
            f"/v{requested['version']}/acceptance_contract.json"
        )
        candidates = [
            str(item.get("Key", ""))
            for item in response.get("Contents", [])
            if str(item.get("Key", "")).startswith(ACCEPTANCE_CONTRACT_PREFIX)
            and str(item.get("Key", "")).endswith(suffix)
            and str(item.get("Key", ""))[
                len(ACCEPTANCE_CONTRACT_PREFIX) : -len(suffix)
            ].casefold()
            == requested["benchmark_id"].casefold()
        ]
        if len(candidates) > 1:
            raise ValueError("acceptance-contract lookup is ambiguous")
        if candidates:
            object_key = candidates[0]
            head = _head_acceptance_contract_metadata(client, object_key)
    if head is None:
        return {
            "ok": False,
            "verified": False,
            "b2_bucket": B2_BUCKET,
            "prefix": ACCEPTANCE_CONTRACT_PREFIX,
            "schema_version": "acceptance_contract.v1",
            "benchmark_id": requested["benchmark_id"],
            "question_id": requested["question_id"],
            "contract_id": requested["contract_id"],
            "version": requested["version"],
            "object_key": object_key,
            "error": "object_not_found",
        }

    size = head.get("size")
    if not isinstance(size, int) or isinstance(size, bool):
        raise ValueError(f"B2 acceptance-contract size missing for {object_key}")
    if size < 1 or size > MAX_ACCEPTANCE_CONTRACT_BYTES:
        raise ValueError(
            f"B2 acceptance-contract size out of bounds for {object_key}"
        )

    response = client.get_object(Bucket=B2_BUCKET, Key=object_key)
    stream = response["Body"]
    try:
        body = stream.read(MAX_ACCEPTANCE_CONTRACT_BYTES + 1)
    finally:
        stream.close()
    if len(body) != size:
        raise ValueError(f"B2 body size mismatch for {object_key}")
    if len(body) > MAX_ACCEPTANCE_CONTRACT_BYTES:
        raise ValueError(
            f"acceptance-contract exceeds {MAX_ACCEPTANCE_CONTRACT_BYTES}-byte limit"
        )

    verified = verify_retrieved_acceptance_contract(
        payload=body,
        benchmark_id=requested["benchmark_id"],
        question_id=requested["question_id"],
        contract_id=requested["contract_id"],
        version=requested["version"],
        expected_size=size,
        stored_contract_sha256=head.get("contract_sha256"),
        stored_object_sha256=head.get("object_sha256"),
        resolved_object_key=object_key,
    )
    return {
        **verified,
        "b2_bucket": B2_BUCKET,
        "etag": head.get("etag"),
    }


@mcp.tool()
async def archive_acceptance_contract(
    contract: dict[str, Any] | None = None,
    contract_json_base64: str = "",
    expected_benchmark_id: str = "",
    expected_question_id: str = "",
    expected_contract_id: str = "",
    expected_version: str = "",
    expected_contract_sha256: str = "",
    expected_sha256: str = "",
    expected_object_key: str = "",
) -> dict[str, Any]:
    """Archive and HEAD-verify one LegalAI acceptance_contract.v1 JSON object in B2.

    Preferred: pass ``contract`` as a structured JSON object (e.g. template
    ``example`` directly). The server validates acceptance_contract.v1, computes
    ``contract_sha256`` (excluding ``content_sha256``), generates the canonical
    object key, serializes stored bytes deterministically, and computes
    ``object_sha256``. No client Base64, Web Crypto, or hash/key work required.

    Legacy: ``contract_json_base64`` plus expected nested identity /
    ``expected_contract_sha256`` remains optional backward compatibility.
    ``verified`` requires HEAD size plus both integrity checks.
    """
    _require_allowed_user()
    assert_canonical_legalai_bucket(B2_BUCKET)
    item = build_acceptance_contract_archive(
        contract=contract,
        contract_json_base64=contract_json_base64 or None,
        expected_benchmark_id=expected_benchmark_id or None,
        expected_question_id=expected_question_id or None,
        expected_contract_id=expected_contract_id or None,
        expected_version=expected_version or None,
        expected_contract_sha256=expected_contract_sha256 or None,
        expected_sha256=expected_sha256 or None,
        expected_object_key=expected_object_key or None,
    )
    client = _b2_client()
    existing = _head_acceptance_contract_metadata(client, item["object_key"])
    if existing is not None:
        size_ok, contract_ok, object_ok, verified = _acceptance_contract_integrity_ok(
            size=existing.get("size"),
            expected_size=item["size"],
            stored_contract_sha256=existing.get("contract_sha256"),
            expected_contract_sha256=item["contract_sha256"],
            stored_object_sha256=existing.get("object_sha256"),
            expected_object_sha256=item["object_sha256"],
        )
        if verified:
            return {
                "ok": True,
                "verified": True,
                "already_present": True,
                "b2_bucket": B2_BUCKET,
                "prefix": ACCEPTANCE_CONTRACT_PREFIX,
                "schema": item["schema"],
                "schema_version": item["schema_version"],
                "contract_id": item["contract_id"],
                "version": item["version"],
                "benchmark_id": item["benchmark_id"],
                "question_id": item["question_id"],
                "object_key": item["object_key"],
                "size": existing["size"],
                "etag": existing["etag"],
                "contract_sha256": existing.get("contract_sha256"),
                "object_sha256": existing.get("object_sha256"),
                "content_sha256": item["content_sha256"],
                "size_match": size_ok,
                "contract_sha256_match": contract_ok,
                "object_sha256_match": object_ok,
            }
        raise ValueError(
            f"archive object already exists with different content: {item['object_key']}"
        )

    assert_archive_objects_absent(
        [item],
        object_exists=lambda key: _b2_object_exists(client, key),
    )
    _put_archive_object_create_only(client, item)
    head = client.head_object(Bucket=B2_BUCKET, Key=item["object_key"])
    meta = head.get("Metadata") or {}
    size_ok, contract_ok, object_ok, verified = _acceptance_contract_integrity_ok(
        size=head.get("ContentLength"),
        expected_size=item["size"],
        stored_contract_sha256=meta.get("contract_sha256"),
        expected_contract_sha256=item["contract_sha256"],
        stored_object_sha256=meta.get("object_sha256") or meta.get("sha256"),
        expected_object_sha256=item["object_sha256"],
    )
    if not verified:
        raise ValueError(
            f"B2 acceptance-contract integrity mismatch for {item['object_key']}"
        )
    return {
        "ok": True,
        "verified": True,
        "already_present": False,
        "b2_bucket": B2_BUCKET,
        "prefix": ACCEPTANCE_CONTRACT_PREFIX,
        "schema": item["schema"],
        "schema_version": item["schema_version"],
        "contract_id": item["contract_id"],
        "version": item["version"],
        "benchmark_id": item["benchmark_id"],
        "question_id": item["question_id"],
        "object_key": item["object_key"],
        "size": head["ContentLength"],
        "etag": (head.get("ETag") or "").strip('"'),
        "contract_sha256": item["contract_sha256"],
        "object_sha256": item["object_sha256"],
        "content_sha256": item["content_sha256"],
        "size_match": size_ok,
        "contract_sha256_match": contract_ok,
        "object_sha256_match": object_ok,
    }


@mcp.tool()
async def verify_acceptance_contract(
    object_key: str,
    expected_contract_sha256: str,
    expected_object_sha256: str,
    expected_size: int,
) -> dict[str, Any]:
    """HEAD-verify one acceptance-contract by key/size/contract_sha256/object_sha256."""
    _require_allowed_user()
    assert_canonical_legalai_bucket(B2_BUCKET)
    key = validate_acceptance_contract_object_key(object_key)
    contract_digest = validate_sha256_hex(
        expected_contract_sha256, label="expected_contract_sha256"
    )
    object_digest = validate_sha256_hex(
        expected_object_sha256, label="expected_object_sha256"
    )
    if not isinstance(expected_size, int) or isinstance(expected_size, bool):
        raise ValueError("expected_size must be an integer")
    if expected_size < 1 or expected_size > 2 * 1024 * 1024:
        raise ValueError("expected_size must be between 1 and 2097152")

    head = _head_acceptance_contract_metadata(_b2_client(), key)
    if head is None:
        return {
            "ok": False,
            "verified": False,
            "b2_bucket": B2_BUCKET,
            "object_key": key,
            "error": "object_not_found",
        }
    size_ok, contract_ok, object_ok, verified = _acceptance_contract_integrity_ok(
        size=head.get("size"),
        expected_size=expected_size,
        stored_contract_sha256=head.get("contract_sha256"),
        expected_contract_sha256=contract_digest,
        stored_object_sha256=head.get("object_sha256"),
        expected_object_sha256=object_digest,
    )
    return {
        "ok": verified,
        "verified": verified,
        "b2_bucket": B2_BUCKET,
        "prefix": ACCEPTANCE_CONTRACT_PREFIX,
        "object_key": key,
        "size": head.get("size"),
        "etag": head.get("etag"),
        "contract_sha256": head.get("contract_sha256"),
        "object_sha256": head.get("object_sha256"),
        "size_match": size_ok,
        "contract_sha256_match": contract_ok,
        "object_sha256_match": object_ok,
    }


@mcp.tool()
async def list_acceptance_contracts(max_keys: int = 200) -> dict[str, Any]:
    """List safe metadata for objects under the acceptance-contracts prefix."""
    _require_allowed_user()
    assert_canonical_legalai_bucket(B2_BUCKET)
    if max_keys < 1 or max_keys > 200:
        raise ValueError("max_keys must be between 1 and 200")
    prefix = ACCEPTANCE_CONTRACT_PREFIX
    response = _b2_client().list_objects_v2(
        Bucket=B2_BUCKET, Prefix=prefix, MaxKeys=max_keys
    )
    objects = [
        {
            "object_key": item["Key"],
            "size": item["Size"],
            "etag": (item.get("ETag") or "").strip('"'),
            "last_modified": item["LastModified"].isoformat(),
        }
        for item in response.get("Contents", [])
    ]
    return {
        "ok": True,
        "b2_bucket": B2_BUCKET,
        "prefix": prefix,
        "schema_version": "acceptance_contract.v1",
        "objects": objects,
        "count": len(objects),
        "truncated": bool(response.get("IsTruncated")),
    }


@mcp.tool()
async def get_artifacts(mission_id: str) -> dict[str, Any]:
    """Publish the proof JSON to B2, verify it, and return the durable object key."""
    _require_allowed_user()
    run = await _resolve_run(mission_id)
    if run is None:
        return {"ok": False, "mission_id": mission_id, "error": "run_not_found"}
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return {"ok": False, "mission_id": mission_id, "error": "run_not_successful", "status": run.get("status"), "conclusion": run.get("conclusion")}

    listing = await _github(
        "GET", f"/repos/{REPOSITORY}/actions/runs/{run['id']}/artifacts"
    )
    artifacts = listing.json().get("artifacts", [])
    artifact = next((a for a in artifacts if a.get("name") == f"hal-proof-{mission_id}"), None)
    if artifact is None:
        return {"ok": False, "mission_id": mission_id, "error": "artifact_not_found"}

    archive = await _github(
        "GET", f"/repos/{REPOSITORY}/actions/artifacts/{artifact['id']}/zip"
    )
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        payload = bundle.read("proof.json")
    parsed = json.loads(payload)
    if parsed.get("mission_id") != mission_id:
        raise ValueError("artifact mission_id mismatch")

    key = f"{B2_PREFIX.strip('/')}/{mission_id}/proof.json"
    client = _b2_client()
    client.put_object(Bucket=B2_BUCKET, Key=key, Body=payload, ContentType="application/json")
    verified = client.head_object(Bucket=B2_BUCKET, Key=key)
    return {
        "ok": True,
        "mission_id": mission_id,
        "run_id": run["id"],
        "head_sha": parsed.get("sha"),
        "b2_bucket": B2_BUCKET,
        "b2_object_key": key,
        "verified": True,
        "content_length": verified["ContentLength"],
        "etag": verified.get("ETag", "").strip('"'),
    }


async def _verify_case00_artifacts(mission_id: str) -> dict[str, Any]:
    """HEAD-verify durable Case-00 candidate artifacts for mission_id."""
    run = await _resolve_case00_run(mission_id)
    if run is None:
        return {"ok": False, "mission_id": mission_id, "error": "run_not_found"}
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return {
            "ok": False,
            "mission_id": mission_id,
            "error": "run_not_successful",
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
        }

    listing = await _github(
        "GET", f"/repos/{REPOSITORY}/actions/runs/{run['id']}/artifacts"
    )
    artifacts = listing.json().get("artifacts", [])
    artifact = None
    question_token: str | None = None
    for candidate in artifacts:
        name = candidate.get("name") or ""
        token = parse_case00_question_token(name, mission_id)
        if token is not None:
            artifact = candidate
            question_token = token
            break
    if artifact is None or question_token is None:
        return {"ok": False, "mission_id": mission_id, "error": "artifact_not_found"}

    archive = await _github(
        "GET", f"/repos/{REPOSITORY}/actions/artifacts/{artifact['id']}/zip"
    )
    result_name = f"case00-{question_token}-result.json"
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        payload = json.loads(bundle.read(result_name))

    durable = payload.get("durable_artifacts") or {}
    objects = durable.get("objects") or []
    if not payload.get("ok") or not case00_durable_objects_complete(
        objects, question_token
    ):
        return {
            "ok": False,
            "mission_id": mission_id,
            "error": "durable_result_incomplete",
        }

    client = _b2_client()
    verified_objects = []
    for item in objects:
        key = item.get("object_key")
        if not isinstance(key, str) or not key.startswith(
            "Benchmarks/Case-00-Triborough/derived/attorney-feedback-eval/candidate-answers/"
        ):
            raise ValueError("artifact object key escaped the canonical Case-00 prefix")
        head = client.head_object(Bucket=durable["bucket"], Key=key)
        if head.get("ContentLength") != item.get("size"):
            raise ValueError(f"B2 size mismatch for {key}")
        verified_objects.append(
            {
                "filename": item.get("filename"),
                "object_key": key,
                "size": head.get("ContentLength"),
                "etag": (head.get("ETag") or "").strip('"'),
            }
        )

    return {
        "ok": True,
        "mission_id": mission_id,
        "run_id": run["id"],
        "verified": True,
        "b2_bucket": durable["bucket"],
        "object_keys": [item["object_key"] for item in verified_objects],
        "objects": verified_objects,
    }


@mcp.tool()
async def get_case00_q1_artifacts(mission_id: str) -> dict[str, Any]:
    """Return and independently HEAD-verify five durable Case-00 Q1 B2 objects."""
    _require_allowed_user()
    return await _verify_case00_artifacts(mission_id)


@mcp.tool()
async def get_case00_artifacts(mission_id: str) -> dict[str, Any]:
    """List and HEAD-verify durable Case-00 B2 objects for mission_id."""
    _require_allowed_user()
    return await _verify_case00_artifacts(mission_id)


@mcp.tool()
async def get_case_artifact(
    mission_id: str,
    filename: str,
) -> dict[str, Any]:
    """Read one allowlisted B2 artifact correlated to a successful case mission.

    Question is taken from the Bridge Case-00 run / verified B2 object set.
    Allowed basenames are exactly ``Q<N>_candidate_answer.json``,
    ``Q<N>_candidate_answer.md`` for that mission's question, plus
    ``generation_manifest.json``, ``model_input_audit.json``, and
    ``case00_attorney_review_packet.md``.
    """
    _require_allowed_user()
    filename = assert_safe_case_artifact_basename(filename)

    # Same Bridge Case-00 identity as get_case00_artifacts / case.list_artifacts —
    # do not require registration in the generic proof get_artifacts run registry.
    run = await _resolve_case00_run(mission_id)
    if run is None:
        return {"ok": False, "mission_id": mission_id, "error": "run_not_found"}
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return {
            "ok": False,
            "mission_id": mission_id,
            "error": "run_not_successful",
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
        }

    listing = await _github(
        "GET", f"/repos/{REPOSITORY}/actions/runs/{run['id']}/artifacts"
    )
    artifacts = listing.json().get("artifacts", [])
    artifact = None
    question_token: str | None = None
    for candidate in artifacts:
        name = candidate.get("name") or ""
        token = parse_case00_question_token(name, mission_id)
        if token is not None:
            artifact = candidate
            question_token = token
            break
    if artifact is None or question_token is None:
        return {"ok": False, "mission_id": mission_id, "error": "artifact_not_found"}

    archive = await _github(
        "GET", f"/repos/{REPOSITORY}/actions/artifacts/{artifact['id']}/zip"
    )
    result_name = f"case00-{question_token}-result.json"
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        payload = json.loads(bundle.read(result_name))

    durable = payload.get("durable_artifacts") or {}
    objects = durable.get("objects") or []
    if not payload.get("ok") or not case00_durable_objects_complete(
        objects, question_token
    ):
        return {
            "ok": False,
            "mission_id": mission_id,
            "error": "durable_result_incomplete",
        }
    if durable.get("bucket") != B2_BUCKET:
        raise ValueError("artifact bucket did not match the configured private bucket")

    objects_token = question_token_from_verified_objects(objects)
    if objects_token is not None and objects_token != question_token:
        raise ValueError("durable artifact question did not match Bridge run metadata")

    allowed = allowed_case_artifact_filenames(question_token)
    if filename not in allowed:
        raise ValueError(
            "filename is not an allowlisted case artifact for this mission question"
        )

    size_limit = case_artifact_size_limit(filename)
    if size_limit is None:
        raise ValueError("filename is not an allowlisted case artifact")

    item = next((entry for entry in objects if entry.get("filename") == filename), None)
    if item is None:
        return {
            "ok": False,
            "mission_id": mission_id,
            "error": "filename_not_found",
            "filename": filename,
        }

    key = item.get("object_key")
    expected_size = item.get("size")
    if (
        not isinstance(key, str)
        or not key.startswith(CASE_ARTIFACT_PREFIX)
        or not key.endswith(f"/{filename}")
    ):
        raise ValueError("artifact object key escaped the canonical case prefix")
    if not isinstance(expected_size, int) or expected_size < 0:
        raise ValueError("artifact result contained an invalid size")
    if expected_size > size_limit:
        raise ValueError(f"artifact exceeds the {size_limit}-byte filename limit")

    client = _b2_client()
    head = client.head_object(Bucket=B2_BUCKET, Key=key)
    actual_size = head.get("ContentLength")
    if actual_size != expected_size:
        raise ValueError(f"B2 size mismatch for {key}")
    actual_etag = (head.get("ETag") or "").strip('"')
    expected_etag = item.get("etag")
    if expected_etag and actual_etag != expected_etag:
        raise ValueError(f"B2 ETag mismatch for {key}")

    response = client.get_object(Bucket=B2_BUCKET, Key=key)
    stream = response["Body"]
    try:
        body = stream.read(size_limit + 1)
    finally:
        stream.close()
    if len(body) != actual_size:
        raise ValueError(f"B2 body size mismatch for {key}")
    if len(body) > size_limit:
        raise ValueError(f"artifact exceeds the {size_limit}-byte filename limit")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("artifact content is not valid UTF-8") from exc

    content: Any
    content_type: str
    if filename.endswith(".json"):
        try:
            content = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("artifact content is not valid JSON") from exc
        content_type = "application/json"
    else:
        content = text
        content_type = "text/markdown"

    return {
        "ok": True,
        "mission_id": mission_id,
        "run_id": run["id"],
        "head_sha": run.get("head_sha"),
        "verified": True,
        "filename": filename,
        "question_id": case00_question_id_from_token(question_token),
        "b2_bucket": B2_BUCKET,
        "object_key": key,
        "size": actual_size,
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
    # One-time migration for the already verified Rennick intake. The operation
    # is create-only and idempotent, so later restarts only compare immutable
    # records; it never accepts a browser upload or overwrites B2 objects.
    try:
        result = _promote_rennick_intake()
        # Railway retains warning-level application output by default; this is
        # operational evidence for the one-time, idempotent migration.
        logger.warning("Rennick verified intake promotion result: %s", result)
    except Exception:  # noqa: BLE001 - preserve service availability and log evidence
        logger.exception("Rennick verified intake promotion failed")
    for case_id, source_sha256 in (
        (RENNICK_CASE_ID, "6394faf9d9ccdf258a061e231bf2ce9a7e27599c27e5187c4234613e876caf77"),
        (SZYMCZYK_CASE_ID, "ff8a0773d740358d56e43055f518e42b6124a4bc4fb00a39abaf85c5393568dc"),
    ):
        try:
            if case_id == SZYMCZYK_CASE_ID:
                _promote_szymczyk_intake(source_sha256)
            logger.warning("Verified case index result: %s", _build_verified_case_index(case_id, source_sha256))
        except Exception:  # noqa: BLE001 - preserve service availability and log evidence
            logger.exception("Verified case index failed for %s", case_id)
    app = create_http_app()
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
