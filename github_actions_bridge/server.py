from __future__ import annotations

import asyncio
import io
import json
import os
import time
import uuid
import zipfile
from typing import Any, Iterable, Literal

import boto3
import httpx
from cryptography.fernet import Fernet
from fastmcp import FastMCP
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from starlette.requests import Request
from starlette.responses import JSONResponse

from botocore.exceptions import ClientError

from storage_policy import (
    archive_create_only_put_params,
    assert_archive_objects_absent,
    build_attorney_review_archive,
    build_review_packet_archive,
    inventory_prefix,
    map_archive_put_precondition_failure,
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
        "get_case_artifact",
        "list_case00_storage",
        "archive_case00_attorney_feedback",
        "archive_case00_review_packet",
    }
)


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


async def validate_required_production_tools() -> None:
    """Preflight: ensure required production tools are registered before serving."""
    assert_required_production_tools(await list_registered_tool_names())


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
ALLOWED_GITHUB_LOGIN = os.environ.get("ALLOWED_GITHUB_LOGIN", "nhpcorp35")
GITHUB_API = "https://api.github.com"
CASE_ARTIFACT_PREFIX = (
    "Benchmarks/Case-00-Triborough/derived/"
    "attorney-feedback-eval/candidate-answers/"
)
CASE_ARTIFACT_LIMITS = {
    "Q1_candidate_answer.json": 1_000_000,
    "Q1_candidate_answer.md": 100_000,
    "generation_manifest.json": 100_000,
    "model_input_audit.json": 100_000,
}

auth_provider = GitHubProvider(
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
        "Use get_case_artifact to read one allowlisted, mission-correlated artifact "
        "after a successful run. Case-00 storage tools expose allowlisted inventory "
        "metadata, archive a fixed attorney-feedback package, and archive one DOCX "
        "review packet under canonical B2 prefixes without accepting bucket or key "
        "inputs."
    ),
    auth=auth_provider,
)


def _require_allowed_user() -> str:
    token = get_access_token()
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


async def _resolve_case00_run(mission_id: str) -> dict[str, Any] | None:
    response = await _github(
        "GET",
        f"/repos/{REPOSITORY}/actions/workflows/{CASE00_WORKFLOW}/runs",
        params={"event": "workflow_dispatch", "per_page": 50},
    )
    marker = f"hal-case00-q1-{mission_id}"
    for run in response.json().get("workflow_runs", []):
        if marker in (run.get("display_title") or ""):
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


@mcp.tool()
async def submit_case00_q1(
    ref: str, authorization_confirmed: bool, mission_id: str | None = None
) -> dict[str, Any]:
    """Dispatch generation-only Case-00 Q1 at an exact commit SHA."""
    _require_allowed_user()
    if not authorization_confirmed:
        raise ValueError(
            "authorization_confirmed must be true before private evidence is sent"
        )
    normalized_ref = ref.strip().lower()
    if len(normalized_ref) != 40 or any(ch not in "0123456789abcdef" for ch in normalized_ref):
        raise ValueError("ref must be an exact 40-character lowercase commit SHA")
    mission_id = mission_id or str(uuid.uuid4())
    await _github(
        "POST",
        f"/repos/{REPOSITORY}/actions/workflows/{CASE00_WORKFLOW}/dispatches",
        json={
            "ref": CASE00_WORKFLOW_BRANCH,
            "inputs": {
                "mission_id": mission_id,
                "legalai_ref": normalized_ref,
                "authorization_confirmed": "true",
            },
        },
    )
    return {
        "ok": True,
        "mission_id": mission_id,
        "status": "dispatched",
        "repository": REPOSITORY,
        "requested_ref": normalized_ref,
        "workflow": CASE00_WORKFLOW,
    }


@mcp.tool()
async def get_case00_q1_run(mission_id: str) -> dict[str, Any]:
    """Return the current GitHub status for a Case-00 Q1 run."""
    _require_allowed_user()
    return _run_result(mission_id, await _resolve_case00_run(mission_id))


@mcp.tool()
async def cancel_case00_q1_run(mission_id: str) -> dict[str, Any]:
    """Cancel the Case-00 Q1 GitHub Actions run correlated with mission_id."""
    _require_allowed_user()
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
    """Create one archive object; never overwrite. Precondition failures are collisions."""
    try:
        client.put_object(
            Bucket=B2_BUCKET,
            Key=item["object_key"],
            Body=item["payload"],
            ContentType=item["content_type"],
            Metadata={"sha256": item["sha256"]},
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
    # Defense in depth: reject known collisions before create-only puts.
    assert_archive_objects_absent(
        items,
        object_exists=lambda key: _b2_object_exists(client, key),
    )
    verified_objects = []
    # DOCX first, manifest last. Any failure after a successful put leaves a
    # partial archive; reruns fail closed via preflight + IfNoneMatch='*'.
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


@mcp.tool()
async def get_case00_q1_artifacts(mission_id: str) -> dict[str, Any]:
    """Return and independently HEAD-verify the four durable Case-00 Q1 B2 objects."""
    _require_allowed_user()
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
    artifact_name = f"hal-case00-q1-{mission_id}"
    artifact = next(
        (a for a in listing.json().get("artifacts", []) if a.get("name") == artifact_name),
        None,
    )
    if artifact is None:
        return {"ok": False, "mission_id": mission_id, "error": "artifact_not_found"}

    archive = await _github(
        "GET", f"/repos/{REPOSITORY}/actions/artifacts/{artifact['id']}/zip"
    )
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        payload = json.loads(bundle.read("case00-q1-result.json"))

    durable = payload.get("durable_artifacts") or {}
    objects = durable.get("objects") or []
    if not payload.get("ok") or len(objects) != 4:
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
async def get_case_artifact(
    mission_id: str,
    filename: Literal[
        "Q1_candidate_answer.json",
        "Q1_candidate_answer.md",
        "generation_manifest.json",
        "model_input_audit.json",
    ],
) -> dict[str, Any]:
    """Read one allowlisted B2 artifact correlated to a successful case mission."""
    _require_allowed_user()
    size_limit = CASE_ARTIFACT_LIMITS.get(filename)
    if size_limit is None:
        raise ValueError("filename is not an allowlisted case artifact")

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
    artifact_name = f"hal-case00-q1-{mission_id}"
    artifact = next(
        (a for a in listing.json().get("artifacts", []) if a.get("name") == artifact_name),
        None,
    )
    if artifact is None:
        return {"ok": False, "mission_id": mission_id, "error": "artifact_not_found"}

    archive = await _github(
        "GET", f"/repos/{REPOSITORY}/actions/artifacts/{artifact['id']}/zip"
    )
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        payload = json.loads(bundle.read("case00-q1-result.json"))

    durable = payload.get("durable_artifacts") or {}
    objects = durable.get("objects") or []
    if not payload.get("ok") or len(objects) != 4:
        return {
            "ok": False,
            "mission_id": mission_id,
            "error": "durable_result_incomplete",
        }
    if durable.get("bucket") != B2_BUCKET:
        raise ValueError("artifact bucket did not match the configured private bucket")

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
        "b2_bucket": B2_BUCKET,
        "object_key": key,
        "size": actual_size,
        "etag": actual_etag,
        "content_type": content_type,
        "content": content,
    }


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "service": "hal-github-actions-bridge",
            "deployed_commit_sha": get_deployed_commit_sha(),
            "registered_tools": await list_registered_tool_names(),
            "time": int(time.time()),
        }
    )


def main() -> None:
    asyncio.run(validate_required_production_tools())
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
