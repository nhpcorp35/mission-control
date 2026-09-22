import unittest
from github_actions_bridge.authority_verification import authority_verification_check


class AuthorityVerificationTests(unittest.TestCase):
    def test_party_citations_are_extracted_with_record_locations_but_not_treated_as_law(self):
        raw = (b'{"filename":"Plaintiff Memorandum.pdf","page_number":4,"text":"See 2024 NY Slip Op 06327 and N.Y. Navigation Law \\u00a7 15."}\n' b'{"filename":"Reply.pdf","page_number":9,"text":"The filing also invokes 2024 NY Slip Op 06327 and CPLR 3212."}\n')
        result = authority_verification_check(raw)
        self.assertFalse(result["model_called"])
        self.assertEqual(result["verified_primary_authorities"], [])
        candidates = {item["citation"]: item for item in result["candidates"]}
        self.assertEqual(set(candidates), {"2024 NY Slip Op 06327", "N.Y. Navigation Law § 15", "CPLR 3212"})
        slip = candidates["2024 NY Slip Op 06327"]
        self.assertEqual(slip["status"], "party_cited_unverified")
        self.assertEqual(slip["record_citations"], [{"filename": "Plaintiff Memorandum.pdf", "page_number": 4}, {"filename": "Reply.pdf", "page_number": 9}])
        self.assertIn("Do not treat", slip["verification_requirement"])

    def test_invalid_index_and_limit_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "verified page index"):
            authority_verification_check(b"not-json")
        with self.assertRaisesRegex(ValueError, "limit"):
            authority_verification_check(b"", limit=0)

    def test_curated_official_identity_is_not_promoted_to_a_verified_holding(self):
        raw = (b'{"filename":"Affirmation.pdf","page_number":7,"text":"See 193 A.D.3d 710."}\n')
        result = authority_verification_check(raw)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["status"], "official_primary_source_identified")
        self.assertEqual(candidate["primary_source"]["title"], "Kuzmicki v Bentley Yacht Club")
        self.assertIn("nycourts.gov", candidate["primary_source"]["source_url"])
        self.assertEqual(result["verified_primary_authorities"], [])
        self.assertIn("specific proposition remains unverified", candidate["verification_requirement"])

    def test_additional_official_identity_match_is_available_without_a_holding(self):
        raw = (b'{"filename":"Affirmation.pdf","page_number":7,"text":"See 162 A.D.3d 634."}\n')
        result = authority_verification_check(raw)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["primary_source"]["title"], "Ciringione v Ryan")
        self.assertEqual(candidate["primary_source"]["reporter_page"], 634)
        self.assertEqual(result["verified_primary_authorities"], [])


if __name__ == "__main__":
    unittest.main()
