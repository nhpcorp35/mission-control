from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
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

# The single private packet that defines canonical Case-00 question headings.
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
MAX_CASE00_QUESTION_HEADING_CHARS = 2000
_CASE00_PACKET_QUESTION_HEADING_RE = re.compile(
    r"^## (Q[1-9]\d*)\.\s+(.+?)\s*$", re.MULTILINE
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
    submit path omits them so legacy workflow defaults remain intact.
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


@mcp.tool()
async def get_case00_question(question_id: str) -> dict[str, Any]:
    """Read one verified question heading from the fixed canonical Case-00 packet.

    This is a read-only, allowlisted B2 retrieval. It returns only the requested
    ``## QN.`` heading, never the full attorney packet or an arbitrary object.
    """
    _require_allowed_user()
    qid = str(question_id or "").strip()
    if not _CASE00_QUESTION_ID_RE.fullmatch(qid):
        raise ValueError("question_id must match Q followed by a positive integer")

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
        packet = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("canonical Case-00 attorney packet is not UTF-8") from exc

    headings: dict[str, str] = {}
    for found_id, heading in _CASE00_PACKET_QUESTION_HEADING_RE.findall(packet):
        if found_id in headings:
            raise ValueError(
                "canonical Case-00 attorney packet has duplicate question headings"
            )
        headings[found_id] = heading.strip()
    question_text = headings.get(qid)
    if not question_text:
        return {
            "ok": False,
            "question_id": qid,
            "object_key": CANONICAL_CASE00_ATTORNEY_PACKET_KEY,
            "size": CANONICAL_CASE00_ATTORNEY_PACKET_SIZE,
            "sha256": CANONICAL_CASE00_ATTORNEY_PACKET_SHA256,
            "error": "question_not_found",
        }
    if len(question_text) > MAX_CASE00_QUESTION_HEADING_CHARS:
        raise ValueError("canonical Case-00 question heading exceeds size limit")
    return {
        "ok": True,
        "question_id": qid,
        "question_text": question_text,
        "object_key": CANONICAL_CASE00_ATTORNEY_PACKET_KEY,
        "size": CANONICAL_CASE00_ATTORNEY_PACKET_SIZE,
        "sha256": CANONICAL_CASE00_ATTORNEY_PACKET_SHA256,
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
    app = create_http_app()
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
