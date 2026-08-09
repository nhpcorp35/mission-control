from __future__ import annotations

import json
import unittest

from github_actions_bridge.storage_policy import (
    CASE00_PREFIXES,
    build_attorney_review_archive,
    inventory_prefix,
)


class Case00StoragePolicyTests(unittest.TestCase):
    def test_inventory_prefix_is_allowlisted(self) -> None:
        self.assertEqual(
            inventory_prefix("attorney_reviews"), CASE00_PREFIXES["attorney_reviews"]
        )
        with self.assertRaises(ValueError):
            inventory_prefix("../../other-bucket")

    def test_archive_is_deterministic_and_confined(self) -> None:
        kwargs = {
            "evaluation_date": "2026-08-02",
            "original_packet_md": "# Packet",
            "feedback_email_md": "# Feedback",
            "structured_evaluation_json": json.dumps({"Q1": "incorrect"}),
            "archived_by": "nhpcorp35",
        }
        first_id, first_items = build_attorney_review_archive(**kwargs)
        second_id, second_items = build_attorney_review_archive(**kwargs)
        self.assertEqual(first_id, second_id)
        self.assertEqual(
            [item["object_key"] for item in first_items],
            [item["object_key"] for item in second_items],
        )
        self.assertEqual(len(first_items), 4)
        for item in first_items:
            self.assertTrue(
                item["object_key"].startswith(CASE00_PREFIXES["attorney_reviews"])
            )

    def test_archive_rejects_invalid_inputs(self) -> None:
        with self.assertRaises(ValueError):
            build_attorney_review_archive(
                evaluation_date="2026-02-30",
                original_packet_md="packet",
                feedback_email_md="feedback",
                structured_evaluation_json="{}",
                archived_by="nhpcorp35",
            )
        with self.assertRaises(ValueError):
            build_attorney_review_archive(
                evaluation_date="2026-08-02",
                original_packet_md="packet",
                feedback_email_md="feedback",
                structured_evaluation_json="[]",
                archived_by="nhpcorp35",
            )


if __name__ == "__main__":
    unittest.main()
