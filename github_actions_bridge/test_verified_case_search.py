import unittest
from github_actions_bridge.verified_case_search import search_index_jsonl, search_page_records


class VerifiedCaseSearchTests(unittest.TestCase):
    def test_returns_ranked_exact_page_citations(self):
        records = [
            {"filename": "A.pdf", "page_number": 2, "text": "riparian rights and riparian access"},
            {"filename": "B.pdf", "page_number": 1, "text": "access only"},
        ]
        results = search_page_records(records, "riparian access")
        self.assertEqual([(item["filename"], item["page_number"]) for item in results], [("A.pdf", 2), ("B.pdf", 1)])

    def test_rejects_empty_query(self):
        with self.assertRaises(ValueError):
            search_page_records([], "!")

    def test_reads_jsonl_index(self):
        raw = b'{"filename":"A.pdf","page_number":3,"text":"riparian law"}\n'
        self.assertEqual(search_index_jsonl(raw, "riparian")[0]["page_number"], 3)
