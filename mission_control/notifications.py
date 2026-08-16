"""Phase 2C/2D durable mission notification outbox and delivery backends.

Opt-in generic HMAC webhook and native Pushover delivery for phase_change,
stale, recovery, and terminal events only (never heartbeats). Failures never
mutate mission status. Network delivery stays off request/wait/status paths.

Orchestrated workflows enqueue one durable ``terminal`` row with a
namespaced outbox identity (``workflow:<workflow_id>`` +
``workflow-terminal:<state>``) so they cannot collide with standalone run
terminals. Payloads are allowlisted workflow fields only.

Pushover phone alerts are tuned for one audible notification per normal
mission: routine ``phase_change`` events remain durable for inspection but are
intentionally skipped for the native Pushover backend (webhook delivery of
``phase_change`` is unchanged). Stale and recovery alerts still deliver.

Heartbeat stale/recovery pairing is restart-safe: once a stale event is
durably enqueued, SQLite records an open stale episode so the first later
healthy observation enqueues exactly one paired recovery (independent of
in-memory wait cursors). Open-episode transitions and the corresponding
outbox insert run in one ``BEGIN IMMEDIATE`` transaction with a conditional
UPDATE (CAS) so terminal-versus-healthy races across connections admit
exactly one winner. Terminalization while still stale closes the episode
without a false recovery.

Once a run is terminal, pending or in-flight heartbeat ``stale`` /
paired ``recovery`` (dedupe ``recovery:stale:*``) rows are auditably
skipped rather than delivered. Terminal-dependent delivery finalization
(success, retry, and dead-letter) uses one ``BEGIN IMMEDIATE`` primitive
that re-reads canonical run status in the same transaction as the CAS
state transition, so a concurrent terminal commit cannot lose to
``delivered`` / retry / dead-letter. Missing ``runs`` table or run
row/status fail closed (auditable ``skipped``, no paging). Interrupted-run
startup ``recovery`` rows and ``terminal`` / ``phase_change`` events are
preserved.

Workflow child runs are identified only from the durable
``workflow_steps.child_run_id`` relationship in the shared SQLite
database (never names, prefixes, process memory, or caller flags).
Synthetic workflow outbox ids (``workflow:<workflow_id>``) are never
children. Child ``stale``, ``recovery``, ``phase_change``, and
``terminal`` rows remain auditable but transition to ``skipped`` with
``workflow_child_suppressed`` at enqueue, claim, and the same race-safe
delivery finalization boundary so they cannot page once membership
exists. Standalone missions and workflow-level terminal alerts are
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import socket
import sqlite3
import threading
import time
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse
import uuid

import httpx

from mission_control.monitoring import (
    HEARTBEAT_STALE_THRESHOLD_SECONDS,
    HeartbeatHealth,
    classify_heartbeat_health,
    validate_stale_threshold_seconds,
)
from mission_control.run_registry import (
    TERMINAL_STATUSES,
    RunPhase,
    RunRecord,
    RunStatus,
    is_terminal_status,
    resolve_db_path,
    sanitize_progress,
)

logger = logging.getLogger(__name__)

_OUTBOX_TABLE = "notification_outbox"
_STALE_EPISODE_TABLE = "notification_stale_episodes"
# Shared SQLite table owned by WorkflowRegistry; queried, never migrated here.
_WORKFLOW_STEPS_TABLE = "workflow_steps"
_STALE_EPISODE_OPEN = "open"
_STALE_EPISODE_RECOVERED = "recovered"
_STALE_EPISODE_CLOSED_TERMINAL = "closed_terminal"
_SQLITE_BUSY_TIMEOUT_MS = 5000

# Delivery bounds (operator-overridable via env).
DEFAULT_WEBHOOK_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_BACKOFF_MAX_SECONDS = 300.0
DEFAULT_CLAIM_LEASE_SECONDS = 30.0
DEFAULT_WORKER_POLL_SECONDS = 1.0
NOTIFICATION_INSPECT_MAX_EVENTS = 64

# Opt-in configuration (safe no-config: disabled when unset).
WEBHOOK_URL_ENV = "MISSION_CONTROL_NOTIFICATIONS_WEBHOOK_URL"
WEBHOOK_SECRET_ENV = "MISSION_CONTROL_NOTIFICATIONS_WEBHOOK_SECRET"
PUSHOVER_USER_KEY_ENV = "MISSION_CONTROL_NOTIFICATIONS_PUSHOVER_USER_KEY"
PUSHOVER_APP_TOKEN_ENV = "MISSION_CONTROL_NOTIFICATIONS_PUSHOVER_APP_TOKEN"
PUSHOVER_DEVICE_ENV = "MISSION_CONTROL_NOTIFICATIONS_PUSHOVER_DEVICE"
PUSHOVER_PRIORITY_ENV = "MISSION_CONTROL_NOTIFICATIONS_PUSHOVER_PRIORITY"
PUSHOVER_SOUND_ENV = "MISSION_CONTROL_NOTIFICATIONS_PUSHOVER_SOUND"
ENABLED_ENV = "MISSION_CONTROL_NOTIFICATIONS_ENABLED"
TIMEOUT_ENV = "MISSION_CONTROL_NOTIFICATIONS_TIMEOUT_SECONDS"
MAX_ATTEMPTS_ENV = "MISSION_CONTROL_NOTIFICATIONS_MAX_ATTEMPTS"
BACKOFF_BASE_ENV = "MISSION_CONTROL_NOTIFICATIONS_BACKOFF_BASE_SECONDS"
BACKOFF_MAX_ENV = "MISSION_CONTROL_NOTIFICATIONS_BACKOFF_MAX_SECONDS"
CLAIM_LEASE_ENV = "MISSION_CONTROL_NOTIFICATIONS_CLAIM_LEASE_SECONDS"
ALLOW_HTTP_ENV = "MISSION_CONTROL_NOTIFICATIONS_ALLOW_HTTP"
WORKER_POLL_ENV = "MISSION_CONTROL_NOTIFICATIONS_WORKER_POLL_SECONDS"

SIGNATURE_HEADER = "X-Mission-Control-Signature"
TIMESTAMP_HEADER = "X-Mission-Control-Timestamp"
EVENT_ID_HEADER = "X-Mission-Control-Event-Id"
EVENT_KIND_HEADER = "X-Mission-Control-Event-Kind"

# Fixed official Pushover Messages API (no user-configurable endpoint).
PUSHOVER_API_HOST = "api.pushover.net"
PUSHOVER_MESSAGES_PATH = "/1/messages.json"
PUSHOVER_API_URL = f"https://{PUSHOVER_API_HOST}{PUSHOVER_MESSAGES_PATH}"
DEFAULT_PUSHOVER_PRIORITY = 0
# Official Pushover default sound name (explicit in Messages API form).
DEFAULT_PUSHOVER_SOUND = "pushover"
# Emergency priority (2) requires retry/expire and is rejected for safety.
ALLOWED_PUSHOVER_PRIORITIES = frozenset({-2, -1, 0, 1})
PUSHOVER_TITLE_MAX_CHARS = 100
PUSHOVER_MESSAGE_MAX_CHARS = 400
PUSHOVER_DEVICE_MAX_CHARS = 64
PUSHOVER_SOUND_MAX_CHARS = 32
# Terminal outbox reason when Pushover skips routine phase_change (not dead).
PUSHOVER_PHASE_CHANGE_SUPPRESSED = "pushover_phase_change_suppressed"
# One-time hotfix: suppress stale/recovery rows enqueued before the 2026-08-14
# redeploy so the delivery worker does not replay the pre-container backlog.
LEGACY_PREDEPLOY_BACKLOG_CUTOFF_UTC = datetime(
    2026, 8, 14, 16, 38, 0, tzinfo=timezone.utc
)
LEGACY_PREDEPLOY_BACKLOG_SUPPRESSED = "legacy_predeploy_backlog_suppressed"
# Generic rule: heartbeat stale / paired recovery must not page after the run
# is already terminal (auditable skipped; history retained).
STALE_RECOVERY_TERMINAL_RUN_SUPPRESSED = "stale_recovery_terminal_run_suppressed"
# Fail-closed disposition when runs table / run row / status cannot be read.
STALE_RECOVERY_RUN_STATUS_UNAVAILABLE = "stale_recovery_run_status_unavailable"
# Durable child-run phone suppression (all event kinds; history retained).
WORKFLOW_CHILD_SUPPRESSED = "workflow_child_suppressed"
# Paired heartbeat recovery dedupe prefix (not interrupted-run startup recovery).
_HEARTBEAT_RECOVERY_DEDUPE_PREFIX = "recovery:stale:"

# Active delivery backend when notifications are opted in.
BACKEND_NONE = "none"
BACKEND_WEBHOOK = "webhook"
BACKEND_PUSHOVER = "pushover"

# Payload / inspection allowlists (never stdout/stderr/secrets/mission YAML).
_PAYLOAD_ALLOWED_KEYS = frozenset(
    {
        "run_id",
        "event_kind",
        "status",
        "phase",
        "progress",
        "heartbeat_health",
        "occurred_at",
        "dedupe_key",
        "workflow_id",
        "child_run_count",
        "fix_cycle_count",
        "credit_units_used",
    }
)
_PAYLOAD_COUNT_KEYS = frozenset(
    {"child_run_count", "fix_cycle_count", "credit_units_used"}
)
_PAYLOAD_COUNT_MAX = 1_000_000
WORKFLOW_OUTBOX_RUN_ID_PREFIX = "workflow:"
WORKFLOW_TERMINAL_DEDUPE_PREFIX = "workflow-terminal:"
_INSPECT_ALLOWED_KEYS = frozenset(
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

_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


class NotificationEventKind(str, Enum):
    """Events eligible for durable notification (not heartbeats)."""

    PHASE_CHANGE = "phase_change"
    STALE = "stale"
    RECOVERY = "recovery"
    TERMINAL = "terminal"


class DeliveryState(str, Enum):
    """Durable delivery lifecycle for an outbox row."""

    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    DELIVERED = "delivered"
    DEAD = "dead"
    # Terminal non-failure: intentionally not delivered (e.g. Pushover filter).
    SKIPPED = "skipped"


EMITTED_EVENT_KINDS = frozenset(kind.value for kind in NotificationEventKind)

# Native Pushover phone alerts: terminal + exceptional stale/recovery only.
# Routine phase_change stays durable but is not POSTed to Pushover.
PUSHOVER_DELIVERABLE_EVENT_KINDS = frozenset(
    {
        NotificationEventKind.STALE.value,
        NotificationEventKind.RECOVERY.value,
        NotificationEventKind.TERMINAL.value,
    }
)


def is_heartbeat_stale_or_paired_recovery(
    event_kind: str | NotificationEventKind,
    dedupe_key: str | None = None,
) -> bool:
    """True for heartbeat stale or paired recovery (not startup recovery)."""
    kind = (
        event_kind.value
        if isinstance(event_kind, NotificationEventKind)
        else str(event_kind)
    )
    if kind == NotificationEventKind.STALE.value:
        return True
    if kind != NotificationEventKind.RECOVERY.value:
        return False
    return str(dedupe_key or "").startswith(_HEARTBEAT_RECOVERY_DEDUPE_PREFIX)


@dataclass(frozen=True)
class NotificationConfig:
    """Resolved opt-in notification configuration (secrets never stringified)."""

    enabled: bool
    webhook_url: str | None
    timeout_seconds: float
    max_attempts: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    claim_lease_seconds: float
    allow_http: bool
    worker_poll_seconds: float
    _secret: str | None
    _pushover_user_key: str | None = None
    _pushover_app_token: str | None = None
    pushover_device: str | None = None
    pushover_priority: int = DEFAULT_PUSHOVER_PRIORITY
    pushover_sound: str | None = None

    @property
    def has_secret(self) -> bool:
        return bool(self._secret)

    @property
    def has_pushover_user_key(self) -> bool:
        return bool(self._pushover_user_key)

    @property
    def has_pushover_app_token(self) -> bool:
        return bool(self._pushover_app_token)

    @property
    def webhook_ready(self) -> bool:
        return bool(self.webhook_url and self.has_secret)

    @property
    def pushover_ready(self) -> bool:
        return bool(self.has_pushover_user_key and self.has_pushover_app_token)

    def secret_for_signing(self) -> str | None:
        """Return the webhook secret for HMAC only (callers must not log)."""
        return self._secret

    def pushover_credentials(self) -> tuple[str, str] | None:
        """Return ``(user_key, app_token)`` for Pushover HTTP only (never log)."""
        if not self.pushover_ready:
            return None
        assert self._pushover_user_key is not None
        assert self._pushover_app_token is not None
        return self._pushover_user_key, self._pushover_app_token

    def effective_claim_lease_seconds(self) -> float:
        """Lease must outlive a single HTTP attempt so active claims survive."""
        return float(
            max(self.claim_lease_seconds, self.timeout_seconds + 5.0)
        )

    def __repr__(self) -> str:
        return (
            "NotificationConfig("
            f"enabled={self.enabled!r}, "
            f"webhook_url={'set' if self.webhook_url else None}, "
            f"has_secret={self.has_secret}, "
            f"pushover_user_key={'set' if self.has_pushover_user_key else None}, "
            f"pushover_app_token={'set' if self.has_pushover_app_token else None}, "
            f"pushover_device={'set' if self.pushover_device else None}, "
            f"pushover_priority={self.pushover_priority!r}, "
            f"pushover_sound={'set' if self.pushover_sound else None}, "
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"max_attempts={self.max_attempts!r}, "
            f"allow_http={self.allow_http!r})"
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


class WorkflowTerminalSource(Protocol):
    """Minimal workflow view needed to enqueue a terminal notification."""

    workflow_id: str
    state: Any
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    fix_cycle_count: int
    child_run_count: int
    credit_units_used: int


def _workflow_state_value(state: Any) -> str:
    if hasattr(state, "value"):
        return str(state.value)
    return str(state)


def _bounded_count(raw: Any) -> int:
    if isinstance(raw, bool):
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(value, _PAYLOAD_COUNT_MAX))


def workflow_terminal_run_id(workflow_id: str) -> str:
    """Stable outbox ``run_id`` that cannot collide with mission run UUIDs."""
    return f"{WORKFLOW_OUTBOX_RUN_ID_PREFIX}{str(workflow_id).strip()}"[:64]


def workflow_terminal_dedupe_key(state: str) -> str:
    """Durable unique key for one workflow terminal row (restart-stable)."""
    return f"{WORKFLOW_TERMINAL_DEDUPE_PREFIX}{str(state).strip()[:48]}"[:128]


def is_workflow_outbox_run_id(run_id: str | None) -> bool:
    return str(run_id or "").startswith(WORKFLOW_OUTBOX_RUN_ID_PREFIX)


def _parse_dt(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < 1:
        return default
    return value


def _env_flag(name: str, *, environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    raw = (env.get(name) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _env_optional_str(
    name: str,
    *,
    environ: Mapping[str, str],
    max_chars: int,
) -> str | None:
    raw = (environ.get(name) or "").strip()
    if not raw:
        return None
    return raw[:max_chars]


def _has_ascii_control_chars(value: str) -> bool:
    """True when ``value`` contains ASCII C0 controls or DEL."""
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def sanitize_pushover_device_or_sound(
    raw: str | None,
    *,
    max_chars: int,
) -> str | None:
    """Reject optional device/sound values that contain control characters.

    Control characters are omitted entirely (option unset) rather than stripped
    into a different effective name. Printable text is length-bounded.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if _has_ascii_control_chars(text):
        return None
    return text[:max_chars]


def resolve_pushover_sound(sound: str | None) -> str:
    """Return the Messages API sound name (default official ``pushover``).

    Explicit ``MISSION_CONTROL_NOTIFICATIONS_PUSHOVER_SOUND`` overrides win
    when they pass validation; otherwise the standard sound is used. OS Focus
    / silent modes on the device can still suppress audible playback.
    """
    safe = sanitize_pushover_device_or_sound(
        sound, max_chars=PUSHOVER_SOUND_MAX_CHARS
    )
    return safe or DEFAULT_PUSHOVER_SOUND


def should_deliver_pushover_event(event_kind: str | NotificationEventKind) -> bool:
    """True when the native Pushover backend should HTTP-deliver ``event_kind``.

    Routine ``phase_change`` events are durable for inspection but intentionally
    skipped for phone alerts so a normal mission yields one terminal alert.
    """
    kind = (
        event_kind.value
        if isinstance(event_kind, NotificationEventKind)
        else str(event_kind)
    )
    return kind in PUSHOVER_DELIVERABLE_EVENT_KINDS


def parse_pushover_priority(raw: str | None) -> int:
    """Parse Pushover priority; reject emergency (2) and unknown values."""
    if raw is None or not str(raw).strip():
        return DEFAULT_PUSHOVER_PRIORITY
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ValueError("pushover priority must be an integer") from exc
    if value == 2:
        raise ValueError(
            "pushover emergency priority is not supported; use -2..1"
        )
    if value not in ALLOWED_PUSHOVER_PRIORITIES:
        raise ValueError("pushover priority must be one of -2, -1, 0, 1")
    return value


def load_notification_config(
    environ: Mapping[str, str] | None = None,
) -> NotificationConfig:
    """Load opt-in notification settings. Unset backends → disabled."""
    env = os.environ if environ is None else environ
    url = (env.get(WEBHOOK_URL_ENV) or "").strip() or None
    secret = (env.get(WEBHOOK_SECRET_ENV) or "").strip() or None
    pushover_user = _env_optional_str(
        PUSHOVER_USER_KEY_ENV, environ=env, max_chars=128
    )
    pushover_token = _env_optional_str(
        PUSHOVER_APP_TOKEN_ENV, environ=env, max_chars=128
    )
    pushover_device = sanitize_pushover_device_or_sound(
        _env_optional_str(
            PUSHOVER_DEVICE_ENV, environ=env, max_chars=PUSHOVER_DEVICE_MAX_CHARS
        ),
        max_chars=PUSHOVER_DEVICE_MAX_CHARS,
    )
    pushover_sound = sanitize_pushover_device_or_sound(
        _env_optional_str(
            PUSHOVER_SOUND_ENV, environ=env, max_chars=PUSHOVER_SOUND_MAX_CHARS
        ),
        max_chars=PUSHOVER_SOUND_MAX_CHARS,
    )
    try:
        pushover_priority = parse_pushover_priority(
            env.get(PUSHOVER_PRIORITY_ENV)
        )
    except ValueError:
        pushover_priority = DEFAULT_PUSHOVER_PRIORITY

    webhook_ready = bool(url and secret)
    pushover_ready = bool(pushover_user and pushover_token)
    enabled_raw = (env.get(ENABLED_ENV) or "").strip().lower()
    if enabled_raw in {"0", "false", "no", "off"}:
        enabled = False
    elif enabled_raw in {"1", "true", "yes", "on"}:
        enabled = webhook_ready or pushover_ready
    else:
        # Safe default: enable when either backend is fully configured.
        enabled = webhook_ready or pushover_ready

    return NotificationConfig(
        enabled=enabled,
        webhook_url=url,
        timeout_seconds=_env_float(
            TIMEOUT_ENV, DEFAULT_WEBHOOK_TIMEOUT_SECONDS
        ),
        max_attempts=_env_int(MAX_ATTEMPTS_ENV, DEFAULT_MAX_ATTEMPTS),
        backoff_base_seconds=_env_float(
            BACKOFF_BASE_ENV, DEFAULT_BACKOFF_BASE_SECONDS
        ),
        backoff_max_seconds=_env_float(
            BACKOFF_MAX_ENV, DEFAULT_BACKOFF_MAX_SECONDS
        ),
        claim_lease_seconds=_env_float(
            CLAIM_LEASE_ENV, DEFAULT_CLAIM_LEASE_SECONDS
        ),
        allow_http=_env_flag(ALLOW_HTTP_ENV, environ=env),
        worker_poll_seconds=_env_float(
            WORKER_POLL_ENV, DEFAULT_WORKER_POLL_SECONDS
        ),
        _secret=secret,
        _pushover_user_key=pushover_user,
        _pushover_app_token=pushover_token,
        pushover_device=pushover_device,
        pushover_priority=pushover_priority,
        pushover_sound=pushover_sound,
    )


def resolve_delivery_backend(
    config: NotificationConfig | None = None,
) -> str:
    """Return the active delivery backend (never both).

    Dual-config policy: when webhook and Pushover are both fully configured,
    prefer the existing HMAC webhook backend so operators who already rely on
    webhooks are unchanged and users do not receive duplicate alerts. To use
    Pushover only, leave webhook URL/secret unset.
    """
    cfg = config if config is not None else load_notification_config()
    if not cfg.enabled:
        return BACKEND_NONE
    if cfg.webhook_ready:
        return BACKEND_WEBHOOK
    if cfg.pushover_ready:
        return BACKEND_PUSHOVER
    return BACKEND_NONE


def is_webhook_configured(
    config: NotificationConfig | None = None,
) -> bool:
    """Return True when the HMAC webhook backend is opted in and ready."""
    cfg = config if config is not None else load_notification_config()
    return bool(cfg.enabled and cfg.webhook_ready)


def is_pushover_configured(
    config: NotificationConfig | None = None,
) -> bool:
    """Return True when the Pushover backend is opted in and ready."""
    cfg = config if config is not None else load_notification_config()
    return bool(cfg.enabled and cfg.pushover_ready)


def is_notifications_configured(
    config: NotificationConfig | None = None,
) -> bool:
    """Return True when any delivery backend is opted in and fully configured."""
    return resolve_delivery_backend(config) != BACKEND_NONE


def notification_backend_health(
    config: NotificationConfig | None = None,
) -> dict[str, Any]:
    """Bounded redacted backend-health metadata for inspection.

    Never reveals whether secret values match, never echoes credentials, and
    never includes webhook URL, user key, or app token material.
    """
    cfg = config if config is not None else load_notification_config()
    backend = resolve_delivery_backend(cfg)
    return {
        "notifications_enabled": backend != BACKEND_NONE,
        "active_backend": backend,
        "webhook_configured": bool(cfg.webhook_ready),
        "pushover_configured": bool(cfg.pushover_ready),
        "pushover_device_set": bool(cfg.pushover_device),
        "pushover_sound_set": bool(cfg.pushover_sound),
        "pushover_priority": int(cfg.pushover_priority),
        "dual_backend_policy": "prefer_webhook",
    }


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if any(ip in network for network in _PRIVATE_NETWORKS):
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def resolve_webhook_ip_targets(
    host: str,
    port: int,
) -> list[tuple[int, str]]:
    """Resolve host to public IP targets ``(family, ip_text)``.

    Raises ValueError when resolution fails or any address is blocked.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("webhook URL host could not be resolved") from exc

    if not infos:
        raise ValueError("webhook URL host could not be resolved")

    targets: list[tuple[int, str]] = []
    seen: set[str] = set()
    for info in infos:
        family = info[0]
        sockaddr = info[4]
        ip_text = sockaddr[0]
        if ip_text in seen:
            continue
        seen.add(ip_text)
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError as exc:
            raise ValueError("webhook URL resolved to an invalid address") from exc
        if _ip_is_blocked(ip):
            raise ValueError("webhook URL resolves to a blocked address")
        targets.append((family, ip_text))
    if not targets:
        raise ValueError("webhook URL host could not be resolved")
    return targets


def validate_webhook_url(
    url: str,
    *,
    allow_http: bool | None = None,
) -> str:
    """Validate webhook URL and reject SSRF-prone targets.

    Production default is HTTPS-only. HTTP is permitted only when
    ``allow_http`` is true (explicit arg or ``MISSION_CONTROL_NOTIFICATIONS_ALLOW_HTTP``).
    Resolves DNS and rejects private/loopback/link-local answers. Raises
    ValueError on rejection (message never includes secrets).
    """
    text = (url or "").strip()
    if not text:
        raise ValueError("webhook URL is empty")
    parsed = urlparse(text)
    http_ok = (
        bool(allow_http)
        if allow_http is not None
        else _env_flag(ALLOW_HTTP_ENV)
    )
    if parsed.scheme == "https":
        pass
    elif parsed.scheme == "http" and http_ok:
        pass
    elif parsed.scheme == "http":
        raise ValueError("webhook URL scheme must be https")
    else:
        raise ValueError("webhook URL scheme must be https")
    if not parsed.hostname:
        raise ValueError("webhook URL host is required")
    if parsed.username or parsed.password:
        raise ValueError("webhook URL must not include credentials")
    host = parsed.hostname
    if host.lower() in {"localhost", "metadata.google.internal"}:
        raise ValueError("webhook URL host is not allowed")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    resolve_webhook_ip_targets(host, port)
    return text


def post_webhook_ssrf_safe(
    url: str,
    *,
    content: bytes,
    headers: Mapping[str, str],
    timeout_seconds: float,
    allow_http: bool = False,
    client: httpx.Client | None = None,
) -> httpx.Response:
    """POST webhook using validate-then-connect IP pinning (no redirects).

    DNS is resolved once; the TCP/TLS connection uses a validated public IP
    while preserving Host/SNI for the original hostname. This closes the
    classic validate-then-re-resolve rebinding window for this hop. Redirects
    are disabled so a later Location cannot pivot to a private target.
    """
    validated = validate_webhook_url(url, allow_http=allow_http)
    parsed = urlparse(validated)
    assert parsed.hostname is not None
    host = parsed.hostname
    scheme = parsed.scheme
    port = parsed.port or (443 if scheme == "https" else 80)
    targets = resolve_webhook_ip_targets(host, port)
    _family, ip_text = targets[0]
    # Bracket IPv6 literals for URL embedding.
    host_for_url = f"[{ip_text}]" if ":" in ip_text else ip_text
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    pinned_url = f"{scheme}://{host_for_url}:{port}{path}"

    merged = {str(k): str(v) for k, v in headers.items()}
    merged["Host"] = host if parsed.port is None else f"{host}:{parsed.port}"

    owns_client = client is None
    timeout = _bounded_httpx_timeout(timeout_seconds)
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        request = client.build_request(
            "POST",
            pinned_url,
            content=content,
            headers=merged,
            timeout=timeout,
        )
        # Pin TLS SNI / cert verification to the original hostname.
        request.extensions["sni_hostname"] = host
        return client.send(request, follow_redirects=False)
    finally:
        if owns_client:
            client.close()


def _bounded_httpx_timeout(timeout_seconds: float) -> httpx.Timeout:
    bound = float(max(0.1, timeout_seconds))
    return httpx.Timeout(
        connect=bound,
        read=bound,
        write=bound,
        pool=bound,
    )


def format_pushover_title(
    event_kind: str,
    *,
    payload: Mapping[str, Any] | None = None,
) -> str:
    """Concise title identifying Mission Control and severity."""
    kind = str(event_kind or "event").strip().lower() or "event"
    clean = (
        sanitize_notification_payload(payload) if payload is not None else {}
    )
    workflowish = bool(clean.get("workflow_id")) or is_workflow_outbox_run_id(
        str(clean.get("run_id") or "")
    )
    if workflowish:
        if kind == NotificationEventKind.TERMINAL.value:
            severity = "workflow terminal"
        else:
            severity = f"workflow {kind[:24]}"
    elif kind == NotificationEventKind.TERMINAL.value:
        severity = "terminal"
    elif kind == NotificationEventKind.STALE.value:
        severity = "stale"
    elif kind == NotificationEventKind.RECOVERY.value:
        severity = "recovery"
    elif kind == NotificationEventKind.PHASE_CHANGE.value:
        severity = "phase_change"
    else:
        severity = kind[:32]
    return f"Mission Control · {severity}"[:PUSHOVER_TITLE_MAX_CHARS]


def format_pushover_message(payload: Mapping[str, Any]) -> str:
    """Concise body with run identity, phase/status, and safe progress only."""
    clean = sanitize_notification_payload(payload)
    workflow_id = clean.get("workflow_id")
    if workflow_id:
        parts = [
            f"workflow={workflow_id}",
            f"kind={clean.get('event_kind') or 'event'}",
            f"status={clean.get('status') or 'unknown'}",
        ]
        occurred = clean.get("occurred_at")
        if occurred:
            parts.append(f"occurred_at={str(occurred)[:64]}")
        for count_key, label in (
            ("child_run_count", "child_runs"),
            ("fix_cycle_count", "fix_cycles"),
            ("credit_units_used", "credits"),
        ):
            if count_key in clean and clean[count_key] is not None:
                parts.append(f"{label}={clean[count_key]}")
        return "; ".join(parts)[:PUSHOVER_MESSAGE_MAX_CHARS]
    parts = [
        f"run={clean.get('run_id') or 'unknown'}",
        f"kind={clean.get('event_kind') or 'event'}",
        f"status={clean.get('status') or 'unknown'}",
        f"phase={clean.get('phase') or 'unknown'}",
    ]
    progress = clean.get("progress")
    if isinstance(progress, Mapping):
        step = str(progress.get("step") or "").strip()
        detail = str(progress.get("detail") or "").strip()
        if step:
            parts.append(f"step={step[:64]}")
        if detail:
            parts.append(f"detail={detail[:120]}")
    return "; ".join(parts)[:PUSHOVER_MESSAGE_MAX_CHARS]


def build_pushover_form(
    *,
    user_key: str,
    app_token: str,
    title: str,
    message: str,
    priority: int,
    device: str | None = None,
    sound: str | None = None,
) -> dict[str, str]:
    """Build official Messages API form fields (never log the result)."""
    if priority not in ALLOWED_PUSHOVER_PRIORITIES:
        raise ValueError("unsupported pushover priority")
    form: dict[str, str] = {
        "token": app_token,
        "user": user_key,
        "title": title[:PUSHOVER_TITLE_MAX_CHARS],
        "message": message[:PUSHOVER_MESSAGE_MAX_CHARS],
        "priority": str(int(priority)),
    }
    if device:
        safe_device = sanitize_pushover_device_or_sound(
            device, max_chars=PUSHOVER_DEVICE_MAX_CHARS
        )
        if safe_device:
            form["device"] = safe_device
    # Always send an explicit sound; default is the official "pushover" name.
    form["sound"] = resolve_pushover_sound(sound)
    return form


def _pushover_status_is_integer_one(status: Any) -> bool:
    """True only for JSON integer ``1`` (not bool, str, float, or missing)."""
    # bool is a subclass of int — reject True/False explicitly.
    return isinstance(status, int) and not isinstance(status, bool) and status == 1


def is_pushover_success_response(response: httpx.Response) -> bool:
    """Treat only HTTP 2xx with JSON integer ``status == 1`` as delivered.

    Boolean ``true``, string ``\"1\"``, floats, missing status, non-objects, and
    malformed/non-JSON bodies are never success.
    """
    if not (200 <= response.status_code < 300):
        return False
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError, TypeError):
        return False
    if not isinstance(body, Mapping):
        return False
    return _pushover_status_is_integer_one(body.get("status"))


def classify_pushover_http_failure(status_code: int) -> tuple[str, bool]:
    """Return ``(error_code, retryable)`` for a non-success Pushover HTTP code.

    Invalid credentials and other 4xx (except 429) are permanent. Timeouts are
    handled separately. 429 and 5xx are retryable. HTTP 2xx with a rejected or
    malformed JSON body must use ``classify_pushover_response`` instead.
    """
    if status_code == 429:
        return "pushover_rate_limited", True
    if 500 <= status_code <= 599:
        return f"pushover_http_{status_code}", True
    if status_code in {400, 401, 403}:
        return "pushover_invalid_credentials_or_request", False
    if 400 <= status_code <= 499:
        return f"pushover_http_{status_code}", False
    return f"pushover_http_{status_code}", True


def _normalize_pushover_rejection_error(
    _body: Mapping[str, Any] | None = None,
) -> str:
    """Stable permanent-failure code; provider response bodies are discarded.

    Never append ``errors[]``, ``request``, token/user fields, or any other
    provider-supplied text — redaction is insufficient when the provider echoes
    opaque credential values without sensitive keywords.
    """
    return "pushover_rejected"


def classify_pushover_response(response: httpx.Response) -> tuple[str, bool]:
    """Classify a non-success Pushover response for durable outbox semantics.

    Policy:
    - Syntactically valid JSON object whose ``status`` is an integer other than
      ``1`` (including ``0``) is a **permanent** rejection, even on HTTP 2xx.
      ``last_error`` is always the stable code ``pushover_rejected`` (provider
      error bodies are discarded, not redacted or echoed).
    - Ordinary non-retryable 4xx stay permanent; 429 and 5xx stay retryable.
    - Malformed / non-JSON / empty / non-object 2xx bodies, or 2xx bodies whose
      ``status`` is missing or not an integer, are **retryable** (then ``dead``
      after max attempts). They are never marked delivered: acceptance is
      uncertain, so we prefer bounded retries over a false success or an
      immediate permanent drop.
    """
    status_code = int(response.status_code)
    if not (200 <= status_code < 300):
        return classify_pushover_http_failure(status_code)

    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError, TypeError):
        return "pushover_malformed_response", True

    if not isinstance(body, Mapping):
        return "pushover_malformed_response", True

    if "status" not in body:
        return "pushover_malformed_response", True

    status = body.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        return "pushover_malformed_response", True

    if status != 1:
        return _normalize_pushover_rejection_error(body), False

    # Integer status 1 but caller decided it was not success — treat as ambiguous.
    return "pushover_malformed_response", True


def post_pushover_message(
    *,
    form: Mapping[str, str],
    timeout_seconds: float,
    client: httpx.Client | None = None,
) -> httpx.Response:
    """POST to the fixed official Pushover HTTPS API with IP pinning.

    Destination host is always ``api.pushover.net``. Redirects are disabled.
    """
    # Reuse webhook SSRF/DNS helpers against the fixed official host only.
    validate_webhook_url(PUSHOVER_API_URL, allow_http=False)
    targets = resolve_webhook_ip_targets(PUSHOVER_API_HOST, 443)
    _family, ip_text = targets[0]
    host_for_url = f"[{ip_text}]" if ":" in ip_text else ip_text
    pinned_url = f"https://{host_for_url}:443{PUSHOVER_MESSAGES_PATH}"
    timeout = _bounded_httpx_timeout(timeout_seconds)
    headers = {
        "Host": PUSHOVER_API_HOST,
        "User-Agent": "mission-control-notifications/2d-pushover",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        request = client.build_request(
            "POST",
            pinned_url,
            data=dict(form),
            headers=headers,
            timeout=timeout,
        )
        request.extensions["sni_hostname"] = PUSHOVER_API_HOST
        return client.send(request, follow_redirects=False)
    finally:
        if owns_client:
            client.close()


def compute_backoff_seconds(
    attempt_count: int,
    *,
    base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
) -> float:
    """Exponential backoff after a failed attempt (attempt_count is 1-based)."""
    if attempt_count < 1:
        return 0.0
    delay = base_seconds * (2 ** (attempt_count - 1))
    return float(min(delay, max_seconds))


def sign_webhook_body(
    body: bytes,
    *,
    secret: str,
    timestamp: str | None = None,
) -> tuple[str, str]:
    """Return ``(timestamp, signature_header_value)`` for HMAC-SHA256.

    Signature header format: ``t=<unix>,v1=<hex>``.
    Signed payload: ``{timestamp}.{body}``.
    """
    ts = timestamp if timestamp is not None else str(int(time.time()))
    message = ts.encode("utf-8") + b"." + body
    digest = hmac.new(
        secret.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()
    return ts, f"t={ts},v1={digest}"


def verify_webhook_signature(
    body: bytes,
    *,
    secret: str,
    signature_header: str,
    max_skew_seconds: float = 300.0,
    now: float | None = None,
) -> bool:
    """Verify a timestamped HMAC signature (testing / receiver helper)."""
    parts: dict[str, str] = {}
    for piece in signature_header.split(","):
        if "=" not in piece:
            return False
        key, value = piece.split("=", 1)
        parts[key.strip()] = value.strip()
    ts = parts.get("t")
    digest = parts.get("v1")
    if not ts or not digest:
        return False
    try:
        ts_int = int(ts)
    except ValueError:
        return False
    clock = time.time() if now is None else now
    if abs(clock - ts_int) > max_skew_seconds:
        return False
    expected_ts, expected_header = sign_webhook_body(
        body, secret=secret, timestamp=ts
    )
    _ = expected_ts
    return hmac.compare_digest(signature_header.strip(), expected_header)


def redact_notification_error(message: str | None) -> str | None:
    """Bound and scrub delivery error text for logs/API (never echo secrets)."""
    if message is None:
        return None
    text = " ".join(str(message).split())
    if not text:
        return None
    lowered = text.lower()
    for needle in (
        "secret",
        "token",
        "password",
        "authorization",
        "bearer",
        "api_key",
        "api-key",
        "user_key",
        "user key",
        "app_token",
        "app token",
        WEBHOOK_SECRET_ENV.lower(),
        WEBHOOK_URL_ENV.lower(),
        PUSHOVER_USER_KEY_ENV.lower(),
        PUSHOVER_APP_TOKEN_ENV.lower(),
    ):
        if needle in lowered:
            text = "[redacted]"
            break
    if len(text) > 160:
        text = text[:157] + "..."
    return text


def sanitize_notification_payload(
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Allowlist and bound a notification payload for storage/delivery/API."""
    if not isinstance(value, Mapping):
        return {}
    progress = sanitize_progress(value.get("progress"))
    payload: dict[str, Any] = {}
    for key in _PAYLOAD_ALLOWED_KEYS:
        if key not in value:
            continue
        raw = value[key]
        if key == "progress":
            payload[key] = progress
            continue
        if raw is None:
            payload[key] = None
            continue
        if key in _PAYLOAD_COUNT_KEYS:
            payload[key] = _bounded_count(raw)
            continue
        text = str(raw)
        if key == "dedupe_key":
            payload[key] = text[:128]
        elif key in {
            "run_id",
            "event_kind",
            "status",
            "phase",
            "heartbeat_health",
            "workflow_id",
        }:
            payload[key] = text[:64]
        elif key == "occurred_at":
            payload[key] = text[:64]
        else:
            payload[key] = text[:160]
    return payload


def build_workflow_terminal_payload(
    workflow: WorkflowTerminalSource,
    *,
    event_kind: NotificationEventKind | str = NotificationEventKind.TERMINAL,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    """Build an allowlisted workflow terminal payload (no YAML/errors/secrets)."""
    kind = (
        event_kind.value
        if isinstance(event_kind, NotificationEventKind)
        else str(event_kind)
    )
    state_value = _workflow_state_value(workflow.state)
    run_id = workflow_terminal_run_id(workflow.workflow_id)
    dedupe = workflow_terminal_dedupe_key(state_value)
    clock = (
        occurred_at
        or getattr(workflow, "completed_at", None)
        or getattr(workflow, "updated_at", None)
        or _utc_now()
    )
    return sanitize_notification_payload(
        {
            "run_id": run_id,
            "event_kind": kind,
            "status": state_value[:64],
            "phase": "workflow",
            "workflow_id": str(workflow.workflow_id)[:64],
            "occurred_at": _format_dt(clock),
            "dedupe_key": dedupe,
            "child_run_count": workflow.child_run_count,
            "fix_cycle_count": workflow.fix_cycle_count,
            "credit_units_used": workflow.credit_units_used,
        }
    )


def build_event_payload(
    record: RunRecord,
    *,
    event_kind: NotificationEventKind | str,
    dedupe_key: str,
    occurred_at: datetime | None = None,
    heartbeat_health: str | None = None,
) -> dict[str, Any]:
    """Build a redacted event payload from a run record."""
    kind = (
        event_kind.value
        if isinstance(event_kind, NotificationEventKind)
        else str(event_kind)
    )
    clock = occurred_at or _utc_now()
    health = heartbeat_health
    if health is None and kind == NotificationEventKind.STALE.value:
        health = HeartbeatHealth.STALE.value
    elif health is None and is_terminal_status(record.status):
        health = HeartbeatHealth.TERMINAL.value
    return sanitize_notification_payload(
        {
            "run_id": record.run_id,
            "event_kind": kind,
            "status": record.status.value
            if isinstance(record.status, RunStatus)
            else str(record.status),
            "phase": record.phase.value
            if isinstance(record.phase, RunPhase)
            else str(record.phase),
            "progress": record.progress,
            "heartbeat_health": health,
            "occurred_at": _format_dt(clock),
            "dedupe_key": dedupe_key,
        }
    )


def redact_outbox_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project an outbox row for API/MCP inspection (no secrets)."""
    payload = {}
    raw_payload = row.get("payload_json")
    if isinstance(raw_payload, str) and raw_payload:
        try:
            parsed = json.loads(raw_payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}
        if isinstance(parsed, dict):
            payload = sanitize_notification_payload(parsed)
    elif isinstance(raw_payload, Mapping):
        payload = sanitize_notification_payload(raw_payload)

    view: dict[str, Any] = {
        "event_id": str(row.get("event_id", ""))[:64],
        "run_id": str(row.get("run_id", ""))[:64],
        "event_kind": str(row.get("event_kind", ""))[:64],
        "delivery_state": str(row.get("delivery_state", ""))[:32],
        "attempt_count": int(row.get("attempt_count") or 0),
        "next_attempt_at": row.get("next_attempt_at"),
        "last_error": redact_notification_error(
            str(row["last_error"]) if row.get("last_error") is not None else None
        ),
        "delivered_at": row.get("delivered_at"),
        "created_at": row.get("created_at"),
        "status": payload.get("status"),
        "phase": payload.get("phase"),
        "progress": payload.get("progress"),
        "heartbeat_health": payload.get("heartbeat_health"),
        "occurred_at": payload.get("occurred_at"),
    }
    return {k: view[k] for k in _INSPECT_ALLOWED_KEYS if k in view}


@dataclass
class EnqueueResult:
    """Outcome of an idempotent enqueue attempt."""

    created: bool
    event_id: str | None
    skipped_reason: str | None = None


class NotificationOutbox:
    """SQLite-backed durable notification outbox tied to mission runs."""

    def __init__(
        self,
        db_path: str | None = None,
        *,
        config: NotificationConfig | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._db_path = os.path.abspath(
            os.path.expanduser(db_path or resolve_db_path())
        )
        self._lock = threading.RLock()
        self._config = config
        self._http_client = http_client
        self._owner_token = f"{os.getpid()}:{uuid.uuid4().hex[:12]}"
        parent = os.path.dirname(self._db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
        self._ensure_schema()

    @property
    def db_path(self) -> str:
        return self._db_path

    @property
    def owner_token(self) -> str:
        return self._owner_token

    def reload_config(self) -> NotificationConfig:
        """Refresh configuration from the environment."""
        self._config = load_notification_config()
        return self._config

    @property
    def config(self) -> NotificationConfig:
        if self._config is None:
            self._config = load_notification_config()
        return self._config

    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_OUTBOX_TABLE} (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    delivery_state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    delivered_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claim_owner TEXT,
                    claim_expires_at TEXT,
                    UNIQUE (run_id, event_kind, dedupe_key)
                )
                """
            )
            columns = {
                row["name"]
                for row in self._conn.execute(
                    f"PRAGMA table_info({_OUTBOX_TABLE})"
                ).fetchall()
            }
            if "claim_owner" not in columns:
                self._conn.execute(
                    f"ALTER TABLE {_OUTBOX_TABLE} ADD COLUMN claim_owner TEXT"
                )
            if "claim_expires_at" not in columns:
                self._conn.execute(
                    f"ALTER TABLE {_OUTBOX_TABLE} "
                    "ADD COLUMN claim_expires_at TEXT"
                )
            self._conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_notification_outbox_delivery
                ON {_OUTBOX_TABLE} (delivery_state, next_attempt_at)
                """
            )
            self._conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_notification_outbox_run
                ON {_OUTBOX_TABLE} (run_id, created_at)
                """
            )
            self._conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_notification_outbox_claim
                ON {_OUTBOX_TABLE} (delivery_state, claim_expires_at)
                """
            )
            self._conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_STALE_EPISODE_TABLE} (
                    run_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL,
                    stale_dedupe_key TEXT NOT NULL,
                    stale_event_id TEXT,
                    stale_heartbeat_at TEXT,
                    state TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    resolved_at TEXT
                )
                """
            )
            self._conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_notification_stale_episodes_state
                ON {_STALE_EPISODE_TABLE} (state, run_id)
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _begin_immediate_unlocked(self) -> None:
        """Take a reserved write lock (cross-connection / cross-process)."""
        # End any implicit transaction so BEGIN IMMEDIATE can acquire the
        # reserved lock against other NotificationOutbox connections.
        self._conn.commit()
        self._conn.execute("BEGIN IMMEDIATE")

    def _rollback_unlocked(self) -> None:
        try:
            self._conn.rollback()
        except sqlite3.Error:
            pass

    def enqueue(
        self,
        *,
        run_id: str,
        event_kind: NotificationEventKind | str,
        dedupe_key: str,
        payload: Mapping[str, Any],
        occurred_at: datetime | None = None,
    ) -> EnqueueResult:
        """Idempotently enqueue an event. Safe across restarts/repeated checks.

        Always persists eligible events (for inspection) even when webhook
        delivery is disabled (opt-in no-config remains safe: no delivery).
        """
        kind = (
            event_kind.value
            if isinstance(event_kind, NotificationEventKind)
            else str(event_kind)
        )
        if kind not in EMITTED_EVENT_KINDS:
            return EnqueueResult(
                created=False,
                event_id=None,
                skipped_reason="filtered_event_kind",
            )
        if kind == "heartbeat" or "heartbeat" in kind:
            return EnqueueResult(
                created=False,
                event_id=None,
                skipped_reason="heartbeat_filtered",
            )

        clean = sanitize_notification_payload(payload)
        clean["run_id"] = str(run_id)[:64]
        clean["event_kind"] = kind
        clean["dedupe_key"] = str(dedupe_key)[:128]
        if "occurred_at" not in clean or not clean["occurred_at"]:
            clean["occurred_at"] = _format_dt(occurred_at or _utc_now())

        event_id = str(uuid.uuid4())
        now = _format_dt(_utc_now())
        assert now is not None
        with self._lock:
            try:
                child_skip = self._is_durable_workflow_child_unlocked(
                    str(run_id)[:64]
                )
                delivery_state = (
                    DeliveryState.SKIPPED.value
                    if child_skip
                    else DeliveryState.PENDING.value
                )
                last_error = (
                    WORKFLOW_CHILD_SUPPRESSED if child_skip else None
                )
                next_attempt_at = None if child_skip else now
                self._conn.execute(
                    f"""
                    INSERT INTO {_OUTBOX_TABLE} (
                        event_id,
                        run_id,
                        event_kind,
                        dedupe_key,
                        payload_json,
                        delivery_state,
                        attempt_count,
                        next_attempt_at,
                        last_error,
                        delivered_at,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, NULL, ?, ?)
                    """,
                    (
                        event_id,
                        str(run_id)[:64],
                        kind,
                        str(dedupe_key)[:128],
                        json.dumps(clean, separators=(",", ":"), sort_keys=True),
                        delivery_state,
                        next_attempt_at,
                        last_error,
                        now,
                        now,
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    pass
                existing = self._conn.execute(
                    f"""
                    SELECT event_id FROM {_OUTBOX_TABLE}
                    WHERE run_id = ? AND event_kind = ? AND dedupe_key = ?
                    """,
                    (str(run_id)[:64], kind, str(dedupe_key)[:128]),
                ).fetchone()
                return EnqueueResult(
                    created=False,
                    event_id=existing["event_id"] if existing else None,
                    skipped_reason="duplicate",
                )

        logger.info(
            "notification enqueued run_id=%s event_kind=%s event_id=%s",
            run_id,
            kind,
            event_id,
        )
        wake_notification_delivery()
        return EnqueueResult(created=True, event_id=event_id)

    def enqueue_for_record(
        self,
        record: RunRecord,
        *,
        event_kind: NotificationEventKind | str,
        dedupe_key: str,
        occurred_at: datetime | None = None,
        heartbeat_health: str | None = None,
    ) -> EnqueueResult:
        """Enqueue from a run record with a redacted payload."""
        payload = build_event_payload(
            record,
            event_kind=event_kind,
            dedupe_key=dedupe_key,
            occurred_at=occurred_at,
            heartbeat_health=heartbeat_health,
        )
        return self.enqueue(
            run_id=record.run_id,
            event_kind=event_kind,
            dedupe_key=dedupe_key,
            payload=payload,
            occurred_at=occurred_at,
        )

    def maybe_enqueue_phase_change(
        self,
        record: RunRecord,
        *,
        previous_phase: str | None,
    ) -> EnqueueResult:
        """Emit phase_change when phase advanced (not on heartbeat-only touch)."""
        phase_value = (
            record.phase.value
            if isinstance(record.phase, RunPhase)
            else str(record.phase)
        )
        if previous_phase is not None and previous_phase == phase_value:
            return EnqueueResult(
                created=False,
                event_id=None,
                skipped_reason="unchanged_phase",
            )
        started = _format_dt(record.phase_started_at) or _format_dt(_utc_now())
        dedupe = f"phase:{phase_value}:{started}"
        return self.enqueue_for_record(
            record,
            event_kind=NotificationEventKind.PHASE_CHANGE,
            dedupe_key=dedupe,
            occurred_at=record.phase_started_at,
        )

    def maybe_enqueue_terminal(self, record: RunRecord) -> EnqueueResult:
        """Emit terminal once per terminal status snapshot.

        Closes any open stale episode without emitting recovery so a run that
        becomes terminal while still stale cannot produce a false recovery.
        Episode close uses a SQLite ``BEGIN IMMEDIATE`` CAS so a concurrent
        healthy observation on another connection cannot insert recovery after
        ``closed_terminal``.
        """
        if not is_terminal_status(record.status):
            return EnqueueResult(
                created=False,
                event_id=None,
                skipped_reason="not_terminal",
            )
        status_value = (
            record.status.value
            if isinstance(record.status, RunStatus)
            else str(record.status)
        )
        completed = _format_dt(record.completed_at) or _format_dt(_utc_now())
        dedupe = f"terminal:{status_value}:{completed}"
        with self._lock:
            self._close_open_stale_episode_terminal_unlocked(
                record.run_id,
                resolved_at=record.completed_at or _utc_now(),
            )
            # Drop obsolete heartbeat stale/recovery before they can page.
            self._suppress_stale_recovery_for_terminal_run_unlocked(
                str(record.run_id)
            )
        return self.enqueue_for_record(
            record,
            event_kind=NotificationEventKind.TERMINAL,
            dedupe_key=dedupe,
            occurred_at=record.completed_at,
            heartbeat_health=HeartbeatHealth.TERMINAL.value,
        )

    def maybe_enqueue_workflow_terminal(
        self, workflow: WorkflowTerminalSource
    ) -> EnqueueResult:
        """Enqueue one durable terminal event for a terminal workflow.

        Identity is namespaced so it cannot collide with standalone run
        ``terminal`` rows. Repeats/restarts hit the same unique
        ``(run_id, event_kind, dedupe_key)``. Callers should enqueue
        *before* ``WorkflowRegistry.mark_notification_emitted``.
        """
        from mission_control.workflow_registry import is_terminal_workflow_state

        if not is_terminal_workflow_state(workflow.state):
            return EnqueueResult(
                created=False,
                event_id=None,
                skipped_reason="not_terminal",
            )
        payload = build_workflow_terminal_payload(workflow)
        state_value = _workflow_state_value(workflow.state)
        return self.enqueue(
            run_id=workflow_terminal_run_id(workflow.workflow_id),
            event_kind=NotificationEventKind.TERMINAL,
            dedupe_key=workflow_terminal_dedupe_key(state_value),
            payload=payload,
            occurred_at=getattr(workflow, "completed_at", None),
        )

    def maybe_enqueue_recovery(self, record: RunRecord) -> EnqueueResult:
        """Emit recovery for interrupted-run startup recovery."""
        completed = _format_dt(record.completed_at) or _format_dt(_utc_now())
        dedupe = f"recovery:{completed}"
        return self.enqueue_for_record(
            record,
            event_kind=NotificationEventKind.RECOVERY,
            dedupe_key=dedupe,
            occurred_at=record.completed_at,
            heartbeat_health=HeartbeatHealth.TERMINAL.value,
        )

    def maybe_enqueue_stale(
        self,
        record: RunRecord,
        *,
        now: datetime | None = None,
        stale_threshold_seconds: float = HEARTBEAT_STALE_THRESHOLD_SECONDS,
    ) -> EnqueueResult:
        """Observe heartbeat health and pair stale/recovery notifications.

        Production wait and notification-inspection paths call this on each
        observation (not bare ``GET /runs/{run_id}`` status). When health is
        ``stale``, enqueues at most one stale event per heartbeat observation
        window and durably opens a stale episode. When health is later
        ``healthy``, enqueues exactly one paired recovery for that episode
        (restart-safe; independent of in-memory monitoring cursors). Terminal
        observations close an open episode without recovery. Open-episode
        transitions and paired outbox inserts share one ``BEGIN IMMEDIATE``
        transaction with conditional UPDATE/CAS.
        """
        try:
            threshold = validate_stale_threshold_seconds(
                stale_threshold_seconds
            )
        except ValueError:
            return EnqueueResult(
                created=False,
                event_id=None,
                skipped_reason="invalid_stale_threshold",
            )
        clock = now or _utc_now()
        health = classify_heartbeat_health(
            record,
            now=clock,
            stale_threshold_seconds=threshold,
        )
        if health is HeartbeatHealth.STALE:
            return self._enqueue_stale_and_open_episode(
                record, now=clock
            )
        if health is HeartbeatHealth.HEALTHY:
            return self._enqueue_paired_recovery_if_open(
                record, now=clock
            )
        if health is HeartbeatHealth.TERMINAL:
            with self._lock:
                self._close_open_stale_episode_terminal_unlocked(
                    record.run_id,
                    resolved_at=clock,
                )
            return EnqueueResult(
                created=False,
                event_id=None,
                skipped_reason="terminal",
            )
        return EnqueueResult(
            created=False,
            event_id=None,
            skipped_reason="not_stale",
        )

    def _get_open_stale_episode_unlocked(
        self, run_id: str
    ) -> sqlite3.Row | None:
        return self._conn.execute(
            f"""
            SELECT * FROM {_STALE_EPISODE_TABLE}
            WHERE run_id = ? AND state = ?
            """,
            (str(run_id), _STALE_EPISODE_OPEN),
        ).fetchone()

    def _close_open_stale_episode_terminal_unlocked(
        self,
        run_id: str,
        *,
        resolved_at: datetime,
    ) -> bool:
        """CAS open → closed_terminal under BEGIN IMMEDIATE (no recovery)."""
        try:
            self._begin_immediate_unlocked()
            won = self._resolve_open_stale_episode_unlocked(
                run_id,
                state=_STALE_EPISODE_CLOSED_TERMINAL,
                resolved_at=resolved_at,
                commit=False,
            )
            self._conn.commit()
            return won
        except sqlite3.Error:
            self._rollback_unlocked()
            raise

    def _open_stale_episode_unlocked(
        self,
        *,
        run_id: str,
        episode_id: str,
        stale_dedupe_key: str,
        stale_event_id: str | None,
        stale_heartbeat_at: str | None,
        opened_at: datetime,
        commit: bool = True,
    ) -> None:
        existing = self._conn.execute(
            f"SELECT * FROM {_STALE_EPISODE_TABLE} WHERE run_id = ?",
            (str(run_id),),
        ).fetchone()
        if existing is not None:
            if existing["state"] == _STALE_EPISODE_OPEN:
                return
            if existing["stale_dedupe_key"] == stale_dedupe_key:
                # Same stale window already resolved; do not reopen.
                return
        opened_s = _format_dt(opened_at)
        assert opened_s is not None
        self._conn.execute(
            f"""
            INSERT INTO {_STALE_EPISODE_TABLE} (
                run_id,
                episode_id,
                stale_dedupe_key,
                stale_event_id,
                stale_heartbeat_at,
                state,
                opened_at,
                resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(run_id) DO UPDATE SET
                episode_id = excluded.episode_id,
                stale_dedupe_key = excluded.stale_dedupe_key,
                stale_event_id = excluded.stale_event_id,
                stale_heartbeat_at = excluded.stale_heartbeat_at,
                state = excluded.state,
                opened_at = excluded.opened_at,
                resolved_at = NULL
            """,
            (
                str(run_id)[:64],
                episode_id,
                stale_dedupe_key[:128],
                stale_event_id,
                stale_heartbeat_at,
                _STALE_EPISODE_OPEN,
                opened_s,
            ),
        )
        if commit:
            self._conn.commit()

    def _resolve_open_stale_episode_unlocked(
        self,
        run_id: str,
        *,
        state: str,
        resolved_at: datetime,
        episode_id: str | None = None,
        commit: bool = True,
    ) -> bool:
        resolved_s = _format_dt(resolved_at)
        assert resolved_s is not None
        if episode_id is None:
            cursor = self._conn.execute(
                f"""
                UPDATE {_STALE_EPISODE_TABLE}
                SET state = ?, resolved_at = ?
                WHERE run_id = ? AND state = ?
                """,
                (
                    state,
                    resolved_s,
                    str(run_id),
                    _STALE_EPISODE_OPEN,
                ),
            )
        else:
            cursor = self._conn.execute(
                f"""
                UPDATE {_STALE_EPISODE_TABLE}
                SET state = ?, resolved_at = ?
                WHERE run_id = ? AND episode_id = ? AND state = ?
                """,
                (
                    state,
                    resolved_s,
                    str(run_id),
                    episode_id,
                    _STALE_EPISODE_OPEN,
                ),
            )
        if commit:
            self._conn.commit()
        return int(cursor.rowcount or 0) > 0

    def _insert_outbox_row_unlocked(
        self,
        *,
        event_id: str,
        run_id: str,
        event_kind: str,
        dedupe_key: str,
        payload: Mapping[str, Any],
        now_s: str,
    ) -> None:
        """Insert one outbox row; caller owns the surrounding transaction."""
        clean = sanitize_notification_payload(payload)
        clean["run_id"] = str(run_id)[:64]
        clean["event_kind"] = event_kind
        clean["dedupe_key"] = str(dedupe_key)[:128]
        if "occurred_at" not in clean or not clean["occurred_at"]:
            clean["occurred_at"] = now_s
        child_skip = self._is_durable_workflow_child_unlocked(str(run_id)[:64])
        delivery_state = (
            DeliveryState.SKIPPED.value
            if child_skip
            else DeliveryState.PENDING.value
        )
        last_error = WORKFLOW_CHILD_SUPPRESSED if child_skip else None
        next_attempt_at = None if child_skip else now_s
        self._conn.execute(
            f"""
            INSERT INTO {_OUTBOX_TABLE} (
                event_id,
                run_id,
                event_kind,
                dedupe_key,
                payload_json,
                delivery_state,
                attempt_count,
                next_attempt_at,
                last_error,
                delivered_at,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, NULL, ?, ?)
            """,
            (
                event_id,
                str(run_id)[:64],
                event_kind,
                str(dedupe_key)[:128],
                json.dumps(clean, separators=(",", ":"), sort_keys=True),
                delivery_state,
                next_attempt_at,
                last_error,
                now_s,
                now_s,
            ),
        )

    def _enqueue_stale_and_open_episode(
        self,
        record: RunRecord,
        *,
        now: datetime,
    ) -> EnqueueResult:
        """Durably enqueue stale and open the pairing episode in one commit."""
        hb = _format_dt(record.heartbeat_at) or "absent"
        dedupe = f"stale:{hb}"
        kind = NotificationEventKind.STALE.value
        run_id = str(record.run_id)[:64]
        with self._lock:
            try:
                self._begin_immediate_unlocked()
                existing = self._conn.execute(
                    f"""
                    SELECT event_id FROM {_OUTBOX_TABLE}
                    WHERE run_id = ? AND event_kind = ? AND dedupe_key = ?
                    """,
                    (run_id, kind, dedupe[:128]),
                ).fetchone()
                if existing is not None:
                    event_id = str(existing["event_id"])
                    self._open_stale_episode_unlocked(
                        run_id=run_id,
                        episode_id=str(uuid.uuid4()),
                        stale_dedupe_key=dedupe,
                        stale_event_id=event_id,
                        stale_heartbeat_at=hb if hb != "absent" else None,
                        opened_at=now,
                        commit=False,
                    )
                    self._conn.commit()
                    return EnqueueResult(
                        created=False,
                        event_id=event_id,
                        skipped_reason="duplicate",
                    )

                event_id = str(uuid.uuid4())
                episode_id = str(uuid.uuid4())
                payload = build_event_payload(
                    record,
                    event_kind=NotificationEventKind.STALE,
                    dedupe_key=dedupe,
                    occurred_at=now,
                    heartbeat_health=HeartbeatHealth.STALE.value,
                )
                now_s = _format_dt(now)
                assert now_s is not None
                self._insert_outbox_row_unlocked(
                    event_id=event_id,
                    run_id=run_id,
                    event_kind=kind,
                    dedupe_key=dedupe,
                    payload=payload,
                    now_s=now_s,
                )
                self._open_stale_episode_unlocked(
                    run_id=run_id,
                    episode_id=episode_id,
                    stale_dedupe_key=dedupe,
                    stale_event_id=event_id,
                    stale_heartbeat_at=hb if hb != "absent" else None,
                    opened_at=now,
                    commit=False,
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._rollback_unlocked()
                row = self._conn.execute(
                    f"""
                    SELECT event_id FROM {_OUTBOX_TABLE}
                    WHERE run_id = ? AND event_kind = ? AND dedupe_key = ?
                    """,
                    (run_id, kind, dedupe[:128]),
                ).fetchone()
                event_id = str(row["event_id"]) if row else None
                if event_id is not None:
                    try:
                        self._begin_immediate_unlocked()
                        self._open_stale_episode_unlocked(
                            run_id=run_id,
                            episode_id=str(uuid.uuid4()),
                            stale_dedupe_key=dedupe,
                            stale_event_id=event_id,
                            stale_heartbeat_at=hb if hb != "absent" else None,
                            opened_at=now,
                            commit=False,
                        )
                        self._conn.commit()
                    except sqlite3.Error:
                        self._rollback_unlocked()
                        raise
                return EnqueueResult(
                    created=False,
                    event_id=event_id,
                    skipped_reason="duplicate",
                )
            except sqlite3.Error:
                self._rollback_unlocked()
                raise

        logger.info(
            "notification enqueued run_id=%s event_kind=%s event_id=%s",
            run_id,
            kind,
            event_id,
        )
        wake_notification_delivery()
        return EnqueueResult(created=True, event_id=event_id)

    def _enqueue_paired_recovery_if_open(
        self,
        record: RunRecord,
        *,
        now: datetime,
    ) -> EnqueueResult:
        """Enqueue one recovery for an open stale episode (idempotent).

        CAS ``open`` → ``recovered`` and insert the paired recovery row in the
        same ``BEGIN IMMEDIATE`` transaction so a concurrent terminal close on
        another connection cannot observe recovery after ``closed_terminal``.
        """
        run_id = str(record.run_id)[:64]
        kind = NotificationEventKind.RECOVERY.value
        with self._lock:
            try:
                self._begin_immediate_unlocked()
                episode = self._get_open_stale_episode_unlocked(run_id)
                if episode is None:
                    self._conn.commit()
                    return EnqueueResult(
                        created=False,
                        event_id=None,
                        skipped_reason="no_open_stale_episode",
                    )
                episode_id = str(episode["episode_id"])
                dedupe = f"recovery:stale:{episode_id}"
                existing = self._conn.execute(
                    f"""
                    SELECT event_id FROM {_OUTBOX_TABLE}
                    WHERE run_id = ? AND event_kind = ? AND dedupe_key = ?
                    """,
                    (run_id, kind, dedupe[:128]),
                ).fetchone()
                won = self._resolve_open_stale_episode_unlocked(
                    run_id,
                    state=_STALE_EPISODE_RECOVERED,
                    resolved_at=now,
                    episode_id=episode_id,
                    commit=False,
                )
                if not won:
                    self._conn.commit()
                    return EnqueueResult(
                        created=False,
                        event_id=None,
                        skipped_reason="no_open_stale_episode",
                    )
                if existing is not None:
                    self._conn.commit()
                    return EnqueueResult(
                        created=False,
                        event_id=str(existing["event_id"]),
                        skipped_reason="duplicate",
                    )
                event_id = str(uuid.uuid4())
                payload = build_event_payload(
                    record,
                    event_kind=NotificationEventKind.RECOVERY,
                    dedupe_key=dedupe,
                    occurred_at=now,
                    heartbeat_health=HeartbeatHealth.HEALTHY.value,
                )
                now_s = _format_dt(now)
                assert now_s is not None
                self._insert_outbox_row_unlocked(
                    event_id=event_id,
                    run_id=run_id,
                    event_kind=kind,
                    dedupe_key=dedupe,
                    payload=payload,
                    now_s=now_s,
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._rollback_unlocked()
                row = self._conn.execute(
                    f"""
                    SELECT event_id FROM {_OUTBOX_TABLE}
                    WHERE run_id = ? AND event_kind = ? AND dedupe_key = ?
                    """,
                    (
                        run_id,
                        kind,
                        f"recovery:stale:{episode_id}"[:128],
                    ),
                ).fetchone()
                return EnqueueResult(
                    created=False,
                    event_id=str(row["event_id"]) if row else None,
                    skipped_reason="duplicate",
                )
            except sqlite3.Error:
                self._rollback_unlocked()
                raise

        logger.info(
            "notification enqueued run_id=%s event_kind=%s event_id=%s",
            run_id,
            kind,
            event_id,
        )
        wake_notification_delivery()
        return EnqueueResult(created=True, event_id=event_id)

    def list_for_run(
        self,
        run_id: str,
        *,
        limit: int = NOTIFICATION_INSPECT_MAX_EVENTS,
    ) -> list[dict[str, Any]]:
        """Return bounded redacted outbox rows for a run (newest last)."""
        bound = max(1, min(int(limit), NOTIFICATION_INSPECT_MAX_EVENTS))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM {_OUTBOX_TABLE}
                WHERE run_id = ?
                ORDER BY created_at ASC, event_id ASC
                LIMIT ?
                """,
                (str(run_id), bound),
            ).fetchall()
        return [redact_outbox_row(dict(row)) for row in rows]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        """Return one redacted outbox event, or None."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM {_OUTBOX_TABLE} WHERE event_id = ?",
                (str(event_id),),
            ).fetchone()
        if row is None:
            return None
        return redact_outbox_row(dict(row))

    def count_pending(self) -> int:
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT COUNT(*) AS count FROM {_OUTBOX_TABLE}
                WHERE delivery_state IN (?, ?)
                """,
                (DeliveryState.PENDING.value, DeliveryState.IN_FLIGHT.value),
            ).fetchone()
            return int(row["count"])

    def suppress_legacy_predeploy_backlog(self) -> int:
        """Idempotently skip pre-deploy stale/recovery outbox backlog.

        Runs in one ``BEGIN IMMEDIATE`` transaction. Only
        ``notification_outbox`` rows with ``event_kind`` in
        ``('stale', 'recovery')``, ``created_at`` strictly before the
        canonical cutoff, and ``delivery_state`` in
        ``('pending', 'in_flight')`` are updated to ``skipped`` with
        ``last_error=legacy_predeploy_backlog_suppressed``. Claims are
        cleared; audit fields (``event_id``, ``run_id``, ``payload_json``,
        ``attempt_count``, ``next_attempt_at``, ``created_at``,
        ``delivered_at``) are preserved. Terminal/phase_change rows and
        already-terminal delivery states are never touched. On failure the
        transaction is rolled back and the error propagates so callers must
        not start the delivery worker.
        """
        cutoff_s = _format_dt(LEGACY_PREDEPLOY_BACKLOG_CUTOFF_UTC)
        assert cutoff_s is not None
        now_s = _format_dt(_utc_now())
        assert now_s is not None
        with self._lock:
            try:
                self._begin_immediate_unlocked()
                cursor = self._conn.execute(
                    f"""
                    UPDATE {_OUTBOX_TABLE}
                    SET delivery_state = ?,
                        last_error = ?,
                        claim_owner = NULL,
                        claim_expires_at = NULL,
                        updated_at = ?
                    WHERE event_kind IN (?, ?)
                      AND created_at < ?
                      AND delivery_state IN (?, ?)
                    """,
                    (
                        DeliveryState.SKIPPED.value,
                        LEGACY_PREDEPLOY_BACKLOG_SUPPRESSED,
                        now_s,
                        NotificationEventKind.STALE.value,
                        NotificationEventKind.RECOVERY.value,
                        cutoff_s,
                        DeliveryState.PENDING.value,
                        DeliveryState.IN_FLIGHT.value,
                    ),
                )
                affected = int(cursor.rowcount or 0)
                self._conn.commit()
            except Exception:
                self._rollback_unlocked()
                raise
        logger.info(
            "legacy predeploy notification backlog suppressed count=%s",
            affected,
        )
        return affected

    def _runs_table_exists_unlocked(self) -> bool:
        row = self._conn.execute(
            """
            SELECT 1 AS ok FROM sqlite_master
            WHERE type = 'table' AND name = 'runs'
            """
        ).fetchone()
        return row is not None

    def _read_run_status_unlocked(self, run_id: str) -> str | None:
        if not self._runs_table_exists_unlocked():
            return None
        row = self._conn.execute(
            "SELECT status FROM runs WHERE run_id = ?",
            (str(run_id)[:64],),
        ).fetchone()
        if row is None:
            return None
        return str(row["status"])

    def _workflow_steps_table_exists_unlocked(self) -> bool:
        row = self._conn.execute(
            """
            SELECT 1 AS ok FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (_WORKFLOW_STEPS_TABLE,),
        ).fetchone()
        return row is not None

    def _is_durable_workflow_child_unlocked(self, run_id: str) -> bool:
        """True when ``workflow_steps.child_run_id`` durably names this run.

        Synthetic workflow outbox identities are never children. A missing
        ``workflow_steps`` table means standalone (not a child).
        """
        rid = str(run_id or "").strip()[:64]
        if not rid or is_workflow_outbox_run_id(rid):
            return False
        if not self._workflow_steps_table_exists_unlocked():
            return False
        row = self._conn.execute(
            f"""
            SELECT 1 AS ok FROM {_WORKFLOW_STEPS_TABLE}
            WHERE child_run_id = ?
            LIMIT 1
            """,
            (rid,),
        ).fetchone()
        return row is not None

    def is_durable_workflow_child_run(self, run_id: str) -> bool:
        """Return whether ``run_id`` is a durable workflow child (locked)."""
        with self._lock:
            return self._is_durable_workflow_child_unlocked(run_id)

    def _suppress_stale_recovery_sql_params(
        self, *, now_s: str, run_id: str | None = None
    ) -> tuple[str, tuple[Any, ...]]:
        """Build UPDATE ... WHERE for heartbeat stale/recovery on terminal runs."""
        terminal_list = sorted(TERMINAL_STATUSES)
        placeholders = ", ".join("?" for _ in terminal_list)
        sets_and_kinds = f"""
            UPDATE {_OUTBOX_TABLE}
            SET delivery_state = ?,
                last_error = ?,
                claim_owner = NULL,
                claim_expires_at = NULL,
                next_attempt_at = NULL,
                updated_at = ?
            WHERE delivery_state IN (?, ?)
              AND (
                event_kind = ?
                OR (
                  event_kind = ?
                  AND dedupe_key LIKE ?
                )
              )
              AND EXISTS (
                SELECT 1 FROM runs r
                WHERE r.run_id = {_OUTBOX_TABLE}.run_id
                  AND r.status IN ({placeholders})
              )
        """
        params: list[Any] = [
            DeliveryState.SKIPPED.value,
            STALE_RECOVERY_TERMINAL_RUN_SUPPRESSED,
            now_s,
            DeliveryState.PENDING.value,
            DeliveryState.IN_FLIGHT.value,
            NotificationEventKind.STALE.value,
            NotificationEventKind.RECOVERY.value,
            f"{_HEARTBEAT_RECOVERY_DEDUPE_PREFIX}%",
            *terminal_list,
        ]
        if run_id is not None:
            sets_and_kinds += f" AND {_OUTBOX_TABLE}.run_id = ?"
            params.append(str(run_id)[:64])
        return sets_and_kinds, tuple(params)

    def _suppress_stale_recovery_for_terminal_run_unlocked(
        self, run_id: str
    ) -> int:
        """Suppress pending/in-flight heartbeat stale/recovery for one run.

        Caller holds ``self._lock``. Uses ``BEGIN IMMEDIATE``. No-op when the
        ``runs`` table is absent or the run is not terminal. Does not delete
        rows; marks them ``skipped`` with an auditable reason.
        """
        if not self._runs_table_exists_unlocked():
            return 0
        status = self._read_run_status_unlocked(run_id)
        if status is None or not is_terminal_status(status):
            return 0
        now_s = _format_dt(_utc_now())
        assert now_s is not None
        sql, params = self._suppress_stale_recovery_sql_params(
            now_s=now_s, run_id=run_id
        )
        try:
            self._begin_immediate_unlocked()
            cursor = self._conn.execute(sql, params)
            affected = int(cursor.rowcount or 0)
            self._conn.commit()
        except Exception:
            self._rollback_unlocked()
            raise
        if affected:
            logger.info(
                "stale/recovery suppressed for terminal run count=%s",
                affected,
            )
        return affected

    def suppress_stale_recovery_for_terminal_run(self, run_id: str) -> int:
        """Idempotently skip heartbeat stale/recovery once ``run_id`` is terminal.

        Only ``stale`` and paired heartbeat ``recovery`` (dedupe prefix
        ``recovery:stale:``) rows in ``pending`` / ``in_flight`` are updated to
        ``skipped`` with ``last_error=stale_recovery_terminal_run_suppressed``.
        Interrupted-run startup recovery, ``terminal``, ``phase_change``, and
        already-terminal delivery states are never touched. History is retained.
        """
        with self._lock:
            return self._suppress_stale_recovery_for_terminal_run_unlocked(
                run_id
            )

    def _suppress_workflow_child_sql_params(
        self, *, now_s: str, run_id: str | None = None
    ) -> tuple[str, tuple[Any, ...]]:
        """Build UPDATE ... WHERE for durable workflow-child outbox rows."""
        sql = f"""
            UPDATE {_OUTBOX_TABLE}
            SET delivery_state = ?,
                last_error = ?,
                claim_owner = NULL,
                claim_expires_at = NULL,
                next_attempt_at = NULL,
                updated_at = ?
            WHERE delivery_state IN (?, ?)
              AND {_OUTBOX_TABLE}.run_id NOT LIKE ?
              AND EXISTS (
                SELECT 1 FROM {_WORKFLOW_STEPS_TABLE} ws
                WHERE ws.child_run_id = {_OUTBOX_TABLE}.run_id
              )
        """
        params: list[Any] = [
            DeliveryState.SKIPPED.value,
            WORKFLOW_CHILD_SUPPRESSED,
            now_s,
            DeliveryState.PENDING.value,
            DeliveryState.IN_FLIGHT.value,
            f"{WORKFLOW_OUTBOX_RUN_ID_PREFIX}%",
        ]
        if run_id is not None:
            sql += f" AND {_OUTBOX_TABLE}.run_id = ?"
            params.append(str(run_id)[:64])
        return sql, tuple(params)

    def _suppress_workflow_child_rows_unlocked(
        self, *, now_s: str, run_id: str | None = None
    ) -> int:
        """Skip pending/in-flight child rows. Caller holds the lock.

        No-op when ``workflow_steps`` is absent. Does not begin/commit.
        """
        if not self._workflow_steps_table_exists_unlocked():
            return 0
        sql, params = self._suppress_workflow_child_sql_params(
            now_s=now_s, run_id=run_id
        )
        cursor = self._conn.execute(sql, params)
        return int(cursor.rowcount or 0)

    def suppress_workflow_child_outbox(self, run_id: str | None = None) -> int:
        """Idempotently skip pending/in-flight rows for durable workflow children.

        Synthetic ``workflow:`` outbox identities are never touched. History is
        retained with ``last_error=workflow_child_suppressed``.
        """
        now_s = _format_dt(_utc_now())
        assert now_s is not None
        with self._lock:
            try:
                self._begin_immediate_unlocked()
                affected = self._suppress_workflow_child_rows_unlocked(
                    now_s=now_s, run_id=run_id
                )
                self._conn.commit()
            except Exception:
                self._rollback_unlocked()
                raise
        if affected:
            logger.info(
                "workflow child notification rows suppressed count=%s",
                affected,
            )
        return affected

    def _heartbeat_stale_recovery_suppress_reason_unlocked(
        self, run_id: str
    ) -> str | None:
        """Return auditable skip reason, or None when delivery may proceed.

        Fail closed when the ``runs`` table or run row/status is unavailable.
        Uses canonical ``TERMINAL_STATUSES`` via ``is_terminal_status``.
        """
        if not self._runs_table_exists_unlocked():
            return STALE_RECOVERY_RUN_STATUS_UNAVAILABLE
        status = self._read_run_status_unlocked(run_id)
        if status is None or not str(status).strip():
            return STALE_RECOVERY_RUN_STATUS_UNAVAILABLE
        if is_terminal_status(status):
            return STALE_RECOVERY_TERMINAL_RUN_SUPPRESSED
        return None

    def _claimed_row_suppress_reason_unlocked(
        self, row: sqlite3.Row
    ) -> str | None:
        """Return skip reason for a claimed row, or None when delivery may proceed.

        Child membership is checked first from durable ``workflow_steps``.
        Heartbeat stale/paired recovery still fail closed on terminal/unavailable
        run status so prior terminalization races cannot page.
        """
        if self._is_durable_workflow_child_unlocked(str(row["run_id"] or "")):
            return WORKFLOW_CHILD_SUPPRESSED
        if is_heartbeat_stale_or_paired_recovery(
            row["event_kind"], row["dedupe_key"]
        ):
            return self._heartbeat_stale_recovery_suppress_reason_unlocked(
                row["run_id"]
            )
        return None

    def _finalize_claimed_outbox_row(
        self,
        row: sqlite3.Row,
        *,
        active_delivery_state: str | None = None,
        attempt_count: int | None = None,
        next_attempt_at: str | None = None,
        last_error: str | None = None,
        delivered_at: str | None = None,
        clear_error: bool = False,
    ) -> str:
        """Atomic claimed-row finalization (child membership + terminal races).

        Under one ``BEGIN IMMEDIATE`` transaction, re-reads durable child
        membership and (for heartbeat stale/recovery) canonical run status,
        then chooses the permitted CAS while still ``in_flight``:

        - workflow child → ``skipped`` (``workflow_child_suppressed``)
        - heartbeat stale/recovery terminal or status unavailable → ``skipped``
        - else if ``active_delivery_state`` is set → that state
        - else → leave the claim unchanged (``active``; pre-send check)

        Never overwrites ``skipped`` or any non-``in_flight`` state. Returns
        ``skipped``, ``delivered``, ``pending``, ``dead``, ``active``, or
        ``cas_missed``.
        """
        now_s = _format_dt(_utc_now())
        assert now_s is not None
        outcome = "cas_missed"
        log_reason: str | None = None
        with self._lock:
            try:
                self._begin_immediate_unlocked()
                reason = self._claimed_row_suppress_reason_unlocked(row)
                if reason is not None:
                    cursor = self._conn.execute(
                        f"""
                        UPDATE {_OUTBOX_TABLE}
                        SET delivery_state = ?,
                            last_error = ?,
                            claim_owner = NULL,
                            claim_expires_at = NULL,
                            next_attempt_at = NULL,
                            updated_at = ?
                        WHERE event_id = ?
                          AND delivery_state = ?
                        """,
                        (
                            DeliveryState.SKIPPED.value,
                            reason,
                            now_s,
                            row["event_id"],
                            DeliveryState.IN_FLIGHT.value,
                        ),
                    )
                    self._conn.commit()
                    if int(cursor.rowcount or 0) > 0:
                        outcome = "skipped"
                        log_reason = reason
                    else:
                        outcome = "cas_missed"
                elif active_delivery_state is None:
                    self._conn.commit()
                    outcome = "active"
                else:
                    sets = [
                        "delivery_state = ?",
                        "claim_owner = NULL",
                        "claim_expires_at = NULL",
                        "updated_at = ?",
                        "next_attempt_at = ?",
                    ]
                    params: list[Any] = [
                        active_delivery_state,
                        now_s,
                        next_attempt_at,
                    ]
                    if attempt_count is not None:
                        sets.append("attempt_count = ?")
                        params.append(attempt_count)
                    if clear_error:
                        sets.append("last_error = NULL")
                    elif last_error is not None:
                        sets.append("last_error = ?")
                        params.append(last_error)
                    if delivered_at is not None:
                        sets.append("delivered_at = ?")
                        params.append(delivered_at)
                    params.extend(
                        [row["event_id"], DeliveryState.IN_FLIGHT.value]
                    )
                    cursor = self._conn.execute(
                        f"""
                        UPDATE {_OUTBOX_TABLE}
                        SET {', '.join(sets)}
                        WHERE event_id = ?
                          AND delivery_state = ?
                        """,
                        tuple(params),
                    )
                    self._conn.commit()
                    if int(cursor.rowcount or 0) > 0:
                        outcome = active_delivery_state
                    else:
                        outcome = "cas_missed"
            except Exception:
                self._rollback_unlocked()
                raise
        if outcome == "skipped" and log_reason is not None:
            logger.info(
                "notification skipped event_id=%s run_id=%s event_kind=%s "
                "reason=%s",
                row["event_id"],
                row["run_id"],
                row["event_kind"],
                log_reason,
            )
        return outcome

    def _finalize_terminal_dependent_outbox_row(
        self,
        row: sqlite3.Row,
        *,
        active_delivery_state: str | None = None,
        attempt_count: int | None = None,
        next_attempt_at: str | None = None,
        last_error: str | None = None,
        delivered_at: str | None = None,
        clear_error: bool = False,
    ) -> str:
        """Atomic terminal-dependent finalization for heartbeat stale/recovery.

        Under one ``BEGIN IMMEDIATE`` transaction, reads canonical run status
        and chooses the permitted CAS transition while still ``in_flight``:

        - terminal or status unavailable → ``skipped`` (fail closed; no paging)
        - else if ``active_delivery_state`` is set → that state (delivered /
          pending retry / dead-letter)
        - else → leave the claim unchanged (``active``; pre-send check)

        Never overwrites ``skipped`` or any non-``in_flight`` state. Returns
        ``skipped``, ``delivered``, ``pending``, ``dead``, ``active``, or
        ``cas_missed``.
        """
        if not is_heartbeat_stale_or_paired_recovery(
            row["event_kind"], row["dedupe_key"]
        ):
            raise ValueError(
                "terminal-dependent finalize requires heartbeat stale/recovery"
            )
        return self._finalize_claimed_outbox_row(
            row,
            active_delivery_state=active_delivery_state,
            attempt_count=attempt_count,
            next_attempt_at=next_attempt_at,
            last_error=last_error,
            delivered_at=delivered_at,
            clear_error=clear_error,
        )

    def _try_suppress_claimed_stale_recovery_if_terminal(
        self, row: sqlite3.Row
    ) -> bool:
        """Delivery-time guard: skip claimed heartbeat stale/recovery if needed.

        Thin wrapper over ``_finalize_terminal_dependent_outbox_row`` (no active
        transition). Returns True when the row was transitioned to ``skipped``.
        """
        if not is_heartbeat_stale_or_paired_recovery(
            row["event_kind"], row["dedupe_key"]
        ):
            return False
        return self._finalize_terminal_dependent_outbox_row(row) == "skipped"

    def reclaim_stale_claims(
        self,
        *,
        now: datetime | None = None,
    ) -> int:
        """Return stale ``in_flight`` rows to ``pending`` for retry.

        Only reclaim claims whose lease has expired (or legacy rows with no
        lease). Active leases are left untouched so concurrent workers cannot
        steal in-progress deliveries.
        """
        clock = now or _utc_now()
        now_s = _format_dt(clock)
        assert now_s is not None
        with self._lock:
            cursor = self._conn.execute(
                f"""
                UPDATE {_OUTBOX_TABLE}
                SET delivery_state = ?,
                    claim_owner = NULL,
                    claim_expires_at = NULL,
                    updated_at = ?,
                    next_attempt_at = COALESCE(next_attempt_at, ?)
                WHERE delivery_state = ?
                  AND (
                    claim_expires_at IS NULL
                    OR claim_expires_at <= ?
                  )
                """,
                (
                    DeliveryState.PENDING.value,
                    now_s,
                    now_s,
                    DeliveryState.IN_FLIGHT.value,
                    now_s,
                ),
            )
            self._conn.commit()
            reclaimed = int(cursor.rowcount or 0)
            child_skipped = self._suppress_workflow_child_rows_unlocked(
                now_s=now_s
            )
        if reclaimed:
            logger.info(
                "notification reclaimed stale in_flight count=%s",
                reclaimed,
            )
        if child_skipped:
            logger.info(
                "workflow child notification rows suppressed count=%s",
                child_skipped,
            )
        return reclaimed

    def _claim_due_events(self, *, limit: int = 16) -> list[sqlite3.Row]:
        config = self.config
        lease_seconds = config.effective_claim_lease_seconds()
        now = _utc_now()
        now_s = _format_dt(now)
        assert now_s is not None
        expires_s = _format_dt(
            datetime.fromtimestamp(
                now.timestamp() + lease_seconds, tz=timezone.utc
            )
        )
        owner = self._owner_token
        claimed: list[sqlite3.Row] = []
        with self._lock:
            # Crash recovery: free expired leases before selecting due work.
            self._conn.execute(
                f"""
                UPDATE {_OUTBOX_TABLE}
                SET delivery_state = ?,
                    claim_owner = NULL,
                    claim_expires_at = NULL,
                    updated_at = ?,
                    next_attempt_at = COALESCE(next_attempt_at, ?)
                WHERE delivery_state = ?
                  AND (
                    claim_expires_at IS NULL
                    OR claim_expires_at <= ?
                  )
                """,
                (
                    DeliveryState.PENDING.value,
                    now_s,
                    now_s,
                    DeliveryState.IN_FLIGHT.value,
                    now_s,
                ),
            )
            child_skipped = self._suppress_workflow_child_rows_unlocked(
                now_s=now_s
            )
            if child_skipped:
                logger.info(
                    "workflow child notification rows suppressed count=%s",
                    child_skipped,
                )
            rows = self._conn.execute(
                f"""
                SELECT * FROM {_OUTBOX_TABLE}
                WHERE delivery_state = ?
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (DeliveryState.PENDING.value, now_s, limit),
            ).fetchall()
            for row in rows:
                cursor = self._conn.execute(
                    f"""
                    UPDATE {_OUTBOX_TABLE}
                    SET delivery_state = ?,
                        claim_owner = ?,
                        claim_expires_at = ?,
                        updated_at = ?
                    WHERE event_id = ?
                      AND delivery_state = ?
                      AND attempt_count = ?
                      AND (
                        claim_owner IS NULL
                        OR claim_expires_at IS NULL
                        OR claim_expires_at <= ?
                      )
                    """,
                    (
                        DeliveryState.IN_FLIGHT.value,
                        owner,
                        expires_s,
                        now_s,
                        row["event_id"],
                        DeliveryState.PENDING.value,
                        row["attempt_count"],
                        now_s,
                    ),
                )
                if cursor.rowcount == 1:
                    claimed.append(row)
            self._conn.commit()
        return claimed

    def _clear_claim_fields(
        self,
        event_id: str,
        *,
        delivery_state: str,
        attempt_count: int | None = None,
        next_attempt_at: str | None = None,
        last_error: str | None = None,
        delivered_at: str | None = None,
        clear_error: bool = False,
        only_if_in_flight: bool = False,
    ) -> bool:
        now_s = _format_dt(_utc_now())
        with self._lock:
            sets = [
                "delivery_state = ?",
                "claim_owner = NULL",
                "claim_expires_at = NULL",
                "updated_at = ?",
                "next_attempt_at = ?",
            ]
            params: list[Any] = [
                delivery_state,
                now_s,
                next_attempt_at,
            ]
            if attempt_count is not None:
                sets.append("attempt_count = ?")
                params.append(attempt_count)
            if clear_error:
                sets.append("last_error = NULL")
            elif last_error is not None:
                sets.append("last_error = ?")
                params.append(last_error)
            if delivered_at is not None:
                sets.append("delivered_at = ?")
                params.append(delivered_at)
            params.append(event_id)
            where = "event_id = ?"
            if only_if_in_flight:
                where += " AND delivery_state = ?"
                params.append(DeliveryState.IN_FLIGHT.value)
            cursor = self._conn.execute(
                f"UPDATE {_OUTBOX_TABLE} SET {', '.join(sets)} WHERE {where}",
                tuple(params),
            )
            self._conn.commit()
            return int(cursor.rowcount or 0) > 0

    def _mark_delivered(self, event_id: str) -> bool:
        """CAS ``in_flight`` → ``delivered``. Returns False if claim was lost."""
        now_s = _format_dt(_utc_now())
        assert now_s is not None
        return self._clear_claim_fields(
            event_id,
            delivery_state=DeliveryState.DELIVERED.value,
            next_attempt_at=None,
            delivered_at=now_s,
            clear_error=True,
            only_if_in_flight=True,
        )

    def _finalize_successful_delivery(self, row: sqlite3.Row) -> str:
        """CAS deliver, or skip if membership/terminal race forbids paging.

        All claimed rows finalize under one ``BEGIN IMMEDIATE`` that re-reads
        durable child membership and (for heartbeat stale/recovery) run status
        so a concurrent child bind or terminal commit cannot lose to
        ``delivered``.

        Returns ``delivered``, ``skipped``, or ``cas_missed``.
        """
        now_s = _format_dt(_utc_now())
        assert now_s is not None
        outcome = self._finalize_claimed_outbox_row(
            row,
            active_delivery_state=DeliveryState.DELIVERED.value,
            next_attempt_at=None,
            delivered_at=now_s,
            clear_error=True,
        )
        if outcome == "cas_missed":
            logger.info(
                "notification deliver CAS missed event_id=%s run_id=%s "
                "event_kind=%s (already terminalized or reclaimed)",
                row["event_id"],
                row["run_id"],
                row["event_kind"],
            )
        return outcome

    def _mark_skipped(
        self,
        event_id: str,
        *,
        reason: str,
    ) -> None:
        """Terminal non-failure: intentionally not delivered (no retries)."""
        safe_reason = (
            redact_notification_error(reason) or PUSHOVER_PHASE_CHANGE_SUPPRESSED
        )
        self._clear_claim_fields(
            event_id,
            delivery_state=DeliveryState.SKIPPED.value,
            next_attempt_at=None,
            last_error=safe_reason,
        )

    def _mark_retry_or_dead(
        self,
        event_id: str,
        *,
        attempt_count: int,
        error: str,
        max_attempts: int,
        backoff_base_seconds: float,
        backoff_max_seconds: float,
    ) -> None:
        safe_error = redact_notification_error(error) or "delivery_failed"
        now = _utc_now()
        now_s = _format_dt(now)
        assert now_s is not None
        if attempt_count >= max_attempts:
            state = DeliveryState.DEAD.value
            next_at = None
        else:
            state = DeliveryState.PENDING.value
            delay = compute_backoff_seconds(
                attempt_count,
                base_seconds=backoff_base_seconds,
                max_seconds=backoff_max_seconds,
            )
            next_at = _format_dt(
                datetime.fromtimestamp(now.timestamp() + delay, tz=timezone.utc)
            )
        self._clear_claim_fields(
            event_id,
            delivery_state=state,
            attempt_count=attempt_count,
            next_attempt_at=next_at,
            last_error=safe_error,
            only_if_in_flight=True,
        )

    def _finalize_failed_delivery(
        self,
        row: sqlite3.Row,
        *,
        attempt_count: int,
        error: str,
        max_attempts: int,
        backoff_base_seconds: float,
        backoff_max_seconds: float,
    ) -> str:
        """Retry/dead-letter finalization; suppressed rows use the CAS.

        Heartbeat stale/paired recovery never transitions to retry or
        dead-letter when the run is terminal or status is unavailable.
        Workflow children skip instead of retrying. Returns the resulting
        delivery state name, ``skipped``, or ``cas_missed``.
        """
        safe_error = redact_notification_error(error) or "delivery_failed"
        now = _utc_now()
        if attempt_count >= max_attempts:
            state = DeliveryState.DEAD.value
            next_at = None
        else:
            state = DeliveryState.PENDING.value
            delay = compute_backoff_seconds(
                attempt_count,
                base_seconds=backoff_base_seconds,
                max_seconds=backoff_max_seconds,
            )
            next_at = _format_dt(
                datetime.fromtimestamp(now.timestamp() + delay, tz=timezone.utc)
            )
        return self._finalize_claimed_outbox_row(
            row,
            active_delivery_state=state,
            attempt_count=attempt_count,
            next_attempt_at=next_at,
            last_error=safe_error,
        )

    def _deliver_one(self, row: sqlite3.Row, config: NotificationConfig) -> None:
        """Attempt one backend delivery. Never mutates mission/run status."""
        # Race-safe: skip workflow children and obsolete heartbeat stale/
        # recovery once the run is terminal (even when backends are disabled).
        pre = self._finalize_claimed_outbox_row(row)
        if pre != "active":
            return

        backend = resolve_delivery_backend(config)
        if backend == BACKEND_NONE:
            # Opt-in off: leave pending so inspection still works; do not HTTP.
            # Re-check child membership and terminal status atomically.
            self._finalize_claimed_outbox_row(
                row,
                active_delivery_state=DeliveryState.PENDING.value,
                next_attempt_at=_format_dt(_utc_now()),
            )
            return

        if backend == BACKEND_PUSHOVER:
            self._deliver_pushover(row, config)
            return

        self._deliver_webhook(row, config)

    def _deliver_webhook(
        self, row: sqlite3.Row, config: NotificationConfig
    ) -> None:
        """HMAC webhook delivery path (unchanged semantics)."""
        assert config.webhook_url is not None
        secret = config.secret_for_signing()
        assert secret is not None

        attempt_count = int(row["attempt_count"]) + 1
        try:
            body_obj = {
                "event_id": row["event_id"],
                "run_id": row["run_id"],
                "event_kind": row["event_kind"],
                "payload": sanitize_notification_payload(
                    json.loads(row["payload_json"])
                ),
            }
            body = json.dumps(
                body_obj, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
            timestamp, signature = sign_webhook_body(body, secret=secret)
            headers = {
                "Content-Type": "application/json",
                SIGNATURE_HEADER: signature,
                TIMESTAMP_HEADER: timestamp,
                EVENT_ID_HEADER: row["event_id"],
                EVENT_KIND_HEADER: row["event_kind"],
                "User-Agent": "mission-control-notifications/2c",
            }
            if self._http_client is not None:
                # Test/injected transport: still disable redirects and re-validate.
                validate_webhook_url(
                    config.webhook_url, allow_http=config.allow_http
                )
                response = self._http_client.post(
                    config.webhook_url,
                    content=body,
                    headers=headers,
                    timeout=config.timeout_seconds,
                    follow_redirects=False,
                )
            else:
                response = post_webhook_ssrf_safe(
                    config.webhook_url,
                    content=body,
                    headers=headers,
                    timeout_seconds=config.timeout_seconds,
                    allow_http=config.allow_http,
                )

            if 200 <= response.status_code < 300:
                outcome = self._finalize_successful_delivery(row)
                if outcome == "delivered":
                    logger.info(
                        "notification delivered backend=webhook event_id=%s "
                        "run_id=%s event_kind=%s status_code=%s",
                        row["event_id"],
                        row["run_id"],
                        row["event_kind"],
                        response.status_code,
                    )
                return

            error = f"http_status_{response.status_code}"
        except ValueError as exc:
            # Permanent URL/policy failure — do not retry forever.
            error = redact_notification_error(str(exc)) or "invalid_webhook_url"
            self._finalize_failed_delivery(
                row,
                attempt_count=config.max_attempts,
                error=error,
                max_attempts=config.max_attempts,
                backoff_base_seconds=config.backoff_base_seconds,
                backoff_max_seconds=config.backoff_max_seconds,
            )
            logger.warning(
                "notification permanent failure backend=webhook event_id=%s "
                "run_id=%s event_kind=%s reason=invalid_target",
                row["event_id"],
                row["run_id"],
                row["event_kind"],
            )
            return
        except Exception as exc:  # noqa: BLE001 — durable retry path
            error = redact_notification_error(f"{type(exc).__name__}") or (
                "delivery_error"
            )

        self._finalize_failed_delivery(
            row,
            attempt_count=attempt_count,
            error=error,
            max_attempts=config.max_attempts,
            backoff_base_seconds=config.backoff_base_seconds,
            backoff_max_seconds=config.backoff_max_seconds,
        )
        logger.warning(
            "notification delivery failed backend=webhook event_id=%s "
            "run_id=%s event_kind=%s attempt=%s",
            row["event_id"],
            row["run_id"],
            row["event_kind"],
            attempt_count,
        )

    def _deliver_pushover(
        self, row: sqlite3.Row, config: NotificationConfig
    ) -> None:
        """Native Pushover Messages API delivery (fixed official host)."""
        credentials = config.pushover_credentials()
        assert credentials is not None
        user_key, app_token = credentials

        event_kind = str(row["event_kind"] or "")
        if not should_deliver_pushover_event(event_kind):
            # Keep the durable outbox row; do not HTTP, dead, or retry.
            self._mark_skipped(
                row["event_id"],
                reason=PUSHOVER_PHASE_CHANGE_SUPPRESSED,
            )
            logger.info(
                "notification skipped backend=pushover event_id=%s "
                "run_id=%s event_kind=%s reason=%s",
                row["event_id"],
                row["run_id"],
                event_kind,
                PUSHOVER_PHASE_CHANGE_SUPPRESSED,
            )
            return

        attempt_count = int(row["attempt_count"]) + 1
        try:
            payload = sanitize_notification_payload(
                json.loads(row["payload_json"])
            )
            title = format_pushover_title(event_kind, payload=payload)
            message = format_pushover_message(payload)
            form = build_pushover_form(
                user_key=user_key,
                app_token=app_token,
                title=title,
                message=message,
                priority=config.pushover_priority,
                device=config.pushover_device,
                sound=config.pushover_sound,
            )
            if self._http_client is not None:
                validate_webhook_url(PUSHOVER_API_URL, allow_http=False)
                response = self._http_client.post(
                    PUSHOVER_API_URL,
                    data=form,
                    headers={
                        "User-Agent": (
                            "mission-control-notifications/2d-pushover"
                        ),
                    },
                    timeout=_bounded_httpx_timeout(config.timeout_seconds),
                    follow_redirects=False,
                )
            else:
                response = post_pushover_message(
                    form=form,
                    timeout_seconds=config.timeout_seconds,
                )

            if is_pushover_success_response(response):
                outcome = self._finalize_successful_delivery(row)
                if outcome == "delivered":
                    logger.info(
                        "notification delivered backend=pushover event_id=%s "
                        "run_id=%s event_kind=%s status_code=%s",
                        row["event_id"],
                        row["run_id"],
                        row["event_kind"],
                        response.status_code,
                    )
                return

            error, retryable = classify_pushover_response(response)
            if not retryable:
                self._finalize_failed_delivery(
                    row,
                    attempt_count=config.max_attempts,
                    error=error,
                    max_attempts=config.max_attempts,
                    backoff_base_seconds=config.backoff_base_seconds,
                    backoff_max_seconds=config.backoff_max_seconds,
                )
                logger.warning(
                    "notification permanent failure backend=pushover "
                    "event_id=%s run_id=%s event_kind=%s reason=%s",
                    row["event_id"],
                    row["run_id"],
                    row["event_kind"],
                    error,
                )
                return
        except ValueError as exc:
            error = (
                redact_notification_error(str(exc)) or "invalid_pushover_config"
            )
            self._finalize_failed_delivery(
                row,
                attempt_count=config.max_attempts,
                error=error,
                max_attempts=config.max_attempts,
                backoff_base_seconds=config.backoff_base_seconds,
                backoff_max_seconds=config.backoff_max_seconds,
            )
            logger.warning(
                "notification permanent failure backend=pushover event_id=%s "
                "run_id=%s event_kind=%s reason=invalid_config",
                row["event_id"],
                row["run_id"],
                row["event_kind"],
            )
            return
        except Exception as exc:  # noqa: BLE001 — durable retry path
            error = redact_notification_error(f"{type(exc).__name__}") or (
                "delivery_error"
            )

        self._finalize_failed_delivery(
            row,
            attempt_count=attempt_count,
            error=error,
            max_attempts=config.max_attempts,
            backoff_base_seconds=config.backoff_base_seconds,
            backoff_max_seconds=config.backoff_max_seconds,
        )
        logger.warning(
            "notification delivery failed backend=pushover event_id=%s "
            "run_id=%s event_kind=%s attempt=%s",
            row["event_id"],
            row["run_id"],
            row["event_kind"],
            attempt_count,
        )

    def process_due_deliveries(self, *, limit: int = 16) -> int:
        """Process due outbox rows. Returns number of delivery attempts.

        Delivery failures never alter mission/run status. When notifications
        are not configured, claimed rows are returned to pending without HTTP.
        """
        config = self.config
        claimed = self._claim_due_events(limit=limit)
        for row in claimed:
            try:
                self._deliver_one(row, config)
            except Exception:  # noqa: BLE001 — worker must keep draining
                logger.exception(
                    "notification delivery crashed event_id=%s run_id=%s",
                    row["event_id"],
                    row["run_id"],
                )
                try:
                    self._finalize_failed_delivery(
                        row,
                        attempt_count=int(row["attempt_count"]) + 1,
                        error="delivery_exception",
                        max_attempts=config.max_attempts,
                        backoff_base_seconds=config.backoff_base_seconds,
                        backoff_max_seconds=config.backoff_max_seconds,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "notification failed to record delivery exception "
                        "event_id=%s",
                        row["event_id"],
                    )
        return len(claimed)


# Process-local outbox singleton (same DB path as the default run registry).
_default_outbox: NotificationOutbox | None = None
_default_outbox_lock = threading.Lock()

# Best-effort wake hook for the durable background delivery worker.
_delivery_wake_callbacks: list[Any] = []
_delivery_wake_lock = threading.Lock()


def register_delivery_wake_callback(callback: Any) -> None:
    """Register a no-arg callback invoked after durable enqueue."""
    with _delivery_wake_lock:
        if callback not in _delivery_wake_callbacks:
            _delivery_wake_callbacks.append(callback)


def unregister_delivery_wake_callback(callback: Any) -> None:
    with _delivery_wake_lock:
        _delivery_wake_callbacks[:] = [
            item for item in _delivery_wake_callbacks if item is not callback
        ]


def wake_notification_delivery() -> None:
    """Wake background delivery without performing synchronous HTTP."""
    with _delivery_wake_lock:
        callbacks = list(_delivery_wake_callbacks)
    for callback in callbacks:
        try:
            callback()
        except Exception:  # noqa: BLE001 — wake must never break enqueue
            logger.exception("notification delivery wake callback failed")


def get_notification_outbox(
    db_path: str | None = None,
) -> NotificationOutbox:
    """Return the process notification outbox (creates on first use)."""
    global _default_outbox
    with _default_outbox_lock:
        if db_path is not None:
            return NotificationOutbox(db_path)
        if _default_outbox is None:
            _default_outbox = NotificationOutbox()
        return _default_outbox


def reset_notification_outbox_for_tests() -> None:
    """Close and clear the process singleton (tests only)."""
    global _default_outbox
    with _default_outbox_lock:
        if _default_outbox is not None:
            try:
                _default_outbox.close()
            except Exception:  # noqa: BLE001
                pass
            _default_outbox = None


class NotificationDeliveryWorker:
    """Single background drainer: wake-on-enqueue + periodic due retries.

    Never mutates mission/run status. Tolerates delivery exceptions and
    refuses to start a second concurrent worker thread for the same instance.
    """

    def __init__(
        self,
        outbox: NotificationOutbox,
        *,
        poll_seconds: float | None = None,
        name: str = "notification-delivery-worker",
    ) -> None:
        self._outbox = outbox
        self._poll_seconds = (
            float(poll_seconds)
            if poll_seconds is not None
            else float(outbox.config.worker_poll_seconds)
        )
        self._name = name
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        self._drain_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def kick(self) -> None:
        """Signal that due work may exist (non-blocking, no HTTP)."""
        self._wake.set()

    def start(self) -> bool:
        """Start the worker thread. Returns False if already running."""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._wake.set()
            thread = threading.Thread(
                target=self._run,
                name=self._name,
                daemon=True,
            )
            self._thread = thread
            thread.start()
            register_delivery_wake_callback(self.kick)
            return True

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop the worker cleanly and unregister wake callbacks."""
        with self._lifecycle_lock:
            unregister_delivery_wake_callback(self.kick)
            self._stop.set()
            self._wake.set()
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def drain_once(self, *, limit: int = 16) -> int:
        """Reclaim stale leases and process due rows (serialized)."""
        with self._drain_lock:
            try:
                self._outbox.reclaim_stale_claims()
            except sqlite3.ProgrammingError:
                logger.warning(
                    "notification reclaim skipped; outbox database closed"
                )
                return 0
            except Exception:  # noqa: BLE001
                logger.exception("notification reclaim failed")
            try:
                return self._outbox.process_due_deliveries(limit=limit)
            except sqlite3.ProgrammingError:
                logger.warning(
                    "notification drain skipped; outbox database closed"
                )
                return 0
            except Exception:  # noqa: BLE001
                logger.exception("notification drain failed")
                return 0

    def _run(self) -> None:
        while not self._stop.is_set():
            self.drain_once()
            self._wake.clear()
            # Wait for enqueue wake or periodic retry poll.
            self._wake.wait(timeout=self._poll_seconds)
