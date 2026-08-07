"""Synthetic regressions for party-role answer-materiality filtering."""

from __future__ import annotations

import copy
import json
import re
import unittest

from engines import drafting_engine as de


def _hit(
    *,
    result_id: str,
    nyscef: int,
    page: int,
    doc_type: str,
    filename: str,
    excerpt: str,
    classifications=None,
    assertion_kind: str = "verified_record_fact",
    score: float = 10.0,
):
    return {
        "result_id": result_id,
        "page_id": f"nyscef-{nyscef}-p{page}",
        "nyscef_document_number": nyscef,
        "pdf_page": page,
        "source_filename": filename,
        "document_type": doc_type,
        "excerpt": excerpt,
        "classifications": list(classifications or []),
        "assertion_kind": assertion_kind,
        "case_map_linkage": None,
        "exhibit_segment": None,
        "score": score,
    }


def _mixed_party_role_hits():
    pleading = _hit(
        result_id="plead-1",
        nyscef=201,
        page=5,
        doc_type="complaint",
        filename="nyscef_doc_no_201_summons_complaint.pdf",
        excerpt=(
            "PARTIES\n"
            "1. Plaintiff Alpine Freight LP is a limited liability partnership "
            "authorized to do business in this state.\n"
            "2. Defendant Harbor Gate Carrier Inc. is a domestic corporation.\n"
            "3. Mesa Trailer Repair LLC, third-party defendant, was joined herein "
            "as a necessary party."
        ),
        classifications=["party_identity"],
    )
    motion = _hit(
        result_id="motion-1",
        nyscef=202,
        page=1,
        doc_type="motion",
        filename="nyscef_doc_no_202_notice_of_motion.pdf",
        excerpt=(
            "Notice of Motion for Summary Judgment returnable June 1, 2024. "
            "Movant seeks dismissal. Caption lists Alpine Freight LP against "
            "Harbor Gate Carrier Inc. without assigning procedural roles."
        ),
        classifications=["motion"],
    )
    rji = _hit(
        result_id="rji-1",
        nyscef=203,
        page=1,
        doc_type="other",
        filename="nyscef_doc_no_203_rji.pdf",
        excerpt=(
            "Request for Judicial Intervention. RJI addendum repeats the caption "
            "Alpine Freight LP v. Harbor Gate Carrier Inc. and a conference date "
            "without explaining party roles."
        ),
        classifications=["procedural"],
    )
    amended = _hit(
        result_id="amended-1",
        nyscef=204,
        page=1,
        doc_type="complaint",
        filename="nyscef_doc_no_204_amended_complaint.pdf",
        excerpt=(
            "AMENDED COMPLAINT. Plaintiff Alpine Freight LP remains plaintiff. "
            "Harbor Gate Carrier Inc. is incorrectly named and is now known as "
            "Harbor Gate Logistics Inc., substituted as defendant. Party status "
            "as to Mesa Trailer Repair LLC is disputed."
        ),
        classifications=["party_identity"],
    )
    qualification = _hit(
        result_id="order-role-1",
        nyscef=205,
        page=1,
        doc_type="order",
        filename="nyscef_doc_no_205_decision_and_order.pdf",
        excerpt=(
            "Decision and Order. IT IS HEREBY ORDERED that Mesa Trailer Repair LLC "
            "is dismissed as a party, without prejudice to renewal if capacity is "
            "later established. The caption role conflict remains unresolved."
        ),
        classifications=["court_order"],
    )
    unrelated_affirmation = _hit(
        result_id="aff-1",
        nyscef=206,
        page=1,
        doc_type="affirmation",
        filename="nyscef_doc_no_206_affirmation_of_service.pdf",
        excerpt=(
            "Affirmation of service. Deponent mailed papers on May 1, 2024. "
            "Procedural calendar notation without role assignments."
        ),
        classifications=["procedural"],
    )
    unrelated_order = _hit(
        result_id="order-noise-1",
        nyscef=207,
        page=1,
        doc_type="order",
        filename="nyscef_doc_no_207_scheduling_order.pdf",
        excerpt=(
            "Scheduling Order. IT IS HEREBY ORDERED that the conference is adjourned "
            "and the procedural calendar is updated."
        ),
        classifications=["court_order"],
    )
    return [
        pleading,
        motion,
        rji,
        amended,
        qualification,
        unrelated_affirmation,
        unrelated_order,
    ]


class PartyRoleAnswerMaterialityTests(unittest.TestCase):
    def setUp(self):
        self.party_question = (
            "Who are the parties and what are their roles in this action?"
        )
        self.motion_question = (
            "What relief does the notice of motion for summary judgment seek?"
        )
        self.hits = _mixed_party_role_hits()
        self.retrieval = {
            "query": self.party_question,
            "results": copy.deepcopy(self.hits),
            # Poison inputs that must never enter generation.
            "provisional_answer": "PROVISIONAL_SHOULD_NOT_APPEAR",
            "gold_answer": "GOLD_SHOULD_NOT_APPEAR",
        }

    def test_detects_party_role_intent_not_motion(self):
        self.assertTrue(de.detect_party_role_question_intent(self.party_question))
        self.assertFalse(de.detect_party_role_question_intent(self.motion_question))
        self.assertFalse(
            de.detect_party_role_question_intent(
                "What did the court order regarding the conference date?"
            )
        )

    def test_mixed_evidence_excludes_motion_and_rji(self):
        packet = de.build_evidence_packet(self.party_question, self.retrieval)
        page_ids = {hit["page_id"] for hit in packet["retrieval_hits"]}
        self.assertIn("nyscef-201-p5", page_ids)
        self.assertNotIn("nyscef-202-p1", page_ids)
        self.assertNotIn("nyscef-203-p1", page_ids)
        self.assertNotIn("nyscef-206-p1", page_ids)
        self.assertNotIn("nyscef-207-p1", page_ids)
        self.assertEqual(packet["materiality_filter"]["intent"], "party_role")
        self.assertGreater(packet["materiality_filter"]["excluded_hit_count"], 0)

    def test_direct_pleading_evidence_remains(self):
        packet = de.build_evidence_packet(self.party_question, self.retrieval)
        kept = {
            hit["page_id"]: hit["excerpt"] for hit in packet["retrieval_hits"]
        }
        self.assertIn("nyscef-201-p5", kept)
        self.assertIn("Plaintiff Alpine Freight LP is a limited liability partnership", kept["nyscef-201-p5"])
        self.assertIn("joined herein", kept["nyscef-201-p5"])

    def test_amended_conflicting_or_changed_role_remains(self):
        packet = de.build_evidence_packet(self.party_question, self.retrieval)
        page_ids = {hit["page_id"] for hit in packet["retrieval_hits"]}
        self.assertIn("nyscef-204-p1", page_ids)
        amended = next(
            hit for hit in packet["retrieval_hits"] if hit["page_id"] == "nyscef-204-p1"
        )
        self.assertIn("incorrectly named", amended["excerpt"])
        self.assertIn("substituted as defendant", amended["excerpt"])

    def test_uncertainty_and_qualification_evidence_remains(self):
        packet = de.build_evidence_packet(self.party_question, self.retrieval)
        page_ids = {hit["page_id"] for hit in packet["retrieval_hits"]}
        self.assertIn("nyscef-205-p1", page_ids)
        order = next(
            hit for hit in packet["retrieval_hits"] if hit["page_id"] == "nyscef-205-p1"
        )
        self.assertIn("dismissed as a party", order["excerpt"])
        self.assertIn("capacity", order["excerpt"])
        self.assertIn("unresolved", order["excerpt"].lower())

    def test_strict_necessity_prunes_everything_outside_protected_group(self):
        caption = _hit(
            result_id="caption", nyscef=301, page=1, doc_type="complaint",
            filename="initiating_pleading.pdf",
            excerpt=("SUPREME COURT\nFirst Listed Ventures LLC and Late Listed "
                     "Holdings Inc., Plaintiffs, v. Common Carrier LLC, Defendant."),
        )
        same_filing_narrative = _hit(
            result_id="narrative", nyscef=301, page=2, doc_type="complaint",
            filename="initiating_pleading.pdf",
            excerpt="Plaintiffs describe a commercial relationship with the defendant.",
        )
        parties_one = _hit(
            result_id="parties-1", nyscef=301, page=3, doc_type="complaint",
            filename="initiating_pleading.pdf",
            excerpt="PARTIES\nPlaintiff First Listed Ventures LLC is a domestic company.",
        )
        parties_one["party_role_section_expanded"] = True
        parties_two = _hit(
            result_id="parties-2", nyscef=301, page=4, doc_type="complaint",
            filename="initiating_pleading.pdf",
            excerpt=("Plaintiff Late Listed Holdings Inc. is a domestic corporation. "
                     "Defendant Common Carrier LLC is a limited liability company."),
        )
        parties_two["party_role_section_expanded"] = True
        name_only = _hit(
            result_id="name-only", nyscef=301, page=5, doc_type="complaint",
            filename="initiating_pleading.pdf", excerpt="Late Listed Holdings Inc.",
        )
        later_answer = _hit(
            result_id="later-answer", nyscef=302, page=1, doc_type="answer",
            filename="later_answer.pdf",
            excerpt="Defendant Common Carrier LLC answers and denies the allegations.",
        )
        exhibit = _hit(
            result_id="exhibit", nyscef=303, page=1, doc_type="other",
            filename="relationship_exhibit.pdf",
            excerpt="The companies maintained a commercial relationship.",
        )
        adjacent = _hit(
            result_id="adjacent", nyscef=304, page=1, doc_type="other",
            filename="change_filing.pdf", excerpt="Background facts only.",
        )
        actual_change = _hit(
            result_id="actual-change", nyscef=304, page=2, doc_type="other",
            filename="change_filing.pdf",
            excerpt="New Harbor LLC was added as a defendant and necessary party.",
        )
        metadata_only = _hit(
            result_id="metadata-only", nyscef=305, page=1, doc_type="complaint",
            filename="amended_complaint_substitution.pdf",
            excerpt="Defendant Common Carrier LLC denies a shipping allegation.",
        )
        duplicate_change = copy.deepcopy(actual_change)
        duplicate_change["result_id"] = "actual-change-duplicate"

        packet = de.build_evidence_packet(
            self.party_question,
            {"results": [caption, same_filing_narrative, parties_one, parties_two,
                         name_only, later_answer, exhibit, adjacent, actual_change,
                         metadata_only, duplicate_change]},
        )
        kept = packet["retrieval_hits"]
        kept_ids = [hit["result_id"] for hit in kept]

        self.assertEqual(
            set(kept_ids), {"caption", "parties-1", "parties-2", "actual-change"}
        )
        self.assertEqual(kept_ids.count("actual-change"), 1)
        self.assertLessEqual(len(kept), 12)
        self.assertLessEqual(
            packet["materiality_filter"]["packet_budget"]["serialized_chars"],
            24000,
        )
        citations = {(hit["nyscef_document_number"], hit["pdf_page"], hit["page_id"])
                     for hit in kept}
        self.assertIn((301, 4, "nyscef-301-p4"), citations)
        combined = " ".join(hit["excerpt"] for hit in kept)
        for identity in (
            "First Listed Ventures LLC", "Late Listed Holdings Inc.",
            "Common Carrier LLC",
        ):
            self.assertIn(identity, combined)

    def test_each_text_demonstrated_material_change_kind_survives(self):
        controlling = _hit(
            result_id="control", nyscef=401, page=1, doc_type="complaint",
            filename="pleading.pdf",
            excerpt="Alpha LLC, Plaintiff, v. Beta Inc., Defendant.",
        )
        exception_texts = {
            "addition": "Gamma LLC was added as a defendant.",
            "dismissal": "Gamma LLC was dismissed as a party.",
            "substitution": "Gamma LLC was substituted as defendant.",
            "amendment": "The amended complaint adds Gamma LLC.",
            "role": "Gamma LLC's party status is disputed.",
            "identity": "Gamma LLC was incorrectly named as Gamma Inc.",
            "capacity": "Gamma LLC appears in a representative capacity.",
            "joinder": "Gamma LLC was joined as a necessary party.",
        }
        exceptions = [
            _hit(result_id=key, nyscef=410 + index, page=1, doc_type="other",
                 filename="filing.pdf", excerpt=text)
            for index, (key, text) in enumerate(exception_texts.items())
        ]
        kept, _ = de.filter_hits_for_party_role_materiality([controlling] + exceptions)
        self.assertEqual(
            {hit["result_id"] for hit in kept}, {"control", *exception_texts.keys()}
        )

    def test_motion_questions_keep_motion_evidence(self):
        motion_hit = self.hits[1]
        retrieval = {
            "query": self.motion_question,
            "results": copy.deepcopy(
                [motion_hit, self.hits[0], self.hits[2], self.hits[5]]
            ),
        }
        packet = de.build_evidence_packet(self.motion_question, retrieval)
        page_ids = [hit["page_id"] for hit in packet["retrieval_hits"]]
        self.assertEqual(len(page_ids), 4)
        self.assertIn("nyscef-202-p1", page_ids)
        self.assertIn("nyscef-203-p1", page_ids)
        self.assertNotIn("materiality_filter", packet)

    def test_generated_propositions_remain_citation_grounded(self):
        packet = de.build_evidence_packet(self.party_question, self.retrieval)
        allowed_page_ids = {hit["page_id"] for hit in packet["retrieval_hits"]}
        pleading = next(
            hit for hit in packet["retrieval_hits"] if hit["page_id"] == "nyscef-201-p5"
        )
        payload = {
            "proposed_answer": (
                "Alpine Freight LP is plaintiff; Harbor Gate Carrier Inc. is defendant."
            ),
            "propositions": [
                {
                    "proposition_id": "P1",
                    "text": "Alpine Freight LP is identified as plaintiff.",
                    "classification": "verified_record_fact",
                    "nyscef_document_number": pleading["nyscef_document_number"],
                    "page_id": pleading["page_id"],
                    "pdf_page": pleading["pdf_page"],
                    "source_excerpt": (
                        "Plaintiff Alpine Freight LP is a limited liability partnership"
                    ),
                    "confidence": 0.91,
                    "rationale": "Party identity appears on the operative pleading page.",
                    "polarity": "supporting",
                },
                {
                    "proposition_id": "P2",
                    "text": "Hallucinated citation outside the filtered packet.",
                    "classification": "verified_record_fact",
                    "nyscef_document_number": 202,
                    "page_id": "nyscef-202-p1",
                    "pdf_page": 1,
                    "source_excerpt": "Notice of Motion for Summary Judgment",
                    "confidence": 0.4,
                    "rationale": "Should be removed if not in retrieval context used.",
                    "polarity": "supporting",
                },
            ],
            "supporting_evidence": [],
            "contrary_evidence": [],
            "unresolved_questions": [],
            "documents_pages_reviewed": [],
            "confidence": 0.9,
            "attorney_review": {
                "requires_attorney_review": True,
                "review_notes": "Confirm party roster.",
                "legal_conclusions_labeled": True,
                "coverage_conclusion": None,
            },
            "review_scope": {
                "completeness": "not_established",
                "qualification": "Limited to filtered party-role evidence.",
            },
        }

        # Validate against the filtered generation packet hits only.
        filtered_retrieval = {
            "query": self.party_question,
            "results": list(packet["retrieval_hits"]),
        }
        validated = de.validate_attorney_qa_response(
            payload,
            question=self.party_question,
            retrieval=filtered_retrieval,
        )
        kept_ids = {p["proposition_id"] for p in validated["propositions"]}
        self.assertEqual(kept_ids, {"P1"})
        self.assertTrue(
            all(p["page_id"] in allowed_page_ids for p in validated["propositions"])
        )
        removed_ids = {
            item["proposition_id"]
            for item in validated["audit"]["removed_propositions"]
        }
        self.assertIn("P2", removed_ids)

    def test_no_provisional_or_gold_in_generation_inputs(self):
        captured = {"calls": []}

        def _model(system_prompt, user_prompt):
            captured["calls"].append(
                {"system": system_prompt, "user": user_prompt}
            )
            packet = de.build_evidence_packet(self.party_question, self.retrieval)
            pleading = packet["retrieval_hits"][0]
            expected = de.extract_party_role_expected_attributes(packet)
            answer_bits = []
            for party in expected:
                bit = (
                    f"{party.get('procedural_role') or 'party'} "
                    f"{party.get('identity')}"
                ).strip()
                if party.get("entity_type"):
                    bit += f" is a {party['entity_type']}"
                if party.get("residence_or_ppb"):
                    bit += f"; {party['residence_or_ppb']}"
                if party.get("pleaded_role_basis"):
                    bit += f" ({party['pleaded_role_basis']})"
                answer_bits.append(bit + ".")
            return {
                "proposed_answer": " ".join(answer_bits) or "Parties identified.",
                "propositions": [
                    {
                        "proposition_id": "P1",
                        "text": answer_bits[0] if answer_bits else "Plaintiff identified.",
                        "classification": "verified_record_fact",
                        "nyscef_document_number": pleading["nyscef_document_number"],
                        "page_id": pleading["page_id"],
                        "pdf_page": pleading["pdf_page"],
                        "source_excerpt": pleading["excerpt"][:80],
                        "confidence": 0.8,
                        "rationale": "From filtered pleading hit.",
                        "polarity": "supporting",
                    }
                ],
                "supporting_evidence": [],
                "contrary_evidence": [],
                "unresolved_questions": [],
                "documents_pages_reviewed": [],
                "confidence": 0.8,
                "attorney_review": {
                    "requires_attorney_review": True,
                    "review_notes": "Review party roster.",
                    "legal_conclusions_labeled": True,
                    "coverage_conclusion": None,
                },
                "review_scope": {
                    "completeness": "not_established",
                    "qualification": "Filtered packet only.",
                },
            }

        result = de.answer_attorney_record_question(
            self.party_question,
            self.retrieval,
            model_call=_model,
        )
        self.assertEqual(result["status"], de.STATUS_READY)
        for call in captured["calls"]:
            blob = (call["system"] + "\n" + call["user"]).lower()
            self.assertNotIn("provisional_should_not_appear", blob)
            self.assertNotIn("gold_should_not_appear", blob)
            self.assertNotIn("provisional_answer", blob)
            self.assertNotIn("gold_answer", blob)
        first = captured["calls"][0]
        packet_json = first["user"].split("\n\n", 2)[1]
        user_packet = json.loads(packet_json)
        self.assertNotIn("provisional_answer", user_packet)
        self.assertNotIn("gold_answer", user_packet)
        self.assertIn("materially useful", first["system"].lower())
        self.assertIn("citation-grounded", first["system"].lower())

    def test_non_party_questions_preserve_unfiltered_packet(self):
        order_hit = self.hits[6]
        retrieval = {
            "query": "What did the scheduling order adjourn?",
            "results": [order_hit, self.hits[1]],
        }
        packet = de.build_evidence_packet(
            "What did the scheduling order adjourn?",
            retrieval,
        )
        self.assertEqual(packet["retrieval_hit_count"], 2)
        self.assertNotIn("materiality_filter", packet)
        self.assertEqual(
            [hit["page_id"] for hit in packet["retrieval_hits"]],
            ["nyscef-207-p1", "nyscef-202-p1"],
        )


class PartyRoleDraftingCompletenessTests(unittest.TestCase):
    """Focused synthetic regressions for party-role expected-attribute parsing."""

    def setUp(self):
        self.party_question = (
            "Who are the parties and what are their roles in this action?"
        )

    def _extract(self, excerpt: str, question: str = None):
        packet = {
            "question": question or self.party_question,
            "retrieval_hits": [{"excerpt": excerpt}],
        }
        return de.extract_party_role_expected_attributes(packet)

    def _by_identity(self, expected):
        return {
            de.normalize_citation_text(item["identity"]): item for item in expected
        }

    def test_corporation_with_principal_place_of_business(self):
        expected = self._extract(
            "1. Defendant Atlas Hauling Inc. was and still is a domestic "
            "corporation with its principal place of business at 100 Main "
            "Street, Albany, NY."
        )
        party = self._by_identity(expected)["atlas hauling inc"]
        self.assertEqual(party["procedural_role"], "defendant")
        self.assertEqual(party["entity_type"], "domestic corporation")
        self.assertIn("principal place of business", party["residence_or_ppb"].lower())
        self.assertIn("100 main street", party["residence_or_ppb"].lower())

    def test_individual_with_residence(self):
        expected = self._extract(
            "3. Victor Rodriguez is an individual residing at 12 Oak Lane, "
            "Buffalo, NY."
        )
        party = self._by_identity(expected)["victor rodriguez"]
        self.assertEqual(party["entity_type"], "individual")
        self.assertIn("residing at", party["residence_or_ppb"].lower())
        self.assertIn("12 oak lane", party["residence_or_ppb"].lower())

    def test_llc_with_principal_place_of_business(self):
        expected = self._extract(
            "4. XYZ LLC is a limited liability company with a principal place "
            "of business at 55 Commerce Blvd, Rochester, NY."
        )
        party = self._by_identity(expected)["xyz llc"]
        self.assertEqual(party["entity_type"], "limited liability company")
        self.assertIn("principal place of business", party["residence_or_ppb"].lower())
        self.assertIn("55 commerce blvd", party["residence_or_ppb"].lower())

    def test_ocr_fractured_corporation_and_company_wording(self):
        expected = self._extract(
            "1. Defendant Harbor Gate Carrier Inc. was and still is a "
            "domesti c corporation with its principal place of business at "
            "9 Pier Road, Queens, NY.\n"
            "2. Nimbus Freight LLC is a limited liability com pany with a "
            "principal place of business at 3 Depot Ave."
        )
        by_name = self._by_identity(expected)
        harbor = by_name["harbor gate carrier inc"]
        self.assertEqual(harbor["entity_type"], "domestic corporation")
        self.assertIn("principal place of business", harbor["residence_or_ppb"].lower())
        nimbus = by_name["nimbus freight llc"]
        self.assertEqual(nimbus["entity_type"], "limited liability company")
        self.assertIn("principal place of business", nimbus["residence_or_ppb"].lower())

    def test_role_before_name(self):
        expected = self._extract(
            "Defendant Summit Bridge Corp. is a domestic corporation."
        )
        party = self._by_identity(expected)["summit bridge corp"]
        self.assertEqual(party["procedural_role"], "defendant")
        self.assertEqual(party["entity_type"], "domestic corporation")

    def test_role_after_name(self):
        expected = self._extract(
            "Summit Bridge Corp. is a defendant and a domestic corporation."
        )
        party = self._by_identity(expected)["summit bridge corp"]
        self.assertEqual(party["procedural_role"], "defendant")
        self.assertEqual(party["entity_type"], "domestic corporation")

    def test_numbered_multiline_allegation(self):
        expected = self._extract(
            "1. Defendant Harbor Gate Carrier Inc. was and still is a\n"
            "domestic corporation with its principal place of business\n"
            "at 9 Pier Road, Queens, NY."
        )
        self.assertEqual(len(expected), 1)
        party = expected[0]
        self.assertEqual(party["identity"], "Harbor Gate Carrier Inc")
        self.assertEqual(party["procedural_role"], "defendant")
        self.assertEqual(party["entity_type"], "domestic corporation")
        self.assertIn("9 pier road", party["residence_or_ppb"].lower())

    def test_multiple_distinct_parties(self):
        expected = self._extract(
            "1. Plaintiff Cedar Ridge Logistics LLC is a domestic corporation.\n"
            "2. Defendant Pine Harbor Depot Inc. is a limited liability company.\n"
            "3. Victor Rodriguez is an individual residing at 12 Oak Lane."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 3)
        self.assertEqual(by_name["cedar ridge logistics llc"]["procedural_role"], "plaintiff")
        self.assertEqual(by_name["pine harbor depot inc"]["procedural_role"], "defendant")
        self.assertEqual(by_name["victor rodriguez"]["entity_type"], "individual")

    def test_grouped_notice_defendant_basis(self):
        expected = self._extract(
            "1. Defendant Atlas Hauling Inc. is a domestic corporation.\n"
            "2. Defendant Beta Logistics LLC is a limited liability company.\n"
            "3. The foregoing defendants are notice defendants because they "
            "were served with the notice of pendency."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 2)
        self.assertEqual(by_name["atlas hauling inc"]["pleaded_role_basis"], "notice defendant")
        self.assertEqual(by_name["beta logistics llc"]["pleaded_role_basis"], "notice defendant")
        self.assertNotIn("foregoing defendants", by_name)

    def test_no_cross_party_attribute_assignment(self):
        expected = self._extract(
            "1. Defendant Alpha Corp. is a domestic corporation with its "
            "principal place of business at 1 Main St.\n"
            "2. Defendant Beta LLC is a limited liability company."
        )
        by_name = self._by_identity(expected)
        alpha = by_name["alpha corp"]
        beta = by_name["beta llc"]
        self.assertEqual(alpha["entity_type"], "domestic corporation")
        self.assertIn("1 main st", alpha["residence_or_ppb"].lower())
        self.assertEqual(beta["entity_type"], "limited liability company")
        self.assertIsNone(beta["residence_or_ppb"])
        self.assertNotIn("1 main st", (beta.get("residence_or_ppb") or "").lower())

    def test_placeholder_identity_group(self):
        expected = self._extract(
            "5. The John Does 1-10 are placeholder defendants whose identities "
            "are presently unknown."
        )
        party = self._by_identity(expected)["john does 1-10"]
        self.assertEqual(party["procedural_role"], "defendant")
        self.assertIsNone(party.get("entity_type"))

    def test_multiword_organization_ending_inc(self):
        expected = self._extract(
            "1. Defendant North Star Shipping Lines Inc. was and still is a "
            "domestic corporation."
        )
        party = self._by_identity(expected)["north star shipping lines inc"]
        self.assertEqual(party["identity"], "North Star Shipping Lines Inc")
        self.assertEqual(party["procedural_role"], "defendant")
        self.assertEqual(party["entity_type"], "domestic corporation")

    def test_organization_followed_by_entity_type_language(self):
        expected = self._extract(
            "2. Plaintiff Cedar Basin Holdings LLC is a limited liability "
            "company organized under the laws of New York."
        )
        party = self._by_identity(expected)["cedar basin holdings llc"]
        self.assertEqual(party["identity"], "Cedar Basin Holdings LLC")
        self.assertEqual(party["procedural_role"], "plaintiff")
        self.assertEqual(party["entity_type"], "limited liability company")

    def test_slash_separated_john_jane_placeholder_with_numeric_range(self):
        expected = self._extract(
            "4. The John/Jane Does 1-10 are placeholder defendants whose "
            "identities are presently unknown."
        )
        self.assertEqual(len(expected), 1)
        party = expected[0]
        self.assertEqual(party["identity"], "John/Jane Does 1-10")
        self.assertEqual(party["procedural_role"], "defendant")
        self.assertNotIn("Jane Does 1-10", self._by_identity(expected))

    def test_slash_separated_ordinary_party_name(self):
        expected = self._extract(
            "5. Defendant Smith/Jones Partners LLP is a limited liability "
            "partnership."
        )
        party = self._by_identity(expected)["smith/jones partners llp"]
        self.assertEqual(party["identity"], "Smith/Jones Partners LLP")
        self.assertEqual(party["procedural_role"], "defendant")
        self.assertEqual(party["entity_type"], "limited liability partnership")
        self.assertNotIn("jones partners llp", self._by_identity(expected))

    def test_xyz_style_corporation_placeholder_with_numeric_range(self):
        expected = self._extract(
            "6. The XYZ CORPS. 1–5 are placeholder defendants."
        )
        self.assertEqual(len(expected), 1)
        party = expected[0]
        self.assertEqual(party["identity"], "XYZ CORPS. 1–5")
        self.assertEqual(party["procedural_role"], "defendant")

    def test_punctuation_inside_organization_name(self):
        expected = self._extract(
            "7. Defendant O'Brien-Marks Freight, Inc. is a domestic corporation."
        )
        party = self._by_identity(expected)["o'brien-marks freight, inc"]
        self.assertEqual(party["identity"], "O'Brien-Marks Freight, Inc")
        self.assertEqual(party["procedural_role"], "defendant")
        self.assertEqual(party["entity_type"], "domestic corporation")
        self.assertNotIn("inc", self._by_identity(expected))

    def test_no_trailing_allegation_text_capture(self):
        expected = self._extract(
            "1. Defendant Atlas Hauling Inc. was and still is a domestic "
            "corporation and denies each and every allegation herein."
        )
        self.assertEqual(len(expected), 1)
        party = expected[0]
        self.assertEqual(party["identity"], "Atlas Hauling Inc")
        self.assertNotIn("denies", party["identity"].lower())
        self.assertNotIn("allegation", party["identity"].lower())
        self.assertEqual(party["entity_type"], "domestic corporation")

    def test_no_adjacent_party_merging(self):
        expected = self._extract(
            "1. Defendant Alpha Corp. is a domestic corporation.\n"
            "2. Defendant Beta LLC is a limited liability company."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 2)
        self.assertEqual(by_name["alpha corp"]["identity"], "Alpha Corp")
        self.assertEqual(by_name["beta llc"]["identity"], "Beta LLC")
        identities = [item["identity"] for item in expected]
        self.assertTrue(all("Beta" not in name for name in identities if "Alpha" in name))
        self.assertTrue(all("Alpha" not in name for name in identities if "Beta" in name))

    def test_non_party_isolation_preserved(self):
        expected = self._extract(
            "Notice of Motion for Summary Judgment returnable June 1, 2024. "
            "Movant seeks dismissal of the complaint. The contract was signed "
            "in Albany without assigning procedural roles.",
            question="What relief does the notice of motion seek?",
        )
        self.assertEqual(expected, [])

    def test_corporation_with_quoted_parenthetical_alias(self):
        expected = self._extract(
            '1. Defendant Acme Shipping Corporation ("Acme") is a domestic '
            "corporation with its principal place of business at 100 Main "
            "Street, Albany, NY."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        party = by_name["acme shipping corporation"]
        self.assertEqual(party["identity"], "Acme Shipping Corporation")
        self.assertEqual(party["procedural_role"], "defendant")
        self.assertEqual(party["entity_type"], "domestic corporation")
        self.assertNotIn("acme", by_name)

    def test_llc_with_the_company_alias(self):
        expected = self._extract(
            '2. Plaintiff Harbor Logistics LLC ("the Company") is a limited '
            "liability company with a principal place of business at "
            "55 Commerce Blvd, Rochester, NY."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        party = by_name["harbor logistics llc"]
        self.assertEqual(party["identity"], "Harbor Logistics LLC")
        self.assertEqual(party["procedural_role"], "plaintiff")
        self.assertEqual(party["entity_type"], "limited liability company")
        self.assertNotIn("the company", by_name)
        self.assertNotIn("company", by_name)

    def test_individual_with_surname_alias(self):
        expected = self._extract(
            '3. Defendant Victor Rodriguez ("Rodriguez") is an individual '
            "residing at 12 Oak Lane, Buffalo, NY."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        party = by_name["victor rodriguez"]
        self.assertEqual(party["identity"], "Victor Rodriguez")
        self.assertEqual(party["procedural_role"], "defendant")
        self.assertEqual(party["entity_type"], "individual")
        self.assertIn("12 oak lane", party["residence_or_ppb"].lower())
        self.assertNotIn("rodriguez", by_name)

    def test_later_shorthand_maps_to_canonical(self):
        expected = self._extract(
            '1. Defendant Acme Shipping Corporation ("Acme") is a domestic '
            "corporation.\n"
            "2. Acme is a defendant that received notice of the action."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        self.assertEqual(
            by_name["acme shipping corporation"]["identity"],
            "Acme Shipping Corporation",
        )
        self.assertNotIn("acme", by_name)

    def test_alias_not_emitted_as_separate_identity(self):
        expected = self._extract(
            '1. Defendant Acme Shipping Corporation ("Acme") is a domestic '
            "corporation.\n"
            '2. Plaintiff Harbor Logistics LLC ("the Company") is a limited '
            "liability company.\n"
            "3. Acme denies the Harbor Logistics LLC allegations."
        )
        identities = [item["identity"] for item in expected]
        self.assertEqual(
            sorted(identities),
            ["Acme Shipping Corporation", "Harbor Logistics LLC"],
        )
        by_name = self._by_identity(expected)
        self.assertNotIn("acme", by_name)
        self.assertNotIn("the company", by_name)

    def test_full_plaintiff_collective_underwriters_shorthand(self):
        expected = self._extract(
            '1. Plaintiff Certain Underwriters at Lloyd\'s of London '
            '("Underwriters") are associations.\n'
            "2. Underwriters are plaintiffs that issued the subject policy."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        party = by_name["certain underwriters at lloyd's of london"]
        self.assertEqual(
            party["identity"], "Certain Underwriters at Lloyd's of London"
        )
        self.assertEqual(party["procedural_role"], "plaintiff")
        self.assertEqual(party["entity_type"], "association")
        self.assertNotIn("underwriters", by_name)

    def test_corporation_one_word_alias_consolidation(self):
        expected = self._extract(
            '1. Defendant Full Corporate Name, Inc. ("Short") is a domestic '
            "corporation.\n"
            "2. Short is a defendant that received notice of the action."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        self.assertEqual(
            by_name["full corporate name, inc"]["identity"],
            "Full Corporate Name, Inc",
        )
        self.assertNotIn("short", by_name)

    def test_llc_shortened_alias_consolidation(self):
        expected = self._extract(
            '1. Plaintiff Full LLC Name ("Harbor") is a limited liability '
            "company.\n"
            "2. Harbor is a plaintiff organized under New York law."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        self.assertEqual(by_name["full llc name"]["identity"], "Full LLC Name")
        self.assertEqual(by_name["full llc name"]["procedural_role"], "plaintiff")
        self.assertNotIn("harbor", by_name)

    def test_later_alias_only_allegation_maps_to_canonical(self):
        expected = self._extract(
            '1. Defendant Acme Shipping Corporation ("Acme") is a domestic '
            "corporation.\n"
            "2. Acme is a defendant that received notice of the action."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        self.assertEqual(
            by_name["acme shipping corporation"]["identity"],
            "Acme Shipping Corporation",
        )
        self.assertNotIn("acme", by_name)

    def test_alias_only_attributes_merge_into_canonical(self):
        expected = self._extract(
            '1. Defendant Acme Shipping Corporation ("Acme") is a domestic '
            "corporation.\n"
            "2. Acme has its principal place of business at 100 Main Street, "
            "Albany, NY."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        party = by_name["acme shipping corporation"]
        self.assertEqual(party["identity"], "Acme Shipping Corporation")
        self.assertEqual(party["entity_type"], "domestic corporation")
        self.assertIn("principal place of business", party["residence_or_ppb"].lower())
        self.assertIn("100 main street", party["residence_or_ppb"].lower())
        self.assertNotIn("acme", by_name)

    def test_unrelated_similar_token_does_not_merge(self):
        expected = self._extract(
            '1. Defendant Acme Shipping Corporation ("Acme") is a domestic '
            "corporation.\n"
            "2. Defendant North Acme Holdings LLC is a limited liability "
            "company with its principal place of business at 9 Pier Road.\n"
            "3. North Acme Holdings LLC has its principal place of business "
            "at 9 Pier Road, Queens, NY."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 2)
        acme = by_name["acme shipping corporation"]
        north = by_name["north acme holdings llc"]
        self.assertEqual(acme["identity"], "Acme Shipping Corporation")
        self.assertEqual(north["identity"], "North Acme Holdings LLC")
        self.assertIsNone(acme.get("residence_or_ppb"))
        self.assertIn("9 pier road", north["residence_or_ppb"].lower())
        self.assertNotIn("acme", by_name)

    def test_canonical_identity_preserved_when_only_later_shorthand_appears(self):
        expected = self._extract(
            '1. Plaintiff Harbor Logistics LLC ("the Company") is a limited '
            "liability company.\n"
            "2. the Company has a principal place of business at "
            "55 Commerce Blvd, Rochester, NY."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        party = by_name["harbor logistics llc"]
        self.assertEqual(party["identity"], "Harbor Logistics LLC")
        self.assertIn("55 commerce blvd", party["residence_or_ppb"].lower())
        self.assertNotIn("the company", by_name)
        self.assertNotIn("company", by_name)

    def test_slash_individual_placeholder_preserved(self):
        expected = self._extract(
            "4. The John/Jane Does 1-10 are placeholder defendants whose "
            "identities are presently unknown."
        )
        self.assertEqual(len(expected), 1)
        party = expected[0]
        self.assertEqual(party["identity"], "John/Jane Does 1-10")
        self.assertEqual(party["procedural_role"], "defendant")
        by_name = self._by_identity(expected)
        self.assertNotIn("jane does 1-10", by_name)
        self.assertNotIn("john does 1-10", by_name)

    def test_organization_placeholder_numeric_range_preserved(self):
        expected = self._extract(
            "6. The XYZ CORPS. 1–5 are placeholder defendants."
        )
        self.assertEqual(len(expected), 1)
        party = expected[0]
        self.assertEqual(party["identity"], "XYZ CORPS. 1–5")
        self.assertIn("1–5", party["identity"])
        self.assertEqual(party["procedural_role"], "defendant")

    def test_hyphen_en_dash_comparison_normalization(self):
        expected = self._extract(
            "6. The XYZ CORPS. 1–5 are placeholder defendants."
        )
        party = expected[0]
        self.assertEqual(party["identity"], "XYZ CORPS. 1–5")
        self.assertTrue(
            de._party_role_attribute_present(
                party["identity"],
                de.normalize_citation_text("Caption lists xyz corps. 1-5."),
            )
        )
        self.assertEqual(
            de._normalize_party_role_match_text("XYZ CORPS. 1–5"),
            de._normalize_party_role_match_text("xyz corps. 1-5"),
        )

    def test_case_tolerant_canonical_comparison(self):
        expected = self._extract(
            "4. The John/Jane Does 1-10 are placeholder defendants."
        )
        party = expected[0]
        self.assertEqual(party["identity"], "John/Jane Does 1-10")
        self.assertTrue(
            de._party_role_attribute_present(
                party["identity"],
                de.normalize_citation_text(
                    "The pleading names john/jane does 1-10 as placeholders."
                ),
            )
        )

    def test_ocr_fractured_needle_matches_clean_residence_haystack(self):
        """Fractured expected residence/PPB fragments must match clean drafts."""
        clean_draft = de.normalize_citation_text(
            "Defendant maintains its principal place of business at "
            "35-06 Union Street, Queens, New York."
        )
        self.assertTrue(de._party_role_attribute_present("35- 06", clean_draft))
        self.assertTrue(de._party_role_attribute_present("U nion", clean_draft))
        self.assertTrue(
            de._party_role_attribute_present("35- 06 U nion", clean_draft)
        )
        self.assertTrue(
            de._ocr_flexible_phrase_present(
                "35- 06",
                de._normalize_party_role_match_text(clean_draft),
            )
        )
        self.assertTrue(
            de._ocr_flexible_phrase_present(
                "U nion",
                de._normalize_party_role_match_text(clean_draft),
            )
        )

    def test_ocr_fractured_full_ppb_address_with_commas(self):
        """Full OCR-fractured PPB/address needles must tolerate comma separators."""
        clean_draft = de.normalize_citation_text(
            "Defendant maintains its principal place of business at "
            "35-06 Union Street, Queens, New York."
        )
        hay = de._normalize_party_role_match_text(clean_draft)
        fractured_full = "35- 06 U nion Street, Queens, New York"
        fractured_no_comma_needle = "35- 06 U nion Street Queens New York"
        self.assertTrue(de._ocr_flexible_phrase_present(fractured_full, hay))
        self.assertTrue(
            de._ocr_flexible_phrase_present(fractured_no_comma_needle, hay)
        )
        self.assertTrue(
            de._party_role_attribute_present(fractured_full, clean_draft)
        )
        self.assertTrue(
            de._party_role_attribute_present(fractured_no_comma_needle, clean_draft)
        )
        # Exact-match path still works for clean full addresses.
        self.assertTrue(
            de._party_role_attribute_present(
                "35-06 Union Street, Queens, New York",
                clean_draft,
            )
        )

    def test_parenthetical_alias_no_trailing_text_capture(self):
        expected = self._extract(
            '1. Defendant Acme Shipping Corporation ("Acme") was and still is '
            "a domestic corporation and denies each and every allegation herein."
        )
        self.assertEqual(len(expected), 1)
        party = expected[0]
        self.assertEqual(party["identity"], "Acme Shipping Corporation")
        self.assertNotIn("denies", party["identity"].lower())
        self.assertNotIn("allegation", party["identity"].lower())
        self.assertNotIn("acme)", party["identity"].lower())

    def test_parenthetical_alias_no_adjacent_party_merging(self):
        expected = self._extract(
            '1. Defendant Alpha Corp. ("Alpha") is a domestic corporation.\n'
            '2. Defendant Beta LLC ("Beta") is a limited liability company.'
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 2)
        self.assertEqual(by_name["alpha corp"]["identity"], "Alpha Corp")
        self.assertEqual(by_name["beta llc"]["identity"], "Beta LLC")
        identities = [item["identity"] for item in expected]
        self.assertTrue(
            all("Beta" not in name for name in identities if "Alpha" in name)
        )
        self.assertTrue(
            all("Alpha" not in name for name in identities if "Beta" in name)
        )

    def test_caption_boundary_x_horizontal_rule_plaintiff(self):
        expected = self._extract(
            "-----------------------------------X\n"
            "PARTY NAME,\n"
            "Plaintiff"
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        party = by_name["party name"]
        self.assertEqual(party["identity"], "PARTY NAME")
        self.assertEqual(party["procedural_role"], "plaintiff")
        self.assertNotIn("x party name", by_name)

    def test_caption_boundary_x_ocr_spaced_role(self):
        expected = self._extract(
            "-----------------------------------X\n"
            "ACME CORP,\n"
            "Pla intiff"
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        party = by_name["acme corp"]
        self.assertEqual(party["identity"], "ACME CORP")
        self.assertEqual(party["procedural_role"], "plaintiff")
        self.assertNotIn("x acme corp", by_name)

    def test_caption_without_boundary_x(self):
        expected = self._extract(
            "PARTY NAME,\n"
            "Plaintiff"
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        party = by_name["party name"]
        self.assertEqual(party["identity"], "PARTY NAME")
        self.assertEqual(party["procedural_role"], "plaintiff")

    def test_legitimate_ordinary_x_leading_identity(self):
        expected = self._extract(
            "1. Defendant X Freight LLC is a domestic corporation."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        party = by_name["x freight llc"]
        self.assertEqual(party["identity"], "X Freight LLC")
        self.assertEqual(party["procedural_role"], "defendant")
        self.assertEqual(party["entity_type"], "domestic corporation")

    def test_caption_boundary_x_no_duplicate_after_normalization(self):
        expected = self._extract(
            "-----------------------------------X\n"
            "PARTY NAME,\n"
            "Plaintiff\n"
            "\n"
            "1. Plaintiff PARTY NAME is a domestic corporation."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        party = by_name["party name"]
        self.assertEqual(party["identity"], "PARTY NAME")
        self.assertEqual(party["procedural_role"], "plaintiff")
        self.assertEqual(party["entity_type"], "domestic corporation")
        self.assertNotIn("x party name", by_name)

    def test_digit_leading_llc_later_shorthand_consolidates(self):
        expected = self._extract(
            "1. Defendant 123 Freight LLC is a limited liability company.\n"
            "2. Freight is a defendant that received notice of the action."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        party = by_name["123 freight llc"]
        self.assertEqual(party["identity"], "123 Freight LLC")
        self.assertEqual(party["procedural_role"], "defendant")
        self.assertEqual(party["entity_type"], "limited liability company")
        self.assertNotIn("freight", by_name)

    def test_alias_bucket_before_canonical_definition_rekeys(self):
        expected = self._extract(
            "1. Acme is a defendant with its principal place of business at "
            "100 Main Street, Albany, NY.\n"
            '2. Defendant Acme Shipping Corporation ("Acme") is a domestic '
            "corporation."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        party = by_name["acme shipping corporation"]
        self.assertEqual(party["identity"], "Acme Shipping Corporation")
        self.assertEqual(party["procedural_role"], "defendant")
        self.assertEqual(party["entity_type"], "domestic corporation")
        self.assertIn("principal place of business", party["residence_or_ppb"].lower())
        self.assertIn("100 main street", party["residence_or_ppb"].lower())
        self.assertNotIn("acme", by_name)

    def test_alias_rekey_to_canonical_removes_standalone_alias(self):
        expected = self._extract(
            "1. Short is a plaintiff organized under New York law.\n"
            '2. Plaintiff Full LLC Name ("Short") is a limited liability company.'
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        self.assertEqual(by_name["full llc name"]["identity"], "Full LLC Name")
        self.assertEqual(by_name["full llc name"]["procedural_role"], "plaintiff")
        self.assertNotIn("short", by_name)

    def test_attributes_preserved_during_alias_rekey(self):
        expected = self._extract(
            "1. Harbor is a plaintiff with its principal place of business at "
            "55 Commerce Blvd, Rochester, NY.\n"
            '2. Plaintiff Harbor Logistics LLC ("Harbor") is a limited liability '
            "company."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        party = by_name["harbor logistics llc"]
        self.assertEqual(party["identity"], "Harbor Logistics LLC")
        self.assertEqual(party["procedural_role"], "plaintiff")
        self.assertEqual(party["entity_type"], "limited liability company")
        self.assertIn("55 commerce blvd", party["residence_or_ppb"].lower())
        self.assertNotIn("harbor", by_name)

    def test_full_plaintiff_caption_collective_shorthand_consolidates(self):
        expected = self._extract(
            "Certain Underwriters at Lloyd's of London,\n"
            "Plaintiff\n"
            "\n"
            "1. Underwriters are associations that issued the subject policy."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        party = by_name["certain underwriters at lloyd's of london"]
        self.assertEqual(
            party["identity"], "Certain Underwriters at Lloyd's of London"
        )
        self.assertEqual(party["procedural_role"], "plaintiff")
        self.assertEqual(party["entity_type"], "association")
        self.assertNotIn("underwriters", by_name)

    def test_full_defendant_caption_shortened_company_reference(self):
        expected = self._extract(
            "PARTY NAME,\n"
            "Plaintiff,\n"
            "                 -against-\n"
            "Full Corporate Name, Inc.,\n"
            "Defendant\n"
            "\n"
            "1. Full Corporate Name, Inc. is a domestic corporation.\n"
            "2. Corporate Name is a defendant that received notice of the action."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 2)
        defendant = by_name["full corporate name, inc"]
        self.assertEqual(defendant["identity"], "Full Corporate Name, Inc")
        self.assertEqual(defendant["procedural_role"], "defendant")
        self.assertEqual(defendant["entity_type"], "domestic corporation")
        self.assertEqual(by_name["party name"]["procedural_role"], "plaintiff")
        self.assertNotIn("corporate name", by_name)
        self.assertNotIn("inc", by_name)

    def test_multiline_against_defendant_caption_lists(self):
        expected = self._extract(
            "Alpha Holdings LLC,\n"
            "and\n"
            "Beta Logistics Inc.,\n"
            "Plaintiffs,\n"
            "                 -against-\n"
            "Gamma Corp.,\n"
            "and\n"
            "Delta Freight LLC,\n"
            "Defendants."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 4)
        self.assertEqual(by_name["alpha holdings llc"]["procedural_role"], "plaintiff")
        self.assertEqual(by_name["beta logistics inc"]["procedural_role"], "plaintiff")
        self.assertEqual(by_name["gamma corp"]["procedural_role"], "defendant")
        self.assertEqual(by_name["delta freight llc"]["procedural_role"], "defendant")

    def test_ocr_spaced_plaintiff_defendant_caption_roles(self):
        expected = self._extract(
            "PARTY NAME,\n"
            "Pla intiff,\n"
            "                 -against-\n"
            "OTHER PARTY,\n"
            "Defen dant"
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 2)
        self.assertEqual(by_name["party name"]["identity"], "PARTY NAME")
        self.assertEqual(by_name["party name"]["procedural_role"], "plaintiff")
        self.assertEqual(by_name["other party"]["identity"], "OTHER PARTY")
        self.assertEqual(by_name["other party"]["procedural_role"], "defendant")

    def test_ambiguous_shorthand_does_not_merge(self):
        expected = self._extract(
            "Alpha Harbor LLC,\n"
            "Plaintiff,\n"
            "                 -against-\n"
            "Beta Harbor Inc.,\n"
            "Defendant\n"
            "\n"
            "1. Harbor is a plaintiff that filed the complaint."
        )
        by_name = self._by_identity(expected)
        self.assertIn("alpha harbor llc", by_name)
        self.assertIn("beta harbor inc", by_name)
        self.assertEqual(by_name["alpha harbor llc"]["procedural_role"], "plaintiff")
        self.assertEqual(by_name["beta harbor inc"]["procedural_role"], "defendant")
        # Shared token alone is ambiguous; do not collapse into either party.
        self.assertEqual(len(by_name), 3)
        self.assertIn("harbor", by_name)

    def test_no_standalone_alias_after_caption_shorthand_consolidation(self):
        expected = self._extract(
            "Certain Underwriters at Lloyd's of London,\n"
            "Plaintiff,\n"
            "                 -against-\n"
            "Full Corporate Name, Inc.,\n"
            "Defendant\n"
            "\n"
            "1. Underwriters are associations.\n"
            "2. Corporate Name is a domestic corporation."
        )
        by_name = self._by_identity(expected)
        identities = sorted(item["identity"] for item in expected)
        self.assertEqual(
            identities,
            [
                "Certain Underwriters at Lloyd's of London",
                "Full Corporate Name, Inc",
            ],
        )
        self.assertNotIn("underwriters", by_name)
        self.assertNotIn("corporate name", by_name)
        self.assertEqual(
            by_name["certain underwriters at lloyd's of london"]["entity_type"],
            "association",
        )
        self.assertEqual(
            by_name["full corporate name, inc"]["entity_type"],
            "domestic corporation",
        )

    def test_court_header_preserves_full_plaintiff_identity(self):
        expected = self._extract(
            "SUPREME COURT OF THE STATE OF NEW YORK\n"
            "-----------------------------------X\n"
            "Certain Underwriters at Lloyd's of London,\n"
            "Plaintiff,\n"
            "                 -against-\n"
            "Full Corporate Name, Inc.,\n"
            "Defendant"
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 2)
        party = by_name["certain underwriters at lloyd's of london"]
        self.assertEqual(
            party["identity"], "Certain Underwriters at Lloyd's of London"
        )
        self.assertEqual(party["procedural_role"], "plaintiff")
        self.assertNotIn("supreme court", by_name)
        self.assertEqual(
            by_name["full corporate name, inc"]["procedural_role"], "defendant"
        )

    def test_county_venue_header_preserves_full_plaintiff_identity(self):
        expected = self._extract(
            "COUNTY OF QUEENS\n"
            "Venue: Kings County\n"
            "-----------------------------------X\n"
            "Alpha Harbor Logistics LLC,\n"
            "Plaintiff,\n"
            "                 -against-\n"
            "Beta Carrier Inc.,\n"
            "Defendant"
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 2)
        party = by_name["alpha harbor logistics llc"]
        self.assertEqual(party["identity"], "Alpha Harbor Logistics LLC")
        self.assertEqual(party["procedural_role"], "plaintiff")
        self.assertNotIn("county of queens", by_name)
        self.assertNotIn("kings county", by_name)
        self.assertEqual(by_name["beta carrier inc"]["procedural_role"], "defendant")

    def test_index_number_header_stripped_before_plaintiff_parse(self):
        expected = self._extract(
            "Index No. 712345/2020\n"
            "-----------------------------------X\n"
            "Harbor Logistics LLC,\n"
            "Plaintiff,\n"
            "                 -against-\n"
            "Summit Bridge Corp.,\n"
            "Defendant"
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 2)
        party = by_name["harbor logistics llc"]
        self.assertEqual(party["identity"], "Harbor Logistics LLC")
        self.assertEqual(party["procedural_role"], "plaintiff")
        self.assertTrue(
            all("712345" not in (item["identity"] or "") for item in expected)
        )
        self.assertEqual(by_name["summit bridge corp"]["procedural_role"], "defendant")

    def test_multiline_caption_admin_headers_preserve_full_plaintiff(self):
        expected = self._extract(
            "SUPREME COURT OF THE STATE OF NEW YORK\n"
            "COUNTY OF QUEENS\n"
            "IAS Part 12\n"
            "Index No. 712345/2020\n"
            "-----------------------------------X\n"
            "Certain Underwriters at Lloyd's of London,\n"
            "Plaintiff,\n"
            "                 -against-\n"
            "Full Corporate Name, Inc.,\n"
            "Defendant"
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 2)
        party = by_name["certain underwriters at lloyd's of london"]
        self.assertEqual(
            party["identity"], "Certain Underwriters at Lloyd's of London"
        )
        self.assertEqual(party["procedural_role"], "plaintiff")
        self.assertEqual(
            by_name["full corporate name, inc"]["identity"], "Full Corporate Name, Inc"
        )
        self.assertEqual(
            by_name["full corporate name, inc"]["procedural_role"], "defendant"
        )

    def test_caption_header_with_ocr_spaced_plaintiff_role(self):
        expected = self._extract(
            "SUPREME COURT OF THE STATE OF NEW YORK\n"
            "COUNTY OF QUEENS\n"
            "-----------------------------------X\n"
            "PARTY NAME,\n"
            "Pla intiff,\n"
            "                 -against-\n"
            "OTHER PARTY,\n"
            "Defen dant"
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 2)
        self.assertEqual(by_name["party name"]["identity"], "PARTY NAME")
        self.assertEqual(by_name["party name"]["procedural_role"], "plaintiff")
        self.assertEqual(by_name["other party"]["identity"], "OTHER PARTY")
        self.assertEqual(by_name["other party"]["procedural_role"], "defendant")

    def test_legitimate_party_name_with_geographic_word_preserved(self):
        expected = self._extract(
            "Queens Harbor Freight LLC,\n"
            "Plaintiff,\n"
            "                 -against-\n"
            "County Logistics Inc.,\n"
            "Defendant"
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 2)
        self.assertEqual(
            by_name["queens harbor freight llc"]["identity"],
            "Queens Harbor Freight LLC",
        )
        self.assertEqual(
            by_name["queens harbor freight llc"]["procedural_role"], "plaintiff"
        )
        self.assertEqual(
            by_name["county logistics inc"]["identity"], "County Logistics Inc"
        )
        self.assertEqual(
            by_name["county logistics inc"]["procedural_role"], "defendant"
        )

    def test_caption_headers_later_shorthand_consolidates_to_full_plaintiff(self):
        expected = self._extract(
            "SUPREME COURT OF THE STATE OF NEW YORK\n"
            "COUNTY OF QUEENS\n"
            "Index No. 712345/2020\n"
            "-----------------------------------X\n"
            "Certain Underwriters at Lloyd's of London,\n"
            "Plaintiff,\n"
            "                 -against-\n"
            "Full Corporate Name, Inc.,\n"
            "Defendant\n"
            "\n"
            "1. Underwriters are associations that issued the subject policy.\n"
            "2. Corporate Name is a domestic corporation."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 2)
        party = by_name["certain underwriters at lloyd's of london"]
        self.assertEqual(
            party["identity"], "Certain Underwriters at Lloyd's of London"
        )
        self.assertEqual(party["procedural_role"], "plaintiff")
        self.assertEqual(party["entity_type"], "association")
        self.assertNotIn("underwriters", by_name)
        self.assertNotIn("corporate name", by_name)
        self.assertEqual(
            by_name["full corporate name, inc"]["procedural_role"], "defendant"
        )
        identities = [item["identity"] for item in expected]
        self.assertTrue(
            all(
                not re.search(r"(?i)\bsupreme\s+court\b|\bcounty\s+of\b|\bindex\s+no", name)
                for name in identities
            )
        )

    def test_ocr_split_legal_suffix_in_c_healed_to_inc(self):
        expected = self._extract(
            "1. Defendant Harbor Bulkheading IN C. is a domestic corporation."
        )
        by_name = self._by_identity(expected)
        self.assertIn("harbor bulkheading inc", by_name)
        self.assertNotIn("harbor bulkheading in c", by_name)
        party = by_name["harbor bulkheading inc"]
        self.assertEqual(party["identity"], "Harbor Bulkheading INC")
        self.assertEqual(party["procedural_role"], "defendant")
        self.assertEqual(party["entity_type"], "domestic corporation")

    def test_ocr_split_surname_co_llins_healed_to_collins(self):
        expected = self._extract(
            "1. Defendant CO LLINS is an individual residing at "
            "12 Oak Lane, Buffalo, NY."
        )
        by_name = self._by_identity(expected)
        self.assertIn("collins", by_name)
        self.assertNotIn("co llins", by_name)
        party = by_name["collins"]
        self.assertEqual(party["identity"], "COLLINS")
        self.assertEqual(party["entity_type"], "individual")
        self.assertIn("12 oak lane", party["residence_or_ppb"].lower())

    def test_ocr_multiple_fractures_in_one_identity(self):
        expected = self._extract(
            "1. Plaintiff CO LLINS Freight IN C. is a domestic corporation."
        )
        by_name = self._by_identity(expected)
        self.assertIn("collins freight inc", by_name)
        self.assertEqual(len(by_name), 1)
        party = by_name["collins freight inc"]
        self.assertEqual(party["identity"], "COLLINS Freight INC")
        self.assertEqual(party["procedural_role"], "plaintiff")
        self.assertEqual(party["entity_type"], "domestic corporation")

    def test_ocr_clean_identity_unchanged(self):
        expected = self._extract(
            "1. Defendant Atlas Hauling Inc. is a domestic corporation."
        )
        by_name = self._by_identity(expected)
        party = by_name["atlas hauling inc"]
        self.assertEqual(party["identity"], "Atlas Hauling Inc")
        self.assertEqual(party["procedural_role"], "defendant")

    def test_ocr_legitimate_multiword_name_stays_separated(self):
        expected = self._extract(
            "1. Plaintiff John Smith is an individual residing at "
            "9 Pier Road, Queens, NY."
        )
        by_name = self._by_identity(expected)
        self.assertIn("john smith", by_name)
        self.assertNotIn("johnsmith", by_name)
        party = by_name["john smith"]
        self.assertEqual(party["identity"], "John Smith")
        self.assertEqual(party["entity_type"], "individual")

    def test_ocr_healed_and_unhealed_identity_buckets_merge(self):
        expected = self._extract(
            "1. Defendant Harbor Bulkheading IN C. is a domestic corporation.\n"
            "2. Harbor Bulkheading Inc. has its principal place of business at "
            "100 Main Street, Albany, NY."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        self.assertIn("harbor bulkheading inc", by_name)
        self.assertNotIn("harbor bulkheading in c", by_name)
        party = by_name["harbor bulkheading inc"]
        self.assertEqual(party["procedural_role"], "defendant")
        self.assertEqual(party["entity_type"], "domestic corporation")
        self.assertIn("principal place of business", party["residence_or_ppb"].lower())
        self.assertIn("100 main street", party["residence_or_ppb"].lower())

    def test_ocr_merge_preserves_party_attributes(self):
        expected = self._extract(
            "1. CO LLINS is a domestic corporation.\n"
            "2. Defendant COLLINS has its principal place of business at "
            "55 Commerce Blvd, Rochester, NY and is a notice defendant."
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 1)
        party = by_name["collins"]
        self.assertEqual(party["identity"], "COLLINS")
        self.assertEqual(party["procedural_role"], "defendant")
        self.assertEqual(party["entity_type"], "domestic corporation")
        self.assertIn("55 commerce blvd", party["residence_or_ppb"].lower())
        self.assertEqual(party["pleaded_role_basis"], "notice defendant")

    def test_leading_folio_before_caption_header_preserves_plaintiff(self):
        expected = self._extract(
            "12\n"
            "SUPREME COURT OF THE STATE OF NEW YORK\n"
            "COUNTY OF QUEENS\n"
            "-----------------------------------X\n"
            "Certain Underwriters at Lloyd's of London,\n"
            "Plaintiff,\n"
            "                 -against-\n"
            "Full Corporate Name, Inc.,\n"
            "Defendant"
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 2)
        party = by_name["certain underwriters at lloyd's of london"]
        self.assertEqual(
            party["identity"], "Certain Underwriters at Lloyd's of London"
        )
        self.assertEqual(party["procedural_role"], "plaintiff")
        self.assertTrue(
            all(not re.match(r"^\d+$", (item["identity"] or "").strip()) for item in expected)
        )
        self.assertEqual(
            by_name["full corporate name, inc"]["procedural_role"], "defendant"
        )

    def test_same_line_folio_before_caption_header_preserves_plaintiff(self):
        expected = self._extract(
            "12 SUPREME COURT OF THE STATE OF NEW YORK\n"
            "COUNTY OF QUEENS\n"
            "-----------------------------------X\n"
            "Harbor Logistics LLC,\n"
            "Plaintiff,\n"
            "                 -against-\n"
            "Summit Bridge Corp.,\n"
            "Defendant"
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 2)
        party = by_name["harbor logistics llc"]
        self.assertEqual(party["identity"], "Harbor Logistics LLC")
        self.assertEqual(party["procedural_role"], "plaintiff")
        self.assertEqual(by_name["summit bridge corp"]["procedural_role"], "defendant")

    def test_caption_without_folio_still_extracts_full_plaintiff(self):
        expected = self._extract(
            "SUPREME COURT OF THE STATE OF NEW YORK\n"
            "COUNTY OF QUEENS\n"
            "-----------------------------------X\n"
            "Certain Underwriters at Lloyd's of London,\n"
            "Plaintiff,\n"
            "                 -against-\n"
            "Full Corporate Name, Inc.,\n"
            "Defendant"
        )
        by_name = self._by_identity(expected)
        self.assertEqual(len(by_name), 2)
        party = by_name["certain underwriters at lloyd's of london"]
        self.assertEqual(
            party["identity"], "Certain Underwriters at Lloyd's of London"
        )
        self.assertEqual(party["procedural_role"], "plaintiff")

    def test_roman_ii_llc_not_joined_by_prefix_fracture_healing(self):
        expected = self._extract(
            "1. Defendant II LLC is a limited liability company with a "
            "principal place of business at 55 Commerce Blvd, Rochester, NY."
        )
        by_name = self._by_identity(expected)
        self.assertIn("ii llc", by_name)
        self.assertNotIn("iillc", by_name)
        party = by_name["ii llc"]
        self.assertEqual(party["identity"], "II LLC")
        self.assertEqual(party["entity_type"], "limited liability company")

    def test_roman_iii_llc_preserved(self):
        expected = self._extract(
            "1. Plaintiff III LLC is a limited liability company."
        )
        by_name = self._by_identity(expected)
        self.assertIn("iii llc", by_name)
        self.assertNotIn("iiillc", by_name)
        party = by_name["iii llc"]
        self.assertEqual(party["identity"], "III LLC")
        self.assertEqual(party["procedural_role"], "plaintiff")

    def test_roman_ii_corporation_not_over_joined(self):
        expected = self._extract(
            "1. Defendant II Corporation is a domestic corporation."
        )
        by_name = self._by_identity(expected)
        self.assertIn("ii corporation", by_name)
        self.assertNotIn("iicorporation", by_name)
        party = by_name["ii corporation"]
        self.assertEqual(party["identity"], "II Corporation")
        self.assertEqual(party["entity_type"], "domestic corporation")

    def test_ordinary_ocr_prefix_fracture_healing_unchanged(self):
        expected = self._extract(
            "1. Defendant CO LLINS Freight IN C. is a domestic corporation."
        )
        by_name = self._by_identity(expected)
        self.assertIn("collins freight inc", by_name)
        self.assertNotIn("co llins freight in c", by_name)
        party = by_name["collins freight inc"]
        self.assertEqual(party["identity"], "COLLINS Freight INC")
        self.assertEqual(party["procedural_role"], "defendant")


if __name__ == "__main__":
    unittest.main()
