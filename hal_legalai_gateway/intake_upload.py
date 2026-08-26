"""Short-lived, bounded browser upload tickets for the Rennick intake pair."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Callable


TTL_SECONDS = 15 * 60


@dataclass
class IntakeUploadSession:
    expires_at: int
    source: bytes | None = None


class IntakeUploadSessions:
    """In-process, fail-closed upload sessions.

    A ticket is signed, short lived, and only permits the one fixed source / manifest
    pair. The source bytes are retained only until the manifest arrives or expiry.
    """

    def __init__(self, signing_key: str, *, now: Callable[[], float] = time.time) -> None:
        self._key = signing_key.encode("utf-8")
        self._now = now
        self._sessions: dict[str, IntakeUploadSession] = {}

    def issue(self) -> str:
        nonce = secrets.token_urlsafe(24)
        expires_at = int(self._now()) + TTL_SECONDS
        payload = json.dumps({"n": nonce, "e": expires_at}, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        signature = hmac.new(self._key, encoded.encode(), hashlib.sha256).hexdigest()
        self._sessions[nonce] = IntakeUploadSession(expires_at=expires_at)
        self._prune()
        return f"{encoded}.{signature}"

    def _nonce(self, ticket: str) -> str | None:
        try:
            encoded, signature = ticket.split(".", 1)
            expected = hmac.new(self._key, encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return None
            padded = encoded + "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
            nonce, expires_at = payload["n"], int(payload["e"])
        except (KeyError, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        session = self._sessions.get(nonce)
        if session is None or expires_at != session.expires_at or expires_at < int(self._now()):
            return None
        return nonce

    def store_source(self, ticket: str, source: bytes) -> bool:
        nonce = self._nonce(ticket)
        if nonce is None:
            return False
        self._sessions[nonce].source = source
        return True

    def take_source(self, ticket: str) -> bytes | None:
        nonce = self._nonce(ticket)
        if nonce is None:
            return None
        source = self._sessions[nonce].source
        del self._sessions[nonce]
        return source

    def _prune(self) -> None:
        now = int(self._now())
        for nonce, session in list(self._sessions.items()):
            if session.expires_at < now:
                del self._sessions[nonce]
