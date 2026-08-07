"""Tests for deterministic candidate-vs-reference diagnostics."""

from __future__ import annotations

import unittest

from case00_attorney_eval.adapter import ProvisionalAnswerArtifact
from case00_attorney_eval.diagnostics import (
    compare_candidate_to_reference,
    extract_party_role_pairs,
)
from case00_attorney_eval.evaluate import evaluate_question
from pathlib import Path

from test_case00_attorney_eval import _bundle, _label


REF = (
    "Cedar Ridge Logistics LLC is the plaintiff in this insurance dispute. "
    "Meadow Bridge Repair Inc. is a defendant. "
    "The complaint alleges Meadow Bridge failed to defend under the policy. "
    "See NYSCEF Doc No. 101 at page 1."
)

COMPLETE = (
    "Cedar Ridge Logistics LLC is the plaintiff. "
    "Meadow Bridge Repair Inc. is a defendant. "
    "The complaint alleges Meadow Bridge failed to defend under the policy. "
    "See NYSCEF Doc No. 101 at page 1."
)

INCOMPLETE = (
    "Cedar Ridge Logistics LLC is the plaintiff. "
    "See NYSCEF Doc No. 101."
)

UNSUPPORTED = (
    "Cedar Ridge Logistics LLC is the plaintiff. "
    "Meadow Bridge Repair Inc. is a defendant. "
    "Oceanic Underwriters Ltd is also a defendant on the policy. "
    "The complaint alleges Meadow Bridge failed to defend under the policy. "
    "See NYSCEF Doc No. 101 at page 1."
)


class PartyRoleExtractionTests(unittest.TestCase):
    def test_extracts_pairs(self):
        pairs = extract_party_role_pairs(REF)
        roles = {p["party_normalized"]: p["role"] for p in pairs}
        self.assertEqual(roles.get("cedar ridge logistics"), "plaintiff")
        self.assertEqual(roles.get("meadow bridge repair"), "defendant")


class DiagnosticComparisonTests(unittest.TestCase):
    def test_correct_candidate(self):
        result = compare_candidate_to_reference(
            candidate_text=COMPLETE,
            reference_text=REF,
            reference_status="provisional",
            reference_usable=True,
            label_record=_label("Q1", verdict="incorrect", materiality="material"),
        )
        self.assertTrue(result["comparison_performed"])
        self.assertFalse(result["numeric_scores_fabricated"])
        self.assertEqual(result["llm_judge"], "disabled")
        self.assertFalse(result["provisional_treated_as_approved"])
        self.assertEqual(result["missing_material_facts"], [])
        self.assertEqual(result["party_role_mismatches"], [])
        self.assertTrue(result["attorney_label_material_errors"]["unresolved_material_errors"])

    def test_incomplete_candidate(self):
        result = compare_candidate_to_reference(
            candidate_text=INCOMPLETE,
            reference_text=REF,
            reference_status="provisional",
            reference_usable=True,
            label_record=_label("Q1"),
        )
        self.assertTrue(result["comparison_performed"])
        self.assertGreater(len(result["missing_material_facts"]), 0)
        self.assertTrue(
            any(m["type"] == "missing_party" for m in result["party_role_mismatches"])
        )

    def test_unsupported_extra_assertions(self):
        result = compare_candidate_to_reference(
            candidate_text=UNSUPPORTED,
            reference_text=REF,
            reference_status="provisional",
            reference_usable=True,
            label_record=_label("Q1", verdict="correct", materiality="none", categories=[]),
        )
        self.assertGreater(len(result["unsupported_or_extra_assertions"]), 0)
        self.assertTrue(
            any(m["type"] == "extra_party" for m in result["party_role_mismatches"])
        )
        self.assertFalse(
            result["attorney_label_material_errors"]["unresolved_material_errors"]
        )

    def test_provisional_placeholder_skips_comparison(self):
        result = compare_candidate_to_reference(
            candidate_text=COMPLETE,
            reference_text=None,
            reference_status="provisional_placeholder",
            reference_usable=False,
            label_record=_label("Q5"),
        )
        self.assertFalse(result["comparison_performed"])
        self.assertEqual(result["missing_material_facts"], [])
        self.assertIn("No usable reference", result["note"])

    def test_evaluate_question_embeds_diagnostics(self):
        from case00_attorney_eval.adapter import ProvisionalAnswerArtifact

        prov = ProvisionalAnswerArtifact(
            question_id="Q1",
            path=Path("Q1.md"),
            status="provisional_awaiting_john_approval",
            is_placeholder=False,
            body=REF,
            raw_markdown="x",
        )
        record = evaluate_question(
            _bundle(provisional=prov),
            review_status={},
            candidate_answer=INCOMPLETE,
        )
        diag = record["candidate_vs_reference_diagnostics"]
        self.assertTrue(diag["comparison_performed"])
        self.assertGreater(diag["counts"]["missing_material_facts"], 0)
        self.assertFalse(diag["provisional_treated_as_approved"])


if __name__ == "__main__":
    unittest.main()
