"""Focused tests for the Case-00 attorney cognition review packet."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from case00_attorney_eval.review_packet import (
    PACKET_FILENAME,
    render_attorney_review_packet,
    write_attorney_review_packet,
)


class AttorneyReviewPacketTests(unittest.TestCase):
    def _candidate(self) -> dict:
        return {
            "status": "candidate",
            "attorney_approved": False,
            "question_id": "Q1",
            "question_text": "Who are the parties?",
            "proposed_answer": "Alpha LLC is the plaintiff.",
            "generation_commit": "a" * 40,
            "generated_at": "2026-08-17T00:00:00+00:00",
            "model": "test-model",
            "provider": "injected",
            "confidence": 0.82,
            "propositions": [{
                "proposition_id": "P1",
                "text": "Alpha LLC is the plaintiff.",
                "classification": "record_fact",
                "confidence": 0.91,
                "rationale": "The filed caption expressly identifies the party.",
                "polarity": "supporting",
                "nyscef_document_number": 101,
                "pdf_page": 4,
                "source_excerpt": "Plaintiff Alpha LLC",
            }],
            "supporting_evidence": [{
                "description": "Caption identifies Alpha LLC.",
                "filing_number": 101,
                "exhibit": "A",
                "page_id": "nyscef-101-page-0004",
                "source_path": "/case/filings/complaint.pdf",
            }],
            "contrary_evidence": [{
                "text": "A later filing disputes the role.",
                "document_number": 202,
                "page_number": 7,
            }],
            "unresolved_questions": [
                "Whether the later filing changes the procedural posture."
            ],
            "documents_pages_reviewed": [{
                "source_filename": "complaint.pdf",
                "pdf_page_number": 9,
                "source_path": "/case/filings/complaint.pdf",
            }],
            "review_scope": "Confirm the pleaded role against the complete docket.",
            "attorney_review": {
                "requires_attorney_review": True,
                "review_notes": "Verify the party roles against the complete docket.",
                "legal_conclusions_labeled": True,
                "coverage_conclusion": "Retrieved evidence is not the complete record.",
            },
        }

    def _evaluation(self) -> dict:
        return {
            "corpus_id": "case-00",
            "benchmark_id": "benchmark-01",
            "packet_id": "packet-01",
            "questions": [{
                "question_id": "Q1",
                "question_text": "Who are the parties?",
                "reference_answer_status": {"status": "provisional"},
                "flags": {"requires_attorney_review": True},
                "candidate_vs_reference_diagnostics": {
                    "comparison_performed": True,
                    "method": "deterministic_rule_based",
                    "reference_status": "provisional",
                    "note": "Provisional comparison only.",
                    "missing_material_facts": ["Defendant role not addressed."],
                    "unsupported_or_extra_assertions": ["Extra assertion."],
                    "party_role_mismatches": [{
                        "type": "role_mismatch",
                        "party": "Alpha LLC",
                        "expected_role": "defendant",
                        "candidate_role": "plaintiff",
                    }],
                    "citation_evidence_coverage": {
                        "available": True,
                        "reference_citations": ["NYSCEF 101"],
                        "candidate_citations": ["NYSCEF 101"],
                        "gaps": [],
                    },
                },
            }],
        }

    def test_all_required_sections_and_reasoning_are_rendered(self):
        packet = render_attorney_review_packet(
            self._candidate(), self._evaluation()
        )
        headings = [
            "Case / Question Identity and Generation Provenance",
            "Question",
            "Proposed Answer",
            "Material Propositions",
            "Supporting Evidence",
            "Contrary Evidence and Contradictions",
            "Unresolved Factual, Evidentiary, or Procedural Questions",
            "Candidate-vs-Reference Diagnostics",
            "Confidence, Limitations, and Attorney-Review Scope",
            "Attorney Decision Checklist",
        ]
        for heading in headings:
            self.assertIn(heading, packet)
        self.assertIn("Alpha LLC is the plaintiff.", packet)
        self.assertIn("Classification:** record_fact", packet)
        self.assertIn("Proposition confidence:** 0.91", packet)
        self.assertIn(
            "The filed caption expressly identifies the party.", packet
        )
        self.assertIn("Polarity:** supporting", packet)
        self.assertIn("Plaintiff Alpha LLC", packet)
        self.assertIn("Defendant role not addressed.", packet)
        self.assertIn("Provisional comparison only.", packet)
        self.assertIn(
            "Verify the party roles against the complete docket.", packet
        )
        self.assertIn("Legal conclusions labeled:** Yes", packet)
        self.assertIn(
            "Retrieved evidence is not the complete record.", packet
        )
        self.assertIn("NOT ATTORNEY-APPROVED", packet)

    def test_evidence_locators_are_visible_next_to_evidence(self):
        packet = render_attorney_review_packet(
            self._candidate(), self._evaluation()
        )
        for expected in (
            "NYSCEF document:",
            "PDF page:",
            "Filing:",
            "Exhibit:",
            "Page ID:",
            "nyscef-101-page-0004",
            "Source path:",
            "/case/filings/complaint.pdf",
            "complaint.pdf",
            "PDF page: `9`",
            "Document:",
            "Page:",
        ):
            self.assertIn(expected, packet)

    def test_candidate_review_requirement_cannot_be_downgraded(self):
        candidate = self._candidate()
        evaluation = self._evaluation()
        evaluation["questions"][0]["flags"]["requires_attorney_review"] = False
        packet = render_attorney_review_packet(candidate, evaluation)
        self.assertIn("Requires attorney review:** Yes", packet)

    def test_checklist_is_rendered(self):
        packet = render_attorney_review_packet(
            self._candidate(), self._evaluation()
        )
        for choice in ("Accept", "Revise", "Reject", "Investigate further", "Notes"):
            self.assertIn(choice, packet)

    def test_missing_optional_data_is_explicit(self):
        packet = render_attorney_review_packet(
            {
                "question_id": "Q1",
                "question_text": "Question?",
                "proposed_answer": "Answer.",
            },
            {"questions": [{"question_id": "Q1"}]},
        )
        self.assertGreaterEqual(packet.count("None identified."), 5)
        self.assertIn("Not available", packet)

    def test_packet_is_written_next_to_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate_path = Path(tmp) / "Q1_candidate_answer.json"
            candidate_path.write_text(
                json.dumps(self._candidate()), encoding="utf-8"
            )
            packet_path = write_attorney_review_packet(
                candidate_path, self._evaluation()
            )
            self.assertEqual(packet_path.name, PACKET_FILENAME)
            self.assertEqual(packet_path.parent, candidate_path.parent.resolve())
            self.assertTrue(packet_path.is_file())
            self.assertIn(
                "Caption identifies Alpha LLC.",
                packet_path.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
