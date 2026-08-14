"""Focused tests for Phase 2D native Pushover notification delivery."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import app.api as api_module
from app.api import app
from mission_control.notifications import (
    BACKEND_NONE,
    BACKEND_PUSHOVER,
    BACKEND_WEBHOOK,
    DEFAULT_PUSHOVER_SOUND,
    PUSHOVER_API_URL,
    PUSHOVER_APP_TOKEN_ENV,
    PUSHOVER_DEVICE_ENV,
    PUSHOVER_MESSAGE_MAX_CHARS,
    PUSHOVER_PHASE_CHANGE_SUPPRESSED,
    PUSHOVER_PRIORITY_ENV,
    PUSHOVER_SOUND_ENV,
    PUSHOVER_TITLE_MAX_CHARS,
    PUSHOVER_USER_KEY_ENV,
    WEBHOOK_SECRET_ENV,
    WEBHOOK_URL_ENV,
    DeliveryState,
    NotificationConfig,
    NotificationDeliveryWorker,
    NotificationEventKind,
    NotificationOutbox,
    build_pushover_form,
    classify_pushover_http_failure,
    classify_pushover_response,
    format_pushover_message,
    format_pushover_title,
    is_notifications_configured,
    is_pushover_success_response,
    load_notification_config,
    notification_backend_health,
    parse_pushover_priority,
    redact_notification_error,
    resolve_delivery_backend,
    resolve_pushover_sound,
    sanitize_pushover_device_or_sound,
    should_deliver_pushover_event,
)
from mission_control.run_registry import (
    RunPhase,
    RunRegistry,
    RunStatus,
)

TEST_API_KEY = "mc_test_authentication_key"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_API_KEY}"}
os.environ["MISSION_CONTROL_API_KEY"] = TEST_API_KEY

_PUBLIC_WEBHOOK = "https://example.com/hooks/mission-control"
_TEST_USER = "pushover-user-key-fixture-aaaa"
_TEST_TOKEN = "pushover-app-token-fixture-bbbb"


def _pushover_config(
    *,
    enabled: bool = True,
    user_key: str | None = _TEST_USER,
    app_token: str | None = _TEST_TOKEN,
    device: str | None = None,
    priority: int = 0,
    sound: str | None = None,
    max_attempts: int = 3,
    timeout_seconds: float = 2.0,
    backoff_base_seconds: float = 0.01,
    backoff_max_seconds: float = 1.0,
    claim_lease_seconds: float = 30.0,
    worker_poll_seconds: float = 0.05,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
) -> NotificationConfig:
    return NotificationConfig(
        enabled=enabled,
        webhook_url=webhook_url,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        backoff_base_seconds=backoff_base_seconds,
        backoff_max_seconds=backoff_max_seconds,
        claim_lease_seconds=claim_lease_seconds,
        allow_http=False,
        worker_poll_seconds=worker_poll_seconds,
        _secret=webhook_secret,
        _pushover_user_key=user_key,
        _pushover_app_token=app_token,
        pushover_device=device,
        pushover_priority=priority,
        pushover_sound=sound,
    )


def _record(registry: RunRegistry):
    return registry.create_run()


class TestPushoverConfig(unittest.TestCase):
    def test_no_config_disabled(self) -> None:
        cfg = load_notification_config(environ={})
        self.assertFalse(is_notifications_configured(cfg))
        self.assertEqual(resolve_delivery_backend(cfg), BACKEND_NONE)
        health = notification_backend_health(cfg)
        self.assertFalse(health["notifications_enabled"])
        self.assertEqual(health["active_backend"], BACKEND_NONE)
        self.assertNotIn(_TEST_USER, repr(cfg))
        self.assertNotIn(_TEST_TOKEN, repr(cfg))

    def test_pushover_only_enables(self) -> None:
        cfg = load_notification_config(
            environ={
                PUSHOVER_USER_KEY_ENV: _TEST_USER,
                PUSHOVER_APP_TOKEN_ENV: _TEST_TOKEN,
            }
        )
        self.assertTrue(is_notifications_configured(cfg))
        self.assertEqual(resolve_delivery_backend(cfg), BACKEND_PUSHOVER)
        self.assertNotIn(_TEST_USER, repr(cfg))
        self.assertNotIn(_TEST_TOKEN, repr(cfg))

    def test_both_backends_prefer_webhook(self) -> None:
        cfg = load_notification_config(
            environ={
                WEBHOOK_URL_ENV: _PUBLIC_WEBHOOK,
                WEBHOOK_SECRET_ENV: "webhook-secret-value",
                PUSHOVER_USER_KEY_ENV: _TEST_USER,
                PUSHOVER_APP_TOKEN_ENV: _TEST_TOKEN,
            }
        )
        self.assertEqual(resolve_delivery_backend(cfg), BACKEND_WEBHOOK)
        health = notification_backend_health(cfg)
        self.assertTrue(health["webhook_configured"])
        self.assertTrue(health["pushover_configured"])
        self.assertEqual(health["active_backend"], BACKEND_WEBHOOK)
        self.assertEqual(health["dual_backend_policy"], "prefer_webhook")

    def test_partial_pushover_not_enabled(self) -> None:
        cfg = load_notification_config(
            environ={PUSHOVER_USER_KEY_ENV: _TEST_USER}
        )
        self.assertFalse(is_notifications_configured(cfg))
        self.assertEqual(resolve_delivery_backend(cfg), BACKEND_NONE)

    def test_device_priority_sound_options(self) -> None:
        cfg = load_notification_config(
            environ={
                PUSHOVER_USER_KEY_ENV: _TEST_USER,
                PUSHOVER_APP_TOKEN_ENV: _TEST_TOKEN,
                PUSHOVER_DEVICE_ENV: "iphone",
                PUSHOVER_PRIORITY_ENV: "1",
                PUSHOVER_SOUND_ENV: "cosmic",
            }
        )
        self.assertEqual(cfg.pushover_device, "iphone")
        self.assertEqual(cfg.pushover_priority, 1)
        self.assertEqual(cfg.pushover_sound, "cosmic")
        health = notification_backend_health(cfg)
        self.assertTrue(health["pushover_device_set"])
        self.assertTrue(health["pushover_sound_set"])
        self.assertEqual(health["pushover_priority"], 1)

    def test_emergency_priority_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_pushover_priority("2")
        cfg = load_notification_config(
            environ={
                PUSHOVER_USER_KEY_ENV: _TEST_USER,
                PUSHOVER_APP_TOKEN_ENV: _TEST_TOKEN,
                PUSHOVER_PRIORITY_ENV: "2",
            }
        )
        self.assertEqual(cfg.pushover_priority, 0)

    def test_device_and_sound_reject_control_characters(self) -> None:
        self.assertIsNone(
            sanitize_pushover_device_or_sound("phone\nname", max_chars=64)
        )
        self.assertIsNone(
            sanitize_pushover_device_or_sound("cos\x00mic", max_chars=32)
        )
        self.assertEqual(
            sanitize_pushover_device_or_sound("iphone", max_chars=64),
            "iphone",
        )
        cfg = load_notification_config(
            environ={
                PUSHOVER_USER_KEY_ENV: _TEST_USER,
                PUSHOVER_APP_TOKEN_ENV: _TEST_TOKEN,
                PUSHOVER_DEVICE_ENV: "bad\tdevice",
                PUSHOVER_SOUND_ENV: "good_sound",
            }
        )
        self.assertIsNone(cfg.pushover_device)
        self.assertEqual(cfg.pushover_sound, "good_sound")
        form = build_pushover_form(
            user_key=_TEST_USER,
            app_token=_TEST_TOKEN,
            title="t",
            message="m",
            priority=0,
            device="ok\rdevice",
            sound="pushover",
        )
        self.assertNotIn("device", form)
        self.assertEqual(form["sound"], "pushover")


class TestPushoverFormatting(unittest.TestCase):
    def test_terminal_stale_recovery_titles(self) -> None:
        self.assertEqual(
            format_pushover_title("terminal"),
            "Mission Control · terminal",
        )
        self.assertEqual(
            format_pushover_title("stale"),
            "Mission Control · stale",
        )
        self.assertEqual(
            format_pushover_title("recovery"),
            "Mission Control · recovery",
        )
        self.assertLessEqual(
            len(format_pushover_title("terminal")),
            PUSHOVER_TITLE_MAX_CHARS,
        )

    def test_message_bounds_and_no_secrets(self) -> None:
        payload = {
            "run_id": "run-abc",
            "event_kind": "terminal",
            "status": "failed",
            "phase": "executing",
            "progress": {
                "step": "executing",
                "detail": "safe detail",
                "stdout": "LEAK_STDOUT",
                "stderr": "LEAK_STDERR",
            },
            "instructions": "do not include",
            "webhook_url": "https://evil.example/hook",
        }
        body = format_pushover_message(payload)
        self.assertIn("run=run-abc", body)
        self.assertIn("status=failed", body)
        self.assertIn("phase=executing", body)
        self.assertIn("detail=safe detail", body)
        self.assertNotIn("LEAK_STDOUT", body)
        self.assertNotIn("LEAK_STDERR", body)
        self.assertNotIn("do not include", body)
        self.assertNotIn("evil.example", body)
        self.assertLessEqual(len(body), PUSHOVER_MESSAGE_MAX_CHARS)

    def test_form_includes_options_without_emergency(self) -> None:
        form = build_pushover_form(
            user_key=_TEST_USER,
            app_token=_TEST_TOKEN,
            title="Mission Control · terminal",
            message="run=x",
            priority=1,
            device="pixel",
            sound="pushover",
        )
        self.assertEqual(form["priority"], "1")
        self.assertEqual(form["device"], "pixel")
        self.assertEqual(form["sound"], "pushover")
        with self.assertRaises(ValueError):
            build_pushover_form(
                user_key=_TEST_USER,
                app_token=_TEST_TOKEN,
                title="t",
                message="m",
                priority=2,
            )


class TestPushoverDelivery(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)

    def tearDown(self) -> None:
        self.registry.close()
        os.unlink(self._db_path)

    def _outbox(self, handler, **cfg_kwargs) -> NotificationOutbox:
        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, follow_redirects=False)
        return NotificationOutbox(
            self._db_path,
            config=_pushover_config(**cfg_kwargs),
            http_client=client,
        )

    def test_successful_delivery(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"status": 1, "request": "abc"})

        outbox = self._outbox(handler)
        try:
            record = _record(self.registry)
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=PUSHOVER_API_URL,
            ):
                outbox.enqueue_for_record(
                    record,
                    event_kind=NotificationEventKind.TERMINAL,
                    dedupe_key="terminal:ok",
                )
                outbox.process_due_deliveries()
            events = outbox.list_for_run(record.run_id)
            self.assertEqual(events[0]["delivery_state"], "delivered")
            self.assertEqual(len(seen), 1)
            self.assertEqual(str(seen[0].url), PUSHOVER_API_URL)
            body = seen[0].content.decode("utf-8")
            self.assertIn("title=", body)
            self.assertIn("Mission", body)
            self.assertIn("terminal", body)
            self.assertIn(f"sound={DEFAULT_PUSHOVER_SOUND}", body)
            self.assertNotIn(_TEST_USER, json.dumps(events))
            self.assertNotIn(_TEST_TOKEN, json.dumps(events))
        finally:
            outbox.close()

    def test_invalid_credentials_permanent(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "status": 0,
                    "errors": ["application token is invalid"],
                    "token": "invalid",
                },
            )

        outbox = self._outbox(handler, max_attempts=5)
        try:
            record = _record(self.registry)
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=PUSHOVER_API_URL,
            ):
                outbox.enqueue_for_record(
                    record,
                    event_kind=NotificationEventKind.STALE,
                    dedupe_key="stale:1",
                )
                outbox.process_due_deliveries()
            events = outbox.list_for_run(record.run_id)
            self.assertEqual(events[0]["delivery_state"], "dead")
            err = events[0]["last_error"] or ""
            self.assertNotIn(_TEST_TOKEN, err)
            self.assertNotIn(_TEST_USER, err)
            self.assertIn("pushover_invalid", err)
        finally:
            outbox.close()

    def test_http_200_status_zero_permanent_dead_once(self) -> None:
        """HTTP 2xx + JSON status 0 must dead immediately (no retries)."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(
                200,
                json={
                    "status": 0,
                    "errors": ["application token is invalid"],
                    "token": _TEST_TOKEN,
                    "user": _TEST_USER,
                },
            )

        outbox = self._outbox(handler, max_attempts=5)
        try:
            record = _record(self.registry)
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=PUSHOVER_API_URL,
            ):
                outbox.enqueue_for_record(
                    record,
                    event_kind=NotificationEventKind.TERMINAL,
                    dedupe_key="terminal:status0",
                )
                outbox.process_due_deliveries()
                # A second drain must not re-attempt a permanent dead row.
                with outbox._lock:
                    outbox._conn.execute(
                        "UPDATE notification_outbox SET next_attempt_at = NULL"
                    )
                    outbox._conn.commit()
                outbox.process_due_deliveries()
            events = outbox.list_for_run(record.run_id)
            self.assertEqual(events[0]["delivery_state"], "dead")
            self.assertEqual(calls["n"], 1)
            self.assertEqual(events[0]["attempt_count"], 5)
            err = events[0]["last_error"] or ""
            self.assertEqual(err, "pushover_rejected")
            self.assertNotIn(_TEST_TOKEN, err)
            self.assertNotIn(_TEST_USER, err)
            self.assertNotIn("application token", err)
            self.assertNotIn("token", err.lower())
            dump = json.dumps(events)
            self.assertNotIn(_TEST_TOKEN, dump)
            self.assertNotIn(_TEST_USER, dump)
        finally:
            outbox.close()

    def test_status_one_only_success_shapes(self) -> None:
        """Only JSON integer status 1 counts as success; other shapes fail closed."""
        self.assertTrue(
            is_pushover_success_response(
                httpx.Response(200, json={"status": 1, "request": "x"})
            )
        )
        non_success_bodies = [
            {"status": True},
            {"status": "1"},
            {},  # missing status
            {"status": [1]},
            {"status": 1.0},
            {"status": 0},
            {"status": 2},
        ]
        for body in non_success_bodies:
            with self.subTest(body=body):
                response = httpx.Response(200, json=body)
                self.assertFalse(is_pushover_success_response(response))

        malformed = httpx.Response(200, content=b"not-json")
        self.assertFalse(is_pushover_success_response(malformed))
        empty = httpx.Response(200, content=b"")
        self.assertFalse(is_pushover_success_response(empty))
        listed = httpx.Response(200, json=[1])
        self.assertFalse(is_pushover_success_response(listed))

        # Clear integer rejection on 2xx → permanent; never echo provider text.
        rejected = httpx.Response(200, json={"status": 0, "errors": ["no"]})
        code, retryable = classify_pushover_response(rejected)
        self.assertFalse(retryable)
        self.assertEqual(code, "pushover_rejected")

        # Ambiguous 2xx bodies → retryable, never delivered.
        for response in (malformed, empty, listed, httpx.Response(200, json={})):
            with self.subTest(kind=response.content[:20]):
                code, retryable = classify_pushover_response(response)
                self.assertTrue(retryable)
                self.assertEqual(code, "pushover_malformed_response")
                self.assertFalse(is_pushover_success_response(response))

    def test_adversarial_provider_error_echo_never_leaks(self) -> None:
        """Provider errors[] / fields must never echo opaque credentials."""
        from urllib.parse import quote

        from mcp_connector.client import project_notification_inspection

        opaque_user = "uK9mX2pL7qR4vN8wY3zA1bC5dE6fG0hJ"
        opaque_token = "aT8nY3qM6sP1wK5xZ2cB9dF4eH7gJ0iL"
        user_enc = quote(opaque_user, safe="")
        token_enc = quote(opaque_token, safe="")
        form_user = quote(f"user={opaque_user}", safe="")
        form_token = quote(f"token={opaque_token}", safe="")
        allowlisted = frozenset(
            {
                "pushover_rejected",
                "pushover_malformed_response",
                "pushover_invalid_credentials_or_request",
                "pushover_rate_limited",
                "delivery_error",
                "invalid_pushover_config",
                "delivery_failed",
            }
        )
        leak_needles = (
            opaque_user,
            opaque_token,
            f"user={opaque_user}",
            f"token={opaque_token}",
            f"User={opaque_user}",
            f"TOKEN={opaque_token}",
            user_enc,
            token_enc,
            form_user,
            form_token,
            f'{{"user":"{opaque_user}"}}',
            f'{{"token":"{opaque_token}"}}',
        )
        adversarial_errors = [
            opaque_user,
            opaque_token,
            f"{opaque_user} {opaque_token}",
            f"user={opaque_user}",
            f"token={opaque_token}",
            f"user={opaque_user} token={opaque_token}",
            f"User={opaque_user} TOKEN={opaque_token}",
            f"rejected for {opaque_user} / {opaque_token}",
            f"attacker prefix user={opaque_user} mid token={opaque_token} suffix",
            user_enc,
            token_enc,
            form_user,
            form_token,
            f"user%3D{user_enc}&token%3D{token_enc}",
            json.dumps({"user": opaque_user, "token": opaque_token}),
            f'{{"user":"{opaque_user}","token":"{opaque_token}"}}',
            f"UsEr KeY {opaque_user} ApP ToKeN {opaque_token}",
        ]

        def _assert_no_leak(blob: str, *, context: str) -> None:
            lowered = blob.lower()
            for needle in leak_needles:
                self.assertNotIn(
                    needle,
                    blob,
                    msg=f"credential echo in {context}: {needle!r}",
                )
                self.assertNotIn(
                    needle.lower(),
                    lowered,
                    msg=f"credential echo (casefold) in {context}: {needle!r}",
                )

        def _assert_allowlisted_error(err: str | None) -> None:
            self.assertIsNotNone(err)
            assert err is not None
            self.assertIn(err, allowlisted)
            self.assertFalse(err.startswith("pushover_rejected:"))
            _assert_no_leak(err, context="last_error")

        for idx, err_text in enumerate(adversarial_errors):
            with self.subTest(error=err_text[:48]):
                calls = {"n": 0}

                def handler(
                    request: httpx.Request,
                    *,
                    _err: str = err_text,
                ) -> httpx.Response:
                    calls["n"] += 1
                    return httpx.Response(
                        200,
                        json={
                            "status": 0,
                            "request": f"req-{opaque_user}-{opaque_token}",
                            "errors": [_err],
                            "user": opaque_user,
                            "token": opaque_token,
                            "User": opaque_user,
                            "TOKEN": opaque_token,
                        },
                    )

                outbox = self._outbox(
                    handler,
                    max_attempts=5,
                    user_key=opaque_user,
                    app_token=opaque_token,
                )
                try:
                    record = _record(self.registry)
                    with patch(
                        "mission_control.notifications.validate_webhook_url",
                        return_value=PUSHOVER_API_URL,
                    ):
                        with self.assertLogs(
                            "mission_control.notifications",
                            level="WARNING",
                        ) as captured:
                            outbox.enqueue_for_record(
                                record,
                                event_kind=NotificationEventKind.TERMINAL,
                                dedupe_key=f"terminal:echo:{idx}",
                            )
                            outbox.process_due_deliveries()

                    self.assertEqual(calls["n"], 1)
                    events = outbox.list_for_run(record.run_id)
                    self.assertEqual(events[0]["delivery_state"], "dead")
                    _assert_allowlisted_error(events[0]["last_error"])
                    self.assertEqual(events[0]["last_error"], "pushover_rejected")

                    # Raw SQLite last_error + payload must stay credential-free.
                    with outbox._lock:
                        row = outbox._conn.execute(
                            "SELECT last_error, payload_json FROM "
                            "notification_outbox WHERE run_id = ?",
                            (record.run_id,),
                        ).fetchone()
                    self.assertIsNotNone(row)
                    assert row is not None
                    _assert_allowlisted_error(row["last_error"])
                    payload = json.loads(row["payload_json"])
                    payload_blob = json.dumps(payload)
                    _assert_no_leak(payload_blob, context="stored payload")
                    for key in ("user", "token", "user_key", "app_token", "secret"):
                        self.assertNotIn(key, payload)

                    log_blob = "\n".join(
                        f"{r.getMessage()} {r.exc_text or ''}"
                        for r in captured.records
                    )
                    _assert_no_leak(log_blob, context="logger output")
                    self.assertIn("pushover_rejected", log_blob)

                    # REST inspection projection.
                    api_registry = self.registry
                    prev_reg = api_module.run_registry
                    prev_out = api_module.notification_outbox
                    api_module.run_registry = api_registry
                    api_module.notification_outbox = outbox
                    try:
                        client = TestClient(app)
                        resp = client.get(
                            f"/runs/{record.run_id}/notifications",
                            headers=AUTH_HEADERS,
                        )
                    finally:
                        api_module.run_registry = prev_reg
                        api_module.notification_outbox = prev_out
                    self.assertEqual(resp.status_code, 200)
                    rest_body = resp.json()
                    rest_blob = json.dumps(rest_body)
                    _assert_no_leak(rest_blob, context="REST notifications")
                    self.assertEqual(
                        rest_body["events"][0]["last_error"],
                        "pushover_rejected",
                    )

                    # MCP / Unified projection surface.
                    projected = project_notification_inspection(
                        rest_body, limit=8
                    )
                    proj_blob = json.dumps(projected)
                    _assert_no_leak(proj_blob, context="MCP/Unified projection")
                    self.assertEqual(
                        projected["events"][0]["last_error"],
                        "pushover_rejected",
                    )

                    # Classifier direct path.
                    code, retryable = classify_pushover_response(
                        httpx.Response(
                            200,
                            json={
                                "status": 0,
                                "errors": [err_text],
                                "user": opaque_user,
                                "token": opaque_token,
                            },
                        )
                    )
                    self.assertFalse(retryable)
                    self.assertEqual(code, "pushover_rejected")
                    _assert_no_leak(code, context="classifier code")
                    _assert_no_leak(repr(code), context="classifier repr")
                finally:
                    outbox.close()

        # String-shaped errors[] also discarded.
        code, retryable = classify_pushover_response(
            httpx.Response(
                200,
                json={
                    "status": 0,
                    "errors": f"user={opaque_user} token={opaque_token}",
                },
            )
        )
        self.assertFalse(retryable)
        self.assertEqual(code, "pushover_rejected")

    def test_timeout_and_5xx_retry(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("slow")
            if calls["n"] == 2:
                return httpx.Response(503, json={"status": 0})
            return httpx.Response(200, json={"status": 1})

        outbox = self._outbox(handler, max_attempts=5)
        try:
            record = _record(self.registry)
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=PUSHOVER_API_URL,
            ):
                outbox.enqueue_for_record(
                    record,
                    event_kind=NotificationEventKind.TERMINAL,
                    dedupe_key="terminal:retry",
                )
                outbox.process_due_deliveries()
                with outbox._lock:
                    outbox._conn.execute(
                        "UPDATE notification_outbox SET next_attempt_at = NULL"
                    )
                    outbox._conn.commit()
                outbox.process_due_deliveries()
                with outbox._lock:
                    outbox._conn.execute(
                        "UPDATE notification_outbox SET next_attempt_at = NULL"
                    )
                    outbox._conn.commit()
                outbox.process_due_deliveries()
            events = outbox.list_for_run(record.run_id)
            self.assertEqual(events[0]["delivery_state"], "delivered")
            self.assertEqual(events[0]["event_kind"], "terminal")
            self.assertEqual(calls["n"], 3)
        finally:
            outbox.close()

    def test_4xx_policy(self) -> None:
        code, retryable = classify_pushover_http_failure(400)
        self.assertFalse(retryable)
        self.assertIn("invalid", code)
        code, retryable = classify_pushover_http_failure(429)
        self.assertTrue(retryable)
        code, retryable = classify_pushover_http_failure(500)
        self.assertTrue(retryable)

        response = httpx.Response(200, json={"status": 0})
        self.assertFalse(is_pushover_success_response(response))
        code, retryable = classify_pushover_response(response)
        self.assertFalse(retryable)
        self.assertEqual(code, "pushover_rejected")
        response = httpx.Response(200, json={"status": 1})
        self.assertTrue(is_pushover_success_response(response))
        code, retryable = classify_pushover_response(
            httpx.Response(400, json={"status": 0})
        )
        self.assertFalse(retryable)
        code, retryable = classify_pushover_response(httpx.Response(429))
        self.assertTrue(retryable)
        code, retryable = classify_pushover_response(httpx.Response(503))
        self.assertTrue(retryable)

    def test_redaction_of_credentials(self) -> None:
        self.assertEqual(
            redact_notification_error(f"bad token {_TEST_TOKEN}"),
            "[redacted]",
        )
        self.assertEqual(
            redact_notification_error(f"user_key={_TEST_USER}"),
            "[redacted]",
        )
        self.assertEqual(
            redact_notification_error(PUSHOVER_APP_TOKEN_ENV + "=x"),
            "[redacted]",
        )

    def test_both_backends_do_not_duplicate_to_pushover(self) -> None:
        seen_hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_hosts.append(request.url.host or "")
            return httpx.Response(200, json={"status": 1, "ok": True})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, follow_redirects=False)
        outbox = NotificationOutbox(
            self._db_path,
            config=_pushover_config(
                webhook_url=_PUBLIC_WEBHOOK,
                webhook_secret="webhook-secret-value",
            ),
            http_client=client,
        )
        try:
            record = _record(self.registry)
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=_PUBLIC_WEBHOOK,
            ):
                outbox.enqueue_for_record(
                    record,
                    event_kind=NotificationEventKind.TERMINAL,
                    dedupe_key="terminal:dual",
                )
                outbox.process_due_deliveries()
            self.assertEqual(seen_hosts, ["example.com"])
            self.assertNotIn("api.pushover.net", seen_hosts)
        finally:
            outbox.close()

    def test_no_config_leaves_pending_without_http(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"status": 1})

        outbox = self._outbox(handler, enabled=False, user_key=None, app_token=None)
        try:
            record = _record(self.registry)
            outbox.enqueue_for_record(
                record,
                event_kind=NotificationEventKind.RECOVERY,
                dedupe_key="recovery:1",
            )
            attempted = outbox.process_due_deliveries()
            self.assertEqual(attempted, 1)
            events = outbox.list_for_run(record.run_id)
            self.assertEqual(events[0]["delivery_state"], "pending")
            self.assertEqual(calls["n"], 0)
        finally:
            outbox.close()

    def test_worker_restart_preserves_pending(self) -> None:
        outbox = NotificationOutbox(
            self._db_path,
            config=_pushover_config(enabled=False),
        )
        try:
            record = _record(self.registry)
            result = outbox.enqueue_for_record(
                record,
                event_kind=NotificationEventKind.TERMINAL,
                dedupe_key="terminal:restart",
            )
            self.assertTrue(result.created)
            event_id = result.event_id
        finally:
            outbox.close()

        outbox2 = NotificationOutbox(
            self._db_path,
            config=_pushover_config(enabled=False),
        )
        try:
            row = outbox2.get_event(event_id or "")
            assert row is not None
            self.assertEqual(row["delivery_state"], DeliveryState.PENDING.value)
        finally:
            outbox2.close()


class TestPushoverAlertTuning(unittest.TestCase):
    """One phone alert per normal mission; phase_change suppressed on Pushover."""

    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)

    def tearDown(self) -> None:
        self.registry.close()
        os.unlink(self._db_path)

    def _outbox(self, handler, **cfg_kwargs) -> NotificationOutbox:
        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, follow_redirects=False)
        return NotificationOutbox(
            self._db_path,
            config=_pushover_config(**cfg_kwargs),
            http_client=client,
        )

    def _drain_with_url_patch(self, outbox: NotificationOutbox) -> None:
        with patch(
            "mission_control.notifications.validate_webhook_url",
            return_value=PUSHOVER_API_URL,
        ):
            outbox.process_due_deliveries()

    def test_should_deliver_pushover_policy(self) -> None:
        self.assertTrue(should_deliver_pushover_event("terminal"))
        self.assertTrue(should_deliver_pushover_event("stale"))
        self.assertTrue(should_deliver_pushover_event("recovery"))
        self.assertFalse(should_deliver_pushover_event("phase_change"))
        self.assertFalse(
            should_deliver_pushover_event(NotificationEventKind.PHASE_CHANGE)
        )
        self.assertEqual(resolve_pushover_sound(None), DEFAULT_PUSHOVER_SOUND)
        self.assertEqual(resolve_pushover_sound("pushover"), "pushover")
        self.assertEqual(resolve_pushover_sound("cosmic"), "cosmic")
        self.assertEqual(resolve_pushover_sound("bad\nsound"), DEFAULT_PUSHOVER_SOUND)
        form = build_pushover_form(
            user_key=_TEST_USER,
            app_token=_TEST_TOKEN,
            title="t",
            message="m",
            priority=0,
            sound=None,
        )
        self.assertEqual(form["sound"], DEFAULT_PUSHOVER_SOUND)

    def test_default_sound_pushover_and_safe_override(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.content.decode("utf-8"))
            return httpx.Response(200, json={"status": 1})

        outbox = self._outbox(handler)
        try:
            record = _record(self.registry)
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=PUSHOVER_API_URL,
            ):
                outbox.enqueue_for_record(
                    record,
                    event_kind=NotificationEventKind.TERMINAL,
                    dedupe_key="terminal:default-sound",
                )
                outbox.process_due_deliveries()
            self.assertEqual(len(seen), 1)
            self.assertIn(f"sound={DEFAULT_PUSHOVER_SOUND}", seen[0])
        finally:
            outbox.close()

        seen.clear()
        outbox2 = self._outbox(handler, sound="cosmic")
        try:
            record = _record(self.registry)
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=PUSHOVER_API_URL,
            ):
                outbox2.enqueue_for_record(
                    record,
                    event_kind=NotificationEventKind.TERMINAL,
                    dedupe_key="terminal:override-sound",
                )
                outbox2.process_due_deliveries()
            self.assertEqual(len(seen), 1)
            self.assertIn("sound=cosmic", seen[0])
            self.assertNotIn(f"sound={DEFAULT_PUSHOVER_SOUND}", seen[0])
        finally:
            outbox2.close()

        cfg = load_notification_config(
            environ={
                PUSHOVER_USER_KEY_ENV: _TEST_USER,
                PUSHOVER_APP_TOKEN_ENV: _TEST_TOKEN,
                PUSHOVER_SOUND_ENV: "magic",
            }
        )
        self.assertEqual(cfg.pushover_sound, "magic")
        self.assertEqual(resolve_pushover_sound(cfg.pushover_sound), "magic")

    def test_terminal_completed_failed_cancelled_one_request_each(self) -> None:
        cases = (
            ("completed", RunStatus.COMPLETED),
            ("failed", RunStatus.FAILED),
            ("cancelled", None),
        )
        for label, status in cases:
            with self.subTest(status=label):
                calls = {"n": 0}

                def handler(request: httpx.Request) -> httpx.Response:
                    calls["n"] += 1
                    return httpx.Response(200, json={"status": 1})

                outbox = self._outbox(handler)
                try:
                    record = _record(self.registry)
                    with patch(
                        "mission_control.notifications.validate_webhook_url",
                        return_value=PUSHOVER_API_URL,
                    ):
                        if status is not None:
                            # Registry durable enqueue + our Pushover worker.
                            self.registry.update_status(record.run_id, status)
                        else:
                            outbox.enqueue(
                                run_id=record.run_id,
                                event_kind=NotificationEventKind.TERMINAL,
                                dedupe_key="terminal:cancelled",
                                payload={
                                    "run_id": record.run_id,
                                    "event_kind": "terminal",
                                    "status": "cancelled",
                                    "phase": "failed",
                                },
                            )
                        outbox.process_due_deliveries(limit=32)
                        outbox.process_due_deliveries(limit=32)
                    self.assertEqual(calls["n"], 1, label)
                    events = outbox.list_for_run(record.run_id)
                    terminal = [
                        e for e in events if e["event_kind"] == "terminal"
                    ]
                    self.assertEqual(len(terminal), 1)
                    self.assertEqual(terminal[0]["delivery_state"], "delivered")
                    for event in events:
                        if event["event_kind"] == "phase_change":
                            self.assertEqual(event["delivery_state"], "skipped")
                finally:
                    outbox.close()

    def test_routine_phase_changes_produce_zero_pushover_requests(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"status": 1})

        phases = (
            RunPhase.QUEUED,
            RunPhase.WORKSPACE_PREPARATION,  # preparing
            RunPhase.AGENT_EXECUTION,
            RunPhase.PERSISTENCE,
        )
        outbox = self._outbox(handler)
        try:
            record = _record(self.registry)
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=PUSHOVER_API_URL,
            ):
                for phase in phases:
                    self.registry.set_phase(record.run_id, phase)
                    record = self.registry.get_run(record.run_id)
                    assert record is not None
                    # Force a distinct previous phase so enqueue always fires.
                    outbox.maybe_enqueue_phase_change(
                        record, previous_phase="__force__"
                    )
                outbox.process_due_deliveries(limit=32)
                outbox.process_due_deliveries(limit=32)
            self.assertEqual(calls["n"], 0)
            events = outbox.list_for_run(record.run_id)
            phase_events = [
                e for e in events if e["event_kind"] == "phase_change"
            ]
            self.assertEqual(len(phase_events), len(phases))
            for event in phase_events:
                self.assertEqual(event["delivery_state"], "skipped")
                self.assertEqual(
                    event["last_error"], PUSHOVER_PHASE_CHANGE_SUPPRESSED
                )
                self.assertNotEqual(event["delivery_state"], "dead")
                self.assertIsNone(event.get("delivered_at"))
            dump = json.dumps(events)
            self.assertNotIn(_TEST_USER, dump)
            self.assertNotIn(_TEST_TOKEN, dump)
            self.assertNotIn(PUSHOVER_USER_KEY_ENV, dump)
            self.assertNotIn(PUSHOVER_APP_TOKEN_ENV, dump)
        finally:
            outbox.close()

    def test_stale_recovery_terminal_three_logical_deliveries(self) -> None:
        kinds_seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            # event_kind is not in form; count requests only.
            kinds_seen.append("http")
            return httpx.Response(200, json={"status": 1})

        outbox = self._outbox(handler)
        try:
            record = _record(self.registry)
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=PUSHOVER_API_URL,
            ):
                outbox.enqueue_for_record(
                    record,
                    event_kind=NotificationEventKind.STALE,
                    dedupe_key="stale:seq",
                )
                outbox.enqueue_for_record(
                    record,
                    event_kind=NotificationEventKind.RECOVERY,
                    dedupe_key="recovery:seq",
                )
                self.registry.update_status(record.run_id, RunStatus.COMPLETED)
                record = self.registry.get_run(record.run_id)
                assert record is not None
                outbox.maybe_enqueue_terminal(record)
                outbox.process_due_deliveries(limit=16)
            self.assertEqual(len(kinds_seen), 3)
            events = outbox.list_for_run(record.run_id)
            by_kind = {e["event_kind"]: e for e in events}
            self.assertEqual(by_kind["stale"]["delivery_state"], "delivered")
            self.assertEqual(by_kind["recovery"]["delivery_state"], "delivered")
            self.assertEqual(by_kind["terminal"]["delivery_state"], "delivered")
        finally:
            outbox.close()

    def test_restart_drain_no_suppressed_retry_or_terminal_duplicate(
        self,
    ) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"status": 1})

        outbox = self._outbox(handler)
        try:
            record = _record(self.registry)
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=PUSHOVER_API_URL,
            ):
                phase = outbox.enqueue_for_record(
                    record,
                    event_kind=NotificationEventKind.PHASE_CHANGE,
                    dedupe_key="phase:restart",
                )
                first = outbox.enqueue_for_record(
                    record,
                    event_kind=NotificationEventKind.TERMINAL,
                    dedupe_key="terminal:restart-idem",
                )
                self.assertTrue(phase.created)
                self.assertTrue(first.created)
                outbox.process_due_deliveries()
                self.assertEqual(calls["n"], 1)
                # Simulate worker restart / repeated drains.
                with outbox._lock:
                    outbox._conn.execute(
                        "UPDATE notification_outbox SET next_attempt_at = NULL"
                    )
                    outbox._conn.commit()
                outbox.process_due_deliveries()
                outbox.reclaim_stale_claims()
                outbox.process_due_deliveries()
                # Idempotent re-enqueue must not create a second terminal row.
                second = outbox.enqueue_for_record(
                    record,
                    event_kind=NotificationEventKind.TERMINAL,
                    dedupe_key="terminal:restart-idem",
                )
                self.assertFalse(second.created)
                self.assertEqual(second.skipped_reason, "duplicate")
                outbox.process_due_deliveries()
            self.assertEqual(calls["n"], 1)
            events = outbox.list_for_run(record.run_id)
            phase_events = [
                e for e in events if e["event_kind"] == "phase_change"
            ]
            terminal = [e for e in events if e["event_kind"] == "terminal"]
            self.assertEqual(len(phase_events), 1)
            self.assertEqual(phase_events[0]["delivery_state"], "skipped")
            self.assertEqual(
                phase_events[0]["last_error"], PUSHOVER_PHASE_CHANGE_SUPPRESSED
            )
            self.assertEqual(len(terminal), 1)
            self.assertEqual(terminal[0]["delivery_state"], "delivered")
        finally:
            outbox.close()

        # Fresh outbox handle (restart) must not re-deliver skipped/delivered.
        outbox2 = self._outbox(handler)
        try:
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=PUSHOVER_API_URL,
            ):
                outbox2.reclaim_stale_claims()
                outbox2.process_due_deliveries()
            self.assertEqual(calls["n"], 1)
        finally:
            outbox2.close()

    def test_webhook_phase_change_delivery_unchanged(self) -> None:
        seen_kinds: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_kinds.append(request.headers.get("X-Mission-Control-Event-Kind", ""))
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, follow_redirects=False)
        outbox = NotificationOutbox(
            self._db_path,
            config=_pushover_config(
                user_key=None,
                app_token=None,
                webhook_url=_PUBLIC_WEBHOOK,
                webhook_secret="webhook-secret-value",
            ),
            http_client=client,
        )
        try:
            record = _record(self.registry)
            self.registry.set_phase(record.run_id, RunPhase.AGENT_EXECUTION)
            record = self.registry.get_run(record.run_id)
            assert record is not None
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=_PUBLIC_WEBHOOK,
            ):
                outbox.maybe_enqueue_phase_change(
                    record, previous_phase="queued"
                )
                outbox.process_due_deliveries()
            self.assertEqual(seen_kinds, ["phase_change"])
            events = outbox.list_for_run(record.run_id)
            self.assertEqual(events[0]["delivery_state"], "delivered")
            self.assertEqual(events[0]["event_kind"], "phase_change")
        finally:
            outbox.close()

    def test_webhook_wins_dual_backend_unchanged(self) -> None:
        seen_hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_hosts.append(request.url.host or "")
            return httpx.Response(200, json={"status": 1, "ok": True})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, follow_redirects=False)
        outbox = NotificationOutbox(
            self._db_path,
            config=_pushover_config(
                webhook_url=_PUBLIC_WEBHOOK,
                webhook_secret="webhook-secret-value",
            ),
            http_client=client,
        )
        try:
            record = _record(self.registry)
            self.registry.set_phase(record.run_id, RunPhase.PERSISTENCE)
            record = self.registry.get_run(record.run_id)
            assert record is not None
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=_PUBLIC_WEBHOOK,
            ):
                outbox.maybe_enqueue_phase_change(
                    record, previous_phase="agent_execution"
                )
                outbox.enqueue_for_record(
                    record,
                    event_kind=NotificationEventKind.TERMINAL,
                    dedupe_key="terminal:dual-wins",
                )
                outbox.process_due_deliveries(limit=16)
            self.assertEqual(seen_hosts, ["example.com", "example.com"])
            self.assertNotIn("api.pushover.net", seen_hosts)
            events = outbox.list_for_run(record.run_id)
            for event in events:
                self.assertEqual(event["delivery_state"], "delivered")
                self.assertNotEqual(event["delivery_state"], "skipped")
        finally:
            outbox.close()

    def test_inspection_identifies_suppressed_without_secret_leak(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": 1})

        outbox = self._outbox(handler)
        try:
            record = _record(self.registry)
            with patch(
                "mission_control.notifications.validate_webhook_url",
                return_value=PUSHOVER_API_URL,
            ):
                outbox.enqueue_for_record(
                    record,
                    event_kind=NotificationEventKind.PHASE_CHANGE,
                    dedupe_key="phase:inspect",
                )
                outbox.process_due_deliveries()
            event = outbox.get_event(outbox.list_for_run(record.run_id)[0]["event_id"])
            assert event is not None
            self.assertEqual(event["delivery_state"], DeliveryState.SKIPPED.value)
            self.assertEqual(event["last_error"], PUSHOVER_PHASE_CHANGE_SUPPRESSED)
            self.assertIn("suppressed", (event["last_error"] or "").lower())
            blob = json.dumps(event)
            self.assertNotIn(_TEST_USER, blob)
            self.assertNotIn(_TEST_TOKEN, blob)
            self.assertNotIn(PUSHOVER_USER_KEY_ENV, blob)
            self.assertNotIn(PUSHOVER_APP_TOKEN_ENV, blob)
        finally:
            outbox.close()


class TestPushoverNonBlockingApi(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)
        self.outbox = self.registry._get_notification_outbox()
        self.outbox._config = _pushover_config(
            timeout_seconds=2.0,
            worker_poll_seconds=0.05,
        )
        self._prev_registry = api_module.run_registry
        self._prev_outbox = api_module.notification_outbox
        self._prev_worker = api_module.notification_delivery_worker
        api_module.run_registry = self.registry
        api_module.notification_outbox = self.outbox
        self.worker = NotificationDeliveryWorker(
            self.outbox, poll_seconds=0.05
        )
        api_module.notification_delivery_worker = self.worker
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.worker.stop(timeout=2.0)
        api_module.run_registry = self._prev_registry
        api_module.notification_outbox = self._prev_outbox
        api_module.notification_delivery_worker = self._prev_worker
        self.outbox.close()
        self.registry.close()
        os.unlink(self._db_path)

    def test_wait_status_not_blocked_by_slow_pushover(self) -> None:
        release = threading.Event()

        def handler(request: httpx.Request) -> httpx.Response:
            release.wait(timeout=5.0)
            return httpx.Response(200, json={"status": 1})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, follow_redirects=False)
        self.outbox._http_client = client
        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.COMPLETED)
        record = self.registry.get_run(record.run_id)
        assert record is not None
        with patch(
            "mission_control.notifications.validate_webhook_url",
            return_value=PUSHOVER_API_URL,
        ):
            self.outbox.enqueue_for_record(
                record,
                event_kind=NotificationEventKind.TERMINAL,
                dedupe_key="terminal:wait",
            )
            self.worker.start()
            started = time.monotonic()
            status = self.client.get(
                f"/runs/{record.run_id}",
                headers=AUTH_HEADERS,
            )
            wait = self.client.post(
                f"/runs/{record.run_id}/wait",
                headers=AUTH_HEADERS,
                json={"timeout_seconds": 0.2, "poll_interval_seconds": 0.05},
            )
            elapsed = time.monotonic() - started
        release.set()
        self.assertEqual(status.status_code, 200)
        self.assertEqual(wait.status_code, 200)
        self.assertLess(elapsed, 1.5)


if __name__ == "__main__":
    unittest.main()
