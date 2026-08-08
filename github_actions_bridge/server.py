from __future__ import annotations

import io
import json
import os
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from typing import Any

import boto3
import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "nhpcorp35/legal-ai")
WORKFLOW = os.environ.get("GITHUB_WORKFLOW", "hal-bridge-proof.yml")
WORKFLOW_BRANCH = os.environ.get("GITHUB_WORKFLOW_BRANCH", "agent/hal-bridge-proof-workflow")
B2_BUCKET = os.environ.get("B2_BUCKET", "legalai-corpus")
B2_PREFIX = os.environ.get("B2_PROOF_PREFIX", "Benchmarks/Bridge-Proof")
GITHUB_API = "https://api.github.com"

mcp = FastMCP(
    "HAL GitHub Actions Bridge",
    instructions=(
        "Dispatch and observe the bounded LegalAI proof workflow. Use submit_run, "
        "then get_run. Use cancel_run for a deliberate long run. After a successful "
        "run, use get_artifacts to copy the harmless proof JSON to B2, verify it, "
        "and retrieve the durable object key."
    ),
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
    json_response=True,
)


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
    response.raise_for_status()
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
    return _run_result(mission_id, await _resolve_run(mission_id))


@mcp.tool()
async def cancel_run(mission_id: str) -> dict[str, Any]:
    """Cancel the GitHub Actions run correlated with mission_id."""
    run = await _resolve_run(mission_id)
    if run is None:
        return {"ok": False, "mission_id": mission_id, "error": "run_not_found"}
    await _github(
        "POST", f"/repos/{REPOSITORY}/actions/runs/{run['id']}/cancel"
    )
    return {"ok": True, "mission_id": mission_id, "run_id": run["id"], "status": "cancellation_requested"}


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
async def get_artifacts(mission_id: str) -> dict[str, Any]:
    """Publish the proof JSON to B2, verify it, and return the durable object key."""
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


async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "hal-github-actions-bridge", "time": int(time.time())})


def create_http_app() -> Starlette:
    streamable_app = mcp.streamable_http_app()
    routes = [Route("/health", health), *streamable_app.routes]

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with mcp.session_manager.run():
            yield

    return Starlette(routes=routes, lifespan=lifespan)


def main() -> None:
    uvicorn.run(create_http_app(), host=mcp.settings.host, port=mcp.settings.port)


if __name__ == "__main__":
    main()

