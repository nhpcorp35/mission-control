"""HAL LegalAI Gateway HTTP + authenticated MCP service (Phase 2)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from starlette.authentication import AuthenticationBackend
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection

from hal_legalai_gateway.config import GatewaySettings, load_settings
from hal_legalai_gateway.health import aggregate_health
from hal_legalai_gateway.mcp_server import (
    CANONICAL_GATEWAY_DISPLAY_NAME,
    canonical_gateway_identity,
    create_mcp_server,
    list_registered_tool_names,
)
from hal_legalai_gateway.forwarding import ToolBinding, forward_mcp_tool
from hal_legalai_gateway.auth import service_authorization_header
from hal_legalai_gateway.request_context import (
    RequestIdMiddleware,
    configure_logging,
    get_correlation_id,
    get_request_id,
)

logger = logging.getLogger(__name__)

REQUIRED_PUBLIC_TOOL_NAMES = frozenset(
    {
        "storage.get_acceptance_contract",
        "storage.archive_acceptance_contract",
        "case.submit",
        "case.status",
        "case.cancel",
        "case.list_artifacts",
    }
)


def required_tool_parity(registered_tools: list[str]) -> dict[str, Any]:
    """Return a stable, non-sensitive public-tool parity record."""
    actual = set(registered_tools)
    missing = sorted(REQUIRED_PUBLIC_TOOL_NAMES - actual)
    return {
        "ok": not missing,
        "required_tools": sorted(REQUIRED_PUBLIC_TOOL_NAMES),
        "missing_tools": missing,
    }

_settings: GatewaySettings | None = None
_mcp: FastMCP | None = None
_registered_tools: list[str] = []
_mcp_http_app: Any = None
_auth_override: AuthProvider | None = None
RENNICK_SOURCE_BYTES_MAX = 50 * 1024 * 1024
RENNICK_MANIFEST_BYTES_MAX = 128 * 1024
RENNICK_BROWSER_SESSION_SECONDS = 15 * 60
_RENNICK_STATE_COOKIE = "rennick_oauth_state"
_RENNICK_SESSION_COOKIE = "rennick_upload_session"
_PORTAL_REVIEW_DECISIONS = frozenset(
    {"accept", "revise", "reject", "investigate_further"}
)
_PORTAL_REVIEW_QUESTIONS = frozenset({"Q1", "Q2", "Q3", "Q4", "Q5"})
_PORTAL_REVIEWER_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")
_PORTAL_REVIEW_ARCHIVE_ID = re.compile(r"^review-\d{8}-[0-9a-f]{12}$")


class _GatewayAuthBackend(AuthenticationBackend):
    """Lazy Bearer backend so auth works after MCP routes are composed into FastAPI.

    FastMCP wraps ``/mcp`` with ``RequireAuthMiddleware``, which expects
    ``AuthenticationMiddleware`` to have populated ``scope["user"]``. Route
    copying alone does not install that middleware on the parent app.
    """

    async def authenticate(self, conn: HTTPConnection):
        provider = _auth_override
        if provider is None and _mcp is not None:
            provider = getattr(_mcp, "auth", None)
        if provider is None:
            return None
        return await BearerAuthBackend(provider).authenticate(conn)




def get_settings() -> GatewaySettings:
    """Return process settings, loading once if lifespan has not run yet."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings

def get_mcp() -> FastMCP | None:
    return _mcp

def reset_settings_for_tests() -> None:
    """Clear cached settings / MCP state (test helper)."""
    global _settings, _mcp, _registered_tools, _mcp_http_app, _auth_override
    _settings = None
    _mcp = None
    _registered_tools = []
    _mcp_http_app = None
    _auth_override = None


async def _archive_portal_case00_feedback(
    request: Request, *, question_id: str
) -> JSONResponse:
    """Archive one authenticated, bounded Case-00 portal submission.

    The portal verifies its B2 packet before calling this bounded relay.  The
    gateway only accepts explicitly supported question IDs, rather than acting
    as a general-purpose archive proxy.
    """
    if question_id not in _PORTAL_REVIEW_QUESTIONS:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    secret = os.environ.get("PORTAL_REVIEW_GATEWAY_SECRET", "")
    supplied = request.headers.get("X-LegalAI-Portal-Secret", "")
    if not secret or not hmac.compare_digest(supplied, secret):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"ok": False, "error": "invalid_submission"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "invalid_submission"}, status_code=400)
    reviewer = payload.get("reviewer")
    decision = payload.get("decision")
    notes = payload.get("notes", "")
    packet = payload.get("original_packet_md")
    if not all(isinstance(value, str) for value in (reviewer, decision, notes, packet)):
        return JSONResponse({"ok": False, "error": "invalid_submission"}, status_code=400)
    reviewer = reviewer.strip().lower()
    decision = decision.strip().lower()
    notes = notes.strip()
    if (
        decision not in _PORTAL_REVIEW_DECISIONS
        or not _PORTAL_REVIEWER_EMAIL.fullmatch(reviewer)
        or not packet.startswith("# Case-00 Attorney Cognition Review Packet v1")
        or f"**Question ID:** {question_id}" not in packet
        or len(packet) > 50_000
        or len(notes) > 12_000
    ):
        return JSONResponse({"ok": False, "error": "invalid_submission"}, status_code=400)
    settings = get_settings()
    binding = ToolBinding(
        "storage.archive_feedback", "storage", "storage", "archive_case00_attorney_feedback"
    )
    evaluation = {
        "case_id": "case-00-triborough",
        "question_id": question_id,
        "reviewer": reviewer,
        "decision": decision,
        "notes": notes,
    }
    email = (
        f"# Case-00 {question_id} attorney feedback\n\n"
        f"Reviewer: {reviewer}\nDecision: {decision}\n\n{notes}\n"
    )
    result = await forward_mcp_tool(
        binding=binding,
        arguments={
            "evaluation_date": time.strftime("%Y-%m-%d"),
            "original_packet_md": packet,
            "feedback_email_md": email,
            "structured_evaluation_json": json.dumps(evaluation, separators=(",", ":")),
        },
        base_url=settings.downstream_by_key("storage").base_url,
        authorization=settings.bridge_authorization,
        connect_timeout_seconds=settings.connect_timeout_seconds,
        read_timeout_seconds=settings.read_timeout_seconds,
        mcp_path=settings.mcp_path_for_service("storage"),
        extra_secrets=settings.secret_values_for_redaction(),
    )
    return JSONResponse(result, status_code=201 if result.get("ok") else 502)


async def _archive_portal_szymczyk_feedback(request: Request) -> JSONResponse:
    """Relay one authenticated Szymczyk portal submission to the B2 bridge."""
    secret = os.environ.get("PORTAL_REVIEW_GATEWAY_SECRET", "")
    supplied = request.headers.get("X-LegalAI-Portal-Secret", "")
    if not secret or not hmac.compare_digest(supplied, secret):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"ok": False, "error": "invalid_submission"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "invalid_submission"}, status_code=400)
    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=settings.connect_timeout_seconds)) as client:
            response = await client.post(settings.downstream_by_key("storage").base_url.rstrip("/") + "/portal/szymczyk/feedback", headers=headers, json=payload)
            result = response.json()
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"ok": False, "error": "archive_unavailable"}, status_code=502)
    return JSONResponse(result if isinstance(result, dict) else {"ok": False, "error": "archive_unavailable"}, status_code=201 if response.status_code == 201 else response.status_code)


async def _szymczyk_feedback_status(request: Request) -> JSONResponse:
    secret = os.environ.get("PORTAL_REVIEW_GATEWAY_SECRET", "")
    if not secret or not hmac.compare_digest(request.headers.get("X-LegalAI-Portal-Secret", ""), secret):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    settings = get_settings(); headers = {}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization: headers["Authorization"] = authorization
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=settings.connect_timeout_seconds)) as client:
            response = await client.get(settings.downstream_by_key("storage").base_url.rstrip("/") + "/portal/szymczyk/feedback/status", headers=headers)
            result = response.json()
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"ok": False, "error": "status_unavailable"}, status_code=502)
    return JSONResponse(result, status_code=response.status_code)


async def _read_latest_szymczyk_feedback(request: Request) -> JSONResponse:
    """Relay the newest Szymczyk feedback through the portal secret boundary."""
    secret = os.environ.get("PORTAL_REVIEW_GATEWAY_SECRET", "")
    if not secret or not hmac.compare_digest(request.headers.get("X-LegalAI-Portal-Secret", ""), secret):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    settings = get_settings()
    headers = {}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=settings.connect_timeout_seconds)) as client:
            response = await client.get(settings.downstream_by_key("storage").base_url.rstrip("/") + "/portal/szymczyk/feedback/latest", headers=headers)
            result = response.json()
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"ok": False, "error": "feedback_read_unavailable"}, status_code=502)
    return JSONResponse(result, status_code=response.status_code)


async def _read_current_szymczyk_review_packet(request: Request) -> JSONResponse:
    """Relay the B2-backed current packet through the existing portal secret."""
    secret = os.environ.get("PORTAL_REVIEW_GATEWAY_SECRET", "")
    if not secret or not hmac.compare_digest(request.headers.get("X-LegalAI-Portal-Secret", ""), secret):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    settings = get_settings(); headers = {}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=settings.connect_timeout_seconds)) as client:
            response = await client.get(settings.downstream_by_key("storage").base_url.rstrip("/") + "/portal/szymczyk/review-packet/current", headers=headers)
            result = response.json()
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"ok": False, "error": "review_packet_read_unavailable"}, status_code=502)
    return JSONResponse(result if isinstance(result, dict) else {"ok": False, "error": "review_packet_read_unavailable"}, status_code=response.status_code)


_PORTAL_SZYMCZYK_CASE_ID = "NY-NewYork-158068-2018-Szymczyk-v-Hudson-36-37"
_PORTAL_SZYMCZYK_SOURCE_SHA256 = "ff8a0773d740358d56e43055f518e42b6124a4bc4fb00a39abaf85c5393568dc"


async def _search_portal_szymczyk_verified_pages(request: Request) -> JSONResponse:
    """Read-only verified-page search for the single attorney workspace matter.

    The browser supplies only a bounded query. The Gateway supplies the
    immutable case/source binding and private Bridge authorization, so this
    surface cannot select another source or obtain credentials.
    """
    secret = os.environ.get("PORTAL_REVIEW_GATEWAY_SECRET", "")
    supplied = request.headers.get("X-LegalAI-Portal-Secret", "")
    if not secret or not hmac.compare_digest(supplied, secret):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=400)
    query = payload.get("query") if isinstance(payload, dict) else None
    if not isinstance(query, str):
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=400)
    query = query.strip()
    if not query or len(query) > 500:
        return JSONResponse({"ok": False, "error": "invalid_query"}, status_code=400)

    result = await _forward_verified_case_operation(
        "search",
        {
            "case_id": _PORTAL_SZYMCZYK_CASE_ID,
            "source_sha256": _PORTAL_SZYMCZYK_SOURCE_SHA256,
            "query": query,
            "limit": 12,
        },
    )
    return JSONResponse(
        result if isinstance(result, dict) else {"ok": False, "error": "search_unavailable"},
        status_code=200 if isinstance(result, dict) and result.get("ok") else 502,
    )


async def _open_portal_szymczyk_verified_pdf(request: Request) -> Response:
    """Open one verified Szymczyk source PDF through the portal secret boundary."""
    secret = os.environ.get("PORTAL_REVIEW_GATEWAY_SECRET", "")
    if not secret or not hmac.compare_digest(request.headers.get("X-LegalAI-Portal-Secret", ""), secret):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=400)
    document_name = payload.get("document_name") if isinstance(payload, dict) else None
    if not isinstance(document_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,180}\.pdf", document_name):
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=400)
    return await _forward_verified_case_pdf(
        {
            "case_id": _PORTAL_SZYMCZYK_CASE_ID,
            "source_sha256": _PORTAL_SZYMCZYK_SOURCE_SHA256,
            "document_name": document_name,
        }
    )


async def _read_portal_case00_feedback(
    request: Request, *, question_id: str
) -> JSONResponse:
    """Read one fixed-format archived portal feedback note.

    This is intentionally separate from the public MCP catalog.  It accepts
    only the portal shared secret and a bounded archive identifier, then uses
    the existing private Bridge read route.  It never exposes B2 credentials.
    """
    if question_id not in _PORTAL_REVIEW_QUESTIONS:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    secret = os.environ.get("PORTAL_REVIEW_GATEWAY_SECRET", "")
    supplied = request.headers.get("X-LegalAI-Portal-Secret", "")
    if not secret or not hmac.compare_digest(supplied, secret):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
        archive_id = str(payload.get("archive_id", ""))
    except (ValueError, json.JSONDecodeError, AttributeError):
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=400)
    if not _PORTAL_REVIEW_ARCHIVE_ID.fullmatch(archive_id):
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=400)

    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=settings.connect_timeout_seconds)
        ) as client:
            response = await client.post(
                f"{settings.downstream_by_key('storage').base_url.rstrip('/')}/case-00/attorney-feedback/read",
                json={"archive_id": archive_id},
                headers=headers,
            )
        result = response.json()
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"ok": False, "error": "feedback_read_unavailable"}, status_code=502)
    if not isinstance(result, dict):
        return JSONResponse({"ok": False, "error": "feedback_read_unavailable"}, status_code=502)
    return JSONResponse(result, status_code=response.status_code)


async def _read_portal_case00_packet(request: Request, *, question_id: str) -> JSONResponse:
    """Read one fixed Case-00 candidate through the private Bridge route."""
    if question_id not in _PORTAL_REVIEW_QUESTIONS:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    secret = os.environ.get("PORTAL_REVIEW_GATEWAY_SECRET", "")
    supplied = request.headers.get("X-LegalAI-Portal-Secret", "")
    if not secret or not hmac.compare_digest(supplied, secret):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=400)
    if not isinstance(payload, dict) or str(payload.get("question_id", "")).upper() != question_id:
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=400)
    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=settings.connect_timeout_seconds)) as client:
            response = await client.post(
                f"{settings.downstream_by_key('storage').base_url.rstrip('/')}/case-00/portal-packet/read",
                json={"question_id": question_id}, headers=headers,
            )
        result = response.json()
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"ok": False, "error": "packet_unavailable"}, status_code=502)
    if not isinstance(result, dict):
        return JSONResponse({"ok": False, "error": "packet_unavailable"}, status_code=502)
    return JSONResponse(result, status_code=response.status_code)



async def _list_portal_registered_cases(request: Request) -> JSONResponse:
    """Return authenticated metadata-only case IDs from the private Bridge."""
    secret = os.environ.get("PORTAL_REVIEW_GATEWAY_SECRET", "")
    supplied = request.headers.get("X-LegalAI-Portal-Secret", "")
    if not secret or not hmac.compare_digest(supplied, secret):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    settings = get_settings()
    headers = {}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=settings.connect_timeout_seconds)) as client:
            response = await client.get(
                f"{settings.downstream_by_key('storage').base_url.rstrip('/')}/cases/registered/list",
                headers=headers,
            )
        result = response.json()
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"ok": False, "error": "registry_unavailable"}, status_code=502)
    if not isinstance(result, dict):
        return JSONResponse({"ok": False, "error": "registry_unavailable"}, status_code=502)
    return JSONResponse(result, status_code=response.status_code)


async def _search_portal_indexed_case(request: Request, case_id: str) -> JSONResponse:
    """Forward a protected verified-record search to the private Bridge."""
    secret = os.environ.get("PORTAL_REVIEW_GATEWAY_SECRET", "")
    supplied = request.headers.get("X-LegalAI-Portal-Secret", "")
    if not secret or not hmac.compare_digest(supplied, secret):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
        query = " ".join(str(payload.get("query", "")).split())
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=400)
    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=settings.connect_timeout_seconds)) as client:
            response = await client.post(
                f"{settings.downstream_by_key('storage').base_url.rstrip('/')}/cases/indexed/search",
                json={"case_id": case_id, "query": query, "limit": 20},
                headers=headers,
            )
        result = response.json()
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"ok": False, "error": "search_unavailable"}, status_code=502)
    if not isinstance(result, dict):
        return JSONResponse({"ok": False, "error": "search_unavailable"}, status_code=502)
    return JSONResponse(result, status_code=response.status_code)


async def _read_portal_case_source_map(request: Request, case_id: str) -> JSONResponse:
    """Forward an authenticated source-map request to the private Bridge."""
    secret = os.environ.get("PORTAL_REVIEW_GATEWAY_SECRET", "")
    supplied = request.headers.get("X-LegalAI-Portal-Secret", "")
    if not secret or not hmac.compare_digest(supplied, secret):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=settings.connect_timeout_seconds)) as client:
            response = await client.post(
                f"{settings.downstream_by_key('storage').base_url.rstrip('/')}/cases/source-map",
                json={"case_id": case_id},
                headers=headers,
            )
        result = response.json()
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"ok": False, "error": "source_map_unavailable"}, status_code=502)
    if not isinstance(result, dict):
        return JSONResponse({"ok": False, "error": "source_map_unavailable"}, status_code=502)
    return JSONResponse(result, status_code=response.status_code)


async def _create_portal_draft_request(request: Request) -> JSONResponse:
    """Forward an authenticated internal draft question to the private Bridge."""
    secret = os.environ.get("PORTAL_REVIEW_GATEWAY_SECRET", "")
    supplied = request.headers.get("X-LegalAI-Portal-Secret", "")
    if not secret or not hmac.compare_digest(supplied, secret):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=400)
    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=settings.connect_timeout_seconds)) as client:
            response = await client.post(
                f"{settings.downstream_by_key('storage').base_url.rstrip('/')}/cases/draft-requests",
                json=payload,
                headers=headers,
            )
        result = response.json()
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"ok": False, "error": "draft_request_unavailable"}, status_code=502)
    if not isinstance(result, dict):
        return JSONResponse({"ok": False, "error": "draft_request_unavailable"}, status_code=502)
    return JSONResponse(result, status_code=response.status_code)


async def _list_portal_draft_requests(request: Request, case_id: str) -> JSONResponse:
    """Read the internal-only draft queue through the private Bridge."""
    secret = os.environ.get("PORTAL_REVIEW_GATEWAY_SECRET", "")
    supplied = request.headers.get("X-LegalAI-Portal-Secret", "")
    if not secret or not hmac.compare_digest(supplied, secret):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    settings = get_settings()
    headers = {"Accept": "application/json"}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=settings.connect_timeout_seconds)) as client:
            response = await client.get(
                f"{settings.downstream_by_key('storage').base_url.rstrip('/')}/cases/{case_id}/draft-requests",
                headers=headers,
            )
        result = response.json()
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"ok": False, "error": "draft_queue_unavailable"}, status_code=502)
    if not isinstance(result, dict):
        return JSONResponse({"ok": False, "error": "draft_queue_unavailable"}, status_code=502)
    return JSONResponse(result, status_code=response.status_code)


async def _open_portal_case_pdf(request: Request, case_id: str) -> Response:
    """Open one verified case PDF through the internal portal boundary."""
    secret = os.environ.get("PORTAL_REVIEW_GATEWAY_SECRET", "")
    supplied = request.headers.get("X-LegalAI-Portal-Secret", "")
    if not secret or not hmac.compare_digest(supplied, secret):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=400)
    document_name = payload.get("document_name") if isinstance(payload, dict) else None
    if not isinstance(document_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,180}\\.pdf", document_name):
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=400)
    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=settings.connect_timeout_seconds)) as client:
            response = await client.post(
                f"{settings.downstream_by_key('storage').base_url.rstrip('/')}/cases/open-pdf",
                headers=headers,
                json={"case_id": case_id, "document_name": document_name},
            )
            content = response.content
    except httpx.HTTPError:
        return JSONResponse({"ok": False, "error": "pdf_unavailable"}, status_code=502)
    if not response.is_success:
        return JSONResponse({"ok": False, "error": "pdf_unavailable"}, status_code=502)
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{document_name}"'},
    )

def _sign_browser_value(payload: dict[str, Any]) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(
        get_settings().jwt_signing_key.encode("utf-8"),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{signature}"


def _verified_browser_value(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        encoded, signature = value.split(".", 1)
        expected = hmac.new(
            get_settings().jwt_signing_key.encode("utf-8"),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if not isinstance(payload, dict) or int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _browser_login(request: Request) -> str | None:
    payload = _verified_browser_value(request.cookies.get(_RENNICK_SESSION_COOKIE))
    login = payload.get("login") if payload else None
    if login != get_settings().allowed_github_login:
        return None
    return str(login)


async def _forward_generic_direct_intake(
    action: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Forward a generic direct-intake control request to the private Bridge."""
    settings = get_settings()
    downstream = settings.downstream_by_key("storage")
    headers = {"Content-Type": "application/json"}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(180.0, connect=settings.connect_timeout_seconds)
    ) as client:
        response = await client.post(
            f"{downstream.base_url.rstrip('/')}/intake/direct/{action}",
            headers=headers,
            json=payload,
        )
    try:
        result = response.json()
    except ValueError:
        result = {"ok": False, "error": "bridge_generic_intake_response_invalid"}
    return result if response.is_success else {
        "ok": False,
        "error": result.get("error", "bridge_generic_intake_failed"),
    }


async def _forward_rennick_pair(source: bytes, manifest: bytes) -> dict[str, Any]:
    """Forward browser-uploaded bytes through the private binary Bridge route."""
    settings = get_settings()
    downstream = settings.downstream_by_key("storage")
    headers = {"X-Rennick-Source-Size": str(len(source))}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    timeout = httpx.Timeout(300.0, connect=settings.connect_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{downstream.base_url.rstrip('/')}/intake/rennick/upload",
            content=source + manifest,
            headers=headers,
        )
    try:
        result = response.json()
    except ValueError:
        result = {"ok": False, "error": "bridge_upload_response_invalid"}
    if response.is_success:
        return result
    return {"ok": False, "error": result.get("error", "bridge_upload_failed")}


async def _forward_rennick_supplement_pair(archive: bytes, manifest: bytes) -> dict[str, Any]:
    """Forward the fixed docket supplement through the private binary Bridge route."""
    settings = get_settings()
    downstream = settings.downstream_by_key("storage")
    headers = {"X-Rennick-Supplement-Archive-Size": str(len(archive))}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    timeout = httpx.Timeout(300.0, connect=settings.connect_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{downstream.base_url.rstrip('/')}/intake/rennick/supplement?archive_size={len(archive)}",
            content=archive + manifest,
            headers=headers,
        )
    try:
        result = response.json()
    except ValueError:
        result = {"ok": False, "error": "bridge_supplement_response_invalid"}
    return result if response.is_success else {"ok": False, "error": result.get("error", "bridge_supplement_failed")}


async def _forward_rennick_direct_supplement(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = get_settings()
    downstream = settings.downstream_by_key("storage")
    headers: dict[str, str] = {}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=settings.connect_timeout_seconds)) as client:
        response = await client.post(
            f"{downstream.base_url.rstrip('/')}/intake/rennick/supplement/direct/{action}",
            headers=headers,
            json=payload,
        )
    try:
        result = response.json()
    except ValueError:
        result = {"ok": False, "error": "bridge_direct_upload_response_invalid"}
    return result if response.is_success else {"ok": False, "error": result.get("error", "bridge_direct_upload_failed")}


async def _forward_rennick_promotion() -> dict[str, Any]:
    """Promote existing verified Rennick bytes without accepting an upload."""
    settings = get_settings()
    downstream = settings.downstream_by_key("storage")
    headers: dict[str, str] = {}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=settings.connect_timeout_seconds)) as client:
        response = await client.post(f"{downstream.base_url.rstrip('/')}/intake/rennick/promote", headers=headers)
    try:
        result = response.json()
    except ValueError:
        result = {"ok": False, "error": "bridge_rennick_promotion_response_invalid"}
    return result if response.is_success else {"ok": False, "error": result.get("error", "bridge_rennick_promotion_failed")}


async def _forward_szymczyk_direct(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = get_settings()
    downstream = settings.downstream_by_key("storage")
    headers = {"Content-Type": "application/json"}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=settings.connect_timeout_seconds)) as client:
        response = await client.post(f"{downstream.base_url.rstrip('/')}/intake/szymczyk/direct/{action}", headers=headers, json=payload or {})
    try:
        result = response.json()
    except ValueError:
        result = {"ok": False, "error": "bridge_szymczyk_upload_response_invalid"}
    return result if response.is_success else {"ok": False, "error": result.get("error", "bridge_szymczyk_upload_failed")}


async def _forward_szymczyk_inspection(payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    downstream = settings.downstream_by_key("storage")
    headers = {"Content-Type": "application/json"}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=settings.connect_timeout_seconds)) as client:
        response = await client.post(f"{downstream.base_url.rstrip('/')}/intake/szymczyk/inspect", headers=headers, json=payload)
    try:
        result = response.json()
    except ValueError:
        result = {"ok": False, "error": "bridge_szymczyk_inspection_response_invalid"}
    return result if response.is_success else {"ok": False, "error": result.get("error", "bridge_szymczyk_inspection_failed")}


async def _forward_szymczyk_identification(payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    downstream = settings.downstream_by_key("storage")
    headers = {"Content-Type": "application/json"}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=settings.connect_timeout_seconds)) as client:
        response = await client.post(f"{downstream.base_url.rstrip('/')}/intake/szymczyk/identify", headers=headers, json=payload)
    try:
        result = response.json()
    except ValueError:
        result = {"ok": False, "error": "bridge_szymczyk_identification_response_invalid"}
    return result if response.is_success else {"ok": False, "error": result.get("error", "bridge_szymczyk_identification_failed")}


async def _forward_szymczyk_promotion(payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    downstream = settings.downstream_by_key("storage")
    headers = {"Content-Type": "application/json"}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=settings.connect_timeout_seconds)) as client:
        response = await client.post(f"{downstream.base_url.rstrip('/')}/intake/szymczyk/promote", headers=headers, json=payload)
    try:
        result = response.json()
    except ValueError:
        result = {"ok": False, "error": "bridge_szymczyk_promotion_response_invalid"}
    return result if response.is_success else {"ok": False, "error": result.get("error", "bridge_szymczyk_promotion_failed")}


async def _forward_szymczyk_inventory(payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    downstream = settings.downstream_by_key("storage")
    headers = {"Content-Type": "application/json"}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=settings.connect_timeout_seconds)) as client:
        response = await client.post(f"{downstream.base_url.rstrip('/')}/intake/szymczyk/inventory", headers=headers, json=payload)
    try:
        result = response.json()
    except ValueError:
        result = {"ok": False, "error": "bridge_szymczyk_inventory_response_invalid"}
    return result if response.is_success else {"ok": False, "error": result.get("error", "bridge_szymczyk_inventory_failed")}


async def _forward_szymczyk_pipeline(payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    downstream = settings.downstream_by_key("storage")
    headers = {"Content-Type": "application/json"}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=settings.connect_timeout_seconds)) as client:
        response = await client.post(f"{downstream.base_url.rstrip('/')}/intake/szymczyk/process", headers=headers, json=payload)
    try:
        result = response.json()
    except ValueError:
        result = {"ok": False, "error": "bridge_szymczyk_pipeline_response_invalid"}
    return result if response.is_success else {"ok": False, "error": result.get("error", "bridge_szymczyk_pipeline_failed")}


async def _forward_verified_case_pages(payload: dict[str, Any]) -> dict[str, Any]:
    """Forward a bounded, authenticated page request to the private Bridge."""
    settings = get_settings()
    downstream = settings.downstream_by_key("storage")
    headers = {"Content-Type": "application/json"}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=settings.connect_timeout_seconds)) as client:
        response = await client.post(f"{downstream.base_url.rstrip('/')}/cases/verified/read-pages", headers=headers, json=payload)
    try:
        result = response.json()
    except ValueError:
        result = {"ok": False, "error": "bridge_verified_case_reader_response_invalid"}
    return result if response.is_success else {"ok": False, "error": result.get("error", "bridge_verified_case_reader_failed")}


async def _forward_verified_case_operation(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings(); downstream = settings.downstream_by_key("storage")
    headers = {"Content-Type": "application/json"}; authorization = service_authorization_header(settings.bridge_authorization)
    if authorization: headers["Authorization"] = authorization
    async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=settings.connect_timeout_seconds)) as client:
        response = await client.post(f"{downstream.base_url.rstrip('/')}/cases/verified/{operation}", headers=headers, json=payload)
    try: result = response.json()
    except ValueError: result = {"ok": False, "error": f"bridge_verified_case_{operation}_response_invalid"}
    return result if response.is_success else {"ok": False, "error": result.get("error", f"bridge_verified_case_{operation}_failed")}



async def _forward_verified_case_pdf(payload: dict[str, Any]) -> Response:
    """Forward one bounded verified PDF from Bridge without exposing its credentials."""
    settings = get_settings()
    downstream = settings.downstream_by_key("storage")
    headers = {"Content-Type": "application/json"}
    authorization = service_authorization_header(settings.bridge_authorization)
    if authorization:
        headers["Authorization"] = authorization
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=settings.connect_timeout_seconds)) as client:
            response = await client.post(
                f"{downstream.base_url.rstrip('/')}/cases/verified/open-pdf",
                headers=headers,
                json=payload,
            )
            content = response.content
    except httpx.HTTPError:
        return JSONResponse({"ok": False, "error": "pdf_unavailable"}, status_code=502)
    if not response.is_success:
        try:
            result = response.json()
        except ValueError:
            result = {"ok": False, "error": "pdf_unavailable"}
        return JSONResponse(result if isinstance(result, dict) else {"ok": False, "error": "pdf_unavailable"}, status_code=502)
    filename = str(payload.get("document_name", "document.pdf"))
    return Response(content=content, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{filename}"'})



def _attach_mcp_routes(application: FastAPI, mcp_app: Any) -> None:
    """Install (or replace) FastMCP routes so lifespan-bound session managers match."""
    application.router.routes = [
        route
        for route in application.router.routes
        if getattr(route, "path", None) not in {
            getattr(mcp_route, "path", None) for mcp_route in mcp_app.routes
        }
    ]
    for route in mcp_app.routes:
        application.router.routes.append(route)


@asynccontextmanager
async def lifespan(application: FastAPI):
    global _settings, _registered_tools, _mcp_http_app, _mcp
    configure_logging()
    # Fail closed on missing GitHub OAuth config / GATEWAY_BRIDGE_AUTHORIZATION.
    _settings = load_settings()
    auth = _auth_override
    _mcp = create_mcp_server(_settings, auth=auth)
    _mcp_http_app = _mcp.http_app(path="/mcp", transport="http")
    _attach_mcp_routes(application, _mcp_http_app)
    _registered_tools = await list_registered_tool_names(_mcp)
    logger.info(
        "HAL LegalAI Gateway starting phase=2 deployed_commit_sha=%s "
        "downstreams=%s registered_tools=%s health_timeout_seconds=%s "
        "connect_timeout_seconds=%s read_timeout_seconds=%s inbound_auth=github_oauth",
        _settings.deployed_commit_sha,
        ",".join(item.key for item in _settings.downstreams),
        ",".join(_registered_tools),
        _settings.health_timeout_seconds,
        _settings.connect_timeout_seconds,
        _settings.read_timeout_seconds,
    )
    async with _mcp_http_app.lifespan(application):
        yield
    logger.info("HAL LegalAI Gateway shutting down")
    _settings = None
    _mcp = None
    _registered_tools = []
    _mcp_http_app = None


def create_app(*, auth_override: AuthProvider | None = None) -> FastAPI:
    """Application factory used by uvicorn and tests.

    ``auth_override`` is for tests only (inject a fixed token verifier). Production
    always uses GitHub OAuth via settings.
    """
    global _auth_override
    _auth_override = auth_override

    application = FastAPI(
        title=CANONICAL_GATEWAY_DISPLAY_NAME,
        description=(
            "Thin authenticated interface consolidation for LegalAI downstream "
            "MCP services. Phase 2 exposes namespaced case/storage/mission/"
            "workflow tools that forward to Bridge, Storage, artifact "
            "retrieval, and Mission Control. Downstream business logic remains "
            "separately deployed. Inbound /mcp uses GitHub OAuth for ChatGPT "
            "Business custom MCP. Canonical plugin identity is "
            f"{CANONICAL_GATEWAY_DISPLAY_NAME}."
        ),
        version="0.2.0",
        lifespan=lifespan,
    )
    # Order: last added is outermost. Request IDs outer; auth populates scope
    # for FastMCP RequireAuthMiddleware on /mcp; /health and /registry stay open.
    application.add_middleware(AuthContextMiddleware)
    application.add_middleware(
        AuthenticationMiddleware,
        backend=_GatewayAuthBackend(),
    )
    application.add_middleware(RequestIdMiddleware)

    @application.get("/")
    async def root(request: Request) -> dict[str, Any]:
        settings = get_settings()
        identity = canonical_gateway_identity(
            public_url=settings.gateway_public_url
        )
        return {
            "service": identity["service_id"],
            "phase": 2,
            "identity": identity,
            "deployed_commit_sha": settings.deployed_commit_sha,
            "request_id": getattr(request.state, "request_id", get_request_id()),
            "correlation_id": getattr(
                request.state, "correlation_id", get_correlation_id()
            ),
            "endpoints": {
                "health": "/health",
                "registry": "/registry",
                "mcp": "/mcp",
            },
            "auth": {
                "inbound": "github_oauth",
                "downstream_bridge": "service_credential",
            },
            "registered_tools": list(_registered_tools),
        }

    @application.get("/.well-known/openid-configuration", include_in_schema=False)
    async def openid_configuration() -> RedirectResponse:
        """Alias OIDC discovery to the OAuth authorization-server metadata.

        ChatGPT's custom-app scanner probes the OIDC well-known path before
        starting OAuth. The Gateway is an OAuth authorization server rather
        than an OpenID Connect identity provider, but its existing OAuth
        metadata has the required authorization, token, and registration
        endpoints.
        """
        base = get_settings().gateway_public_url.rstrip("/")
        return RedirectResponse(
            url=f"{base}/.well-known/oauth-authorization-server",
            status_code=307,
        )

    @application.get("/portal/cases/registered", include_in_schema=False)
    async def list_portal_registered_cases(request: Request) -> JSONResponse:
        return await _list_portal_registered_cases(request)

    @application.post("/portal/cases/{case_id}/search", include_in_schema=False)
    async def search_portal_indexed_case(case_id: str, request: Request) -> JSONResponse:
        return await _search_portal_indexed_case(request, case_id)

    @application.get("/portal/cases/{case_id}/source-map", include_in_schema=False)
    async def read_portal_case_source_map(case_id: str, request: Request) -> JSONResponse:
        return await _read_portal_case_source_map(request, case_id)

    @application.post("/portal/cases/{case_id}/pdf", include_in_schema=False)
    async def open_portal_case_pdf(case_id: str, request: Request) -> Response:
        return await _open_portal_case_pdf(request, case_id)

    @application.post("/portal/cases/draft-request", include_in_schema=False)
    async def create_portal_draft_request(request: Request) -> JSONResponse:
        return await _create_portal_draft_request(request)

    @application.get("/portal/cases/{case_id}/draft-requests", include_in_schema=False)
    async def list_portal_draft_requests(case_id: str, request: Request) -> JSONResponse:
        return await _list_portal_draft_requests(request, case_id)

    @application.post("/portal/case-00/{question_id}/packet", include_in_schema=False)
    async def read_portal_case00_packet(question_id: str, request: Request) -> JSONResponse:
        return await _read_portal_case00_packet(request, question_id=question_id.upper())

    @application.post("/portal/case-00/q4/feedback", include_in_schema=False)
    async def archive_portal_case00_q4_feedback(request: Request) -> JSONResponse:
        return await _archive_portal_case00_feedback(request, question_id="Q4")

    @application.post("/portal/szymczyk/feedback", include_in_schema=False)
    async def archive_portal_szymczyk_feedback(request: Request) -> JSONResponse:
        return await _archive_portal_szymczyk_feedback(request)

    @application.get("/portal/szymczyk/feedback/status", include_in_schema=False)
    async def szymczyk_feedback_status(request: Request) -> JSONResponse:
        return await _szymczyk_feedback_status(request)

    @application.post("/portal/szymczyk/verified-search", include_in_schema=False)
    async def search_portal_szymczyk_verified_pages(request: Request) -> JSONResponse:
        return await _search_portal_szymczyk_verified_pages(request)

    @application.post("/portal/szymczyk/verified-pdf", include_in_schema=False)
    async def open_portal_szymczyk_verified_pdf(request: Request) -> Response:
        return await _open_portal_szymczyk_verified_pdf(request)

    @application.get("/portal/szymczyk/feedback/latest", include_in_schema=False)
    async def read_latest_szymczyk_feedback(request: Request) -> JSONResponse:
        return await _read_latest_szymczyk_feedback(request)

    @application.get("/portal/szymczyk/review-packet/current", include_in_schema=False)
    async def read_current_szymczyk_review_packet(request: Request) -> JSONResponse:
        return await _read_current_szymczyk_review_packet(request)

    @application.post("/portal/case-00/q5/feedback", include_in_schema=False)
    async def archive_portal_case00_q5_feedback(request: Request) -> JSONResponse:
        return await _archive_portal_case00_feedback(request, question_id="Q5")

    @application.post("/portal/case-00/q4/feedback/read", include_in_schema=False)
    async def read_portal_case00_q4_feedback(request: Request) -> JSONResponse:
        return await _read_portal_case00_feedback(request, question_id="Q4")

    @application.post("/portal/case-00/q5/feedback/read", include_in_schema=False)
    async def read_portal_case00_q5_feedback(request: Request) -> JSONResponse:
        return await _read_portal_case00_feedback(request, question_id="Q5")

    @application.get("/intake", include_in_schema=False, response_model=None)
    async def generic_intake_page(request: Request) -> HTMLResponse | RedirectResponse:
        """Authenticated entry point for future verified-case ZIP + manifest intake."""
        requested_case_id = str(request.query_params.get("case_id") or "")
        if not re.fullmatch(r"NY-[A-Za-z]+-[0-9]{6}-[0-9]{4}-[A-Za-z0-9-]{2,80}", requested_case_id):
            requested_case_id = ""
        return_to = "/intake" + (f"?case_id={requested_case_id}" if requested_case_id else "")
        if _browser_login(request) is None:
            nonce = secrets.token_urlsafe(24)
            state = _sign_browser_value(
                {"nonce": nonce, "exp": int(time.time()) + 600, "return_to": return_to}
            )
            settings = get_settings()
            callback = f"{settings.gateway_public_url.rstrip('/')}/auth/callback"
            url = "https://github.com/login/oauth/authorize?" + urlencode(
                {
                    "client_id": settings.github_oauth_client_id,
                    "redirect_uri": callback,
                    "state": state,
                    "scope": "read:user",
                }
            )
            response = RedirectResponse(url=url, status_code=303)
            response.set_cookie(
                _RENNICK_STATE_COOKIE,
                nonce,
                max_age=600,
                httponly=True,
                secure=True,
                samesite="lax",
            )
            return response
        return HTMLResponse('''<!doctype html><meta charset="utf-8"><title>Verified case intake</title>
        <main><h1>Verified case intake</h1>
        <p>Upload the exact source ZIP and its JSON manifest. The system verifies every listed PDF’s size and SHA-256 before making the matter available for search or attorney review.</p>
        <p>Nothing is sent to an attorney, and no legal answer is generated by this step.</p>
        <details><summary>Required manifest format</summary><p>The ZIP may contain only the listed PDFs. Each PDF needs its exact byte count and lowercase SHA-256.</p><pre>{
  "case_id": "NY-County-123456-2026-Example-v-Example",
  "documents": [
    {
      "filename": "filing.pdf",
      "size_bytes": 123456,
      "sha256": "64-lowercase-hex-characters"
    }
  ]
}</pre></details>
        <label>Case ID <input id="case-id" required placeholder="NY-County-123456-2026-Example-v-Example"></label><br>
        <label>Source ZIP <input id="source" type="file" accept=".zip" required></label><br>
        <label>JSON manifest <input id="manifest" type="file" accept=".json,application/json" required></label><br>
        <button id="upload">Upload, verify, and index</button><pre id="status" aria-live="polite"></pre></main>
        <script>
        document.getElementById('case-id').value = __REQUESTED_CASE_ID__;
        const out=document.getElementById('status');
        const jsonHeaders={'Content-Type':'application/json'};
        async function request(path,payload){const r=await fetch(path,{method:'POST',headers:jsonHeaders,body:JSON.stringify(payload)});const result=await r.json().catch(()=>({ok:false,error:'invalid response'}));if(!r.ok||!result.ok)throw new Error(result.error||'request failed');return result;}
        document.getElementById('upload').onclick=async()=>{
          try{
            const caseId=document.getElementById('case-id').value.trim();
            const source=document.getElementById('source').files[0];
            const manifest=document.getElementById('manifest').files[0];
            if(!caseId||!source||!manifest)throw new Error('Enter the case ID and select both files.');
            out.textContent='Preparing private upload…';
            const plan=await request('/intake/direct/prepare',{case_id:caseId,source_filename:source.name,manifest_filename:manifest.name});
            if(source.size>plan.max_source_bytes||manifest.size>plan.max_manifest_bytes)throw new Error('The selected files exceed the verified intake size limit.');
            out.textContent='Uploading source ZIP…';
            let put=await fetch(plan.source.url,{method:'PUT',headers:{'Content-Type':plan.source.content_type},body:source});
            if(!put.ok)throw new Error('Source ZIP upload failed.');
            out.textContent='Uploading manifest…';
            put=await fetch(plan.manifest.url,{method:'PUT',headers:{'Content-Type':plan.manifest.content_type},body:manifest});
            if(!put.ok)throw new Error('Manifest upload failed.');
            out.textContent='Verifying every listed PDF and building the search index…';
            const result=await request('/intake/direct/complete',{upload_id:plan.upload_id,case_id:caseId,source_filename:source.name,manifest_filename:manifest.name});
            out.textContent='Verified and indexed. '+JSON.stringify(result,null,2);
          }catch(error){out.textContent='Intake failed: '+error.message;}
        };
        </script>'''.replace("__REQUESTED_CASE_ID__", json.dumps(requested_case_id)))

    @application.get("/intake/rennick", include_in_schema=False, response_model=None)
    async def rennick_upload_page(request: Request) -> HTMLResponse | RedirectResponse:
        if _browser_login(request) is None:
            nonce = secrets.token_urlsafe(24)
            expires_at = int(time.time()) + 600
            state = _sign_browser_value({"nonce": nonce, "exp": expires_at})
            settings = get_settings()
            # Reuse the callback already registered with GitHub for FastMCP.
            # A middleware below handles only browser-upload sessions; all MCP
            # OAuth callbacks continue to the FastMCP route unchanged.
            callback = f"{settings.gateway_public_url.rstrip('/')}/auth/callback"
            url = "https://github.com/login/oauth/authorize?" + urlencode(
                {"client_id": settings.github_oauth_client_id, "redirect_uri": callback, "state": state, "scope": "read:user"}
            )
            response = RedirectResponse(url=url, status_code=303)
            response.set_cookie(_RENNICK_STATE_COOKIE, nonce, max_age=600, httponly=True, secure=True, samesite="lax")
            return response
        return HTMLResponse(f'''<!doctype html><title>Rennick intake upload</title>
<main><h2>Rennick intake upload</h2><p>Select the exact ZIP and manifest.</p>
<input id="source" type="file" accept=".zip"><br><input id="manifest" type="file" accept=".json"><br>
<button id="upload">Upload and verify</button><hr><h3>Docket supplement: Docs. 5, 18, and 19</h3>
<p>Select the three PDFs together: Docs. 5, 18, and 19.</p><input id="supplement-documents" type="file" accept=".pdf" multiple><br>
<button id="upload-supplement">Upload supplement and verify</button><pre id="status"></pre></main>
<script>
const out=document.getElementById('status');
async function upload(source,manifest) {{ const r=await fetch('/intake/rennick/upload',{{method:'POST',headers:{{'X-Rennick-Source-Size':String(source.size)}},body:new Blob([source,manifest])}}); if(!r.ok) throw new Error(await r.text()); return r.json(); }}
async function uploadSupplement(documents) {{ const plan=await (await fetch('/intake/rennick/supplement/direct/prepare',{{method:'POST'}})).json(); if(!plan.ok)throw new Error(plan.error); const byName=new Map([...documents].map(file=>[file.name,file])); if(byName.size!==3||plan.uploads.some(item=>!byName.has(item.name)))throw new Error('Select exactly Docs. 5, 18, and 19.'); for(const item of plan.uploads){{const r=await fetch(item.url,{{method:'PUT',headers:{{'Content-Type':item.content_type}},body:byName.get(item.name)}});if(!r.ok)throw new Error('B2 upload failed')}} const r=await fetch('/intake/rennick/supplement/direct/complete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{upload_id:plan.upload_id}})}});if(!r.ok)throw new Error(await r.text());return r.json(); }}
document.getElementById('upload').onclick=async()=>{{try{{const s=document.getElementById('source').files[0],m=document.getElementById('manifest').files[0];if(!s||!m)throw new Error('Select both files.');out.textContent='Uploading and verifying…';out.textContent=JSON.stringify(await upload(s,m),null,2)}}catch(e){{out.textContent='Upload failed: '+e.message}}}};
document.getElementById('upload-supplement').onclick=async()=>{{try{{const files=document.getElementById('supplement-documents').files;if(files.length!==3)throw new Error('Select exactly three PDFs.');out.textContent='Uploading supplement and verifying…';out.textContent=JSON.stringify(await uploadSupplement(files),null,2)}}catch(e){{out.textContent='Supplement upload failed: '+e.message}}}};
</script>''')

    @application.get("/intake/szymczyk", include_in_schema=False, response_model=None)
    async def szymczyk_upload_page(request: Request) -> HTMLResponse | RedirectResponse:
        if _browser_login(request) is None:
            nonce = secrets.token_urlsafe(24)
            state = _sign_browser_value({"nonce": nonce, "exp": int(time.time()) + 600, "return_to": "/intake/szymczyk"})
            settings = get_settings()
            callback = f"{settings.gateway_public_url.rstrip('/')}/auth/callback"
            url = "https://github.com/login/oauth/authorize?" + urlencode({"client_id": settings.github_oauth_client_id, "redirect_uri": callback, "state": state, "scope": "read:user"})
            response = RedirectResponse(url=url, status_code=303)
            response.set_cookie(_RENNICK_STATE_COOKIE, nonce, max_age=600, httponly=True, secure=True, samesite="lax")
            return response
        return HTMLResponse('''<!doctype html><title>Szymczyk provisional intake</title><main><h2>Szymczyk provisional intake</h2><p>Select <code>wetransfer_szymczyk-case_2026-08-27_1952</code>. It uploads directly to private storage and is then hash-verified.</p><input id="source" type="file"><br><button id="upload">Upload and verify</button><pre id="status"></pre></main><script>const out=document.getElementById('status');document.getElementById('upload').onclick=async()=>{try{const file=document.getElementById('source').files[0];if(!file)throw new Error('Select the case file.');out.textContent='Preparing direct upload…';const plan=await (await fetch('/intake/szymczyk/direct/prepare',{method:'POST'})).json();if(!plan.ok)throw new Error(plan.error);if(file.size>plan.max_bytes)throw new Error('File exceeds 1 GB limit.');out.textContent='Uploading directly to private storage…';const put=await fetch(plan.url,{method:'PUT',headers:{'Content-Type':plan.content_type},body:file});if(!put.ok)throw new Error('Storage upload failed.');out.textContent='Verifying stored bytes…';const result=await (await fetch('/intake/szymczyk/direct/complete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({upload_id:plan.upload_id})})).json();if(!result.ok)throw new Error(result.error||'verification failed');out.textContent=JSON.stringify(result,null,2)}catch(e){out.textContent='Upload failed: '+e.message}};</script>''')

    async def rennick_oauth_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
        state_payload = _verified_browser_value(state)
        if not code or state_payload is None or not hmac.compare_digest(str(state_payload.get("nonce", "")), request.cookies.get(_RENNICK_STATE_COOKIE, "")):
            return RedirectResponse(url="/intake/rennick", status_code=303)
        settings = get_settings()
        callback = f"{settings.gateway_public_url.rstrip('/')}/auth/callback"
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_response = await client.post("https://github.com/login/oauth/access_token", headers={"Accept": "application/json"}, data={"client_id": settings.github_oauth_client_id, "client_secret": settings.github_oauth_client_secret, "code": code, "redirect_uri": callback})
            token = (token_response.json() if token_response.is_success else {}).get("access_token")
            user_response = await client.get("https://api.github.com/user", headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"}) if token else None
        login = (user_response.json() if user_response is not None and user_response.is_success else {}).get("login")
        if login != settings.allowed_github_login:
            return RedirectResponse(url="/intake/rennick", status_code=303)
        destination = str(state_payload.get("return_to") or "/intake/rennick")
        if destination not in {"/intake", "/intake/rennick", "/intake/szymczyk"} and not re.fullmatch(r"/intake\?case_id=NY-[A-Za-z]+-[0-9]{6}-[0-9]{4}-[A-Za-z0-9-]{2,80}", destination) and not re.fullmatch(r"/intake/szymczyk/(?:identify|promote|inventory|process)\?sha256=[0-9a-f]{64}", destination):
            destination = "/intake/rennick"
        response = RedirectResponse(url=destination, status_code=303)
        response.set_cookie(_RENNICK_SESSION_COOKIE, _sign_browser_value({"login": login, "exp": int(time.time()) + RENNICK_BROWSER_SESSION_SECONDS}), max_age=RENNICK_BROWSER_SESSION_SECONDS, httponly=True, secure=True, samesite="lax")
        response.delete_cookie(_RENNICK_STATE_COOKIE)
        return response

    @application.middleware("http")
    async def rennick_browser_callback(request: Request, call_next: Any) -> Any:
        """Handle only the browser uploader's OAuth callback.

        The existing GitHub OAuth App has ``/auth/callback`` registered for
        FastMCP. Requests without our short-lived browser state cookie pass
        straight through to FastMCP, preserving ChatGPT's OAuth flow.
        """
        if (
            request.method == "GET"
            and request.url.path == "/auth/callback"
            and request.cookies.get(_RENNICK_STATE_COOKIE)
        ):
            return await rennick_oauth_callback(
                request,
                code=request.query_params.get("code", ""),
                state=request.query_params.get("state", ""),
            )
        return await call_next(request)

    @application.post("/intake/direct/prepare", include_in_schema=False)
    async def prepare_generic_intake(request: Request) -> JSONResponse:
        if _browser_login(request) is None:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("invalid request")
            result = await _forward_generic_direct_intake("prepare", payload)
        except (ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "invalid request"}, status_code=400)
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)


    @application.post("/intake/direct/complete", include_in_schema=False)
    async def complete_generic_intake(request: Request) -> JSONResponse:
        if _browser_login(request) is None:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("invalid request")
            result = await _forward_generic_direct_intake("complete", payload)
        except (ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "invalid request"}, status_code=400)
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)


    @application.post("/intake/rennick/upload", include_in_schema=False)
    async def rennick_upload(request: Request) -> JSONResponse:
        if _browser_login(request) is None:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            source_size = int(request.headers.get("X-Rennick-Source-Size", "0"))
        except ValueError:
            source_size = 0
        body = await request.body()
        if not 0 < source_size <= RENNICK_SOURCE_BYTES_MAX or source_size >= len(body):
            return JSONResponse({"ok": False, "error": "invalid_source_size"}, status_code=400)
        source = body[:source_size]
        manifest = body[source_size:]
        if len(manifest) > RENNICK_MANIFEST_BYTES_MAX:
            return JSONResponse({"ok": False, "error": "invalid_manifest_size"}, status_code=400)
        result = await _forward_rennick_pair(source, manifest)
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)

    @application.post("/intake/rennick/promote", include_in_schema=False)
    async def rennick_promote(request: Request) -> JSONResponse:
        if _browser_login(request) is None:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        result = await _forward_rennick_promotion()
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)

    @application.post("/intake/rennick/supplement", include_in_schema=False)
    async def rennick_docket_supplement_upload(request: Request) -> JSONResponse:
        if _browser_login(request) is None:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            archive_size = int(request.query_params.get("archive_size") or request.headers.get("X-Rennick-Supplement-Archive-Size", "0"))
        except ValueError:
            archive_size = 0
        body = await request.body()
        if not 0 < archive_size <= RENNICK_SOURCE_BYTES_MAX or archive_size >= len(body):
            return JSONResponse({"ok": False, "error": "invalid_supplement_archive_size"}, status_code=400)
        archive, manifest = body[:archive_size], body[archive_size:]
        if len(manifest) > RENNICK_MANIFEST_BYTES_MAX:
            return JSONResponse({"ok": False, "error": "invalid_supplement_manifest_size"}, status_code=400)
        result = await _forward_rennick_supplement_pair(archive, manifest)
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)

    @application.post("/intake/rennick/supplement/direct/prepare", include_in_schema=False)
    async def rennick_direct_supplement_prepare(request: Request) -> JSONResponse:
        if _browser_login(request) is None:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        result = await _forward_rennick_direct_supplement("prepare")
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)

    @application.post("/intake/rennick/supplement/direct/complete", include_in_schema=False)
    async def rennick_direct_supplement_complete(request: Request) -> JSONResponse:
        if _browser_login(request) is None:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"ok": False, "error": "invalid_completion_payload"}, status_code=400)
        result = await _forward_rennick_direct_supplement("complete", payload)
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)

    @application.post("/intake/szymczyk/direct/prepare", include_in_schema=False)
    async def szymczyk_direct_prepare(request: Request) -> JSONResponse:
        if _browser_login(request) is None:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        result = await _forward_szymczyk_direct("prepare")
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)

    @application.post("/intake/szymczyk/direct/complete", include_in_schema=False)
    async def szymczyk_direct_complete(request: Request) -> JSONResponse:
        if _browser_login(request) is None:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        result = await _forward_szymczyk_direct("complete", payload)
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)

    @application.get("/intake/szymczyk/inspect", include_in_schema=False)
    async def szymczyk_inspect(request: Request, sha256: str) -> JSONResponse:
        if _browser_login(request) is None:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        result = await _forward_szymczyk_inspection({"sha256": sha256})
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)

    @application.get("/intake/szymczyk/identify", include_in_schema=False, response_model=None)
    async def szymczyk_identify(request: Request, sha256: str) -> JSONResponse | RedirectResponse:
        if _browser_login(request) is None:
            nonce = secrets.token_urlsafe(24)
            return_to = f"/intake/szymczyk/identify?sha256={sha256}"
            state = _sign_browser_value({"nonce": nonce, "exp": int(time.time()) + 600, "return_to": return_to})
            settings = get_settings()
            callback = f"{settings.gateway_public_url.rstrip('/')}/auth/callback"
            url = "https://github.com/login/oauth/authorize?" + urlencode({"client_id": settings.github_oauth_client_id, "redirect_uri": callback, "state": state, "scope": "read:user"})
            response = RedirectResponse(url=url, status_code=303)
            response.set_cookie(_RENNICK_STATE_COOKIE, nonce, max_age=600, httponly=True, secure=True, samesite="lax")
            return response
        result = await _forward_szymczyk_identification({"sha256": sha256})
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)

    @application.get("/intake/szymczyk/promote", include_in_schema=False, response_model=None)
    async def szymczyk_promote(request: Request, sha256: str) -> JSONResponse | RedirectResponse:
        if _browser_login(request) is None:
            nonce = secrets.token_urlsafe(24)
            return_to = f"/intake/szymczyk/promote?sha256={sha256}"
            state = _sign_browser_value({"nonce": nonce, "exp": int(time.time()) + 600, "return_to": return_to})
            settings = get_settings()
            callback = f"{settings.gateway_public_url.rstrip('/')}/auth/callback"
            url = "https://github.com/login/oauth/authorize?" + urlencode({"client_id": settings.github_oauth_client_id, "redirect_uri": callback, "state": state, "scope": "read:user"})
            response = RedirectResponse(url=url, status_code=303)
            response.set_cookie(_RENNICK_STATE_COOKIE, nonce, max_age=600, httponly=True, secure=True, samesite="lax")
            return response
        result = await _forward_szymczyk_promotion({"sha256": sha256})
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)

    @application.get("/intake/szymczyk/inventory", include_in_schema=False, response_model=None)
    async def szymczyk_inventory(request: Request, sha256: str) -> JSONResponse | RedirectResponse:
        if _browser_login(request) is None:
            nonce = secrets.token_urlsafe(24)
            return_to = f"/intake/szymczyk/inventory?sha256={sha256}"
            state = _sign_browser_value({"nonce": nonce, "exp": int(time.time()) + 600, "return_to": return_to})
            settings = get_settings()
            callback = f"{settings.gateway_public_url.rstrip('/')}/auth/callback"
            url = "https://github.com/login/oauth/authorize?" + urlencode({"client_id": settings.github_oauth_client_id, "redirect_uri": callback, "state": state, "scope": "read:user"})
            response = RedirectResponse(url=url, status_code=303)
            response.set_cookie(_RENNICK_STATE_COOKIE, nonce, max_age=600, httponly=True, secure=True, samesite="lax")
            return response
        result = await _forward_szymczyk_inventory({"sha256": sha256})
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)

    @application.get("/intake/szymczyk/process", include_in_schema=False, response_model=None)
    async def szymczyk_pipeline(request: Request, sha256: str) -> JSONResponse | RedirectResponse:
        if _browser_login(request) is None:
            nonce = secrets.token_urlsafe(24)
            return_to = f"/intake/szymczyk/process?sha256={sha256}"
            state = _sign_browser_value({"nonce": nonce, "exp": int(time.time()) + 600, "return_to": return_to})
            settings = get_settings()
            callback = f"{settings.gateway_public_url.rstrip('/')}/auth/callback"
            url = "https://github.com/login/oauth/authorize?" + urlencode({"client_id": settings.github_oauth_client_id, "redirect_uri": callback, "state": state, "scope": "read:user"})
            response = RedirectResponse(url=url, status_code=303)
            response.set_cookie(_RENNICK_STATE_COOKIE, nonce, max_age=600, httponly=True, secure=True, samesite="lax")
            return response
        result = await _forward_szymczyk_pipeline({"sha256": sha256})
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)

    @application.post("/cases/verified/read-pages", include_in_schema=False)
    async def verified_case_pages(request: Request) -> JSONResponse:
        if _browser_login(request) is None:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        result = await _forward_verified_case_pages(payload)
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)

    @application.post("/cases/verified/search", include_in_schema=False)
    async def verified_case_search(request: Request) -> JSONResponse:
        if _browser_login(request) is None: return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try: payload = await request.json()
        except ValueError: return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        result = await _forward_verified_case_operation("search", payload)
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)

    @application.post("/cases/verified/build-index", include_in_schema=False)
    async def verified_case_index(request: Request) -> JSONResponse:
        if _browser_login(request) is None: return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try: payload = await request.json()
        except ValueError: return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        result = await _forward_verified_case_operation("build-index", payload)
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)

    @application.get("/registry")
    async def registry_endpoint() -> dict[str, Any]:
        """Return the machine-readable logical registry (no live secrets)."""
        settings = get_settings()
        payload = settings.registry.as_public_dict()
        payload["identity"] = canonical_gateway_identity(
            public_url=settings.gateway_public_url
        )
        payload["request_id"] = get_request_id()
        payload["correlation_id"] = get_correlation_id()
        payload["resolved_downstreams"] = {
            item.key: {
                "service_id": item.service_id,
                "configured": item.configured,
                "base_url_env": item.base_url_env,
                "base_url": item.base_url,
                "health_url": item.health_url,
            }
            for item in settings.downstreams
        }
        return payload

    @application.get("/health")
    async def health(request: Request) -> JSONResponse:
        """Gateway liveness plus independent downstream health aggregation.

        Always returns HTTP 200 when the gateway process itself is up so a single
        unhealthy downstream cannot take the gateway (or unrelated capabilities)
        out of Railway rotation. Reports exact registered MCP tool names and the
        Railway-provided commit SHA. Never includes secrets.
        """
        settings = get_settings()
        tools = list(_registered_tools)
        if not tools and _mcp is not None:
            tools = await list_registered_tool_names(_mcp)
        payload = await aggregate_health(settings, registered_tools=tools)
        payload["request_id"] = getattr(
            request.state, "request_id", payload.get("request_id")
        )
        payload["correlation_id"] = getattr(
            request.state, "correlation_id", payload.get("correlation_id")
        )
        payload["deployed_commit_sha"] = settings.deployed_commit_sha
        payload["auth"] = {
            "inbound": "github_oauth",
            "downstream_bridge": "service_credential",
        }
        payload["identity"] = canonical_gateway_identity(
            public_url=settings.gateway_public_url
        )
        payload["required_tool_parity"] = required_tool_parity(tools)
        return JSONResponse(payload)

    @application.get("/ready")
    async def ready() -> JSONResponse:
        """Deployment readiness: fail closed on public-tool registration drift."""
        tools = list(_registered_tools)
        if not tools and _mcp is not None:
            tools = await list_registered_tool_names(_mcp)
        parity = required_tool_parity(tools)
        status_code = 200 if parity["ok"] else 503
        return JSONResponse(
            {
                "service": "hal-legalai-gateway",
                "deployed_commit_sha": get_settings().deployed_commit_sha,
                "required_tool_parity": parity,
            },
            status_code=status_code,
        )

    return application


app = create_app()


def main() -> None:
    """Entry point for local/Railway process start."""
    import uvicorn

    # Fail closed on invalid configuration before binding the port.
    load_settings()
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(
        "hal_legalai_gateway.server:app",
        host="0.0.0.0",
        port=port,
        factory=False,
    )


if __name__ == "__main__":
    main()
