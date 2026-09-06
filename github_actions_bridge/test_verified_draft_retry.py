import unittest
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


if __name__ == "__main__":
    unittest.main()
