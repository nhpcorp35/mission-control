"""Phase 2C durable mission notification outbox and webhook delivery.

Opt-in generic webhook notifications for phase_change, stale, recovery, and
terminal events only (never heartbeats). Failures never mutate mission status.
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
from typing import Any, Mapping
from urllib.parse import urlparse
import uuid

import httpx

from mission_control.monitoring import (
    HEARTBEAT_STALE_THRESHOLD_SECONDS,
    HeartbeatHealth,
    classify_heartbeat_health,
)
from mission_control.run_registry import (
    RunPhase,
    RunRecord,
    RunStatus,
    is_terminal_status,
    resolve_db_path,
    sanitize_progress,
)

logger = logging.getLogger(__name__)

_OUTBOX_TABLE = "notification_outbox"
_SQLITE_BUSY_TIMEOUT_MS = 5000

# Delivery bounds (operator-overridable via env).
DEFAULT_WEBHOOK_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_BACKOFF_MAX_SECONDS = 300.0
NOTIFICATION_INSPECT_MAX_EVENTS = 64

# Opt-in configuration (safe no-config: disabled when unset).
WEBHOOK_URL_ENV = "MISSION_CONTROL_NOTIFICATIONS_WEBHOOK_URL"
WEBHOOK_SECRET_ENV = "MISSION_CONTROL_NOTIFICATIONS_WEBHOOK_SECRET"
ENABLED_ENV = "MISSION_CONTROL_NOTIFICATIONS_ENABLED"
TIMEOUT_ENV = "MISSION_CONTROL_NOTIFICATIONS_TIMEOUT_SECONDS"
MAX_ATTEMPTS_ENV = "MISSION_CONTROL_NOTIFICATIONS_MAX_ATTEMPTS"
BACKOFF_BASE_ENV = "MISSION_CONTROL_NOTIFICATIONS_BACKOFF_BASE_SECONDS"
BACKOFF_MAX_ENV = "MISSION_CONTROL_NOTIFICATIONS_BACKOFF_MAX_SECONDS"

SIGNATURE_HEADER = "X-Mission-Control-Signature"
TIMESTAMP_HEADER = "X-Mission-Control-Timestamp"
EVENT_ID_HEADER = "X-Mission-Control-Event-Id"
EVENT_KIND_HEADER = "X-Mission-Control-Event-Kind"

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
    }
)
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


EMITTED_EVENT_KINDS = frozenset(kind.value for kind in NotificationEventKind)


@dataclass(frozen=True)
class NotificationConfig:
    """Resolved opt-in webhook configuration (secrets never stringified)."""

    enabled: bool
    webhook_url: str | None
    timeout_seconds: float
    max_attempts: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    _secret: str | None

    @property
    def has_secret(self) -> bool:
        return bool(self._secret)

    def secret_for_signing(self) -> str | None:
        """Return the webhook secret for HMAC only (callers must not log)."""
        return self._secret

    def __repr__(self) -> str:
        return (
            "NotificationConfig("
            f"enabled={self.enabled!r}, "
            f"webhook_url={'set' if self.webhook_url else None}, "
            f"has_secret={self.has_secret}, "
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"max_attempts={self.max_attempts!r})"
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


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


def load_notification_config(
    environ: Mapping[str, str] | None = None,
) -> NotificationConfig:
    """Load opt-in notification settings. Unset URL/secret → disabled."""
    env = os.environ if environ is None else environ
    url = (env.get(WEBHOOK_URL_ENV) or "").strip() or None
    secret = (env.get(WEBHOOK_SECRET_ENV) or "").strip() or None
    enabled_raw = (env.get(ENABLED_ENV) or "").strip().lower()
    if enabled_raw in {"0", "false", "no", "off"}:
        enabled = False
    elif enabled_raw in {"1", "true", "yes", "on"}:
        enabled = bool(url and secret)
    else:
        # Safe default: enable only when both URL and secret are configured.
        enabled = bool(url and secret)

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
        _secret=secret,
    )


def is_notifications_configured(
    config: NotificationConfig | None = None,
) -> bool:
    """Return True when webhook delivery is opted in and fully configured."""
    cfg = config if config is not None else load_notification_config()
    return bool(cfg.enabled and cfg.webhook_url and cfg.has_secret)


def validate_webhook_url(url: str) -> str:
    """Validate webhook URL and reject SSRF-prone targets.

    Requires http/https with a public resolvable host. Raises ValueError on
    rejection (message never includes secrets).
    """
    text = (url or "").strip()
    if not text:
        raise ValueError("webhook URL is empty")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("webhook URL scheme must be http or https")
    if not parsed.hostname:
        raise ValueError("webhook URL host is required")
    if parsed.username or parsed.password:
        raise ValueError("webhook URL must not include credentials")
    host = parsed.hostname
    if host.lower() in {"localhost", "metadata.google.internal"}:
        raise ValueError("webhook URL host is not allowed")

    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("webhook URL host could not be resolved") from exc

    if not infos:
        raise ValueError("webhook URL host could not be resolved")

    for info in infos:
        sockaddr = info[4]
        ip_text = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError as exc:
            raise ValueError("webhook URL resolved to an invalid address") from exc
        if any(ip in network for network in _PRIVATE_NETWORKS):
            raise ValueError("webhook URL resolves to a blocked address")
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("webhook URL resolves to a blocked address")

    return text


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
        WEBHOOK_SECRET_ENV.lower(),
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
        text = str(raw)
        if key == "dedupe_key":
            payload[key] = text[:128]
        elif key in {"run_id", "event_kind", "status", "phase", "heartbeat_health"}:
            payload[key] = text[:64]
        elif key == "occurred_at":
            payload[key] = text[:64]
        else:
            payload[key] = text[:160]
    return payload


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
                    UNIQUE (run_id, event_kind, dedupe_key)
                )
                """
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
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

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
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        event_id,
                        str(run_id)[:64],
                        kind,
                        str(dedupe_key)[:128],
                        json.dumps(clean, separators=(",", ":"), sort_keys=True),
                        DeliveryState.PENDING.value,
                        now,
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
        """Emit terminal once per terminal status snapshot."""
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
        return self.enqueue_for_record(
            record,
            event_kind=NotificationEventKind.TERMINAL,
            dedupe_key=dedupe,
            occurred_at=record.completed_at,
            heartbeat_health=HeartbeatHealth.TERMINAL.value,
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
        """Emit stale at most once per stale heartbeat observation window."""
        health = classify_heartbeat_health(
            record,
            now=now,
            stale_threshold_seconds=stale_threshold_seconds,
        )
        if health is not HeartbeatHealth.STALE:
            return EnqueueResult(
                created=False,
                event_id=None,
                skipped_reason="not_stale",
            )
        hb = _format_dt(record.heartbeat_at) or "absent"
        dedupe = f"stale:{hb}"
        return self.enqueue_for_record(
            record,
            event_kind=NotificationEventKind.STALE,
            dedupe_key=dedupe,
            occurred_at=now,
            heartbeat_health=HeartbeatHealth.STALE.value,
        )

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

    def _claim_due_events(self, *, limit: int = 16) -> list[sqlite3.Row]:
        now = _utc_now()
        now_s = _format_dt(now)
        assert now_s is not None
        claimed: list[sqlite3.Row] = []
        with self._lock:
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
                    SET delivery_state = ?, updated_at = ?
                    WHERE event_id = ?
                      AND delivery_state = ?
                      AND attempt_count = ?
                    """,
                    (
                        DeliveryState.IN_FLIGHT.value,
                        now_s,
                        row["event_id"],
                        DeliveryState.PENDING.value,
                        row["attempt_count"],
                    ),
                )
                if cursor.rowcount == 1:
                    claimed.append(row)
            self._conn.commit()
        return claimed

    def _mark_delivered(self, event_id: str) -> None:
        now_s = _format_dt(_utc_now())
        with self._lock:
            self._conn.execute(
                f"""
                UPDATE {_OUTBOX_TABLE}
                SET delivery_state = ?,
                    delivered_at = ?,
                    last_error = NULL,
                    updated_at = ?,
                    next_attempt_at = NULL
                WHERE event_id = ?
                """,
                (
                    DeliveryState.DELIVERED.value,
                    now_s,
                    now_s,
                    event_id,
                ),
            )
            self._conn.commit()

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
        with self._lock:
            self._conn.execute(
                f"""
                UPDATE {_OUTBOX_TABLE}
                SET delivery_state = ?,
                    attempt_count = ?,
                    next_attempt_at = ?,
                    last_error = ?,
                    updated_at = ?
                WHERE event_id = ?
                """,
                (
                    state,
                    attempt_count,
                    next_at,
                    safe_error,
                    now_s,
                    event_id,
                ),
            )
            self._conn.commit()

    def _deliver_one(self, row: sqlite3.Row, config: NotificationConfig) -> None:
        """Attempt one webhook delivery. Never mutates mission/run status."""
        if not is_notifications_configured(config):
            # Opt-in off: leave pending so inspection still works; do not HTTP.
            now_s = _format_dt(_utc_now())
            with self._lock:
                self._conn.execute(
                    f"""
                    UPDATE {_OUTBOX_TABLE}
                    SET delivery_state = ?, updated_at = ?
                    WHERE event_id = ? AND delivery_state = ?
                    """,
                    (
                        DeliveryState.PENDING.value,
                        now_s,
                        row["event_id"],
                        DeliveryState.IN_FLIGHT.value,
                    ),
                )
                self._conn.commit()
            return

        assert config.webhook_url is not None
        secret = config.secret_for_signing()
        assert secret is not None

        attempt_count = int(row["attempt_count"]) + 1
        try:
            validate_webhook_url(config.webhook_url)
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
            client = self._http_client
            owns_client = False
            if client is None:
                client = httpx.Client(timeout=config.timeout_seconds)
                owns_client = True
            try:
                response = client.post(
                    config.webhook_url,
                    content=body,
                    headers=headers,
                    timeout=config.timeout_seconds,
                )
            finally:
                if owns_client:
                    client.close()

            if 200 <= response.status_code < 300:
                self._mark_delivered(row["event_id"])
                logger.info(
                    "notification delivered event_id=%s run_id=%s "
                    "event_kind=%s status_code=%s",
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
            self._mark_retry_or_dead(
                row["event_id"],
                attempt_count=config.max_attempts,
                error=error,
                max_attempts=config.max_attempts,
                backoff_base_seconds=config.backoff_base_seconds,
                backoff_max_seconds=config.backoff_max_seconds,
            )
            logger.warning(
                "notification permanent failure event_id=%s run_id=%s "
                "event_kind=%s reason=invalid_target",
                row["event_id"],
                row["run_id"],
                row["event_kind"],
            )
            return
        except Exception as exc:  # noqa: BLE001 — durable retry path
            error = redact_notification_error(f"{type(exc).__name__}") or (
                "delivery_error"
            )

        self._mark_retry_or_dead(
            row["event_id"],
            attempt_count=attempt_count,
            error=error,
            max_attempts=config.max_attempts,
            backoff_base_seconds=config.backoff_base_seconds,
            backoff_max_seconds=config.backoff_max_seconds,
        )
        logger.warning(
            "notification delivery failed event_id=%s run_id=%s "
            "event_kind=%s attempt=%s",
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
            self._deliver_one(row, config)
        return len(claimed)


# Process-local outbox singleton (same DB path as the default run registry).
_default_outbox: NotificationOutbox | None = None
_default_outbox_lock = threading.Lock()


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
