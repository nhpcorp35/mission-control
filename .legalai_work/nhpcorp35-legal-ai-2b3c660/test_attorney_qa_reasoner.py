"""Focused tests for retrieval-grounded attorney Q&A reasoning."""

from __future__ import annotations

import copy
import io
import json
import os
import socket
import tempfile
import unittest
import urllib.error
from unittest import mock

import matter_builder as mb
from engines import drafting_engine as de


def _page(page_number, text, nyscef_document_number, extraction_method="native"):
    return mb.build_page_record(
        page_number,
        text,
        extraction_method,
        nyscef_document_number,
    )


def _doc(nyscef, doc_type, texts, filename=None, **extra):
    pages = [
        _page(i, text, nyscef_document_number=nyscef)
        for i, text in enumerate(texts, start=1)
    ]
    document = {
        "filename": filename or f"nyscef_doc_no_{nyscef}_{doc_type}.pdf",
        "nyscef_document_number": nyscef,
        "type": doc_type,
        "pages": pages,
        "page_count": len(pages),
        "title": extra.pop("title", f"Doc {nyscef}"),
    }
    document.update(extra)
    return document


def _corpus():
    complaint = mb.normalize_document(
        _doc(
            10,
            "complaint",
            [
                "Acme Holdings LLC v. Beta Insurance Co. "
                "Plaintiff alleges premium payment was completed. "
                "FIRST CAUSE OF ACTION for breach of contract. "
                "Policy No. POL-998877 governs coverage. "
                "The occurrence was filed on January 15, 2024. "
                "The complaint was filed on February 1, 2024.",
                "EXHIBIT A",
                "Lease agreement body continuing without label " + ("x" * 80),
            ],
        ),
        include_exhibit_segments=True,
    )
    answer = mb.normalize_document(
        _doc(
            11,
            "answer",
            [
                "Defendant alleges premium payment was never completed. "
                "FIRST AFFIRMATIVE DEFENSE of failure to perform. "
                "Notice of Motion is not in this pleading.",
            ],
        ),
        include_exhibit_segments=True,
    )
    motion = mb.normalize_document(
        _doc(
            12,
            "motion",
            [
                "Notice of Motion for Summary Judgment returnable March 1, 2024. "
                "Movant respectfully seeks dismissal. "
                "Movant argues coverage is void ab initio.",
            ],
        ),
        include_exhibit_segments=True,
    )
    order = mb.normalize_document(
        _doc(
            13,
            "order",
            [
                "Decision and Order. IT IS HEREBY ORDERED that the motion is held.",
            ],
        ),
        include_exhibit_segments=True,
    )
    return [complaint, answer, motion, order]


def _hit(docs, nyscef, page_number, classifications=None, assertion_kind="unknown"):
    doc = next(d for d in docs if d["nyscef_document_number"] == nyscef)
    page = doc["pages"][page_number - 1]
    return {
        "result_id": f"hit-{nyscef}-{page_number}",
        "page_id": page["page_id"],
        "nyscef_document_number": nyscef,
        "pdf_page": page_number,
        "source_filename": doc["filename"],
        "document_type": doc["type"],
        "excerpt": page["text"][:180],
        "classifications": list(classifications or []),
        "assertion_kind": assertion_kind,
        "case_map_linkage": None,
        "exhibit_segment": None,
        "score": 10.0,
    }


def _retrieval(docs, hits, query="premium payment"):
    return {
        "query": query,
        "normalized_query": query,
        "results": hits,
        "result_count": len(hits),
    }


class AttorneyQAReasonerTests(unittest.TestCase):
    def setUp(self):
        self.docs = _corpus()
        self.case_map = mb.build_case_map_from_documents(self.docs)
        self.complaint_hit = _hit(
            self.docs,
            10,
            1,
            classifications=["allegation"],
            assertion_kind="party_allegation",
        )
        self.answer_hit = _hit(
            self.docs,
            11,
            1,
            classifications=["allegation"],
            assertion_kind="party_allegation",
        )
        self.order_hit = _hit(
            self.docs,
            13,
            1,
            classifications=["verified_fact"],
            assertion_kind="verified_record_fact",
        )
        self.motion_hit = _hit(
            self.docs,
            12,
            1,
            classifications=["legal_position"],
            assertion_kind="legal_position",
        )

    def _fake_model(self, payload):
        def _call(_system, _user):
            return copy.deepcopy(payload)

        return _call

    def test_supported_record_fact_with_valid_citation(self):
        retrieval = _retrieval(self.docs, [self.order_hit], query="decision and order")
        excerpt = "IT IS HEREBY ORDERED that the motion is held."
        payload = {
            "proposed_answer": "The court held the motion.",
            "propositions": [
                {
                    "proposition_id": "P1",
                    "text": "The Decision and Order states the motion is held.",
                    "classification": "verified_record_fact",
                    "nyscef_document_number": 13,
                    "page_id": self.order_hit["page_id"],
                    "pdf_page": 1,
                    "source_excerpt": excerpt,
                    "confidence": 0.9,
                    "rationale": "Procedural directive appears on the order page.",
                    "polarity": "supporting",
                }
            ],
            "supporting_evidence": [],
            "contrary_evidence": [],
            "unresolved_questions": [],
            "documents_pages_reviewed": [],
            "confidence": 0.9,
            "attorney_review": {
                "requires_attorney_review": True,
                "review_notes": "Confirm order effect.",
                "legal_conclusions_labeled": True,
                "coverage_conclusion": None,
            },
            "review_scope": {
                "completeness": "not_established",
                "qualification": "Limited to retrieved order page.",
            },
        }
        result = de.answer_attorney_record_question(
            "What did the court order?",
            retrieval,
            documents=self.docs,
            model_call=self._fake_model(payload),
        )
        self.assertEqual(result["status"], de.STATUS_READY)
        self.assertEqual(len(result["propositions"]), 1)
        prop = result["propositions"][0]
        self.assertEqual(prop["classification"], "verified_record_fact")
        self.assertEqual(prop["page_id"], self.order_hit["page_id"])
        self.assertEqual(prop["nyscef_document_number"], 13)
        self.assertTrue(result["attorney_review"]["requires_attorney_review"])

    def test_party_allegation_remains_allegation(self):
        retrieval = _retrieval(self.docs, [self.complaint_hit])
        excerpt = "Plaintiff alleges premium payment was completed."
        payload = {
            "proposed_answer": "Plaintiff alleges premium payment was completed.",
            "propositions": [
                {
                    "proposition_id": "P1",
                    "text": "Plaintiff alleges premium payment was completed.",
                    "classification": "party_allegation",
                    "nyscef_document_number": 10,
                    "page_id": self.complaint_hit["page_id"],
                    "pdf_page": 1,
                    "source_excerpt": excerpt,
                    "confidence": 0.85,
                    "rationale": "Complaint allegation language.",
                    "polarity": "supporting",
                }
            ],
            "confidence": 0.85,
            "attorney_review": {
                "requires_attorney_review": True,
                "review_notes": "Allegation only.",
                "legal_conclusions_labeled": True,
                "coverage_conclusion": None,
            },
        }
        result = de.answer_attorney_record_question(
            "Was premium payment completed?",
            retrieval,
            documents=self.docs,
            model_call=self._fake_model(payload),
        )
        self.assertEqual(
            result["propositions"][0]["classification"], "party_allegation"
        )

    def test_legal_argument_remains_legal_position(self):
        retrieval = _retrieval(self.docs, [self.motion_hit], query="void ab initio")
        excerpt = "Movant argues coverage is void ab initio."
        payload = {
            "proposed_answer": "Movant argues coverage is void ab initio.",
            "propositions": [
                {
                    "proposition_id": "P1",
                    "text": "Movant argues coverage is void ab initio.",
                    "classification": "legal_position",
                    "nyscef_document_number": 12,
                    "page_id": self.motion_hit["page_id"],
                    "pdf_page": 1,
                    "source_excerpt": excerpt,
                    "confidence": 0.8,
                    "rationale": "Motion argument, not a verified fact.",
                    "polarity": "supporting",
                }
            ],
            "confidence": 0.8,
            "attorney_review": {
                "requires_attorney_review": True,
                "review_notes": "Legal position only.",
                "legal_conclusions_labeled": True,
                "coverage_conclusion": None,
            },
        }
        result = de.answer_attorney_record_question(
            "What coverage argument does movant raise?",
            retrieval,
            documents=self.docs,
            model_call=self._fake_model(payload),
        )
        self.assertEqual(result["propositions"][0]["classification"], "legal_position")

    def test_conflicting_evidence_on_both_sides(self):
        retrieval = _retrieval(
            self.docs, [self.complaint_hit, self.answer_hit], query="premium payment"
        )
        payload = {
            "proposed_answer": (
                "The parties dispute whether premium payment was completed."
            ),
            "propositions": [
                {
                    "proposition_id": "P1",
                    "text": "Plaintiff alleges premium payment was completed.",
                    "classification": "party_allegation",
                    "nyscef_document_number": 10,
                    "page_id": self.complaint_hit["page_id"],
                    "pdf_page": 1,
                    "source_excerpt": "Plaintiff alleges premium payment was completed.",
                    "confidence": 0.8,
                    "rationale": "Complaint allegation.",
                    "polarity": "supporting",
                },
                {
                    "proposition_id": "P2",
                    "text": "Defendant alleges premium payment was never completed.",
                    "classification": "party_allegation",
                    "nyscef_document_number": 11,
                    "page_id": self.answer_hit["page_id"],
                    "pdf_page": 1,
                    "source_excerpt": (
                        "Defendant alleges premium payment was never completed."
                    ),
                    "confidence": 0.8,
                    "rationale": "Answer allegation.",
                    "polarity": "contrary",
                },
            ],
            "confidence": 0.7,
            "attorney_review": {
                "requires_attorney_review": True,
                "review_notes": "Competing allegations preserved.",
                "legal_conclusions_labeled": True,
                "coverage_conclusion": None,
            },
            "review_scope": {
                "completeness": "not_established",
                "qualification": "Conflict preserved; no reconciliation.",
            },
        }
        result = de.answer_attorney_record_question(
            "Was premium payment completed?",
            retrieval,
            documents=self.docs,
            model_call=self._fake_model(payload),
        )
        self.assertEqual(len(result["propositions"]), 2)
        self.assertEqual(len(result["supporting_evidence"]), 1)
        self.assertEqual(len(result["contrary_evidence"]), 1)
        polarities = {p["polarity"] for p in result["propositions"]}
        self.assertEqual(polarities, {"supporting", "contrary"})

    def test_unknown_missing_information_unresolved(self):
        retrieval = _retrieval(self.docs, [self.order_hit], query="policy limits")
        payload = {
            "proposed_answer": "Policy limits are not established by retrieved pages.",
            "propositions": [
                {
                    "proposition_id": "P1",
                    "text": "Policy limits are not stated in the retrieved order page.",
                    "classification": "unknown",
                    "confidence": 0.4,
                    "rationale": "Missing from supplied evidence.",
                    "polarity": "unresolved",
                }
            ],
            "unresolved_questions": [
                "What are the stated policy limits in the record?"
            ],
            "confidence": 0.4,
            "attorney_review": {
                "requires_attorney_review": True,
                "review_notes": "Missing evidence.",
                "legal_conclusions_labeled": True,
                "coverage_conclusion": None,
            },
        }
        result = de.answer_attorney_record_question(
            "What are the policy limits?",
            retrieval,
            documents=self.docs,
            model_call=self._fake_model(payload),
        )
        self.assertEqual(result["propositions"][0]["classification"], "unknown")
        self.assertTrue(result["unresolved_questions"])

    def test_invalid_hallucinated_citation_rejected(self):
        retrieval = _retrieval(self.docs, [self.order_hit])
        payload = {
            "proposed_answer": "Invented citation.",
            "propositions": [
                {
                    "proposition_id": "P1",
                    "text": "A secret order grants summary judgment.",
                    "classification": "verified_record_fact",
                    "nyscef_document_number": 99,
                    "page_id": "nyscef-099-page-0001",
                    "pdf_page": 1,
                    "source_excerpt": "grants summary judgment",
                    "confidence": 0.9,
                    "rationale": "Hallucinated.",
                    "polarity": "supporting",
                }
            ],
            "confidence": 0.9,
            "attorney_review": {
                "requires_attorney_review": True,
                "review_notes": "x",
                "legal_conclusions_labeled": True,
                "coverage_conclusion": None,
            },
        }
        result = de.answer_attorney_record_question(
            "Did the court grant SJ?",
            retrieval,
            documents=self.docs,
            model_call=self._fake_model(payload),
        )
        self.assertEqual(result["propositions"], [])
        self.assertTrue(result["audit"]["removed_propositions"])
        reasons = {r["reason"] for r in result["audit"]["rejection_reasons"]}
        self.assertTrue(
            reasons
            & {
                "citation_not_in_retrieval_context",
                "hallucinated_citation",
                "case_map_only_not_proof",
            }
        )

    def test_excerpt_mismatch_rejected(self):
        retrieval = _retrieval(self.docs, [self.order_hit])
        payload = {
            "proposed_answer": "Mismatch.",
            "propositions": [
                {
                    "proposition_id": "P1",
                    "text": "The order says something else.",
                    "classification": "verified_record_fact",
                    "nyscef_document_number": 13,
                    "page_id": self.order_hit["page_id"],
                    "pdf_page": 1,
                    "source_excerpt": "this excerpt does not appear on the page",
                    "confidence": 0.9,
                    "rationale": "Bad excerpt.",
                    "polarity": "supporting",
                }
            ],
            "confidence": 0.9,
            "attorney_review": {
                "requires_attorney_review": True,
                "review_notes": "x",
                "legal_conclusions_labeled": True,
                "coverage_conclusion": None,
            },
        }
        result = de.answer_attorney_record_question(
            "What did the court order?",
            retrieval,
            documents=self.docs,
            model_call=self._fake_model(payload),
        )
        self.assertEqual(result["propositions"], [])
        self.assertEqual(
            result["audit"]["rejection_reasons"][0]["reason"], "excerpt_mismatch"
        )

    def test_unsupported_proposition_removed_and_audited(self):
        retrieval = _retrieval(self.docs, [self.order_hit, self.complaint_hit])
        payload = {
            "proposed_answer": "Mixed.",
            "propositions": [
                {
                    "proposition_id": "P1",
                    "text": "The Decision and Order states the motion is held.",
                    "classification": "verified_record_fact",
                    "nyscef_document_number": 13,
                    "page_id": self.order_hit["page_id"],
                    "pdf_page": 1,
                    "source_excerpt": "IT IS HEREBY ORDERED that the motion is held.",
                    "confidence": 0.9,
                    "rationale": "Order text.",
                    "polarity": "supporting",
                },
                {
                    "proposition_id": "P2",
                    "text": "Plaintiff alleges premium payment was completed.",
                    "classification": "verified_record_fact",
                    "nyscef_document_number": 10,
                    "page_id": self.complaint_hit["page_id"],
                    "pdf_page": 1,
                    "source_excerpt": "Plaintiff alleges premium payment was completed.",
                    "confidence": 0.9,
                    "rationale": "Promoted allegation.",
                    "polarity": "supporting",
                },
            ],
            "confidence": 0.8,
            "attorney_review": {
                "requires_attorney_review": True,
                "review_notes": "Audit promotion.",
                "legal_conclusions_labeled": True,
                "coverage_conclusion": None,
            },
        }
        result = de.answer_attorney_record_question(
            "What is established?",
            retrieval,
            documents=self.docs,
            model_call=self._fake_model(payload),
        )
        self.assertEqual(len(result["propositions"]), 1)
        self.assertEqual(result["propositions"][0]["proposition_id"], "P1")
        self.assertEqual(len(result["audit"]["removed_propositions"]), 1)
        self.assertEqual(
            result["audit"]["removed_propositions"][0]["removal_reason"],
            "allegation_to_fact_promotion",
        )

    def test_case_map_only_assertion_cannot_become_proof(self):
        retrieval = _retrieval(self.docs, [self.order_hit])
        payload = {
            "proposed_answer": "Case map claims coverage.",
            "propositions": [
                {
                    "proposition_id": "P1",
                    "text": "Coverage exists based on case map alone.",
                    "classification": "verified_record_fact",
                    "nyscef_document_number": 13,
                    "page_id": self.order_hit["page_id"],
                    "pdf_page": 1,
                    "source_excerpt": "IT IS HEREBY ORDERED that the motion is held.",
                    "confidence": 0.9,
                    "rationale": "Proven by case map alone as independent proof.",
                    "polarity": "supporting",
                }
            ],
            "confidence": 0.9,
            "attorney_review": {
                "requires_attorney_review": True,
                "review_notes": "x",
                "legal_conclusions_labeled": True,
                "coverage_conclusion": None,
            },
        }
        # Even with a page citation, explicit case-map-alone rationale is rejected
        # when paired with missing retrieval grounding for the claim — here we
        # simulate a proposition that cites a page not about coverage and relies
        # on case-map-only language; force removal via invented content / map rule.
        # Stronger path: no retrieval hit at all for the cited page_id.
        payload["propositions"][0]["page_id"] = "nyscef-010-page-0001"
        payload["propositions"][0]["nyscef_document_number"] = 10
        payload["propositions"][0]["source_excerpt"] = "Policy No. POL-998877"
        result = de.answer_attorney_record_question(
            "Is coverage established?",
            retrieval,
            documents=self.docs,
            case_map=self.case_map,
            model_call=self._fake_model(payload),
        )
        self.assertEqual(result["propositions"], [])
        reasons = {r["reason"] for r in result["audit"]["rejection_reasons"]}
        self.assertTrue(
            reasons
            & {
                "citation_not_in_retrieval_context",
                "case_map_only_not_proof",
            }
        )

    def test_duplicate_ids_deduplicated_safely(self):
        retrieval = _retrieval(self.docs, [self.order_hit])
        excerpt = "IT IS HEREBY ORDERED that the motion is held."
        base = {
            "proposition_id": "P1",
            "text": "The Decision and Order states the motion is held.",
            "classification": "verified_record_fact",
            "nyscef_document_number": 13,
            "page_id": self.order_hit["page_id"],
            "pdf_page": 1,
            "source_excerpt": excerpt,
            "confidence": 0.9,
            "rationale": "Order text.",
            "polarity": "supporting",
        }
        payload = {
            "proposed_answer": "Held.",
            "propositions": [base, dict(base, text="Duplicate id second copy.")],
            "confidence": 0.9,
            "attorney_review": {
                "requires_attorney_review": True,
                "review_notes": "x",
                "legal_conclusions_labeled": True,
                "coverage_conclusion": None,
            },
        }
        result = de.answer_attorney_record_question(
            "What did the court order?",
            retrieval,
            documents=self.docs,
            model_call=self._fake_model(payload),
        )
        self.assertEqual(len(result["propositions"]), 1)
        self.assertIn("P1", result["audit"]["duplicate_proposition_ids"])
        self.assertEqual(len(result["audit"]["removed_propositions"]), 1)

    def test_provider_unavailable_returns_not_ready_evidence_packet(self):
        retrieval = _retrieval(self.docs, [self.complaint_hit, self.answer_hit])
        result = de.answer_attorney_record_question(
            "Was premium payment completed?",
            retrieval,
            documents=self.docs,
            model_call=None,
        )
        self.assertEqual(result["status"], de.STATUS_NOT_READY)
        self.assertFalse(result["audit"]["provider_available"])
        self.assertEqual(len(result["retrieved_evidence"]), 2)
        self.assertEqual(result["propositions"], [])
        self.assertTrue(result["attorney_review"]["requires_attorney_review"])

    def test_deterministic_validator_output(self):
        retrieval = _retrieval(self.docs, [self.order_hit, self.motion_hit])
        payload = {
            "proposed_answer": "Deterministic check.",
            "propositions": [
                {
                    "proposition_id": "P2",
                    "text": "Movant argues coverage is void ab initio.",
                    "classification": "legal_position",
                    "nyscef_document_number": 12,
                    "page_id": self.motion_hit["page_id"],
                    "pdf_page": 1,
                    "source_excerpt": "Movant argues coverage is void ab initio.",
                    "confidence": 0.7,
                    "rationale": "Motion argument.",
                    "polarity": "supporting",
                },
                {
                    "proposition_id": "P1",
                    "text": "The Decision and Order states the motion is held.",
                    "classification": "verified_record_fact",
                    "nyscef_document_number": 13,
                    "page_id": self.order_hit["page_id"],
                    "pdf_page": 1,
                    "source_excerpt": "IT IS HEREBY ORDERED that the motion is held.",
                    "confidence": 0.9,
                    "rationale": "Order text.",
                    "polarity": "supporting",
                },
            ],
            "confidence": 0.8,
            "attorney_review": {
                "requires_attorney_review": True,
                "review_notes": "Deterministic.",
                "legal_conclusions_labeled": True,
                "coverage_conclusion": None,
            },
        }
        a = de.validate_attorney_qa_response(
            payload,
            question="What is on the record?",
            retrieval=retrieval,
            documents=self.docs,
        )
        b = de.validate_attorney_qa_response(
            copy.deepcopy(payload),
            question="What is on the record?",
            retrieval=copy.deepcopy(retrieval),
            documents=copy.deepcopy(self.docs),
        )
        self.assertEqual(a, b)

    def test_attorney_review_fields_present(self):
        retrieval = _retrieval(self.docs, [self.order_hit])
        payload = {
            "proposed_answer": "Held.",
            "propositions": [
                {
                    "proposition_id": "P1",
                    "text": "The Decision and Order states the motion is held.",
                    "classification": "verified_record_fact",
                    "nyscef_document_number": 13,
                    "page_id": self.order_hit["page_id"],
                    "pdf_page": 1,
                    "source_excerpt": "IT IS HEREBY ORDERED that the motion is held.",
                    "confidence": 0.9,
                    "rationale": "Order text.",
                    "polarity": "supporting",
                }
            ],
            "confidence": 0.9,
            "attorney_review": {
                "requires_attorney_review": False,
                "review_notes": "Still force review.",
                "legal_conclusions_labeled": True,
                "coverage_conclusion": None,
            },
        }
        result = de.answer_attorney_record_question(
            "What did the court order?",
            retrieval,
            documents=self.docs,
            model_call=self._fake_model(payload),
        )
        review = result["attorney_review"]
        self.assertTrue(review["requires_attorney_review"])
        self.assertIn("review_notes", review)
        self.assertIn("legal_conclusions_labeled", review)
        self.assertIn("coverage_conclusion", review)

    def test_default_backward_compatible_paths_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = mb.get_matter(
                documents=[
                    _doc(
                        20,
                        "motion",
                        ["Notice of Motion for Summary Judgment " + ("m" * 40)],
                    )
                ],
                matter_folder=tmp,
            )
        self.assertNotIn("canonical_retrieval", baseline)
        self.assertNotIn("retrieval_grounded_qa", baseline)
        self.assertNotIn(
            "retrieval_grounded_qa", baseline["attorney_work_product"]
        )
        expected_keys = {
            "matter_name",
            "case_name",
            "index_number",
            "document_count",
            "documents",
            "groups",
            "grouped_documents",
            "folder",
            "summary",
            "selected_case",
            "issue_packet",
            "contradiction_analysis",
            "attorney_work_product",
            "draft_generation",
            "citation_exhibit_engine",
        }
        self.assertEqual(set(baseline.keys()), expected_keys)

    def test_get_matter_opt_in_attorney_qa(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = [
                _doc(
                    13,
                    "order",
                    [
                        "Decision and Order. IT IS HEREBY ORDERED that the motion is held."
                    ],
                )
            ]

            def fake_model(_system, _user):
                page_id = mb.make_page_id(13, 1)
                return {
                    "proposed_answer": "The motion is held.",
                    "propositions": [
                        {
                            "proposition_id": "P1",
                            "text": "The Decision and Order states the motion is held.",
                            "classification": "verified_record_fact",
                            "nyscef_document_number": 13,
                            "page_id": page_id,
                            "pdf_page": 1,
                            "source_excerpt": (
                                "IT IS HEREBY ORDERED that the motion is held."
                            ),
                            "confidence": 0.9,
                            "rationale": "Order text.",
                            "polarity": "supporting",
                        }
                    ],
                    "confidence": 0.9,
                    "attorney_review": {
                        "requires_attorney_review": True,
                        "review_notes": "Review order.",
                        "legal_conclusions_labeled": True,
                        "coverage_conclusion": None,
                    },
                }

            result = mb.get_matter(
                documents=docs,
                matter_folder=tmp,
                attorney_qa_question="What did the court order?",
                attorney_qa_options={"model_call": fake_model},
                canonical_retrieval_options={"top_k": 5},
            )
        self.assertIn("canonical_retrieval", result)
        self.assertIn("retrieval_grounded_qa", result)
        self.assertIn(
            "retrieval_grounded_qa", result["attorney_work_product"]
        )
        qa = result["retrieval_grounded_qa"]
        self.assertEqual(qa["status"], de.STATUS_READY)
        self.assertTrue(qa["propositions"])


def _valid_openai_answer_payload(page_id, nyscef=13, pdf_page=1):
    return {
        "proposed_answer": "The court held the motion.",
        "propositions": [
            {
                "proposition_id": "P1",
                "text": "The Decision and Order states the motion is held.",
                "classification": "verified_record_fact",
                "nyscef_document_number": nyscef,
                "page_id": page_id,
                "pdf_page": pdf_page,
                "source_excerpt": "IT IS HEREBY ORDERED that the motion is held.",
                "confidence": 0.9,
                "rationale": "Procedural directive appears on the order page.",
                "polarity": "supporting",
            }
        ],
        "supporting_evidence": [
            {
                "page_id": page_id,
                "nyscef_document_number": nyscef,
                "pdf_page": pdf_page,
                "excerpt": "IT IS HEREBY ORDERED that the motion is held.",
                "note": "Order page",
            }
        ],
        "contrary_evidence": [],
        "unresolved_questions": [],
        "documents_pages_reviewed": [
            {
                "nyscef_document_number": nyscef,
                "page_id": page_id,
                "pdf_page": pdf_page,
                "source_filename": "order.pdf",
                "document_type": "order",
            }
        ],
        "confidence": 0.9,
        "attorney_review": {
            "requires_attorney_review": True,
            "review_notes": "Confirm order effect.",
            "legal_conclusions_labeled": True,
            "coverage_conclusion": None,
        },
        "review_scope": {
            "completeness": "not_established",
            "qualification": "Limited to retrieved order page.",
            "explanation": "Single order page in retrieval scope.",
        },
    }


def _responses_body_from_answer(answer_obj, *, status="completed", extra_items=None):
    items = list(extra_items or [])
    items.append(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": json.dumps(answer_obj),
                }
            ],
        }
    )
    return {
        "id": "resp_test",
        "object": "response",
        "status": status,
        "incomplete_details": None,
        "error": None,
        "output": items,
    }


class _FakeHTTPResponse:
    def __init__(self, payload, status=200):
        if isinstance(payload, (dict, list)):
            raw = json.dumps(payload).encode("utf-8")
        elif isinstance(payload, str):
            raw = payload.encode("utf-8")
        else:
            raw = payload
        self._raw = raw
        self.status = status

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class OpenAIResponsesProviderTests(unittest.TestCase):
    def setUp(self):
        self.docs = _corpus()
        self.order_hit = _hit(
            self.docs,
            13,
            1,
            classifications=["court_order", "procedural_fact"],
            assertion_kind="procedural_fact",
        )
        self.retrieval = _retrieval(self.docs, [self.order_hit])
        self._env_stack = mock.patch.dict(os.environ, {}, clear=False)
        self._env_stack.start()
        for key in (
            de.OPENAI_API_KEY_ENV,
            de.LEGALAI_OPENAI_MODEL_ENV,
            de.LEGALAI_OPENAI_ENDPOINT_ENV,
            de.LEGALAI_MODEL_ENDPOINT_ENV,
            de.LEGALAI_MODEL_API_KEY_ENV,
            de.LEGALAI_MODEL_TIMEOUT_ENV,
        ):
            os.environ.pop(key, None)

    def tearDown(self):
        self._env_stack.stop()

    def _answer_payload(self):
        return _valid_openai_answer_payload(self.order_hit["page_id"])

    def test_openai_auto_selected_when_api_key_present(self):
        os.environ[de.OPENAI_API_KEY_ENV] = "sk-test-key-secret"
        provider = de.resolve_model_provider()
        self.assertIsNotNone(provider)
        self.assertTrue(de.model_provider_available())

    def test_missing_api_key_provider_unavailable(self):
        self.assertIsNone(de.resolve_model_provider())
        self.assertFalse(de.model_provider_available())
        result = de.answer_attorney_record_question(
            "What did the court order?",
            self.retrieval,
            documents=self.docs,
        )
        self.assertEqual(result["status"], de.STATUS_NOT_READY)
        self.assertFalse(result["audit"]["provider_available"])
        self.assertIn("OPENAI_API_KEY", result["review_scope"]["reason"])

    def test_generic_endpoint_overrides_openai_auto_selection(self):
        os.environ[de.OPENAI_API_KEY_ENV] = "sk-test-key-secret"
        os.environ[de.LEGALAI_MODEL_ENDPOINT_ENV] = "https://example.test/v1/model"
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["full_url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeHTTPResponse({"proposed_answer": "generic", "propositions": []})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            provider = de.resolve_model_provider()
            raw = provider("sys", "user")
        self.assertEqual(captured["full_url"], "https://example.test/v1/model")
        self.assertEqual(captured["body"]["system"], "sys")
        self.assertNotIn("text", captured["body"])
        self.assertEqual(raw["proposed_answer"], "generic")

    def test_request_endpoint_bearer_model_default_and_schema_shape(self):
        os.environ[de.OPENAI_API_KEY_ENV] = "sk-test-key-secret"
        answer = self._answer_payload()
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["full_url"] = request.full_url
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _FakeHTTPResponse(_responses_body_from_answer(answer))

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = de.answer_attorney_record_question(
                "What did the court order?",
                self.retrieval,
                documents=self.docs,
            )

        self.assertEqual(captured["full_url"], de.DEFAULT_OPENAI_ENDPOINT)
        self.assertEqual(
            captured["headers"].get("authorization"),
            "Bearer sk-test-key-secret",
        )
        self.assertEqual(captured["body"]["model"], de.DEFAULT_OPENAI_MODEL)
        self.assertEqual(
            captured["body"]["instructions"], de.RECORD_ANALYSIS_SYSTEM_PROMPT
        )
        self.assertIn("evidence packet", captured["body"]["input"])
        fmt = captured["body"]["text"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertTrue(fmt["strict"])
        self.assertEqual(fmt["name"], de.ATTORNEY_QA_SCHEMA_NAME)
        self.assertEqual(fmt["schema"]["type"], "object")
        self.assertIn("propositions", fmt["schema"]["required"])
        self.assertEqual(result["status"], de.STATUS_READY)
        # Never leak key into attorney-facing payload / reason strings.
        dumped = json.dumps(result)
        self.assertNotIn("sk-test-key-secret", dumped)

    def test_model_and_endpoint_overrides(self):
        os.environ[de.OPENAI_API_KEY_ENV] = "sk-test-key-secret"
        os.environ[de.LEGALAI_OPENAI_MODEL_ENV] = "gpt-test-override"
        os.environ[de.LEGALAI_OPENAI_ENDPOINT_ENV] = (
            "https://example.openai.test/v1/responses"
        )
        os.environ[de.LEGALAI_MODEL_TIMEOUT_ENV] = "12.5"
        answer = self._answer_payload()
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["full_url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _FakeHTTPResponse(_responses_body_from_answer(answer))

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            de.resolve_model_provider()("sys", "user-q")

        self.assertEqual(
            captured["full_url"], "https://example.openai.test/v1/responses"
        )
        self.assertEqual(captured["body"]["model"], "gpt-test-override")
        self.assertEqual(captured["timeout"], 12.5)

    def test_valid_output_parsing_and_citation_validator(self):
        os.environ[de.OPENAI_API_KEY_ENV] = "sk-test-key-secret"
        answer = self._answer_payload()
        # Inject a hallucinated citation that schema allows but validator rejects.
        answer["propositions"].append(
            {
                "proposition_id": "P2",
                "text": "Hallucinated page claim.",
                "classification": "verified_record_fact",
                "nyscef_document_number": 99,
                "page_id": "nyscef-99-p1",
                "pdf_page": 1,
                "source_excerpt": "not on record",
                "confidence": 0.5,
                "rationale": "Invented.",
                "polarity": "supporting",
            }
        )

        with mock.patch(
            "urllib.request.urlopen",
            return_value=_FakeHTTPResponse(_responses_body_from_answer(answer)),
        ):
            result = de.answer_attorney_record_question(
                "What did the court order?",
                self.retrieval,
                documents=self.docs,
            )
        self.assertEqual(result["status"], de.STATUS_READY)
        self.assertEqual(len(result["propositions"]), 1)
        self.assertEqual(result["propositions"][0]["proposition_id"], "P1")
        removed_ids = [
            item["proposition_id"] for item in result["audit"]["removed_propositions"]
        ]
        self.assertIn("P2", removed_ids)

    def test_multiple_output_items_and_content_parts(self):
        os.environ[de.OPENAI_API_KEY_ENV] = "sk-test-key-secret"
        answer = self._answer_payload()
        body = {
            "id": "resp_multi",
            "status": "completed",
            "error": None,
            "output": [
                {"type": "reasoning", "content": []},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "not-json-preamble"},
                        {
                            "type": "output_text",
                            "text": json.dumps(answer),
                        },
                    ],
                },
            ],
        }
        parsed = de.parse_openai_responses_payload(body)
        self.assertEqual(parsed["proposed_answer"], answer["proposed_answer"])

    def test_refusal_returns_not_ready(self):
        os.environ[de.OPENAI_API_KEY_ENV] = "sk-test-key-secret"
        body = {
            "status": "completed",
            "error": None,
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "refusal",
                            "refusal": "I'm sorry, I cannot assist with that request.",
                        }
                    ],
                }
            ],
        }
        with mock.patch(
            "urllib.request.urlopen", return_value=_FakeHTTPResponse(body)
        ):
            result = de.answer_attorney_record_question(
                "What did the court order?",
                self.retrieval,
                documents=self.docs,
            )
        self.assertEqual(result["status"], de.STATUS_NOT_READY)
        self.assertTrue(result["audit"]["provider_available"])
        self.assertIn("refused", result["review_scope"]["reason"].lower())

    def test_incomplete_response_returns_not_ready(self):
        os.environ[de.OPENAI_API_KEY_ENV] = "sk-test-key-secret"
        body = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "error": None,
            "output": [],
        }
        with mock.patch(
            "urllib.request.urlopen", return_value=_FakeHTTPResponse(body)
        ):
            result = de.answer_attorney_record_question(
                "What did the court order?",
                self.retrieval,
                documents=self.docs,
            )
        self.assertEqual(result["status"], de.STATUS_NOT_READY)
        self.assertIn("Incomplete", result["review_scope"]["reason"])

    def test_malformed_json_returns_not_ready(self):
        os.environ[de.OPENAI_API_KEY_ENV] = "sk-test-key-secret"
        body = {
            "status": "completed",
            "error": None,
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "{not-json"}],
                }
            ],
        }
        with mock.patch(
            "urllib.request.urlopen", return_value=_FakeHTTPResponse(body)
        ):
            result = de.answer_attorney_record_question(
                "What did the court order?",
                self.retrieval,
                documents=self.docs,
            )
        self.assertEqual(result["status"], de.STATUS_NOT_READY)
        self.assertIn("Malformed JSON", result["review_scope"]["reason"])

    def test_schema_invalid_response_returns_not_ready(self):
        os.environ[de.OPENAI_API_KEY_ENV] = "sk-test-key-secret"
        # Valid JSON object but missing required contract keys.
        body = _responses_body_from_answer({"proposed_answer": "only free text"})
        with mock.patch(
            "urllib.request.urlopen", return_value=_FakeHTTPResponse(body)
        ):
            result = de.answer_attorney_record_question(
                "What did the court order?",
                self.retrieval,
                documents=self.docs,
            )
        self.assertEqual(result["status"], de.STATUS_NOT_READY)
        self.assertIn("Schema-invalid", result["review_scope"]["reason"])

    def test_http_error_returns_not_ready(self):
        os.environ[de.OPENAI_API_KEY_ENV] = "sk-test-key-secret"

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                url=request.full_url,
                code=500,
                msg="Server Error",
                hdrs=None,
                fp=io.BytesIO(b'{"error":{"message":"boom"}}'),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = de.answer_attorney_record_question(
                "What did the court order?",
                self.retrieval,
                documents=self.docs,
            )
        self.assertEqual(result["status"], de.STATUS_NOT_READY)
        self.assertIn("HTTP 500", result["review_scope"]["reason"])
        self.assertNotIn("sk-test-key-secret", result["review_scope"]["reason"])

    def test_timeout_returns_not_ready(self):
        os.environ[de.OPENAI_API_KEY_ENV] = "sk-test-key-secret"

        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError(socket.timeout("timed out"))

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = de.answer_attorney_record_question(
                "What did the court order?",
                self.retrieval,
                documents=self.docs,
            )
        self.assertEqual(result["status"], de.STATUS_NOT_READY)
        self.assertIn("transport error", result["review_scope"]["reason"].lower())

    def test_injectable_model_call_unchanged_priority(self):
        os.environ[de.OPENAI_API_KEY_ENV] = "sk-test-key-secret"
        os.environ[de.LEGALAI_MODEL_ENDPOINT_ENV] = "https://example.test/v1/model"
        called = {"urlopen": 0}

        def fake_urlopen(*args, **kwargs):
            called["urlopen"] += 1
            raise AssertionError("urlopen should not run when model_call is injected")

        payload = {
            "proposed_answer": "Injected.",
            "propositions": [
                {
                    "proposition_id": "P1",
                    "text": "The Decision and Order states the motion is held.",
                    "classification": "verified_record_fact",
                    "nyscef_document_number": 13,
                    "page_id": self.order_hit["page_id"],
                    "pdf_page": 1,
                    "source_excerpt": "IT IS HEREBY ORDERED that the motion is held.",
                    "confidence": 0.9,
                    "rationale": "Order text.",
                    "polarity": "supporting",
                }
            ],
            "confidence": 0.9,
            "attorney_review": {
                "requires_attorney_review": True,
                "review_notes": "x",
                "legal_conclusions_labeled": True,
                "coverage_conclusion": None,
            },
        }

        def fake_model(_system, _user):
            return copy.deepcopy(payload)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = de.answer_attorney_record_question(
                "What did the court order?",
                self.retrieval,
                documents=self.docs,
                model_call=fake_model,
            )
        self.assertEqual(called["urlopen"], 0)
        self.assertEqual(result["status"], de.STATUS_READY)
        self.assertEqual(result["proposed_answer"], "Injected.")


if __name__ == "__main__":
    unittest.main()
