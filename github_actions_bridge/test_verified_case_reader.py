import unittest
from github_actions_bridge.verified_case_reader import canonical_source_prefix, validate_page_request


class VerifiedCaseReaderTests(unittest.TestCase):
    def test_builds_only_canonical_source_prefix(self):
        self.assertEqual(canonical_source_prefix("NY-Nassau-613561-2026-Desousa-v-Rennick", "a" * 64), "cases/NY-Nassau-613561-2026-Desousa-v-Rennick/intake/source/" + "a" * 64 + "/")

    def test_limits_and_normalizes_page_requests(self):
        self.assertEqual(validate_page_request("Doc 2.pdf", [4, 2, 4]), ("Doc 2.pdf", [2, 4]))

    def test_refuses_paths_and_unbounded_pages(self):
        with self.assertRaises(ValueError): validate_page_request("../secret.pdf", [1])
        with self.assertRaises(ValueError): validate_page_request("Doc.pdf", list(range(1, 12)))
