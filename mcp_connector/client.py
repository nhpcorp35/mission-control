from __future__ import annotations

import re
import uuid
from typing import Any, Mapping
from urllib.parse import quote

import httpx

from mcp_connector.config import Settings
from mcp_connector.errors import MissionControlError
from mission_control.monitoring import (
    MONITOR_CURSOR_MAX_CHARS,
    normalize_monitor_cursor,
)
from mission_control.notifications import (
    NOTIFICATION_INSPECT_MAX_EVENTS,
    redact_notification_error,
)

# Honor caller-requested wait budgets end-to-end (aligned with
# POST /runs/{run_id}/wait). Default stays short for interactive clients;
# values up to MCP_WAIT_MAX_TIMEOUT_SECONDS are not artificially cut off
# at ~25s. Platform edge proxies may still close long idle HTTP tool
# responses — see MISSION_CONTROL_API.md.
MCP_WAIT_DEFAULT_TIMEOUT_SECONDS = 20.0
MCP_WAIT_MIN_TIMEOUT_SECONDS = 0.1
MCP_WAIT_MAX_TIMEOUT_SECONDS = 3600.0
MCP_WAIT_DEFAULT_POLL_INTERVAL_SECONDS = 2.0
MCP_WAIT_MIN_POLL_INTERVAL_SECONDS = 0.05
MCP_WAIT_MAX_POLL_INTERVAL_SECONDS = 10.0
# Re-export for connector/gateway callers and tests.
MCP_MONITOR_CURSOR_MAX_CHARS = MONITOR_CURSOR_MAX_CHARS

# Phase 2C notification inspection (GET /runs/{run_id}/notifications).
MCP_NOTIFICATION_DEFAULT_LIMIT = NOTIFICATION_INSPECT_MAX_EVENTS
MCP_NOTIFICATION_MAX_LIMIT = NOTIFICATION_INSPECT_MAX_EVENTS
MCP_NOTIFICATION_MAX_RUN_ID_CHARS = 128
_NOTIFICATION_RESPONSE_ALLOWED_KEYS = frozenset(
    {
        "run_id",
        "notifications_enabled",
        "events",
        "truncated",
        "max_events",
    }
)
_NOTIFICATION_EVENT_ALLOWED_KEYS = frozenset(
    {
        "event_id",
        "run_id",
        "event_kind",
        "status",
        "phase",
        "progress",
        "heartbeat_health",
        "occurred_at",
        "delivery_state",
        "attempt_count",
        "next_attempt_at",
        "last_error",
        "delivered_at",
        "created_at",
    }
)
_NOTIFICATION_PROGRESS_ALLOWED_KEYS = frozenset({"step", "detail"})
# Strip if a misbehaving downstream ever echoes these.
_NOTIFICATION_FORBIDDEN_KEYS = frozenset(
    {
        "webhook_url",
        "webhook_secret",
        "secret",
        "claim_owner",
        "payload_json",
        "headers",
        "raw_headers",
        "request_headers",
        "raw_body",
        "request_body",
        "body",
        "authorization",
        "signature",
        "hmac",
        "MISSION_CONTROL_NOTIFICATIONS_WEBHOOK_URL",
        "MISSION_CONTROL_NOTIFICATIONS_WEBHOOK_SECRET",
    }
)


def normalize_mcp_notification_run_id(run_id: str) -> str:
    """Validate ``run_id`` before notification inspection requests."""
    value = str(run_id).strip()
    if not value:
        raise ValueError("run_id must not be empty")
    if len(value) > MCP_NOTIFICATION_MAX_RUN_ID_CHARS:
        raise ValueError(
            "run_id must be at most "
            f"{MCP_NOTIFICATION_MAX_RUN_ID_CHARS} characters"
        )
    if any(ch in value for ch in ("/", "?", "#", "\n", "\r", "\0")):
        raise ValueError("run_id contains invalid characters")
    return value


def normalize_mcp_notification_limit(limit: int | float | None) -> int:
    """Validate and clamp notification inspection ``limit``.

    Values at or below zero are rejected. Values above
    ``MCP_NOTIFICATION_MAX_LIMIT`` are capped (not rejected).
    """
    if limit is None:
        return MCP_NOTIFICATION_DEFAULT_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if value <= 0:
        raise ValueError("limit must be a positive integer")
    if value > MCP_NOTIFICATION_MAX_LIMIT:
        return MCP_NOTIFICATION_MAX_LIMIT
    return value


def project_notification_inspection(
    body: Mapping[str, Any],
    *,
    limit: int,
) -> dict[str, Any]:
    """Allowlist/redact notification inspection payloads and apply ``limit``.

    Never returns webhook URL/secret, claim owner, raw request material, or
    non-allowlisted event fields even if a downstream payload includes them.
    """
    bound = normalize_mcp_notification_limit(limit)
    raw_events = body.get("events") if isinstance(body, Mapping) else None
    events_in = raw_events if isinstance(raw_events, list) else []
    projected_events: list[dict[str, Any]] = []
    for item in events_in:
        if not isinstance(item, Mapping):
            continue
        event: dict[str, Any] = {}
        for key in _NOTIFICATION_EVENT_ALLOWED_KEYS:
            if key not in item or key in _NOTIFICATION_FORBIDDEN_KEYS:
                continue
            if key == "progress":
                progress = item.get("progress")
                if isinstance(progress, Mapping):
                    event["progress"] = {
                        pk: str(progress.get(pk, ""))[:160]
                        for pk in _NOTIFICATION_PROGRESS_ALLOWED_KEYS
                        if pk in progress
                    }
                elif progress is None:
                    event["progress"] = None
                continue
            if key == "last_error":
                event["last_error"] = redact_notification_error(
                    None if item.get("last_error") is None else str(item.get("last_error"))
                )
                continue
            if key == "attempt_count":
                try:
                    event["attempt_count"] = int(item.get("attempt_count") or 0)
                except (TypeError, ValueError):
                    event["attempt_count"] = 0
                continue
            raw = item.get(key)
            event[key] = None if raw is None else str(raw)[:160]
        projected_events.append(event)

    truncated_upstream = bool(body.get("truncated")) if isinstance(body, Mapping) else False
    limited_events = projected_events[:bound]
    truncated = truncated_upstream or len(projected_events) > bound

    max_events = bound
    if isinstance(body, Mapping) and body.get("max_events") is not None:
        try:
            max_events = min(bound, int(body["max_events"]))
        except (TypeError, ValueError):
            max_events = bound

    run_id = ""
    enabled = False
    if isinstance(body, Mapping):
        run_id = str(body.get("run_id") or "")[: MCP_NOTIFICATION_MAX_RUN_ID_CHARS]
        enabled = bool(body.get("notifications_enabled"))

    projected: dict[str, Any] = {
        "run_id": run_id,
        "notifications_enabled": enabled,
        "events": limited_events,
        "truncated": truncated,
        "max_events": max_events,
    }
    # Drop any accidental non-allowlisted keys; never pass forbidden through.
    return {
        key: projected[key]
        for key in _NOTIFICATION_RESPONSE_ALLOWED_KEYS
        if key in projected
    }


def normalize_mcp_wait_timeout(timeout_seconds: float) -> float:
    """Validate and clamp ``timeout_seconds`` for MCP ``wait_for_run``.

    Values at or below zero, or below the minimum, are rejected. Values
    above ``MCP_WAIT_MAX_TIMEOUT_SECONDS`` are capped (not rejected) so
    oversized requests still wait for the maximum supported window.
    """
    value = float(timeout_seconds)
    if value <= 0:
        raise ValueError("timeout_seconds must be a positive number")
    if value < MCP_WAIT_MIN_TIMEOUT_SECONDS:
        raise ValueError(
            "timeout_seconds must be >= "
            f"{MCP_WAIT_MIN_TIMEOUT_SECONDS}"
        )
    if value > MCP_WAIT_MAX_TIMEOUT_SECONDS:
        return MCP_WAIT_MAX_TIMEOUT_SECONDS
    return value


def normalize_mcp_wait_poll_interval(poll_interval_seconds: float) -> float:
    """Validate and clamp ``poll_interval_seconds`` for MCP waits."""
    value = float(poll_interval_seconds)
    if value <= 0:
        raise ValueError(
            "poll_interval_seconds must be a positive number"
        )
    if value < MCP_WAIT_MIN_POLL_INTERVAL_SECONDS:
        raise ValueError(
            "poll_interval_seconds must be >= "
            f"{MCP_WAIT_MIN_POLL_INTERVAL_SECONDS}"
        )
    if value > MCP_WAIT_MAX_POLL_INTERVAL_SECONDS:
        return MCP_WAIT_MAX_POLL_INTERVAL_SECONDS
    return value


def normalize_mcp_wait_cursor(cursor: str | None) -> str | None:
    """Validate optional wait cursor size before forwarding to Mission Control."""
    return normalize_monitor_cursor(cursor)


# Workflow submit/status (POST /workflows, GET /workflows/{workflow_id}).
# Keep aligned with mission_control.workflow_submit.parse_idempotency_key.
MCP_WORKFLOW_MAX_IDEMPOTENCY_KEY_CHARS = 128
_MCP_WORKFLOW_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._~:-]{1,128}$")


def normalize_mcp_workflow_yaml(workflow_yaml: str) -> str:
    """Reject empty workflow YAML without echoing the document."""
    value = "" if workflow_yaml is None else str(workflow_yaml)
    if not value.strip():
        raise ValueError("workflow_yaml must not be empty")
    return value


def normalize_mcp_workflow_id(workflow_id: str) -> str:
    """Accept only canonical registry uuid4/uuid5 workflow IDs.

    Matches ``str(uuid.uuid4())`` / ``str(uuid.uuid5(...))`` as used by the
    workflow registry (lowercase 8-4-4-4-12 with version 4 or 5). Empty
    values and all other malformed IDs — including oversized, noncanonical,
    slash, backslash, dot-segment, percent-encoded separator, query,
    fragment, CR/LF/NUL, and whitespace — are rejected with fixed messages
    that never echo the input.
    """
    value = "" if workflow_id is None else str(workflow_id)
    if not value or not value.strip():
        raise ValueError("workflow_id must not be empty")
    # Canonical uuid4/uuid5 strings are exactly 36 characters.
    if len(value) != 36:
        raise ValueError("workflow_id is invalid")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise ValueError("workflow_id is invalid") from None
    if str(parsed) != value or parsed.version not in (4, 5):
        raise ValueError("workflow_id is invalid")
    return value


def normalize_mcp_workflow_idempotency_key(
    idempotency_key: str | None,
) -> str | None:
    """Return a validated Idempotency-Key, or None when omitted.

    Empty/whitespace keys are treated as omitted. Invalid keys are rejected
    with a message that never includes the raw key, YAML, or secrets.
    """
    if idempotency_key is None:
        return None
    key = str(idempotency_key).strip()
    if not key:
        return None
    if not _MCP_WORKFLOW_IDEMPOTENCY_KEY_RE.match(key):
        raise ValueError("idempotency_key is invalid")
    return key


class MissionControlClient:
    """Thin asynchronous client for the Mission Control REST API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": (
                f"Bearer {self._settings.mission_control_api_key}"
            ),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        request_timeout = (
            self._settings.request_timeout_seconds
            if timeout is None
            else timeout
        )
        request_headers = self._headers()
        if headers:
            request_headers.update(dict(headers))
        try:
            async with httpx.AsyncClient(
                base_url=self._settings.mission_control_url,
                headers=request_headers,
                timeout=request_timeout,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    json=json,
                    params=params,
                )
        except httpx.TimeoutException as exc:
            raise MissionControlError(
                "Mission Control did not respond before the timeout"
            ) from exc
        except httpx.RequestError as exc:
            raise MissionControlError(
                f"Could not reach Mission Control: {exc}"
            ) from exc

        try:
            body: Any = response.json()
        except ValueError:
            body = {"raw_response": response.text[:4000]}

        if response.is_error:
            raise MissionControlError(
                "Mission Control request failed",
                status_code=response.status_code,
                details=body,
            )

        if not isinstance(body, dict):
            raise MissionControlError(
                "Mission Control returned a non-object JSON response",
                status_code=response.status_code,
                details=body,
            )

        return body

    async def submit_run(self, mission_yaml: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/runs",
            json={"mission_yaml": mission_yaml},
        )

    async def submit_structured_run(
        self,
        *,
        mission_id: str,
        title: str,
        instructions: str,
        deliverables: list[Any],
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
        payload: dict[str, Any] = {
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
            "allow_automatic_platform_push": (
                allow_automatic_platform_push
            ),
        }
        # Omit unset persistence_mode so the API/builder can infer push for
        # mutations and none for read-only; explicit values stay authoritative.
        if persistence_mode is not None:
            payload["persistence_mode"] = persistence_mode
        # Omit unset flat approval so nested-only approval is not conflicted
        # by an invented default false.
        if platform_push_approved is not None:
            payload["platform_push_approved"] = platform_push_approved
        if approval is not None:
            payload["approval"] = approval
        return await self._request(
            "POST",
            "/runs/structured",
            json=payload,
        )

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/runs/{run_id}")

    async def submit_workflow(
        self,
        workflow_yaml: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Submit bounded workflow YAML via authenticated POST /workflows.

        Optional ``idempotency_key`` is forwarded as the ``Idempotency-Key``
        header. Empty YAML and invalid keys are rejected locally without
        echoing YAML or secrets. Feature-disabled production remains
        fail-closed at the HTTP API (403).
        """
        yaml_text = normalize_mcp_workflow_yaml(workflow_yaml)
        key = normalize_mcp_workflow_idempotency_key(idempotency_key)
        extra_headers = (
            {"Idempotency-Key": key} if key is not None else None
        )
        return await self._request(
            "POST",
            "/workflows",
            json={"workflow_yaml": yaml_text},
            headers=extra_headers,
        )

    async def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Fetch sanitized workflow status via GET /workflows/{workflow_id}.

        Only canonical uuid4/uuid5 IDs are accepted. Invalid IDs are rejected
        locally and never forwarded. The validated ID is percent-encoded as a
        single path segment before interpolation. Production remains
        fail-closed when workflow orchestration is disabled (HTTP 403). The
        API response is already sanitized (no secrets, no child mission YAML).
        """
        safe_id = normalize_mcp_workflow_id(workflow_id)
        return await self._request(
            "GET",
            f"/workflows/{quote(safe_id, safe='')}",
        )

    async def cancel_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Cancel a workflow via POST /workflows/{workflow_id}/cancel.

        Only canonical uuid4/uuid5 IDs are accepted. Invalid IDs are rejected
        locally and never forwarded. The validated ID is percent-encoded as a
        single path segment before interpolation. Production remains
        fail-closed when workflow orchestration is disabled (HTTP 403). The
        API response is already sanitized (no secrets, no child mission YAML).
        """
        safe_id = normalize_mcp_workflow_id(workflow_id)
        return await self._request(
            "POST",
            f"/workflows/{quote(safe_id, safe='')}/cancel",
        )

    async def list_run_notifications(
        self,
        run_id: str,
        *,
        limit: int | float | None = None,
    ) -> dict[str, Any]:
        """Fetch bounded, redacted notification inspection for a run.

        Forwards to authenticated ``GET /runs/{run_id}/notifications``.
        Applies a safely bounded ``limit``, allowlists response fields, and
        strips webhook/secret/claim/raw-request material even if present
        downstream. Does not change mission wait/cursor behavior.
        """
        safe_run_id = normalize_mcp_notification_run_id(run_id)
        bound = normalize_mcp_notification_limit(limit)
        body = await self._request(
            "GET",
            f"/runs/{safe_run_id}/notifications",
            params={"limit": bound},
        )
        projected = project_notification_inspection(body, limit=bound)
        # Prefer the validated run_id we requested when upstream omits it.
        if not projected.get("run_id"):
            projected["run_id"] = safe_run_id
        return projected

    async def run_repository_command(
        self,
        *,
        repository: str,
        ref: str,
        argv: list[str],
        working_directory: str = ".",
        timeout_seconds: float = 300.0,
        allowed_env_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Execute an allowlisted repository command (POST /repository-commands)."""
        # Bound httpx timeout above the command timeout so the API can respond.
        request_timeout = max(float(timeout_seconds) + 30.0, 60.0)
        return await self._request(
            "POST",
            "/repository-commands",
            json={
                "repository": repository,
                "ref": ref,
                "argv": argv,
                "working_directory": working_directory,
                "timeout_seconds": timeout_seconds,
                "allowed_env_names": list(allowed_env_names or []),
            },
            timeout=request_timeout,
        )

    async def submit_and_wait(
        self,
        mission_yaml: str,
        *,
        timeout_seconds: float = MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = MCP_WAIT_DEFAULT_POLL_INTERVAL_SECONDS,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Submit exact mission YAML, then wait via ``wait_for_run``.

        Reuses the authenticated ``submit_run`` and ``wait_for_run`` paths.
        Wait parameter validation/clamping matches ``wait_for_run`` and runs
        before submission so an invalid timeout does not queue a run.

        On structured submission failure (``ok: false``, no ``run_id``),
        returns that payload without entering the wait loop. On success,
        returns the ``wait_for_run`` payload (accepted ``run_id`` plus the
        final authoritative run fields, including Phase 2B monitoring fields
        and ``wait_expired`` when the caller-requested wait window expires).
        """
        # Validate wait bounds before submit so bad timeouts never queue a run.
        effective_timeout = normalize_mcp_wait_timeout(timeout_seconds)
        effective_poll = normalize_mcp_wait_poll_interval(
            poll_interval_seconds
        )
        effective_cursor = normalize_mcp_wait_cursor(cursor)

        submitted = await self.submit_run(mission_yaml)
        run_id = submitted.get("run_id")
        if submitted.get("ok") is False or not run_id:
            return submitted

        return await self.wait_for_run(
            str(run_id),
            timeout_seconds=effective_timeout,
            poll_interval_seconds=effective_poll,
            cursor=effective_cursor,
        )

    async def wait_for_run(
        self,
        run_id: str,
        *,
        timeout_seconds: float = MCP_WAIT_DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = MCP_WAIT_DEFAULT_POLL_INTERVAL_SECONDS,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Forward to Mission Control ``POST /runs/{run_id}/wait``.

        Mission Control is the source of truth for Phase 2B monitoring fields
        (``heartbeat_health``, ``stale_heartbeat``, ``monitoring_history``,
        ``cursor``, ``stale_threshold_seconds``). This client does not
        fabricate monitoring fields; it validates bounds then returns the
        authoritative wait payload unchanged.

        Optional ``cursor`` resumes bounded history after ``wait_expired``.
        Oversized cursors are rejected before the request is sent. Wait
        expiry never mutates or cancels the run.
        """
        effective_timeout = normalize_mcp_wait_timeout(timeout_seconds)
        effective_poll = normalize_mcp_wait_poll_interval(
            poll_interval_seconds
        )
        effective_cursor = normalize_mcp_wait_cursor(cursor)

        body: dict[str, Any] = {
            "timeout_seconds": effective_timeout,
            "poll_interval_seconds": effective_poll,
        }
        if effective_cursor is not None:
            body["cursor"] = effective_cursor

        # Bound httpx timeout above the wait budget so Mission Control can
        # return wait_expired within the caller-selected duration.
        request_timeout = max(effective_timeout + 30.0, 60.0)
        return await self._request(
            "POST",
            f"/runs/{run_id}/wait",
            json=body,
            timeout=request_timeout,
        )
