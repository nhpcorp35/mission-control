import unittest
from github_actions_bridge.framework_evidence import framework_evidence_check

class FrameworkEvidenceTests(unittest.TestCase):
    def test_separates_party_positions_opinion_regulatory_space_and_authority(self):
        raw = b"\n".join([
            b'{"filename":"Affidavit.pdf","page_number":1,"text":"Plaintiff alleges defendant obstructed riparian access; defendant denies the claim."}',
            b'{"filename":"Austin.pdf","page_number":1,"text":"Engineer Austin expert preliminary opinion applies a 1/4 riparian methodology."}',
            b'{"filename":"DEC.pdf","page_number":2,"text":"DEC permit approval inspection certificate occupancy record."}',
            b'{"filename":"Survey.pdf","page_number":3,"text":"Survey drawing site plan measurement depth maneuvering space."}',
            b'{"filename":"Brief.pdf","page_number":4,"text":"Court case decision N.Y. A.D. CPLR ECL riparian navigation."}',
        ])
        result = framework_evidence_check(raw)
        self.assertTrue(result["ok"]); self.assertFalse(result["model_called"])
        self.assertEqual(result["missing_categories"], [])
        self.assertEqual(result["categories"]["party_positions"]["record_role"], "party_position")
        self.assertEqual(result["categories"]["expert_opinion"]["record_role"], "opinion")
        self.assertIn("not governing law", result["categories"]["expert_opinion"]["rule"])
        self.assertIn("1/4", result["categories"]["expert_opinion"]["rule"])
        self.assertEqual(result["categories"]["regulatory_record"]["results"][0]["filename"], "DEC.pdf")
        self.assertEqual(result["categories"]["drawings_and_space"]["results"][0]["filename"], "Survey.pdf")
        self.assertEqual(result["categories"]["cited_authority"]["results"][0]["filename"], "Brief.pdf")

    def test_returns_attorney_reasoning_acceptance_contract(self):
        result = framework_evidence_check(b'{"filename":"Affidavit.pdf","page_number":1,"text":"Plaintiff alleges obstruction of access."}\n')
        checks = {item["id"]: item for item in result["reasoning_acceptance_checks"]}
        self.assertEqual(set(checks), {"separate_propositions", "competing_positions", "what_each_proves", "authority_application", "outcome_significance", "missing_information"})
        self.assertIn("each side", checks["competing_positions"]["requirement"])
        self.assertIn("what it does not establish", checks["what_each_proves"]["requirement"])
        self.assertIn("requested relief", checks["outcome_significance"]["requirement"])

    def test_reports_missing_categories_without_inference(self):
        result = framework_evidence_check(b'{"filename":"Austin.pdf","page_number":1,"text":"Engineer expert preliminary opinion riparian."}\n')
        self.assertFalse(result["model_called"])
        self.assertIn("regulatory_record", result["missing_categories"])
        self.assertIn("spatial feasibility", result["analysis_guardrails"][1])

if __name__ == "__main__":
    unittest.main()
