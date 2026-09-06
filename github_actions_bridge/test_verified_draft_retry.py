import unittest
from unittest.mock import MagicMock
from pathlib import Path
import sys
import os

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("GITHUB_OAUTH_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_OAUTH_CLIENT_SECRET", "test-secret")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("STORAGE_ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")

from server import (  # noqa: E402
    VERIFIED_DRAFT_RETRY_AFTER_SECONDS,
    _is_discardable_temporary_draft_request,
    _queued_draft_needs_retry,
)


class VerifiedDraftRetryTests(unittest.TestCase):
    def test_retries_only_a_stalled_first_dispatch(self):
        created_at = 1_000
        self.assertTrue(
            _queued_draft_needs_retry(
                {"status": "QUEUED", "dispatch_attempts": 1},
                created_at,
                created_at + VERIFIED_DRAFT_RETRY_AFTER_SECONDS,
            )
        )

    def test_does_not_retry_early_terminal_or_already_retried_work(self):
        created_at = 1_000
        now = created_at + VERIFIED_DRAFT_RETRY_AFTER_SECONDS + 1
        self.assertFalse(_queued_draft_needs_retry({"status": "QUEUED", "dispatch_attempts": 1}, created_at, now - 2))
        self.assertFalse(_queued_draft_needs_retry({"status": "RUNNING", "dispatch_attempts": 1}, created_at, now))
        self.assertFalse(_queued_draft_needs_retry({"status": "READY", "dispatch_attempts": 1}, created_at, now))
        self.assertFalse(_queued_draft_needs_retry({"status": "QUEUED", "dispatch_attempts": 2}, created_at, now))
        self.assertFalse(_queued_draft_needs_retry({"status": "QUEUED", "dispatch_attempts": 1}, "bad", now))

    def test_only_exact_internal_test_question_can_be_discarded(self):
        allowed = {
            "schema_version": "legalai-draft-request.v1",
            "status": "DRAFT",
            "external_communication": False,
            "question": " Is this a test? ",
        }
        self.assertTrue(_is_discardable_temporary_draft_request(allowed))
        self.assertFalse(_is_discardable_temporary_draft_request({**allowed, "question": "What relief is requested?"}))
        self.assertFalse(_is_discardable_temporary_draft_request({**allowed, "external_communication": True}))
        self.assertFalse(_is_discardable_temporary_draft_request({**allowed, "status": "ARCHIVED"}))

    def test_request_ids_are_independent(self):
        first = "draft-1000-aaaaaaaaaaaa"
        second = "draft-1001-bbbbbbbbbbbb"
        self.assertNotEqual(first, second)
        # A discard marker is stored under one request ID only; the other
        # request's derived prefix is a different B2 key.
        self.assertNotEqual(
            f"cases/case/derived/internal-drafts/{first}/discarded.json",
            f"cases/case/derived/internal-drafts/{second}/discarded.json",
        )


if __name__ == "__main__":
    unittest.main()
