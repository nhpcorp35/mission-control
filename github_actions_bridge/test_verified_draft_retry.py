import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
import sys
import os
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("GITHUB_OAUTH_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_OAUTH_CLIENT_SECRET", "test-secret")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("STORAGE_ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")

from server import (  # noqa: E402
    VERIFIED_DRAFT_RETRY_AFTER_SECONDS,
    _collapse_duplicate_draft_requests,
    _is_discardable_temporary_draft_request,
    _newest_draft_request_items,
    _queued_draft_needs_retry,
    _read_case_draft_request_snapshots,
    mcp_draft_status,
    mcp_job_error,
)


class VerifiedDraftRetryTests(unittest.TestCase):
    def test_draft_status_returns_only_safe_failure_details(self):
        status = {
            "status": "FAILED",
            "updated_at": "2026-09-23T16:50:20Z",
            "failure_code": "model_output_validation",
            "failure_stage": "model_validation",
            "exception_type": "valueerror",
            "validation_reason": "incomplete_output_missing_information_invalid_terminal",
            "private_detail": "must not be returned",
        }
        with patch("server._validate_draft_case_id", return_value="case"), \
             patch("server._validate_draft_request_id", return_value="draft-1-aaaaaaaaaaaa"), \
             patch("server._b2_client", return_value=object()), \
             patch("server._draft_request_entry", return_value={}), \
             patch("server._assert_owned_draft"), \
             patch("server._mcp_draft_reviewer", return_value="reviewer@example.com"), \
             patch("server._draft_status_entry", return_value=status):
            result = __import__("asyncio").run(
                mcp_draft_status.fn("case", "draft-1-aaaaaaaaaaaa")
            )
        self.assertEqual(result["failure"]["failure_code"], "model_output_validation")
        self.assertEqual(
            result["failure"]["validation_reason"],
            "incomplete_output_missing_information_invalid_terminal",
        )
        self.assertNotIn("private_detail", result["failure"])

    def test_job_error_returns_persisted_safe_gate_reason(self):
        status = {
            "status": "FAILED",
            "updated_at": "2026-09-18T17:29:19Z",
            "failure_code": "pre_generation_gate",
            "failure_stage": "evidence_retrieval",
            "gate_reason": "ambiguous_third_party_answer",
            "private_detail": "must not be returned",
        }
        with patch("server._validate_draft_case_id", return_value="case"), \
             patch("server._validate_draft_request_id", return_value="draft-1-aaaaaaaaaaaa"), \
             patch("server._b2_client", return_value=object()), \
             patch("server._draft_request_entry", return_value={}), \
             patch("server._assert_owned_draft"), \
             patch("server._mcp_draft_reviewer", return_value="reviewer@example.com"), \
             patch("server._draft_status_entry", return_value=status):
            result = __import__("asyncio").run(
                mcp_job_error("case", "draft-1-aaaaaaaaaaaa")
            )
        self.assertEqual(result["job"]["gate_reason"], "ambiguous_third_party_answer")
        self.assertNotIn("private_detail", result["job"])

    def test_job_error_returns_persisted_safe_validation_reason(self):
        status = {
            "status": "FAILED",
            "updated_at": "2026-09-18T20:41:52Z",
            "failure_code": "model_output_validation",
            "failure_stage": "model_validation",
            "validation_reason": "verified_pleading_called_missing",
            "private_detail": "must not be returned",
        }
        with patch("server._validate_draft_case_id", return_value="case"), \
             patch("server._validate_draft_request_id", return_value="draft-1-aaaaaaaaaaaa"), \
             patch("server._b2_client", return_value=object()), \
             patch("server._draft_request_entry", return_value={}), \
             patch("server._assert_owned_draft"), \
             patch("server._mcp_draft_reviewer", return_value="reviewer@example.com"), \
             patch("server._draft_status_entry", return_value=status):
            result = __import__("asyncio").run(
                mcp_job_error("case", "draft-1-aaaaaaaaaaaa")
            )
        self.assertEqual(
            result["job"]["validation_reason"],
            "verified_pleading_called_missing",
        )
        self.assertNotIn("private_detail", result["job"])

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

    def test_newest_request_is_not_omitted_after_the_first_hundred(self):
        listed = {
            "Contents": [
                {"Key": f"cases/case/derived/draft-requests/draft-{index:04d}-aaaaaaaaaaaa.json"}
                for index in range(101)
            ]
        }
        ordered = _newest_draft_request_items(listed)
        self.assertEqual(len(ordered), 101)
        self.assertTrue(ordered[0]["Key"].endswith("draft-0100-aaaaaaaaaaaa.json"))
        self.assertTrue(ordered[-1]["Key"].endswith("draft-0000-aaaaaaaaaaaa.json"))

    def test_request_snapshots_are_read_with_bounded_parallelism(self):
        lock = threading.Lock()
        active = 0
        peak = 0

        def read_snapshot(client, case_id, item):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return None

        items = [{"Key": f"draft-{index}.json"} for index in range(12)]
        with patch("server._read_case_draft_request_snapshot", side_effect=read_snapshot):
            snapshots = _read_case_draft_request_snapshots(object(), "case", items)

        self.assertEqual(snapshots, [None] * 12)
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 8)

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

    def test_duplicate_question_prefers_ready_draft_without_hiding_other_questions(self):
        requests = [
            {"request_id": "draft-1-aaaaaaaaaaaa", "question": "What relief is requested?", "requested_by": "allen@example.com", "status": "READY", "created_at": 10},
            {"request_id": "draft-2-bbbbbbbbbbbb", "question": " What  relief is requested? ", "requested_by": "Allen@example.com", "status": "QUEUED", "created_at": 20},
            {"request_id": "draft-3-cccccccccccc", "question": "What evidence supports it?", "requested_by": "allen@example.com", "status": "QUEUED", "created_at": 30},
            {"request_id": "draft-4-dddddddddddd", "question": "What relief is requested?", "requested_by": "other@example.com", "status": "QUEUED", "created_at": 40},
        ]
        visible = _collapse_duplicate_draft_requests(requests)
        self.assertEqual(
            [item["request_id"] for item in visible],
            ["draft-4-dddddddddddd", "draft-3-cccccccccccc", "draft-1-aaaaaaaaaaaa"],
        )


    def test_explicit_regeneration_is_visible_over_older_ready_duplicate(self):
        requests = [
            {"request_id": "draft-1-aaaaaaaaaaaa", "question": "What relief is requested?", "requested_by": "allen@example.com", "status": "READY", "created_at": 10},
            {"request_id": "draft-2-bbbbbbbbbbbb", "question": "What relief is requested?", "requested_by": "allen@example.com", "status": "QUEUED", "created_at": 20, "regenerated_from_request_id": "draft-1-aaaaaaaaaaaa"},
        ]
        visible = _collapse_duplicate_draft_requests(requests)
        self.assertEqual([item["request_id"] for item in visible], ["draft-2-bbbbbbbbbbbb"])


if __name__ == "__main__":
    unittest.main()
