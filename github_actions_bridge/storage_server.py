from __future__ import annotations

import os
import time
from typing import Any, Literal

import boto3
from cryptography.fernet import Fernet
from fastmcp import FastMCP
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from starlette.requests import Request
from starlette.responses import JSONResponse

try:
    from github_actions_bridge.storage_policy import (
        build_attorney_review_archive,
        inventory_prefix,
    )
except ImportError:  # pragma: no cover - container flat layout
    from storage_policy import build_attorney_review_archive, inventory_prefix


B2_BUCKET = os.environ.get("B2_BUCKET", "legalai-corpus")
PUBLIC_URL = os.environ.get(
    "BRIDGE_PUBLIC_URL",
    "https://hal-github-actions-bridge-production.up.railway.app",
).rstrip("/")
ALLOWED_GITHUB_LOGIN = os.environ.get("ALLOWED_GITHUB_LOGIN", "nhpcorp35")
STORAGE_MCP_PATH = "/storage/mcp"

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
    "HAL LegalAI Storage Bridge",
    instructions=(
        "Canonical Case-00 storage operations only. Use list_case00_storage for "
        "allowlisted inventory metadata under fixed Case-00 prefixes. Use "
        "archive_case00_attorney_feedback to store the fixed attorney-feedback "
        "package with a server-generated preservation manifest, then "
        "HEAD-verify size and SHA-256 metadata. Arbitrary bucket names and "
        "object keys are not permitted. When mounted on the GitHub Actions "
        f"bridge, this surface is served at {STORAGE_MCP_PATH} on the same "
        "BRIDGE_PUBLIC_URL / GitHub OAuth domain as /mcp."
    ),
    auth=auth_provider,
)

EXPECTED_TOOL_NAMES = (
    "list_case00_storage",
    "archive_case00_attorney_feedback",
)


def _require_allowed_user() -> str:
    token = get_access_token()
    login = token.claims.get("login") if token is not None else None
    if login != ALLOWED_GITHUB_LOGIN:
        raise PermissionError("authenticated GitHub user is not authorized")
    return str(login)


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
        "all", "source", "questions", "candidate_answers", "attorney_reviews"
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


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "service": "hal-legalai-storage-bridge",
            "time": int(time.time()),
        }
    )


def main() -> None:
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
