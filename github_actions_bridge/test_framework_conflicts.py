import unittest
from github_actions_bridge.framework_conflicts import framework_conflict_map

class FrameworkConflictMapTests(unittest.TestCase):
    def test_marks_candidate_tension_only_when_both_sides_have_citations(self):
        raw = b"\n".join([
            b'{"filename":"Austin.pdf","page_number":1,"text":"Engineer expert preliminary opinion applies a riparian rule methodology."}',
            b'{"filename":"Brief.pdf","page_number":2,"text":"Court case decision states riparian law."}',
            b'{"filename":"Survey.pdf","page_number":3,"text":"Survey drawing measurement depth maneuvering space."}',
            b'{"filename":"Affidavit.pdf","page_number":4,"text":"Plaintiff claims defendant obstructed boat vessel access corridor."}',
        ])
        result = framework_conflict_map(raw)
        by_name = {item["name"]: item for item in result["conflicts"]}
        self.assertFalse(result["model_called"])
        self.assertEqual(by_name["opinion_vs_authority"]["status"], "candidate_tension")
        self.assertEqual(by_name["space_proof_vs_access_claim"]["status"], "candidate_tension")
        self.assertEqual(by_name["regulatory_record_vs_claim"]["status"], "insufficient_record")
        self.assertIn("not a finding", result["analysis_guardrail"])
        self.assertIn("filename and page_number", by_name["opinion_vs_authority"]["citation_requirement"])

if __name__ == "__main__":
    unittest.main()
