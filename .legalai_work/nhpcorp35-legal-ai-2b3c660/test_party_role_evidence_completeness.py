"""Synthetic regressions for party-role evidence-completeness corrections."""

from __future__ import annotations

import copy
import json
import re
import unittest

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


def _normalized(doc):
    return mb.normalize_document(doc, include_exhibit_segments=True)


def _long_caption_complaint():
    """Caption lists many parties; role labels appear only after a long name list."""
    plaintiffs = ", ".join(f"Summit Parcel Group {i} LLC" for i in range(1, 18))
    defendants = ", ".join(f"Coastal Hauler Carrier {i} Inc" for i in range(1, 18))
    caption = (
        "SUPREME COURT OF THE STATE OF NEW YORK\n"
        "COUNTY OF EXAMPLE\n"
        f"{plaintiffs},\n"
        "                                   Plaintiffs,\n"
        "                 -against-\n"
        f"{defendants},\n"
        "                                   Defendants.\n"
        "Index No. 555111/2024\n"
    )
    body = (
        "COMPLAINT\n"
        "Plaintiffs, by their attorneys, allege as follows.\n"
    )
    return _normalized(
        _doc(
            501,
            "complaint",
            [caption + body],
            filename="nyscef_doc_no_501_summons_complaint.pdf",
        )
    )


def _multipage_parties_complaint():
    """Initiating pleading with a multi-page PARTIES section then FACTS."""
    return _normalized(
        _doc(
            502,
            "complaint",
            [
                "SUPREME COURT OF THE STATE OF NEW YORK\n"
                "Riverbend Supply Co. v. Lakeshore Depot LLC\n"
                "Summons. Index No. 777888/2024.\n",
                "PARTIES\n"
                "1. Plaintiff Riverbend Supply Co. is a domestic corporation "
                "authorized to do business in this state.\n"
                "2. Defendant Lakeshore Depot LLC is a limited liability company.\n",
                "3. Meadow Bridge Repair Inc., third-party defendant, was joined "
                "herein as a necessary party.\n"
                "4. Prairie Notice Carrier LP is a notice defendant under the policy.\n"
                "5. Summit Named Insured Trust is the named insured on the policy.\n",
                "6. Canyon Guaranty Fund, appellant, seeks review of the order.\n"
                "7. Lakeshore Depot LLC, respondent on appeal, opposes.\n",
                "FACTS\n"
                "8. On January 2, 2024, a shipment was damaged in transit.\n"
                "9. The loss was reported to the carrier the next day.\n",
            ],
            filename="nyscef_doc_no_502_summons_complaint.pdf",
        )
    )


def _filler_filings():
    """High-volume procedural fillers that can crowd diversification/top-k."""
    docs = []
    for nyscef in range(601, 612):
        docs.append(
            _normalized(
                _doc(
                    nyscef,
                    "motion",
                    [
                        "Notice of Motion for Summary Judgment returnable June 1, 2024. "
                        "Movant seeks dismissal on procedural calendar grounds. "
                        + ("z" * 80)
                    ],
                    filename=f"nyscef_doc_no_{nyscef}_notice_of_motion.pdf",
                )
            )
        )
        docs.append(
            _normalized(
                _doc(
                    nyscef + 100,
                    "other",
                    [
                        "Request for Judicial Intervention. RJI addendum repeats a "
                        "caption without explaining party roles. "
                        + ("q" * 80)
                    ],
                    filename=f"nyscef_doc_no_{nyscef + 100}_rji.pdf",
                    title="RJI",
                )
            )
        )
    return docs


def _multi_role_paragraph_page():
    return _normalized(
        _doc(
            503,
            "complaint",
            [
                "SUPREME COURT caption page.\n"
                "Ironclad Freight LP v. Harbor Gate Carrier Inc.\n",
                "PARTIES\n"
                "1. Plaintiff Ironclad Freight LP is a limited liability partnership "
                "authorized to do business in this state.\n"
                "2. Defendant Harbor Gate Carrier Inc. is a domestic corporation.\n"
                "3. Mesa Trailer Repair LLC, third-party defendant, was joined herein "
                "as a necessary party.\n"
                "4. Delta Notice Carrier LLC is a notice defendant.\n"
                "5. Atlas Coverage Trust is the named insured on the relevant policy.\n",
            ],
            filename="nyscef_doc_no_503_complaint.pdf",
        )
    )


def _affirmation_with_caption_shell():
    return _normalized(
        _doc(
            504,
            "affirmation",
            [
                "SUPREME COURT OF THE STATE OF NEW YORK\n"
                "Ironclad Freight LP v. Harbor Gate Carrier Inc.\n"
                "                                   Plaintiffs,\n"
                "                 -against-\n"
                "Harbor Gate Carrier Inc.,\n"
                "                                   Defendants.\n"
                "Affirmation of service. Deponent mailed papers on May 1, 2024.\n"
                "Procedural calendar notation without role assignments.\n",
            ],
            filename="nyscef_doc_no_504_affirmation_of_service.pdf",
        )
    )


class PartyRoleEvidenceCompletenessTests(unittest.TestCase):
    def setUp(self):
        self.party_query = (
            "Who are the parties and what are their roles in this action?"
        )
        self.motion_query = (
            "What relief does the notice of motion for summary judgment seek?"
        )

    def test_multipage_parties_section_pages_all_included(self):
        docs = [_multipage_parties_complaint()] + _filler_filings()
        case_map = mb.build_case_map_from_documents(docs)
        result = mb.retrieve_canonical_records(
            docs,
            self.party_query,
            case_map=case_map,
            top_k=6,
        )
        complaint_pages = {
            hit["pdf_page"]
            for hit in result["results"]
            if hit["nyscef_document_number"] == 502
        }
        # PARTIES spans pages 2-4; page 5 begins FACTS and must not be required.
        self.assertTrue({2, 3, 4}.issubset(complaint_pages))
        for page in (2, 3, 4):
            hit = next(
                h
                for h in result["results"]
                if h["nyscef_document_number"] == 502 and h["pdf_page"] == page
            )
            self.assertTrue(hit.get("page_id"))
            self.assertEqual(hit["page_id"], f"nyscef-502-page-{page:04d}")
            self.assertTrue(hit.get("party_role_section_expanded"))

    def test_expansion_stops_at_next_major_section(self):
        docs = [_multipage_parties_complaint()]
        page_lookup = mb._page_lookup_from_documents(docs)
        section_ids = mb._collect_parties_section_page_ids(page_lookup)
        pages = []
        for page_id in section_ids:
            entry = page_lookup[page_id]
            pages.append(entry["page"]["page_number"])
        self.assertEqual(pages, [2, 3, 4])
        self.assertNotIn(5, pages)

        result = mb.retrieve_canonical_records(
            docs,
            self.party_query,
            top_k=10,
        )
        facts_hits = [
            hit
            for hit in result["results"]
            if hit["nyscef_document_number"] == 502 and hit["pdf_page"] == 5
        ]
        for hit in facts_hits:
            # FACTS may still rank lexically, but must not be injected by
            # contiguous PARTIES-section expansion.
            self.assertNotIn("party_role_section_expanded", hit)
            self.assertFalse(hit.get("party_role_section_expanded"))

    def test_long_caption_remains_material_despite_short_query_window(self):
        doc = _long_caption_complaint()
        page_text = doc["pages"][0]["text"]
        short_window = mb._retrieval_excerpt(
            page_text,
            phrase="parties roles",
            tokens=["parties", "roles"],
            phrases=["who are the parties"],
        )
        self.assertLessEqual(len(short_window), mb.RETRIEVAL_EXCERPT_MAX)
        # Short window can omit trailing role labels on a long caption.
        self.assertFalse(
            re.search(r"(?i)\bplaintiffs?\b", short_window)
            and re.search(r"(?i)\bdefendants?\b", short_window)
        )

        entry = {
            "page": doc["pages"][0],
            "document": doc,
            "nyscef_document_number": 501,
            "filename": doc["filename"],
            "document_type": "complaint",
            "segment": None,
        }
        focused = mb._party_role_evidence_excerpt(
            entry,
            page_text,
            phrase="parties roles",
            tokens=["parties", "roles"],
        )
        self.assertIn("Plaintiffs", focused)
        self.assertIn("Defendants", focused)
        self.assertIn("Summit Parcel Group 17 LLC", focused)
        self.assertIn("Coastal Hauler Carrier 17 Inc", focused)

        hit = {
            "result_id": "caption-1",
            "page_id": doc["pages"][0]["page_id"],
            "nyscef_document_number": 501,
            "pdf_page": 1,
            "source_filename": doc["filename"],
            "document_type": "complaint",
            "excerpt": short_window,
            "page_text": page_text,
            "classifications": [],
            "assertion_kind": "verified_record_fact",
        }
        self.assertTrue(de.hit_is_material_for_party_role_question(hit))
        hit_excerpt_only = dict(hit)
        hit_excerpt_only.pop("page_text")
        # Without full-page text, truncated caption window can fail materiality.
        self.assertFalse(de.hit_is_material_for_party_role_question(hit_excerpt_only))

    def test_late_listed_party_names_preserved_completely(self):
        doc = _long_caption_complaint()
        entry = {
            "page": doc["pages"][0],
            "document": doc,
            "nyscef_document_number": 501,
            "filename": doc["filename"],
            "document_type": "complaint",
            "segment": None,
        }
        excerpt = mb._party_role_evidence_excerpt(entry, doc["pages"][0]["text"])
        self.assertIn("Summit Parcel Group 17 LLC", excerpt)
        self.assertIn("Coastal Hauler Carrier 17 Inc", excerpt)
        # Never truncate mid-token: no partial final token artifact.
        self.assertNotRegex(excerpt, r"Carrier 17$")
        self.assertFalse(excerpt.endswith("Coastal"))
        self.assertFalse(excerpt.endswith("Summit"))

    def test_multiple_role_paragraphs_survive_evidence_construction(self):
        doc = _multi_role_paragraph_page()
        result = mb.retrieve_canonical_records(
            [doc],
            self.party_query,
            top_k=5,
        )
        parties_hit = next(
            hit
            for hit in result["results"]
            if hit["nyscef_document_number"] == 503 and hit["pdf_page"] == 2
        )
        excerpt = parties_hit["excerpt"]
        self.assertIn("Ironclad Freight LP is a limited liability partnership", excerpt)
        self.assertIn("Harbor Gate Carrier Inc. is a domestic corporation", excerpt)
        self.assertIn("joined herein as a necessary party", excerpt)
        self.assertIn("notice defendant", excerpt)
        self.assertIn("named insured", excerpt)

    def test_full_page_materiality_excludes_motion_and_rji(self):
        pleading = {
            "result_id": "p1",
            "page_id": "nyscef-502-p2",
            "nyscef_document_number": 502,
            "pdf_page": 2,
            "source_filename": "nyscef_doc_no_502_summons_complaint.pdf",
            "document_type": "complaint",
            "excerpt": "PARTIES",
            "page_text": (
                "PARTIES\n"
                "1. Plaintiff Riverbend Supply Co. is a domestic corporation.\n"
                "2. Defendant Lakeshore Depot LLC is a limited liability company.\n"
            ),
            "classifications": ["party_identity"],
            "assertion_kind": "verified_record_fact",
        }
        motion = {
            "result_id": "m1",
            "page_id": "nyscef-601-p1",
            "nyscef_document_number": 601,
            "pdf_page": 1,
            "source_filename": "nyscef_doc_no_601_notice_of_motion.pdf",
            "document_type": "motion",
            "excerpt": "Notice of Motion",
            "page_text": (
                "Notice of Motion for Summary Judgment returnable June 1, 2024. "
                "Movant seeks dismissal. Caption lists Riverbend Supply Co. against "
                "Lakeshore Depot LLC without assigning procedural roles."
            ),
            "classifications": ["motion"],
            "assertion_kind": "unknown",
        }
        rji = {
            "result_id": "r1",
            "page_id": "nyscef-701-p1",
            "nyscef_document_number": 701,
            "pdf_page": 1,
            "source_filename": "nyscef_doc_no_701_rji.pdf",
            "document_type": "other",
            "excerpt": "Request for Judicial Intervention",
            "page_text": (
                "Request for Judicial Intervention. RJI addendum repeats the caption "
                "Riverbend Supply Co. v. Lakeshore Depot LLC without explaining roles."
            ),
            "classifications": ["procedural"],
            "assertion_kind": "unknown",
        }
        name_only = {
            "result_id": "n1",
            "page_id": "nyscef-502-p1",
            "nyscef_document_number": 502,
            "pdf_page": 1,
            "source_filename": "nyscef_doc_no_502_summons_complaint.pdf",
            "document_type": "complaint",
            "excerpt": "Riverbend Supply Co.",
            "page_text": "Calendar exhibit list mentioning Riverbend Supply Co. only.",
            "classifications": [],
            "assertion_kind": "unknown",
        }
        self.assertTrue(de.hit_is_material_for_party_role_question(pleading))
        self.assertFalse(de.hit_is_material_for_party_role_question(motion))
        self.assertFalse(de.hit_is_material_for_party_role_question(rji))
        self.assertFalse(de.hit_is_material_for_party_role_question(name_only))

        packet = de.build_evidence_packet(
            self.party_query,
            {"query": self.party_query, "results": [pleading, motion, rji, name_only]},
        )
        page_ids = {hit["page_id"] for hit in packet["retrieval_hits"]}
        self.assertEqual(page_ids, {"nyscef-502-p2"})
        # Compact packet must not forward full page text into generation.
        for hit in packet["retrieval_hits"]:
            self.assertNotIn("page_text", hit)
            self.assertNotIn("full_page_text", hit)

    def test_affirmation_caption_does_not_trigger_complete_caption(self):
        affirmation = _affirmation_with_caption_shell()
        complaint = _multi_role_paragraph_page()
        entry = {
            "page": affirmation["pages"][0],
            "document": affirmation,
            "nyscef_document_number": 504,
            "filename": affirmation["filename"],
            "document_type": "affirmation",
            "segment": None,
        }
        self.assertTrue(
            mb._is_affirmation_or_service_filing(entry, affirmation["pages"][0]["text"])
        )
        self.assertFalse(
            mb._looks_like_caption_bearing_page(
                affirmation["pages"][0]["text"],
                page_number=1,
                kind="other",
            )
        )
        excerpt = mb._party_role_evidence_excerpt(
            entry, affirmation["pages"][0]["text"]
        )
        # Service affirmation may mention names, but must not receive complete
        # caption preservation treatment used for initiating pleadings.
        self.assertNotIn("complete_caption", excerpt.lower())
        result = mb.retrieve_canonical_records(
            [complaint, affirmation],
            self.party_query,
            top_k=8,
        )
        aff_hits = [
            hit
            for hit in result["results"]
            if hit["nyscef_document_number"] == 504
        ]
        for hit in aff_hits:
            self.assertNotEqual(
                hit.get("excerpt"),
                mb._extract_complete_pleading_caption(affirmation["pages"][0]["text"]),
            )

        # Materiality still excludes affirmation service noise.
        hit = {
            "result_id": "a1",
            "page_id": affirmation["pages"][0]["page_id"],
            "nyscef_document_number": 504,
            "pdf_page": 1,
            "source_filename": affirmation["filename"],
            "document_type": "affirmation",
            "excerpt": affirmation["pages"][0]["text"][:240],
            "page_text": affirmation["pages"][0]["text"],
            "classifications": ["procedural"],
            "assertion_kind": "unknown",
        }
        self.assertFalse(de.hit_is_material_for_party_role_question(hit))

    def test_expanded_pages_preserve_stable_citations(self):
        docs = [_multipage_parties_complaint()] + _filler_filings()
        result = mb.retrieve_canonical_records(
            docs,
            self.party_query,
            top_k=5,
        )
        by_page = {
            hit["pdf_page"]: hit
            for hit in result["results"]
            if hit["nyscef_document_number"] == 502 and hit["pdf_page"] in {2, 3, 4}
        }
        self.assertEqual(set(by_page), {2, 3, 4})
        for page, hit in by_page.items():
            self.assertEqual(hit["page_id"], f"nyscef-502-page-{page:04d}")
            self.assertEqual(hit["nyscef_document_number"], 502)
            self.assertEqual(hit["pdf_page"], page)
            self.assertTrue(str(hit["result_id"]).startswith("cret-nyscef-502-page-"))
            self.assertIn(f"{page:04d}", hit["result_id"])

    def test_non_party_and_motion_behavior_unchanged(self):
        docs = [_multipage_parties_complaint()] + _filler_filings()[:4]
        motion_result = mb.retrieve_canonical_records(
            docs,
            self.motion_query,
            top_k=5,
            include_diagnostics=True,
        )
        hints = motion_result["diagnostics"]["query_hints"]
        self.assertFalse(hints.get("party_role_intent"))
        self.assertEqual(motion_result["results"][0]["document_type"], "motion")
        for hit in motion_result["results"]:
            self.assertEqual(hit["component_scores"]["party_role_pleading"], 0.0)
            self.assertIsNone(hit.get("page_text"))
            self.assertNotIn("party_role_section_expanded", hit)

        # Diversification still returns at most top_k for non-party queries.
        self.assertLessEqual(len(motion_result["results"]), 5)

        motion_hit = {
            "result_id": "m1",
            "page_id": "nyscef-601-p1",
            "nyscef_document_number": 601,
            "pdf_page": 1,
            "source_filename": "nyscef_doc_no_601_notice_of_motion.pdf",
            "document_type": "motion",
            "excerpt": (
                "Notice of Motion for Summary Judgment returnable June 1, 2024. "
                "Movant seeks dismissal."
            ),
            "classifications": ["motion"],
            "assertion_kind": "unknown",
        }
        packet = de.build_evidence_packet(
            self.motion_query,
            {"query": self.motion_query, "results": [motion_hit]},
        )
        self.assertNotIn("materiality_filter", packet)
        self.assertEqual(packet["retrieval_hit_count"], 1)

    def test_no_provisional_or_gold_in_generation_inputs(self):
        docs = [_multi_role_paragraph_page()]
        retrieval = mb.retrieve_canonical_records(
            docs,
            self.party_query,
            top_k=5,
        )
        retrieval = dict(retrieval)
        retrieval["provisional_answer"] = "PROVISIONAL_SHOULD_NOT_APPEAR"
        retrieval["gold_answer"] = "GOLD_SHOULD_NOT_APPEAR"

        captured = {"calls": []}

        def _model(system_prompt, user_prompt):
            captured["calls"].append(
                {"system": system_prompt, "user": user_prompt}
            )
            packet = de.build_evidence_packet(self.party_query, retrieval)
            hit = packet["retrieval_hits"][0]
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
                        "nyscef_document_number": hit["nyscef_document_number"],
                        "page_id": hit["page_id"],
                        "pdf_page": hit["pdf_page"],
                        "source_excerpt": (hit.get("excerpt") or "")[:80],
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
            self.party_query,
            retrieval,
            model_call=_model,
        )
        self.assertEqual(result["status"], de.STATUS_READY)
        for call in captured["calls"]:
            blob = (call["system"] + "\n" + call["user"]).lower()
            self.assertNotIn("provisional_should_not_appear", blob)
            self.assertNotIn("gold_should_not_appear", blob)
            self.assertNotIn("provisional_answer", blob)
            self.assertNotIn("gold_answer", blob)
        first_user = captured["calls"][0]["user"]
        # Packet JSON sits between the two leading instruction paragraphs and
        # any trailing party-role completeness instruction.
        packet_json = first_user.split("\n\n", 2)[1]
        user_packet = json.loads(packet_json)
        self.assertNotIn("provisional_answer", user_packet)
        self.assertNotIn("gold_answer", user_packet)
        for hit in user_packet.get("retrieval_hits") or []:
            self.assertNotIn("page_text", hit)


class PartyRoleProceduralBoilerplateFilterTests(unittest.TestCase):
    """Focused synthetic coverage for party-role line-level boilerplate filtering."""

    def _entry(self, text, nyscef=540, doc_type="complaint"):
        doc = _normalized(
            _doc(
                nyscef,
                doc_type,
                [text],
                filename=f"nyscef_doc_no_{nyscef}_{doc_type}.pdf",
            )
        )
        return {
            "page": doc["pages"][0],
            "document": doc,
            "nyscef_document_number": nyscef,
            "filename": doc["filename"],
            "document_type": doc_type,
            "segment": None,
        }, doc["pages"][0]["text"]

    def _party_hit(
        self,
        *,
        excerpt,
        label,
        page_id="nyscef-540-p1",
        nyscef=540,
        pdf_page=1,
    ):
        return {
            "result_id": f"nyscef-{nyscef}-p{pdf_page}",
            "page_id": page_id,
            "nyscef_document_number": nyscef,
            "pdf_page": pdf_page,
            "source_filename": f"nyscef_doc_no_{nyscef}_complaint.pdf",
            "document_type": "complaint",
            "excerpt": excerpt,
            "classifications": ["party_allegation"],
            "assertion_kind": "party_allegation",
            "case_map_linkage": {
                "node_id": "party-1",
                "node_type": "party",
                "collection": "parties",
                "label": label,
                "assertion_kind": "party_allegation",
                "conflicts_with": [],
            },
            "exhibit_segment": None,
            "score": 12.0,
        }

    def test_filing_header_and_nyscef_footer_removed(self):
        text = (
            "FILED: EXAMPLE COUNTY CLERK 03/15/2024 09:41 AM\n"
            "INDEX NO. 812345/2024\n"
            "NYSCEF DOC. NO. 1\n"
            "RECEIVED NYSCEF: 03/15/2024\n"
            "SUPREME COURT OF THE STATE OF NEW YORK\n"
            "COUNTY OF EXAMPLE\n"
            "Harbor Quay Freight LP,\n"
            "                                   Plaintiff,\n"
            "                 -against-\n"
            "Pier Gate Depot Inc.,\n"
            "                                   Defendant.\n"
            "COMPLAINT\n"
            "Plaintiffs, by their attorneys, allege as follows.\n"
        )
        entry, page_text = self._entry(text)
        excerpt = mb._party_role_evidence_excerpt(entry, page_text)
        self.assertNotIn("FILED:", excerpt)
        self.assertNotIn("RECEIVED NYSCEF:", excerpt)
        self.assertNotIn("NYSCEF DOC. NO.", excerpt)
        self.assertNotIn("812345/2024", excerpt)
        self.assertIn("Harbor Quay Freight LP", excerpt)
        self.assertIn("Plaintiff", excerpt)
        self.assertIn("Pier Gate Depot Inc.", excerpt)
        self.assertIn("Defendant", excerpt)

    def test_n_of_n_page_footer_removed(self):
        text = (
            "SUPREME COURT OF THE STATE OF NEW YORK\n"
            "County of Example\n"
            "Beacon Pier Logistics LLC,\n"
            "                                   Plaintiff,\n"
            "                 -against-\n"
            "Harbor Crane Depot Inc.,\n"
            "                                   Defendant.\n"
            "2 of 15\n"
            "PARTIES\n"
            "1. Plaintiff Beacon Pier Logistics LLC is a domestic corporation.\n"
            "3 of 15\n"
        )
        entry, page_text = self._entry(text, nyscef=544)
        excerpt = mb._party_role_evidence_excerpt(entry, page_text)
        self.assertNotIn("2 of 15", excerpt)
        self.assertNotIn("3 of 15", excerpt)
        self.assertFalse(
            re.search(r"(?i)\b\d{1,4}\s+of\s+\d{1,4}\b", excerpt),
        )
        self.assertIn("Beacon Pier Logistics LLC", excerpt)
        self.assertIn("Harbor Crane Depot Inc.", excerpt)
        self.assertIn("PARTIES", excerpt)
        self.assertIn("domestic corporation", excerpt)

    def test_summons_and_default_warning_boilerplate_removed(self):
        text = (
            "SUPREME COURT OF THE STATE OF NEW YORK\n"
            "Riverfront Carrier Co., Plaintiff,\n"
            "                 -against-\n"
            "Lakeside Warehouse Inc., Defendant.\n"
            "SUMMONS\n"
            "TO THE ABOVE NAMED DEFENDANTS\n"
            "YOU ARE HEREBY SUMMONED to answer the complaint in this action "
            "and to serve a copy of your answer on plaintiff's attorneys "
            "within twenty (20) days after the service of this summons.\n"
            "Upon your failure to appear or answer, judgment will be taken "
            "against you by default for the relief demanded in the complaint.\n"
            "The place of trial is designated as the County of Example.\n"
            "2 of 15\n"
            "COMPLAINT\n"
            "PARTIES\n"
            "1. Plaintiff Riverfront Carrier Co. is a domestic corporation.\n"
            "2. Defendant Lakeside Warehouse Inc. is a limited liability company.\n"
        )
        entry, page_text = self._entry(text, nyscef=541)
        excerpt = mb._party_role_evidence_excerpt(entry, page_text)
        self.assertNotIn("SUMMONS", excerpt)
        self.assertNotIn("TO THE ABOVE NAMED DEFENDANTS", excerpt)
        self.assertNotIn("YOU ARE HEREBY SUMMONED", excerpt)
        self.assertNotIn("serve a copy of your answer", excerpt)
        self.assertNotIn("within twenty (20) days after the service", excerpt)
        self.assertNotIn("judgment will be taken against you by default", excerpt)
        self.assertNotIn("Upon your failure to appear or answer", excerpt)
        self.assertNotIn("place of trial is designated", excerpt)
        self.assertNotIn("2 of 15", excerpt)
        self.assertIn("Riverfront Carrier Co.", excerpt)
        self.assertIn("Lakeside Warehouse Inc.", excerpt)
        self.assertIn("domestic corporation", excerpt)
        self.assertIn("limited liability company", excerpt)
        self.assertIn("PARTIES", excerpt)

    def test_mixed_page_retains_caption_parties_and_identity(self):
        text = (
            "FILED: EXAMPLE COUNTY CLERK 04/01/2024 11:15 AM\n"
            "RECEIVED NYSCEF: 04/01/2024\n"
            "This document was electronically filed through NYSCEF case processing.\n"
            "SUPREME COURT OF THE STATE OF NEW YORK\n"
            "North Quay Logistics LP,\n"
            "                                   Plaintiff,\n"
            "                 -against-\n"
            "South Pier Terminal Inc.,\n"
            "                                   Defendant.\n"
            "SUMMONS\n"
            "TO THE ABOVE NAMED DEFENDANT:\n"
            "YOU ARE HEREBY SUMMONED to answer this complaint.\n"
            "Default will be taken against you if you fail to appear.\n"
            "1 of 8\n"
            "PARTIES\n"
            "1. Plaintiff North Quay Logistics LP is a limited liability partnership "
            "with its principal place of business in Albany County.\n"
            "2. Defendant South Pier Terminal Inc. is a notice defendant and a "
            "domestic corporation residing in Erie County.\n"
        )
        entry, page_text = self._entry(text, nyscef=542)
        excerpt = mb._party_role_evidence_excerpt(entry, page_text)
        self.assertNotIn("FILED:", excerpt)
        self.assertNotIn("RECEIVED NYSCEF:", excerpt)
        self.assertNotIn("electronically filed through NYSCEF", excerpt)
        self.assertNotIn("SUMMONS", excerpt)
        self.assertNotIn("TO THE ABOVE NAMED DEFENDANT", excerpt)
        self.assertNotIn("YOU ARE HEREBY SUMMONED", excerpt)
        self.assertNotIn("Default will be taken against you", excerpt)
        self.assertNotIn("1 of 8", excerpt)
        self.assertIn("North Quay Logistics LP", excerpt)
        self.assertIn("South Pier Terminal Inc.", excerpt)
        self.assertIn("Plaintiff", excerpt)
        self.assertIn("Defendant", excerpt)
        self.assertIn("PARTIES", excerpt)
        self.assertIn("limited liability partnership", excerpt)
        self.assertIn("principal place of business", excerpt)
        self.assertIn("notice defendant", excerpt)
        self.assertIn("domestic corporation", excerpt)

    def test_case_map_linkage_label_sanitized_for_party_role_packet(self):
        noisy_label = (
            "FILED: EXAMPLE COUNTY CLERK 05/01/2024 10:00 AM "
            "RECEIVED NYSCEF: 05/01/2024 SUMMONS "
            "TO THE ABOVE NAMED DEFENDANTS 2 of 15 "
            "Plaintiff Cedar Basin Freight LLC is a domestic corporation "
            "with its principal place of business in Kings County."
        )
        hit = self._party_hit(
            excerpt=(
                "PARTIES\n"
                "1. Plaintiff Cedar Basin Freight LLC is a domestic corporation "
                "with its principal place of business in Kings County."
            ),
            label=noisy_label,
            page_id="nyscef-545-p1",
            nyscef=545,
        )
        packet = de.build_evidence_packet(
            "Who are the parties and what are their roles?",
            {"query": "parties roles", "results": [hit]},
        )
        linkage = packet["retrieval_hits"][0]["case_map_linkage"]
        label = linkage.get("label") or ""
        self.assertNotIn("FILED:", label)
        self.assertNotIn("RECEIVED NYSCEF:", label)
        self.assertNotIn("SUMMONS", label)
        self.assertNotIn("TO THE ABOVE NAMED DEFENDANTS", label)
        self.assertNotIn("2 of 15", label)
        self.assertIn("Cedar Basin Freight LLC", label)
        self.assertIn("domestic corporation", label)
        self.assertIn("principal place of business", label)

    def test_boilerplate_only_linkage_label_omitted(self):
        hit = self._party_hit(
            excerpt="PARTIES\n1. Plaintiff Only Caption Co. is a corporation.",
            label="FILED: EXAMPLE COUNTY CLERK 05/02/2024\nSUMMONS\n2 of 9",
            page_id="nyscef-546-p1",
            nyscef=546,
        )
        packet = de.build_evidence_packet(
            "Who are the parties and what roles do they have?",
            {"query": "party roles", "results": [hit]},
        )
        linkage = packet["retrieval_hits"][0]["case_map_linkage"]
        self.assertNotIn("label", linkage)
        self.assertEqual(packet["retrieval_hits"][0]["page_id"], "nyscef-546-p1")
        self.assertEqual(packet["retrieval_hits"][0]["nyscef_document_number"], 546)
        self.assertEqual(packet["retrieval_hits"][0]["pdf_page"], 1)

    def test_non_party_linkage_label_unchanged(self):
        noisy_label = (
            "FILED: EXAMPLE COUNTY CLERK 05/03/2024 RECEIVED NYSCEF: 05/03/2024 "
            "SUMMONS 4 of 12 motion returnable"
        )
        hit = self._party_hit(
            excerpt="The motion is returnable in Part 12.",
            label=noisy_label,
            page_id="nyscef-547-p2",
            nyscef=547,
            pdf_page=2,
        )
        packet = de.build_evidence_packet(
            "What is the motion return date?",
            {"query": "motion return", "results": [hit]},
        )
        linkage = packet["retrieval_hits"][0]["case_map_linkage"]
        self.assertEqual(linkage.get("label"), noisy_label)
        self.assertIsNone(packet.get("materiality_filter"))

    def test_page_id_and_citation_metadata_unchanged(self):
        text = (
            "FILED: EXAMPLE COUNTY CLERK 06/01/2024 09:00 AM\n"
            "RECEIVED NYSCEF: 06/01/2024\n"
            "SUPREME COURT OF THE STATE OF NEW YORK\n"
            "Atlas Parcel Group LLC, Plaintiff,\n"
            "                 -against-\n"
            "Canyon Freight Inc., Defendant.\n"
            "SUMMONS\n"
            "2 of 11\n"
            "PARTIES\n"
            "1. Plaintiff Atlas Parcel Group LLC is a domestic corporation.\n"
        )
        entry, page_text = self._entry(text, nyscef=548)
        page_id = entry["page"]["page_id"]
        excerpt = mb._party_role_evidence_excerpt(entry, page_text)
        hit = self._party_hit(
            excerpt=excerpt,
            label=(
                "FILED: EXAMPLE COUNTY CLERK 06/01/2024 SUMMONS 2 of 11 "
                "Plaintiff Atlas Parcel Group LLC is a domestic corporation."
            ),
            page_id=page_id,
            nyscef=548,
        )
        packet = de.build_evidence_packet(
            "Who are the parties and what are their roles?",
            {"query": "parties roles", "results": [hit]},
        )
        out = packet["retrieval_hits"][0]
        self.assertEqual(out["page_id"], page_id)
        self.assertEqual(out["nyscef_document_number"], 548)
        self.assertEqual(out["pdf_page"], 1)
        self.assertEqual(out["result_id"], "nyscef-548-p1")
        self.assertEqual(out["source_filename"], "nyscef_doc_no_548_complaint.pdf")
        self.assertNotIn("FILED:", out["excerpt"])
        self.assertNotIn("SUMMONS", out["excerpt"])
        self.assertNotIn("2 of 11", out["excerpt"])
        self.assertIn("Atlas Parcel Group LLC", out["excerpt"])
        self.assertIn("PARTIES", out["excerpt"])
        self.assertIn("Atlas Parcel Group LLC", out["case_map_linkage"]["label"])
        self.assertNotIn("FILED:", out["case_map_linkage"]["label"])

    def test_collapsed_caption_summons_span_removed(self):
        text = (
            "SUPREME COURT OF THE STATE OF NEW YORK "
            "Riverfront Carrier Co., Plaintiff, -against- "
            "Lakeside Warehouse Inc., Defendant. "
            "SUMMONS TO THE ABOVE NAMED DEFENDANTS "
            "YOU ARE HEREBY SUMMONED to answer the complaint in this action "
            "and to serve a copy of your answer on plaintiff's attorneys "
            "within twenty (20) days after the service of this summons "
            "PARTIES 1. Plaintiff Riverfront Carrier Co. is a domestic corporation "
            "with its principal place of business in Albany County."
        )
        excerpt = mb._filter_party_role_procedural_boilerplate(text)
        self.assertNotIn("SUMMONS", excerpt)
        self.assertNotIn("TO THE ABOVE NAMED DEFENDANTS", excerpt)
        self.assertNotIn("YOU ARE HEREBY SUMMONED", excerpt)
        self.assertNotIn("serve a copy of your answer", excerpt)
        self.assertNotIn("within twenty (20) days after the service", excerpt)
        self.assertNotIn("complaint in this action", excerpt)
        self.assertIn("Riverfront Carrier Co.", excerpt)
        self.assertIn("Lakeside Warehouse Inc.", excerpt)
        self.assertIn("Plaintiff", excerpt)
        self.assertIn("Defendant", excerpt)
        self.assertIn("PARTIES", excerpt)
        self.assertIn("domestic corporation", excerpt)
        self.assertIn("principal place of business", excerpt)

    def test_collapsed_caption_venue_span_removed(self):
        text = (
            "Harbor Quay Freight LP, Plaintiff, -against- "
            "Pier Gate Depot Inc., Defendant. "
            "The place of trial is designated as the County of Example "
            "PARTIES 1. Plaintiff Harbor Quay Freight LP is a limited liability "
            "partnership. 2. Defendant Pier Gate Depot Inc. is a notice defendant."
        )
        excerpt = mb._filter_party_role_procedural_boilerplate(text)
        self.assertNotIn("place of trial is designated", excerpt)
        self.assertIn("Harbor Quay Freight LP", excerpt)
        self.assertIn("Pier Gate Depot Inc.", excerpt)
        self.assertIn("Plaintiff", excerpt)
        self.assertIn("Defendant", excerpt)
        self.assertIn("PARTIES", excerpt)
        self.assertIn("limited liability partnership", excerpt)
        self.assertIn("notice defendant", excerpt)

    def test_collapsed_caption_appearance_deadline_span_removed(self):
        text = (
            "Beacon Pier Logistics LLC, Plaintiff, -against- "
            "Harbor Crane Depot Inc., Defendant. "
            "You are hereby required to appear and answer "
            "within twenty (20) days after the service of this summons "
            "PARTIES 1. Plaintiff Beacon Pier Logistics LLC is a domestic corporation."
        )
        excerpt = mb._filter_party_role_procedural_boilerplate(text)
        self.assertNotIn("required to appear", excerpt)
        self.assertNotIn("within twenty (20) days after the service", excerpt)
        self.assertIn("Beacon Pier Logistics LLC", excerpt)
        self.assertIn("Harbor Crane Depot Inc.", excerpt)
        self.assertIn("Plaintiff", excerpt)
        self.assertIn("Defendant", excerpt)
        self.assertIn("PARTIES", excerpt)
        self.assertIn("domestic corporation", excerpt)

    def test_collapsed_caption_failure_to_appear_default_warning_removed(self):
        text = (
            "North Quay Logistics LP, Plaintiff, -against- "
            "South Pier Terminal Inc., Defendant. "
            "Upon your failure to appear or answer, judgment will be taken "
            "against you by default for the relief demanded in the complaint "
            "PARTIES 1. Defendant South Pier Terminal Inc. is a domestic corporation "
            "residing in Erie County."
        )
        excerpt = mb._filter_party_role_procedural_boilerplate(text)
        self.assertNotIn("failure to appear", excerpt)
        self.assertNotIn("judgment will be taken against you by default", excerpt)
        self.assertIn("North Quay Logistics LP", excerpt)
        self.assertIn("South Pier Terminal Inc.", excerpt)
        self.assertIn("Plaintiff", excerpt)
        self.assertIn("Defendant", excerpt)
        self.assertIn("PARTIES", excerpt)
        self.assertIn("domestic corporation", excerpt)
        self.assertIn("residing in Erie County", excerpt)

    def test_collapsed_responsive_caption_preserved_around_boilerplate(self):
        text = (
            "Cedar Basin Freight LLC, Plaintiff, -against- "
            "Maple Depot Inc., Defendant. "
            "SUMMONS TO THE ABOVE NAMED DEFENDANTS "
            "YOU ARE HEREBY SUMMONED to answer the complaint "
            "within thirty (30) days after the service of this summons "
            "The place of trial is designated as the County of Kings "
            "Upon your failure to appear or answer, judgment will be taken "
            "against you by default "
            "PARTIES 1. Plaintiff Cedar Basin Freight LLC is a domestic corporation "
            "with its principal place of business in Kings County. "
            "2. Defendant Maple Depot Inc. is a notice defendant and a "
            "limited liability company."
        )
        excerpt = mb._filter_party_role_procedural_boilerplate(text)
        self.assertNotIn("SUMMONS", excerpt)
        self.assertNotIn("YOU ARE HEREBY SUMMONED", excerpt)
        self.assertNotIn("within thirty (30) days after the service", excerpt)
        self.assertNotIn("place of trial is designated", excerpt)
        self.assertNotIn("failure to appear", excerpt)
        self.assertNotIn("judgment will be taken against you by default", excerpt)
        # Caption before and allegations after the removed spans must remain.
        self.assertIn("Cedar Basin Freight LLC", excerpt)
        self.assertIn("Maple Depot Inc.", excerpt)
        self.assertIn("Plaintiff", excerpt)
        self.assertIn("Defendant", excerpt)
        self.assertIn("-against-", excerpt)
        self.assertIn("PARTIES", excerpt)
        self.assertIn("domestic corporation", excerpt)
        self.assertIn("principal place of business", excerpt)
        self.assertIn("notice defendant", excerpt)
        self.assertIn("limited liability company", excerpt)

    def test_collapsed_summons_basis_for_venue_span_removed(self):
        text = (
            "Riverfront Carrier Co., Plaintiff, -against- "
            "Lakeside Warehouse Inc., Defendant. "
            "SUMMONS The basis for venue is the County of Example "
            "PARTIES 1. Plaintiff Riverfront Carrier Co. is a domestic corporation."
        )
        excerpt = mb._filter_party_role_procedural_boilerplate(text)
        self.assertNotIn("SUMMONS", excerpt)
        self.assertNotIn("The basis for venue is", excerpt)
        self.assertNotIn("basis for venue", excerpt)
        self.assertIn("Riverfront Carrier Co.", excerpt)
        self.assertIn("Lakeside Warehouse Inc.", excerpt)
        self.assertIn("Plaintiff", excerpt)
        self.assertIn("Defendant", excerpt)
        self.assertIn("-against-", excerpt)
        self.assertIn("PARTIES", excerpt)
        self.assertIn("domestic corporation", excerpt)

    def test_collapsed_in_case_of_your_failure_span_removed(self):
        text = (
            "Beacon Pier Logistics LLC, Plaintiff, -against- "
            "Harbor Crane Depot Inc., Defendant. "
            "In case of your failure to appear or answer, judgment will be taken "
            "against you by default for the relief demanded in the complaint "
            "PARTIES 1. Plaintiff Beacon Pier Logistics LLC is a domestic corporation."
        )
        excerpt = mb._filter_party_role_procedural_boilerplate(text)
        self.assertNotIn("In case of your failure to appear or answer", excerpt)
        self.assertNotIn("failure to appear", excerpt)
        self.assertNotIn("judgment will be taken against you by default", excerpt)
        self.assertIn("Beacon Pier Logistics LLC", excerpt)
        self.assertIn("Harbor Crane Depot Inc.", excerpt)
        self.assertIn("Plaintiff", excerpt)
        self.assertIn("Defendant", excerpt)
        self.assertIn("-against-", excerpt)
        self.assertIn("PARTIES", excerpt)
        self.assertIn("domestic corporation", excerpt)

    def test_collapsed_summons_venue_and_failure_variants_removed_together(self):
        text = (
            "North Quay Logistics LP, Plaintiff, -against- "
            "South Pier Terminal Inc., Defendant. "
            "SUMMONS The basis for venue is the County of Kings "
            "In case of your failure to appear or answer, judgment will be taken "
            "against you by default "
            "PARTIES 1. Plaintiff North Quay Logistics LP is a limited liability "
            "partnership. 2. Defendant South Pier Terminal Inc. is a notice defendant."
        )
        excerpt = mb._filter_party_role_procedural_boilerplate(text)
        self.assertNotIn("SUMMONS", excerpt)
        self.assertNotIn("The basis for venue is", excerpt)
        self.assertNotIn("basis for venue", excerpt)
        self.assertNotIn("In case of your failure to appear or answer", excerpt)
        self.assertNotIn("failure to appear", excerpt)
        self.assertNotIn("judgment will be taken against you by default", excerpt)
        self.assertIn("North Quay Logistics LP", excerpt)
        self.assertIn("South Pier Terminal Inc.", excerpt)
        self.assertIn("Plaintiff", excerpt)
        self.assertIn("Defendant", excerpt)
        self.assertIn("-against-", excerpt)
        self.assertIn("PARTIES", excerpt)
        self.assertIn("limited liability partnership", excerpt)
        self.assertIn("notice defendant", excerpt)

    def test_collapsed_responsive_caption_preserved_before_venue_failure_variants(self):
        text = (
            "Cedar Basin Freight LLC, Plaintiff, -against- "
            "Maple Depot Inc., Defendant. "
            "SUMMONS The basis for venue is designated as Kings County "
            "In case of your failure to appear or answer, judgment will be taken "
            "against you by default for the relief demanded "
            "PARTIES 1. Plaintiff Cedar Basin Freight LLC is a domestic corporation "
            "with its principal place of business in Kings County."
        )
        excerpt = mb._filter_party_role_procedural_boilerplate(text)
        self.assertNotIn("SUMMONS", excerpt)
        self.assertNotIn("basis for venue", excerpt)
        self.assertNotIn("In case of your failure", excerpt)
        # Responsive caption immediately before the removed spans must remain.
        self.assertIn("Cedar Basin Freight LLC", excerpt)
        self.assertIn("Maple Depot Inc.", excerpt)
        self.assertIn("Plaintiff", excerpt)
        self.assertIn("Defendant", excerpt)
        self.assertIn("-against-", excerpt)
        self.assertRegex(
            excerpt,
            r"Cedar Basin Freight LLC,\s*Plaintiff,\s*-against-\s*"
            r"Maple Depot Inc.,\s*Defendant\.",
        )
        self.assertIn("PARTIES", excerpt)
        self.assertIn("domestic corporation", excerpt)
        self.assertIn("principal place of business", excerpt)

    def test_ordinary_substantive_venue_language_not_removed(self):
        text = (
            "PARTIES 1. Plaintiff Atlas Parcel Group LLC is a domestic corporation. "
            "2. Venue is proper in this County because Defendant Canyon Freight Inc. "
            "maintains its principal place of business here and the shipment transit "
            "occurred in this venue."
        )
        excerpt = mb._filter_party_role_procedural_boilerplate(text)
        self.assertIn("Venue is proper in this County", excerpt)
        self.assertIn("occurred in this venue", excerpt)
        self.assertIn("Atlas Parcel Group LLC", excerpt)
        self.assertIn("Canyon Freight Inc.", excerpt)
        self.assertIn("principal place of business", excerpt)
        self.assertNotIn("The basis for venue is", excerpt)

    def test_ordinary_substantive_failure_answer_language_not_removed(self):
        text = (
            "PARTIES 1. Plaintiff Harbor Quay Freight LP is a limited liability "
            "partnership. 2. Defendant Pier Gate Depot Inc. denied the material "
            "allegations of the complaint and asserted that any failure to answer "
            "interrogatories was cured before the motion practice, and that its "
            "answer raised affirmative defenses under the Policies."
        )
        excerpt = mb._filter_party_role_procedural_boilerplate(text)
        self.assertIn("failure to answer", excerpt)
        self.assertIn("answer raised affirmative defenses", excerpt)
        self.assertIn("denied the material allegations", excerpt)
        self.assertIn("Harbor Quay Freight LP", excerpt)
        self.assertIn("Pier Gate Depot Inc.", excerpt)
        self.assertNotIn("In case of your failure to appear or answer", excerpt)

    def test_ordinary_legal_prose_not_over_filtered(self):
        text = (
            "PARTIES\n"
            "1. Plaintiff Atlas Parcel Group LLC is a domestic corporation "
            "authorized to do business in this state.\n"
            "2. Defendant Canyon Freight Inc. denied the material allegations "
            "of the complaint and asserted affirmative defenses under the Policies.\n"
            "3. Plaintiff filed this action seeking damages for breach of contract "
            "after the shipment was lost in transit.\n"
            "4. Defendant Canyon Freight Inc. is a notice defendant with its "
            "principal place of business in Kings County.\n"
        )
        entry, page_text = self._entry(text, nyscef=543)
        excerpt = mb._party_role_evidence_excerpt(entry, page_text)
        self.assertIn("Atlas Parcel Group LLC", excerpt)
        self.assertIn("domestic corporation", excerpt)
        self.assertIn("denied the material allegations", excerpt)
        self.assertIn("filed this action seeking damages", excerpt)
        self.assertIn("notice defendant", excerpt)
        self.assertIn("principal place of business", excerpt)
        # Narrow patterns must not strip ordinary uses of file/default vocabulary.
        self.assertNotIn("FILED:", excerpt)
        self.assertFalse(
            mb._is_party_role_procedural_boilerplate_line(
                "3. Plaintiff filed this action seeking damages for breach of contract."
            )
        )
        self.assertFalse(
            mb._is_party_role_procedural_boilerplate_line(
                "Defendant defaulted on premium payment obligations under the Policies."
            )
        )
        # Collapsed ordinary prose must likewise survive span-level stripping.
        collapsed = " ".join(text.splitlines())
        collapsed_out = mb._filter_party_role_procedural_boilerplate(collapsed)
        self.assertIn("Atlas Parcel Group LLC", collapsed_out)
        self.assertIn("denied the material allegations", collapsed_out)
        self.assertIn("filed this action seeking damages", collapsed_out)
        self.assertIn("notice defendant", collapsed_out)
        self.assertIn("principal place of business", collapsed_out)
        self.assertFalse(
            mb._is_party_role_procedural_boilerplate_line(
                "Defendant defaulted on premium payment obligations under the Policies."
            )
        )


class PartyRoleExpansionBoundTests(unittest.TestCase):
    def test_section_expansion_respects_explicit_page_bound(self):
        pages = ["PARTIES\n1. Plaintiff Bound Test Co. is a corporation.\n"]
        for i in range(2, 12):
            pages.append(
                f"{i}. Defendant Bound Party {i} Inc. is a domestic corporation "
                "joined for completeness.\n"
            )
        pages.append("FACTS\nThe shipment failed.\n")
        doc = _normalized(
            _doc(
                509,
                "complaint",
                pages,
                filename="nyscef_doc_no_509_complaint.pdf",
            )
        )
        section_ids = mb._collect_parties_section_page_ids(
            mb._page_lookup_from_documents([doc])
        )
        self.assertLessEqual(len(section_ids), mb.PARTY_ROLE_SECTION_EXPAND_MAX_PAGES)
        self.assertEqual(len(section_ids), mb.PARTY_ROLE_SECTION_EXPAND_MAX_PAGES)


class PrefixedPartiesHeadingTests(unittest.TestCase):
    """Synthetic proofs for numbered/prefixed PARTIES heading recognition."""

    def test_number_prefixed_parties_headings_recognized(self):
        for heading in ("14 PARTIES", "14. PARTIES", "14) PARTIES"):
            text = (
                f"{heading}\n"
                "1. Plaintiff Cedar Ridge Logistics LLC is a domestic corporation.\n"
            )
            self.assertTrue(
                mb._page_has_parties_section_heading(text),
                msg=f"failed for {heading!r}",
            )
            self.assertTrue(mb._PARTIES_HEADING_START_RE.match(text))

    def test_section_article_roman_punctuation_prefixed_headings(self):
        samples = (
            "SECTION 2 — PARTIES",
            "SECTION 2: PARTIES",
            "ARTICLE III: PARTIES",
            "ARTICLE III — PARTIES",
            "PART IV. PARTIES",
            "IV. PARTIES",
        )
        for heading in samples:
            text = (
                f"{heading}\n"
                "1. Plaintiff Oakline Carrier Inc. is a domestic corporation.\n"
                "2. Defendant Pine Harbor Depot LLC is a limited liability company.\n"
            )
            self.assertTrue(
                mb._page_has_parties_section_heading(text),
                msg=f"failed for {heading!r}",
            )

    def test_prefixed_stopping_headings_end_contiguous_span(self):
        doc = _normalized(
            _doc(
                520,
                "complaint",
                [
                    "SUPREME COURT OF THE STATE OF NEW YORK\n"
                    "Cedar Ridge Logistics LLC v. Pine Harbor Depot LLC\n",
                    "14 PARTIES\n"
                    "1. Plaintiff Cedar Ridge Logistics LLC is a domestic corporation.\n",
                    "2. Defendant Pine Harbor Depot LLC is a limited liability company.\n",
                    "15 FACTS\n"
                    "3. A shipment was damaged on March 1, 2024.\n",
                ],
                filename="nyscef_doc_no_520_complaint.pdf",
            )
        )
        section_ids = mb._collect_parties_section_page_ids(
            mb._page_lookup_from_documents([doc])
        )
        pages = [
            mb._page_lookup_from_documents([doc])[pid]["page"]["page_number"]
            for pid in section_ids
        ]
        self.assertEqual(pages, [2, 3])
        self.assertNotIn(4, pages)
        self.assertTrue(mb._page_starts_major_pleading_section("15 FACTS\n3. Event.\n"))
        self.assertTrue(
            mb._page_starts_major_pleading_section("SECTION 3 — FACTS\n3. Event.\n")
        )
        self.assertTrue(
            mb._page_starts_major_pleading_section(
                "ARTICLE IV: JURISDICTION\n1. This court has jurisdiction.\n"
            )
        )

    def test_below_cutoff_prefixed_section_pages_force_retained(self):
        docs = [
            _normalized(
                _doc(
                    521,
                    "complaint",
                    [
                        "Summons cover page without role paragraphs.\n",
                        "SECTION 2 — PARTIES\n"
                        "1. Plaintiff North Quay Freight LP is a limited liability "
                        "partnership authorized to do business in this state.\n",
                        "2. Defendant South Pier Warehouse Inc. is a domestic "
                        "corporation.\n",
                        "ARTICLE III: FACTS\n"
                        "3. Cargo was lost in transit.\n",
                    ],
                    filename="nyscef_doc_no_521_summons_complaint.pdf",
                )
            )
        ] + _filler_filings()
        result = mb.retrieve_canonical_records(
            docs,
            "Who are the parties and what are their roles in this action?",
            top_k=4,
        )
        complaint_pages = {
            hit["pdf_page"]
            for hit in result["results"]
            if hit["nyscef_document_number"] == 521
        }
        self.assertTrue({2, 3}.issubset(complaint_pages))
        for page in (2, 3):
            hit = next(
                h
                for h in result["results"]
                if h["nyscef_document_number"] == 521 and h["pdf_page"] == page
            )
            self.assertEqual(hit["page_id"], f"nyscef-521-page-{page:04d}")
            self.assertTrue(str(hit["result_id"]).startswith("cret-nyscef-521-page-"))


class ProceduralNoiseHardExclusionTests(unittest.TestCase):
    def setUp(self):
        self.party_query = (
            "Who are the parties and what are their roles in this action?"
        )

    def _hit(self, **kwargs):
        base = {
            "result_id": kwargs.get("result_id", "x1"),
            "page_id": kwargs.get("page_id", "nyscef-1-p1"),
            "nyscef_document_number": kwargs.get("nyscef", 1),
            "pdf_page": kwargs.get("page", 1),
            "source_filename": kwargs.get("filename", "doc.pdf"),
            "document_type": kwargs.get("doc_type", "other"),
            "excerpt": kwargs.get("excerpt", ""),
            "page_text": kwargs.get("page_text", kwargs.get("excerpt", "")),
            "classifications": list(kwargs.get("classifications") or []),
            "assertion_kind": kwargs.get("assertion_kind", "unknown"),
            "score": kwargs.get("score", 1.0),
        }
        return base

    def test_motions_rji_affirmations_service_orders_with_names_excluded(self):
        noise_hits = [
            self._hit(
                result_id="m1",
                page_id="nyscef-601-p1",
                nyscef=601,
                doc_type="motion",
                filename="nyscef_doc_no_601_notice_of_motion.pdf",
                page_text=(
                    "Notice of Motion for Summary Judgment returnable June 1, 2024. "
                    "Alpha Freight LP are Plaintiffs. Beta Depot Inc. are Defendants. "
                    "Movant seeks dismissal on the procedural calendar."
                ),
            ),
            self._hit(
                result_id="r1",
                page_id="nyscef-602-p1",
                nyscef=602,
                doc_type="other",
                filename="nyscef_doc_no_602_rji.pdf",
                page_text=(
                    "Request for Judicial Intervention. RJI addendum repeats "
                    "Plaintiff Alpha Freight LP and Defendant Beta Depot Inc. "
                    "without changing party status."
                ),
            ),
            self._hit(
                result_id="a1",
                page_id="nyscef-603-p1",
                nyscef=603,
                doc_type="affirmation",
                filename="nyscef_doc_no_603_affirmation_of_service.pdf",
                page_text=(
                    "Affirmation of service. Plaintiff Alpha Freight LP is named in "
                    "the caption. Defendant Beta Depot Inc. received papers by mail."
                ),
            ),
            self._hit(
                result_id="s1",
                page_id="nyscef-604-p1",
                nyscef=604,
                doc_type="affidavit",
                filename="nyscef_doc_no_604_affidavit_of_service.pdf",
                page_text=(
                    "Affidavit of service. Proof of service on Beta Depot Inc. "
                    "Plaintiff Alpha Freight LP appears in the caption block."
                ),
            ),
            self._hit(
                result_id="o1",
                page_id="nyscef-605-p1",
                nyscef=605,
                doc_type="order",
                filename="nyscef_doc_no_605_scheduling_order.pdf",
                page_text=(
                    "Scheduling Order. IT IS HEREBY ORDERED that the conference is "
                    "adjourned. Plaintiff Alpha Freight LP and Defendant Beta Depot "
                    "Inc. shall appear. Procedural calendar updated."
                ),
            ),
        ]
        for hit in noise_hits:
            self.assertFalse(
                de.hit_is_material_for_party_role_question(hit),
                msg=hit["page_id"],
            )

        pleading = self._hit(
            result_id="p1",
            page_id="nyscef-500-p2",
            nyscef=500,
            page=2,
            doc_type="complaint",
            filename="nyscef_doc_no_500_complaint.pdf",
            page_text=(
                "14 PARTIES\n"
                "1. Plaintiff Alpha Freight LP is a limited liability partnership.\n"
                "2. Defendant Beta Depot Inc. is a domestic corporation.\n"
            ),
            excerpt="14 PARTIES\n1. Plaintiff Alpha Freight LP is a limited liability partnership.",
            classifications=["party_identity"],
            assertion_kind="verified_record_fact",
            score=20.0,
        )
        packet = de.build_evidence_packet(
            self.party_query,
            {"query": self.party_query, "results": [pleading] + noise_hits},
        )
        page_ids = {hit["page_id"] for hit in packet["retrieval_hits"]}
        self.assertEqual(page_ids, {"nyscef-500-p2"})

    def test_procedural_record_material_party_change_retained(self):
        order = self._hit(
            result_id="ord-keep",
            page_id="nyscef-610-p1",
            nyscef=610,
            doc_type="order",
            filename="nyscef_doc_no_610_decision_and_order.pdf",
            page_text=(
                "Decision and Order. IT IS HEREBY ORDERED that Canyon Repair LLC is "
                "dismissed as a party, without prejudice to renewal if capacity is "
                "later established. The caption role conflict remains unresolved."
            ),
            excerpt=(
                "Canyon Repair LLC is dismissed as a party, without prejudice to "
                "renewal if capacity is later established."
            ),
            classifications=["court_order"],
            score=8.0,
        )
        motion_add = self._hit(
            result_id="mot-keep",
            page_id="nyscef-611-p1",
            nyscef=611,
            doc_type="motion",
            filename="nyscef_doc_no_611_motion.pdf",
            page_text=(
                "Notice of Motion. Movant seeks leave to amend the complaint to add "
                "as a party Prairie Notice Carrier LP, substituted as defendant for "
                "the incorrectly named Prairie Notice Co."
            ),
            excerpt=(
                "leave to amend the complaint to add as a party Prairie Notice "
                "Carrier LP, substituted as defendant"
            ),
            classifications=["motion"],
            score=7.0,
        )
        self.assertTrue(de.hit_is_material_for_party_role_question(order))
        self.assertTrue(de.hit_is_material_for_party_role_question(motion_add))
        packet = de.build_evidence_packet(
            self.party_query,
            {"query": self.party_query, "results": [order, motion_add]},
        )
        page_ids = {hit["page_id"] for hit in packet["retrieval_hits"]}
        self.assertEqual(page_ids, {"nyscef-610-p1", "nyscef-611-p1"})


class PartyRolePacketBudgetTests(unittest.TestCase):
    def setUp(self):
        self.party_query = (
            "Who are the parties and what are their roles in this action?"
        )

    def test_controlling_pleading_survives_total_budget(self):
        plaintiffs = ", ".join(f"Budget Plaintiff {i} LLC" for i in range(1, 12))
        defendants = ", ".join(f"Budget Defendant {i} Inc" for i in range(1, 12))
        caption = (
            "SUPREME COURT OF THE STATE OF NEW YORK\n"
            f"{plaintiffs},\n"
            "                                   Plaintiffs,\n"
            "                 -against-\n"
            f"{defendants},\n"
            "                                   Defendants.\n"
            "Index No. 121212/2024\n"
        )
        parties = (
            "14 PARTIES\n"
            "1. Plaintiff Budget Plaintiff 1 LLC is a domestic corporation "
            "authorized to do business in this state.\n"
            "2. Defendant Budget Defendant 1 Inc is a domestic corporation.\n"
            "3. Mesa Trailer Repair LLC, third-party defendant, was joined herein "
            "as a necessary party.\n"
        )
        hits = [
            {
                "result_id": "cap-1",
                "page_id": "nyscef-700-page-0001",
                "nyscef_document_number": 700,
                "pdf_page": 1,
                "source_filename": "nyscef_doc_no_700_complaint.pdf",
                "document_type": "complaint",
                "excerpt": caption,
                "page_text": caption + "COMPLAINT\n",
                "classifications": ["party_identity"],
                "assertion_kind": "verified_record_fact",
                "score": 30.0,
            },
            {
                "result_id": "par-2",
                "page_id": "nyscef-700-page-0002",
                "nyscef_document_number": 700,
                "pdf_page": 2,
                "source_filename": "nyscef_doc_no_700_complaint.pdf",
                "document_type": "complaint",
                "excerpt": parties,
                "page_text": parties,
                "classifications": ["party_identity"],
                "assertion_kind": "verified_record_fact",
                "party_role_section_expanded": True,
                "score": 28.0,
            },
        ]
        # Many redundant low-value operative pages to pressure the budget.
        for i in range(20):
            hits.append(
                {
                    "result_id": f"extra-{i}",
                    "page_id": f"nyscef-8{i:02d}-p1",
                    "nyscef_document_number": 800 + i,
                    "pdf_page": 1,
                    "source_filename": f"nyscef_doc_no_{800 + i}_answer.pdf",
                    "document_type": "answer",
                    "excerpt": (
                        f"Answer paragraph restating that Plaintiff Budget Plaintiff "
                        f"1 LLC is plaintiff and Defendant Budget Defendant 1 Inc is "
                        f"defendant without new qualifications. Filler {i}. " + ("x" * 400)
                    ),
                    "page_text": (
                        f"ANSWER. Plaintiff Budget Plaintiff 1 LLC is plaintiff. "
                        f"Defendant Budget Defendant 1 Inc is a domestic corporation. "
                        f"Filler {i}."
                    ),
                    "classifications": ["party_identity"],
                    "assertion_kind": "verified_record_fact",
                    "score": 2.0,
                }
            )
        # Duplicate of controlling parties page (redundant).
        hits.append(dict(hits[1], result_id="par-2-dup"))

        packet = de.build_evidence_packet(
            self.party_query,
            {"query": self.party_query, "results": hits},
        )
        page_ids = [hit["page_id"] for hit in packet["retrieval_hits"]]
        self.assertIn("nyscef-700-page-0001", page_ids)
        self.assertIn("nyscef-700-page-0002", page_ids)
        self.assertEqual(page_ids.count("nyscef-700-page-0002"), 1)
        self.assertLessEqual(len(packet["retrieval_hits"]), de.PARTY_ROLE_PACKET_MAX_HITS)
        self.assertIn("packet_budget", packet["materiality_filter"])
        budget = packet["materiality_filter"]["packet_budget"]
        self.assertLessEqual(budget["serialized_chars"], de.PARTY_ROLE_PACKET_MAX_CHARS)
        self.assertGreaterEqual(budget["excluded_by_budget"], 1)

        caption_hit = next(
            hit for hit in packet["retrieval_hits"] if hit["page_id"] == "nyscef-700-page-0001"
        )
        parties_hit = next(
            hit for hit in packet["retrieval_hits"] if hit["page_id"] == "nyscef-700-page-0002"
        )
        self.assertIn("Budget Plaintiff 11 LLC", caption_hit["excerpt"])
        self.assertIn("Budget Defendant 11 Inc", caption_hit["excerpt"])
        self.assertIn("joined herein as a necessary party", parties_hit["excerpt"])
        # Never truncate mid-name to meet budget.
        self.assertFalse(caption_hit["excerpt"].endswith("Budget"))
        self.assertFalse(parties_hit["excerpt"].endswith("Mesa"))

    def test_controlling_source_protects_initial_and_expanded_section_pages(self):
        def hit(doc_no, page, doc_type, text, *, expanded=False, score=1.0):
            value = {
                "result_id": f"r-{doc_no}-{page}",
                "page_id": f"nyscef-{doc_no}-page-{page:04d}",
                "nyscef_document_number": doc_no,
                "pdf_page": page,
                "source_filename": f"filing_{doc_no}_{doc_type}.pdf",
                "document_type": doc_type,
                "excerpt": text,
                "page_text": text,
                "classifications": ["party_identity"],
                "assertion_kind": "verified_record_fact",
                "score": score,
            }
            if expanded:
                value["party_role_section_expanded"] = True
            return value

        complaint = [
            hit(
                41,
                1,
                "complaint",
                "SUPREME COURT\nNorth Harbor LLC, Plaintiff, -against- East Ridge Inc., Defendant.",
                score=30.0,
            ),
            hit(41, 2, "complaint", "PARTIES\n1. North Harbor LLC is the Plaintiff.", expanded=True, score=29.0),
            # These pages arrived in initial retrieval and intentionally do not
            # carry the expansion marker.
            hit(41, 3, "complaint", "2. East Ridge Inc. is the Defendant.", score=28.0),
            hit(41, 4, "complaint", "3. West Field LP is joined as a necessary party.", score=27.0),
            hit(41, 5, "complaint", "4. South Creek Trust is a notice defendant.", expanded=True, score=26.0),
        ]
        answer = [
            hit(
                52,
                page,
                "answer",
                f"ANSWER page {page}. North Harbor LLC is Plaintiff and East Ridge Inc. is Defendant.",
                expanded=page in {2, 8},
                score=20.0 - page,
            )
            for page in range(1, 9)
        ]
        duplicate = dict(complaint[2], result_id="duplicate-middle")

        selected, meta = de.apply_party_role_packet_budget(
            complaint + answer + [duplicate], max_hits=6, max_chars=24000
        )
        selected_ids = [item["page_id"] for item in selected]

        for page in range(1, 6):
            page_id = f"nyscef-41-page-{page:04d}"
            self.assertIn(page_id, selected_ids)
            protected = next(item for item in selected if item["page_id"] == page_id)
            self.assertTrue(protected.get("controlling_party_role_pleading"))
            self.assertEqual(protected["nyscef_document_number"], 41)
            self.assertEqual(protected["pdf_page"], page)
        self.assertEqual(selected_ids.count("nyscef-41-page-0003"), 1)
        self.assertLessEqual(len(selected_ids), 6)
        self.assertLessEqual(sum(1 for item in selected if item["nyscef_document_number"] == 52), 1)
        self.assertEqual(meta["protected_hit_count"], 5)


class PartyRoleEntityResidenceExtractionTests(unittest.TestCase):
    """Focused regressions for entity/residence/OCR party-role extraction."""

    def setUp(self):
        self.party_query = (
            "Who are the parties and what are their roles in this action?"
        )
        self.motion_query = (
            "What relief does the notice of motion for summary judgment seek?"
        )

    def test_entity_form_and_residence_ppb_lines_retained(self):
        doc = _normalized(
            _doc(
                531,
                "complaint",
                [
                    "SUPREME COURT caption.\nAlpha Carrier LP v. Beta Depot Inc.\n",
                    "PARTIES\n"
                    "1. Plaintiff Alpha Carrier LP is a limited liability partnership "
                    "authorized to do business in this state.\n"
                    "2. Alpha Carrier LP maintained a principal place of business "
                    "located at 10 Harbor Way, Buffalo, NY 14201.\n"
                    "3. Defendant Beta Depot Inc. is a domestic corporation.\n"
                    "4. Beta Depot Inc. maintained a principal place of business "
                    "located at 20 Pier Street, Buffalo, NY 14202.\n"
                    "5. Defendant Carla Rivers is a notice defendant.\n"
                    "6. Carla Rivers is a resident of the State of New York "
                    "residing in Erie County.\n"
                    "7. Defendant Delta Notice Carrier LLC is a notice defendant.\n",
                ],
                filename="nyscef_doc_no_531_complaint.pdf",
            )
        )
        excerpt = mb._party_role_evidence_excerpt(
            {
                "page": doc["pages"][1],
                "document": doc,
                "nyscef_document_number": 531,
                "filename": doc["filename"],
                "document_type": "complaint",
                "segment": None,
            },
            doc["pages"][1]["text"],
        )
        self.assertIn("limited liability partnership", excerpt)
        self.assertIn("domestic corporation", excerpt)
        self.assertIn("principal place of business", excerpt)
        self.assertIn("14201", excerpt)
        self.assertIn("resident of the State of New York", excerpt)
        self.assertIn("notice defendant", excerpt)

    def test_intra_word_ocr_spacing_tolerated_for_entity_forms(self):
        text = (
            "PARTIES\n"
            "1. Defendant Ortov Lighting Inc. is a notice defendant.\n"
            "2. Ortov was and still is a domesti c corporation duly authorized "
            "and existing under the laws of the State of New York.\n"
            "3. President Sai was and still is a domestic limited liability "
            "com pany duly authorized and existing under the laws of the "
            "State of New York.\n"
            "4. Sovereign was and still is a do mestic corporation.\n"
        )
        excerpt = mb._extract_party_role_passages(text)
        self.assertIn("domesti c corporation", excerpt)
        self.assertIn("com pany", excerpt)
        self.assertIn("do mestic corporation", excerpt)
        self.assertIn("notice defendant", excerpt)
        self.assertTrue(
            mb._party_role_unit_has_identity_signal(
                "Ortov was and still is a domesti c corporation duly authorized."
            )
        )
        self.assertEqual(
            mb.heal_ocr_intra_word_spaces("domesti c corporation"),
            "domestic corporation",
        )
        self.assertEqual(
            mb.heal_ocr_intra_word_spaces("limited liability com pany"),
            "limited liability company",
        )

    def test_notice_defendant_allegations_remain_retained(self):
        text = (
            "PARTIES\n"
            "1. Defendant Meadow Bridge Repair Inc. is a notice defendant to "
            "the instant action.\n"
            "2. Meadow is a domestic corporation.\n"
            "3. Prairie Notice Carrier LP is a notice defendant under the policy.\n"
        )
        excerpt = mb._extract_party_role_passages(text)
        self.assertIn("notice defendant", excerpt)
        self.assertIn("Meadow Bridge Repair Inc.", excerpt)
        self.assertIn("Prairie Notice Carrier LP", excerpt)

    def test_non_party_behavior_unchanged_and_procedural_noise_not_retained(self):
        docs = [
            _normalized(
                _doc(
                    532,
                    "complaint",
                    [
                        "Caption page.\n",
                        "PARTIES\n"
                        "1. Plaintiff North Quay Freight LP is a domestic corporation.\n"
                        "2. Defendant South Pier Warehouse Inc. is a domestic "
                        "corporation with its principal place of business in Albany.\n",
                        "FACTS\n3. Cargo was lost in transit on March 1, 2024.\n",
                    ],
                    filename="nyscef_doc_no_532_summons_complaint.pdf",
                )
            ),
            _normalized(
                _doc(
                    533,
                    "motion",
                    [
                        "Notice of Motion for Summary Judgment returnable June 1, 2024. "
                        "Movant seeks dismissal on procedural calendar grounds. "
                        + ("z" * 80)
                    ],
                    filename="nyscef_doc_no_533_notice_of_motion.pdf",
                )
            ),
        ]
        party_result = mb.retrieve_canonical_records(
            docs, self.party_query, top_k=8
        )
        party_packet = de.build_evidence_packet(
            self.party_query, party_result
        )
        combined = " ".join(
            hit.get("excerpt") or "" for hit in party_packet["retrieval_hits"]
        )
        self.assertIn("principal place of business", combined)
        self.assertIn("domestic corporation", combined)

        motion_result = mb.retrieve_canonical_records(
            docs, self.motion_query, top_k=5
        )
        motion_packet = de.build_evidence_packet(
            self.motion_query, motion_result
        )
        self.assertNotIn("materiality_filter", motion_packet)
        for hit in motion_packet["retrieval_hits"]:
            self.assertNotIn("party_role_section_expanded", hit)

        noise = {
            "result_id": "m1",
            "page_id": "nyscef-533-p1",
            "nyscef_document_number": 533,
            "pdf_page": 1,
            "source_filename": "nyscef_doc_no_533_notice_of_motion.pdf",
            "document_type": "motion",
            "excerpt": "Notice of Motion for Summary Judgment.",
            "page_text": (
                "Notice of Motion for Summary Judgment returnable June 1, 2024. "
                "North Quay Freight LP are Plaintiffs. South Pier Warehouse Inc. "
                "are Defendants. Movant seeks dismissal on the procedural calendar."
            ),
            "classifications": [],
            "assertion_kind": "unknown",
        }
        self.assertFalse(de.hit_is_material_for_party_role_question(noise))

    def test_zip_codes_do_not_split_party_paragraphs(self):
        text = (
            "79. Triborough maintained a principal place of business located at "
            "35-06 Farrington St, 2nd Fl, Flushing, NY 11354. "
            "80. That at all times mentioned herein, Triborough transacted "
            "business in the State of New York."
        )
        units = mb._split_passage_units(text)
        self.assertTrue(
            any("11354" in unit and unit.strip().startswith("79.") for unit in units)
        )
        excerpt = mb._extract_party_role_passages("PARTIES\n" + text)
        self.assertIn("11354", excerpt)
        # Party-specific forum business allegations remain in scope.
        self.assertIn("transacted business in the State of New York", excerpt)


class PartyRoleEvidenceScopeCorrectionTests(unittest.TestCase):
    """Generic party-role evidence-scope: intro retained, facts excluded."""

    def setUp(self):
        self.party_query = (
            "Who are the parties and what are their roles in this action?"
        )
        self.motion_query = (
            "What relief does the notice of motion for summary judgment seek?"
        )
        self.page_text = (
            "SUPREME COURT OF THE STATE OF NEW YORK\n"
            "COUNTY OF KINGS\n"
            "Harbor Mill Carrier Inc.,\n"
            "Plaintiff,\n"
            "-against-\n"
            "Northshore Logistics LP,\n"
            "Defendant.\n"
            "\n"
            "NATURE OF THE ACTION\n"
            "1. This is an action for breach of a freight contract arising from "
            "failed delivery of commercial goods.\n"
            "\n"
            "PARTIES\n"
            "2. Plaintiff Harbor Mill Carrier Inc. is a domestic corporation "
            "authorized to do business in this state.\n"
            "3. Harbor Mill Carrier Inc. maintained a principal place of business "
            "in Kings County.\n"
            "4. Defendant Northshore Logistics LP is a limited liability "
            "partnership.\n"
            "5. That at all times mentioned herein, Northshore Logistics LP "
            "transacted business in the State of New York and within this County.\n"
            "6. Venue is proper because Defendant Northshore Logistics LP resides "
            "in Kings County.\n"
            "\n"
            "FACTUAL BACKGROUND\n"
            "7. On March 1, 2024, Plaintiff tendered a shipment of widgets to "
            "Defendant at the Brooklyn terminal with a detailed routing history.\n"
            "8. Defendant diverted the cargo through multiple warehouses over "
            "several weeks and failed to deliver the goods as scheduled.\n"
            "\n"
            "JURISDICTION\n"
            "9. This Court has jurisdiction over this action pursuant to CPLR 301.\n"
        )

    def _entry(self, text, nyscef=560):
        doc = _normalized(
            _doc(
                nyscef,
                "complaint",
                [text],
                filename=f"nyscef_doc_no_{nyscef}_complaint.pdf",
            )
        )
        entry = {
            "page": doc["pages"][0],
            "document": doc,
            "nyscef_document_number": nyscef,
            "filename": doc["filename"],
            "document_type": "complaint",
            "segment": None,
        }
        return entry, doc["pages"][0]["text"]

    def test_intro_retained_for_party_role(self):
        entry, page_text = self._entry(self.page_text)
        excerpt = mb._party_role_evidence_excerpt(entry, page_text)
        self.assertIn("NATURE OF THE ACTION", excerpt)
        self.assertIn("breach of a freight contract", excerpt)

    def test_detailed_facts_excluded(self):
        entry, page_text = self._entry(self.page_text)
        excerpt = mb._party_role_evidence_excerpt(entry, page_text)
        self.assertNotIn("March 1, 2024", excerpt)
        self.assertNotIn("diverted the cargo", excerpt)
        self.assertNotIn("detailed routing history", excerpt)

    def test_transition_heading_stops_retention(self):
        excerpt = mb._extract_party_role_passages(self.page_text)
        self.assertIn("Venue is proper", excerpt)
        # Retention stops at the factual-background transition heading.
        self.assertNotIn("FACTUAL BACKGROUND", excerpt)
        self.assertTrue(
            mb._PARTY_ROLE_HARD_STOP_SECTION_START_RE.match(
                "FACTUAL BACKGROUND\n7. On March 1, 2024, an event occurred.\n"
            )
        )
        self.assertTrue(
            mb._page_starts_major_pleading_section(
                "FACTUAL BACKGROUND\n7. On March 1, 2024, an event occurred.\n"
            )
        )

    def test_party_specific_business_allegation_retained(self):
        entry, page_text = self._entry(self.page_text)
        excerpt = mb._party_role_evidence_excerpt(entry, page_text)
        self.assertIn("transacted business in the State of New York", excerpt)
        self.assertIn("Venue is proper", excerpt)

    def test_generic_unrelated_jurisdiction_allegation_excluded(self):
        entry, page_text = self._entry(self.page_text)
        excerpt = mb._party_role_evidence_excerpt(entry, page_text)
        self.assertNotIn("This Court has jurisdiction", excerpt)
        self.assertNotIn("pursuant to CPLR 301", excerpt)

    def test_caption_and_parties_preserved(self):
        entry, page_text = self._entry(self.page_text)
        excerpt = mb._party_role_evidence_excerpt(entry, page_text)
        self.assertIn("Harbor Mill Carrier Inc.", excerpt)
        self.assertIn("Northshore Logistics LP", excerpt)
        self.assertIn("Plaintiff", excerpt)
        self.assertIn("Defendant", excerpt)
        self.assertIn("PARTIES", excerpt)
        self.assertIn("domestic corporation", excerpt)
        self.assertIn("principal place of business", excerpt)

    def test_non_party_behavior_unchanged(self):
        docs = [
            _normalized(
                _doc(
                    561,
                    "complaint",
                    [self.page_text],
                    filename="nyscef_doc_no_561_complaint.pdf",
                )
            ),
            _normalized(
                _doc(
                    562,
                    "motion",
                    [
                        "Notice of Motion for Summary Judgment returnable June 1, 2024. "
                        "Harbor Mill Carrier Inc. are Plaintiffs. Northshore Logistics LP "
                        "are Defendants. Movant seeks dismissal on the procedural calendar."
                    ],
                    filename="nyscef_doc_no_562_notice_of_motion.pdf",
                )
            ),
        ]
        motion_result = mb.retrieve_canonical_records(
            docs, self.motion_query, top_k=5
        )
        motion_packet = de.build_evidence_packet(
            self.motion_query, motion_result
        )
        self.assertNotIn("materiality_filter", motion_packet)
        for hit in motion_packet["retrieval_hits"]:
            self.assertNotIn("party_role_section_expanded", hit)

        noise = {
            "result_id": "m1",
            "page_id": "nyscef-562-p1",
            "nyscef_document_number": 562,
            "pdf_page": 1,
            "source_filename": "nyscef_doc_no_562_notice_of_motion.pdf",
            "document_type": "motion",
            "excerpt": "Notice of Motion for Summary Judgment.",
            "page_text": docs[1]["pages"][0]["text"],
            "classifications": [],
            "assertion_kind": "unknown",
        }
        self.assertFalse(de.hit_is_material_for_party_role_question(noise))

    def test_budget_not_materially_regressed(self):
        # Scoped excerpts must remain within existing combined budget.
        entry, page_text = self._entry(self.page_text)
        excerpt = mb._party_role_evidence_excerpt(entry, page_text)
        self.assertLessEqual(len(excerpt), mb.PARTY_ROLE_COMBINED_EXCERPT_MAX)
        # Contiguous PARTIES expansion still stops before FACTS pages.
        doc = _normalized(
            _doc(
                563,
                "complaint",
                [
                    "SUPREME COURT\nHarbor Mill Carrier Inc. v. Northshore Logistics LP\n",
                    "PARTIES\n"
                    "1. Plaintiff Harbor Mill Carrier Inc. is a domestic corporation.\n",
                    "2. Defendant Northshore Logistics LP is a limited liability "
                    "partnership.\n",
                    "FACTUAL BACKGROUND\n"
                    "3. On March 1, 2024, cargo was diverted through warehouses.\n",
                ],
                filename="nyscef_doc_no_563_complaint.pdf",
            )
        )
        section_ids = mb._collect_parties_section_page_ids(
            mb._page_lookup_from_documents([doc])
        )
        pages = [
            mb._page_lookup_from_documents([doc])[pid]["page"]["page_number"]
            for pid in section_ids
        ]
        self.assertEqual(pages, [2, 3])
        self.assertNotIn(4, pages)


class PartyRoleIntroductionRetentionTests(unittest.TestCase):
    """Focused generic intro-section retention: colon headings, protection, budgets."""

    def setUp(self):
        self.party_query = (
            "Who are the parties and what are their roles in this action?"
        )
        self.motion_query = (
            "What relief does the notice of motion for summary judgment seek?"
        )
        self.caption = (
            "SUPREME COURT OF THE STATE OF NEW YORK\n"
            "COUNTY OF KINGS\n"
            "Harbor Mill Carrier Inc.,\n"
            "Plaintiff,\n"
            "-against-\n"
            "Northshore Logistics LP,\n"
            "Defendant.\n"
        )

    def _entry(self, text, nyscef=570):
        doc = _normalized(
            _doc(
                nyscef,
                "complaint",
                [text],
                filename=f"nyscef_doc_no_{nyscef}_complaint.pdf",
            )
        )
        entry = {
            "page": doc["pages"][0],
            "document": doc,
            "nyscef_document_number": nyscef,
            "filename": doc["filename"],
            "document_type": "complaint",
            "segment": None,
        }
        return entry, doc["pages"][0]["text"]

    def test_colon_style_introduction_heading_detected(self):
        samples = (
            "Plaintiffs, by their attorneys, allege as follows: INTRODUCTION\n"
            "1. This is an action for breach of a freight contract.\n",
            "Plaintiffs allege as follows: NATURE OF THE ACTION\n"
            "1. This is an action for negligence arising from failed delivery.\n",
            "The complaint states the following: PRELIMINARY STATEMENT\n"
            "This action arises from a commercial carriage dispute.\n",
        )
        for text in samples:
            self.assertTrue(
                mb._PARTY_ROLE_RETAINABLE_SECTION_HEADING_RE.search(text),
                msg=f"colon heading not detected in {text[:60]!r}",
            )
            excerpt = mb._extract_party_role_passages(text)
            self.assertTrue(
                re.search(
                    r"(?i)\b(?:introduction|nature of (?:the )?action|"
                    r"preliminary statement)\b",
                    excerpt,
                ),
                msg=f"heading missing from excerpt: {excerpt!r}",
            )
            self.assertTrue(
                re.search(r"(?i)this is an action|this action arises", excerpt),
                msg=f"intro body missing from excerpt: {excerpt!r}",
            )

    def test_existing_boundary_detection_preserved(self):
        start_text = "INTRODUCTION\n1. This is an action for breach.\n"
        self.assertTrue(mb._PARTY_ROLE_RETAINABLE_SECTION_START_RE.match(start_text))
        newline_text = "Caption block.\nNATURE OF THE ACTION\n1. This is an action.\n"
        self.assertTrue(
            mb._PARTY_ROLE_RETAINABLE_SECTION_HEADING_RE.search(newline_text)
        )
        sentence_text = "Something ends. PRELIMINARY STATEMENT\nThis action arises.\n"
        self.assertTrue(
            mb._PARTY_ROLE_RETAINABLE_SECTION_HEADING_RE.search(sentence_text)
        )
        excerpt = mb._extract_party_role_passages(start_text)
        self.assertIn("INTRODUCTION", excerpt)
        self.assertIn("This is an action for breach", excerpt)

    def test_cross_page_introduction_continuation(self):
        doc = _normalized(
            _doc(
                571,
                "complaint",
                [
                    self.caption
                    + "Plaintiffs allege as follows: INTRODUCTION\n"
                    + "1. This is an action for breach of a freight contract.\n",
                    "2. The opening section continues with a concise statement of "
                    "the commercial carriage dispute without warehouse chronology.\n",
                    "PARTIES\n"
                    "3. Plaintiff Harbor Mill Carrier Inc. is a domestic corporation.\n",
                    "FACTUAL BACKGROUND\n"
                    "4. On March 1, 2024, cargo was diverted through warehouses.\n",
                ],
                filename="nyscef_doc_no_571_complaint.pdf",
            )
        )
        lookup = mb._page_lookup_from_documents([doc])
        intro_ids, continuations = mb._collect_intro_section_page_ids(lookup)
        pages = [lookup[pid]["page"]["page_number"] for pid in intro_ids]
        self.assertEqual(pages, [1, 2])
        self.assertEqual(len(continuations), 1)
        cont_entry = lookup[intro_ids[1]]
        cont_excerpt = mb._party_role_evidence_excerpt(
            cont_entry,
            cont_entry["page"]["text"],
            intro_continuation=True,
        )
        self.assertIn("opening section continues", cont_excerpt)
        self.assertNotIn("March 1, 2024", cont_excerpt)
        self.assertNotIn("warehouses", cont_excerpt)

    def test_introduction_page_marked_protected(self):
        page_text = (
            self.caption
            + "NATURE OF THE ACTION\n"
            + "1. This is an action for breach of a freight contract.\n"
        )
        hit = {
            "result_id": "intro-1",
            "page_id": "nyscef-572-page-0001",
            "nyscef_document_number": 572,
            "pdf_page": 1,
            "source_filename": "nyscef_doc_no_572_complaint.pdf",
            "document_type": "complaint",
            "excerpt": page_text,
            "page_text": page_text,
            "classifications": [],
            "assertion_kind": "verified_record_fact",
            "score": 1.0,
        }
        self.assertTrue(de._hit_is_party_role_caption_or_section_page(hit))
        marked = de._mark_controlling_party_role_group([hit])
        self.assertTrue(marked[0].get("controlling_party_role_pleading"))

    def test_introduction_survives_necessity_filter(self):
        # Intro body alone may lack identity cues; protection must keep it.
        intro_text = (
            "INTRODUCTION\n"
            "1. This is an action for breach of a freight contract arising from "
            "failed delivery of commercial goods.\n"
        )
        hits = [
            {
                "result_id": "cap-1",
                "page_id": "nyscef-573-page-0001",
                "nyscef_document_number": 573,
                "pdf_page": 1,
                "source_filename": "nyscef_doc_no_573_complaint.pdf",
                "document_type": "complaint",
                "excerpt": self.caption,
                "page_text": self.caption + "COMPLAINT\n",
                "classifications": ["party_identity"],
                "assertion_kind": "verified_record_fact",
                "score": 20.0,
            },
            {
                "result_id": "intro-2",
                "page_id": "nyscef-573-page-0002",
                "nyscef_document_number": 573,
                "pdf_page": 2,
                "source_filename": "nyscef_doc_no_573_complaint.pdf",
                "document_type": "complaint",
                "excerpt": intro_text,
                "page_text": intro_text,
                "classifications": [],
                "assertion_kind": "verified_record_fact",
                "party_role_section_expanded": True,
                "score": 0.5,
            },
            {
                "result_id": "noise-3",
                "page_id": "nyscef-574-page-0001",
                "nyscef_document_number": 574,
                "pdf_page": 1,
                "source_filename": "nyscef_doc_no_574_rji.pdf",
                "document_type": "rji",
                "excerpt": "Request for Judicial Intervention calendar notation.",
                "page_text": "Request for Judicial Intervention calendar notation.",
                "classifications": [],
                "assertion_kind": "unknown",
                "score": 5.0,
            },
        ]
        kept, meta = de.filter_hits_for_party_role_materiality(hits)
        page_ids = [hit["page_id"] for hit in kept]
        self.assertIn("nyscef-573-page-0002", page_ids)
        self.assertNotIn("nyscef-574-page-0001", page_ids)
        intro_hit = next(h for h in kept if h["page_id"] == "nyscef-573-page-0002")
        self.assertTrue(intro_hit.get("controlling_party_role_pleading"))
        self.assertEqual(meta["intent"], "party_role")

    def test_introduction_survives_packet_budget(self):
        intro_text = (
            "INTRODUCTION\n"
            "1. This is an action for breach of a freight contract.\n"
        )
        parties = (
            "PARTIES\n"
            "1. Plaintiff Harbor Mill Carrier Inc. is a domestic corporation.\n"
            "2. Defendant Northshore Logistics LP is a limited liability partnership.\n"
        )
        hits = [
            {
                "result_id": "cap-1",
                "page_id": "nyscef-575-page-0001",
                "nyscef_document_number": 575,
                "pdf_page": 1,
                "source_filename": "nyscef_doc_no_575_complaint.pdf",
                "document_type": "complaint",
                "excerpt": self.caption,
                "page_text": self.caption,
                "classifications": ["party_identity"],
                "assertion_kind": "verified_record_fact",
                "score": 30.0,
            },
            {
                "result_id": "intro-2",
                "page_id": "nyscef-575-page-0002",
                "nyscef_document_number": 575,
                "pdf_page": 2,
                "source_filename": "nyscef_doc_no_575_complaint.pdf",
                "document_type": "complaint",
                "excerpt": intro_text,
                "page_text": intro_text,
                "classifications": [],
                "assertion_kind": "verified_record_fact",
                "party_role_section_expanded": True,
                "score": 1.0,
            },
            {
                "result_id": "par-3",
                "page_id": "nyscef-575-page-0003",
                "nyscef_document_number": 575,
                "pdf_page": 3,
                "source_filename": "nyscef_doc_no_575_complaint.pdf",
                "document_type": "complaint",
                "excerpt": parties,
                "page_text": parties,
                "classifications": ["party_identity"],
                "assertion_kind": "verified_record_fact",
                "party_role_section_expanded": True,
                "score": 28.0,
            },
        ]
        for i in range(20):
            hits.append(
                {
                    "result_id": f"extra-{i}",
                    "page_id": f"nyscef-9{i:02d}-p1",
                    "nyscef_document_number": 900 + i,
                    "pdf_page": 1,
                    "source_filename": f"nyscef_doc_no_{900 + i}_answer.pdf",
                    "document_type": "answer",
                    "excerpt": (
                        "Answer restating plaintiff and defendant roles. "
                        + ("x" * 400)
                    ),
                    "page_text": (
                        "ANSWER. Plaintiff Harbor Mill Carrier Inc. is plaintiff. "
                        "Defendant Northshore Logistics LP is a domestic corporation."
                    ),
                    "classifications": ["party_identity"],
                    "assertion_kind": "verified_record_fact",
                    "score": 2.0,
                }
            )
        packet = de.build_evidence_packet(
            self.party_query,
            {"query": self.party_query, "results": hits},
        )
        page_ids = [hit["page_id"] for hit in packet["retrieval_hits"]]
        self.assertIn("nyscef-575-page-0002", page_ids)
        self.assertIn("nyscef-575-page-0003", page_ids)
        self.assertLessEqual(len(packet["retrieval_hits"]), de.PARTY_ROLE_PACKET_MAX_HITS)

    def test_factual_transition_stops_and_excludes_full_facts(self):
        page_text = (
            self.caption
            + "Plaintiffs allege as follows: INTRODUCTION\n"
            + "1. This is an action for breach of a freight contract.\n"
            + "\n"
            + "PARTIES\n"
            + "2. Plaintiff Harbor Mill Carrier Inc. is a domestic corporation.\n"
            + "\n"
            + "FACTUAL BACKGROUND\n"
            + "3. On March 1, 2024, Plaintiff tendered a shipment of widgets with "
            + "a detailed routing history through multiple warehouses.\n"
            + "4. Defendant diverted the cargo over several weeks.\n"
        )
        entry, text = self._entry(page_text, nyscef=576)
        excerpt = mb._party_role_evidence_excerpt(entry, text)
        self.assertIn("INTRODUCTION", excerpt)
        self.assertIn("breach of a freight contract", excerpt)
        self.assertIn("PARTIES", excerpt)
        self.assertNotIn("March 1, 2024", excerpt)
        self.assertNotIn("detailed routing history", excerpt)
        self.assertNotIn("diverted the cargo", excerpt)
        self.assertNotIn("FACTUAL BACKGROUND", excerpt)

    def test_caption_and_parties_behavior_preserved(self):
        page_text = (
            self.caption
            + "NATURE OF THE ACTION\n"
            + "1. This is an action for breach of a freight contract.\n"
            + "\n"
            + "PARTIES\n"
            + "2. Plaintiff Harbor Mill Carrier Inc. is a domestic corporation "
            + "authorized to do business in this state.\n"
            + "3. Defendant Northshore Logistics LP is a limited liability "
            + "partnership.\n"
        )
        entry, text = self._entry(page_text, nyscef=577)
        excerpt = mb._party_role_evidence_excerpt(entry, text)
        self.assertIn("Harbor Mill Carrier Inc.", excerpt)
        self.assertIn("Northshore Logistics LP", excerpt)
        self.assertIn("Plaintiff", excerpt)
        self.assertIn("Defendant", excerpt)
        self.assertIn("PARTIES", excerpt)
        self.assertIn("domestic corporation", excerpt)

    def test_non_party_behavior_unchanged(self):
        docs = [
            _normalized(
                _doc(
                    578,
                    "complaint",
                    [
                        self.caption
                        + "INTRODUCTION\n"
                        + "1. This is an action for breach.\n"
                        + "PARTIES\n"
                        + "2. Plaintiff Harbor Mill Carrier Inc. is a domestic "
                        + "corporation.\n"
                    ],
                    filename="nyscef_doc_no_578_complaint.pdf",
                )
            ),
            _normalized(
                _doc(
                    579,
                    "motion",
                    [
                        "Notice of Motion for Summary Judgment returnable June 1, 2024. "
                        "Harbor Mill Carrier Inc. are Plaintiffs. Northshore Logistics LP "
                        "are Defendants. Movant seeks dismissal on the procedural calendar."
                    ],
                    filename="nyscef_doc_no_579_notice_of_motion.pdf",
                )
            ),
        ]
        motion_result = mb.retrieve_canonical_records(
            docs, self.motion_query, top_k=5
        )
        motion_packet = de.build_evidence_packet(
            self.motion_query, motion_result
        )
        self.assertNotIn("materiality_filter", motion_packet)
        for hit in motion_packet["retrieval_hits"]:
            self.assertNotIn("party_role_section_expanded", hit)

        noise = {
            "result_id": "m1",
            "page_id": "nyscef-579-p1",
            "nyscef_document_number": 579,
            "pdf_page": 1,
            "source_filename": "nyscef_doc_no_579_notice_of_motion.pdf",
            "document_type": "motion",
            "excerpt": "Notice of Motion for Summary Judgment.",
            "page_text": docs[1]["pages"][0]["text"],
            "classifications": [],
            "assertion_kind": "unknown",
        }
        self.assertFalse(de.hit_is_material_for_party_role_question(noise))


class CitationValidationImprovementTests(unittest.TestCase):
    def test_healed_ocr_citations_validate_when_supported(self):
        page = (
            "Ortov was and still is a domesti c corporation duly authorized "
            "and existing under the laws of the State of New York."
        )
        self.assertTrue(
            de.excerpt_occurs_on_page(
                "Ortov was and still is a domestic corporation duly authorized",
                page,
            )
        )
        self.assertTrue(
            de.excerpt_occurs_on_page(
                "domesti c corporation duly authorized",
                page,
            )
        )

    def test_ellipsis_segments_validate_independently(self):
        page = (
            "Plaintiff Alpha Carrier LP is a domestic corporation. "
            "Defendant Beta Depot Inc. maintained a principal place of business "
            "in Albany. Unrelated calendar notation."
        )
        self.assertTrue(
            de.excerpt_occurs_on_page(
                "Alpha Carrier LP is a domestic corporation ... "
                "principal place of business in Albany",
                page,
            )
        )
        # Every substantive segment must be independently supported.
        self.assertFalse(
            de.excerpt_occurs_on_page(
                "Alpha Carrier LP is a domestic corporation ... "
                "completely unsupported invented clause",
                page,
            )
        )

    def test_unsupported_segments_still_fail(self):
        page = "Defendant Beta Depot Inc. is a notice defendant."
        self.assertFalse(
            de.excerpt_occurs_on_page(
                "Beta Depot Inc. is a notice defendant ... phantom third party",
                page,
            )
        )
        self.assertFalse(
            de.excerpt_occurs_on_page(
                "entirely absent quotation",
                page,
            )
        )

    def test_proposition_specific_citations_survive_independently(self):
        docs = [
            _normalized(
                _doc(
                    540,
                    "complaint",
                    [
                        "PARTIES\n"
                        "1. Plaintiff Alpha Carrier LP is a domestic corporation.\n"
                        "2. Defendant Beta Depot Inc. is a notice defendant.\n"
                    ],
                    filename="nyscef_doc_no_540_complaint.pdf",
                )
            )
        ]
        page = docs[0]["pages"][0]
        retrieval = {
            "query": "Who are the parties?",
            "results": [
                {
                    "result_id": "cret-nyscef-540-page-0001",
                    "page_id": page["page_id"],
                    "nyscef_document_number": 540,
                    "pdf_page": 1,
                    "source_filename": docs[0]["filename"],
                    "document_type": "complaint",
                    "excerpt": page["text"],
                    "classifications": ["party_identity"],
                    "assertion_kind": "party_allegation",
                    "score": 10.0,
                }
            ],
        }
        payload = {
            "proposed_answer": "Alpha is plaintiff; Beta is notice defendant.",
            "propositions": [
                {
                    "proposition_id": "P1",
                    "text": "Alpha Carrier LP is a domestic corporation.",
                    "classification": "party_allegation",
                    "nyscef_document_number": 540,
                    "page_id": page["page_id"],
                    "pdf_page": 1,
                    "source_excerpt": (
                        "Plaintiff Alpha Carrier LP is a domestic corporation"
                    ),
                    "confidence": 0.9,
                    "rationale": "Entity form on pleading.",
                    "polarity": "supporting",
                },
                {
                    "proposition_id": "P2",
                    "text": "Invented unsupported claim.",
                    "classification": "party_allegation",
                    "nyscef_document_number": 540,
                    "page_id": page["page_id"],
                    "pdf_page": 1,
                    "source_excerpt": "Alpha Carrier LP ... completely invented segment",
                    "confidence": 0.2,
                    "rationale": "Bad ellipsis citation.",
                    "polarity": "supporting",
                },
                {
                    "proposition_id": "P3",
                    "text": "Beta Depot Inc. is a notice defendant.",
                    "classification": "party_allegation",
                    "nyscef_document_number": 540,
                    "page_id": page["page_id"],
                    "pdf_page": 1,
                    "source_excerpt": (
                        "Defendant Beta Depot Inc. is a notice defendant"
                    ),
                    "confidence": 0.9,
                    "rationale": "Notice-defendant allegation.",
                    "polarity": "supporting",
                },
            ],
            "unresolved_questions": [],
            "needs_review": [],
            "confidence": 0.7,
            "attorney_review": {
                "requires_attorney_review": True,
                "review_notes": "Mixed citations.",
                "legal_conclusions_labeled": True,
                "coverage_conclusion": None,
            },
            "review_scope": {
                "completeness": "not_established",
                "qualification": "Limited to retrieved pleading page.",
            },
        }

        result = de.answer_attorney_record_question(
            "Who are the parties and what are their roles?",
            retrieval,
            documents=docs,
            model_call=lambda _system, _user: json.dumps(payload),
        )
        kept_ids = {p["proposition_id"] for p in result["propositions"]}
        self.assertIn("P1", kept_ids)
        self.assertIn("P3", kept_ids)
        self.assertNotIn("P2", kept_ids)


class PartyRoleDraftingCompletenessTests(unittest.TestCase):
    """Focused regressions for party-role drafting completeness enforcement."""

    def setUp(self):
        self.party_question = (
            "Who are the parties and what are their roles in this action?"
        )
        self.motion_question = (
            "What relief does the notice of motion for summary judgment seek?"
        )
        self.excerpt = (
            "PARTIES\n"
            "1. Plaintiff Cedar Ridge Logistics LLC is a domestic corporation "
            "authorized to do business in this state.\n"
            "2. Cedar Ridge Logistics LLC maintained a principal place of business "
            "located at 10 Harbor Way, Albany, NY 12207.\n"
            "3. Defendant Pine Harbor Depot Inc. is a notice defendant.\n"
            "4. Pine Harbor Depot Inc. is a limited liability company.\n"
            "5. Defendant Oakline Carrier LP is a resident of the State of New York "
            "residing in Erie County.\n"
        )
        self.hit = {
            "result_id": "cret-nyscef-810-page-0001",
            "page_id": "nyscef-810-page-0001",
            "nyscef_document_number": 810,
            "pdf_page": 1,
            "source_filename": "nyscef_doc_no_810_complaint.pdf",
            "document_type": "complaint",
            "excerpt": self.excerpt,
            "classifications": ["party_identity"],
            "assertion_kind": "party_allegation",
            "score": 10.0,
        }
        self.retrieval = {
            "query": self.party_question,
            "results": [self.hit],
            "provisional_answer": "PROVISIONAL_SHOULD_NOT_APPEAR",
            "gold_answer": "GOLD_SHOULD_NOT_APPEAR",
            "attorney_feedback": "FEEDBACK_SHOULD_NOT_APPEAR",
        }

    def _complete_payload(self, packet):
        hit = packet["retrieval_hits"][0]
        expected = de.extract_party_role_expected_attributes(packet)
        bits = []
        for party in expected:
            bit = f"{party.get('procedural_role')} {party.get('identity')}"
            if party.get("entity_type"):
                bit += f" is a {party['entity_type']}"
            if party.get("residence_or_ppb"):
                bit += f"; {party['residence_or_ppb']}"
            if party.get("pleaded_role_basis"):
                bit += f" ({party['pleaded_role_basis']})"
            bits.append(bit + ".")
        answer = " ".join(bits)
        return {
            "proposed_answer": answer,
            "propositions": [
                {
                    "proposition_id": "P1",
                    "text": answer,
                    "classification": "party_allegation",
                    "nyscef_document_number": hit["nyscef_document_number"],
                    "page_id": hit["page_id"],
                    "pdf_page": hit["pdf_page"],
                    "source_excerpt": "Plaintiff Cedar Ridge Logistics LLC is a domestic corporation",
                    "confidence": 0.9,
                    "rationale": "Party roster from pleading.",
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
                "review_notes": "Complete party roster.",
                "legal_conclusions_labeled": True,
                "coverage_conclusion": None,
            },
            "review_scope": {
                "completeness": "not_established",
                "qualification": "Limited to retrieved pleading.",
            },
        }

    def _incomplete_payload(self, packet):
        hit = packet["retrieval_hits"][0]
        return {
            "proposed_answer": "Cedar Ridge is plaintiff; Pine Harbor is defendant.",
            "propositions": [
                {
                    "proposition_id": "P1",
                    "text": "Cedar Ridge Logistics LLC is plaintiff.",
                    "classification": "party_allegation",
                    "nyscef_document_number": hit["nyscef_document_number"],
                    "page_id": hit["page_id"],
                    "pdf_page": hit["pdf_page"],
                    "source_excerpt": "Plaintiff Cedar Ridge Logistics LLC is a domestic corporation",
                    "confidence": 0.5,
                    "rationale": "Identity only.",
                    "polarity": "supporting",
                }
            ],
            "supporting_evidence": [],
            "contrary_evidence": [],
            "unresolved_questions": [],
            "documents_pages_reviewed": [],
            "confidence": 0.5,
            "attorney_review": {
                "requires_attorney_review": True,
                "review_notes": "Incomplete roster.",
                "legal_conclusions_labeled": True,
                "coverage_conclusion": None,
            },
            "review_scope": {
                "completeness": "not_established",
                "qualification": "Sparse draft.",
            },
        }

    def test_final_party_role_prompt_requires_all_five_attribute_categories(self):
        packet = de.build_evidence_packet(self.party_question, self.retrieval)
        prompt = de.build_user_prompt(packet, party_role_completeness=True)
        lowered = prompt.lower()
        self.assertIn("identity", lowered)
        self.assertIn("procedural role", lowered)
        self.assertIn("entity type", lowered)
        self.assertIn("residence or principal place of business", lowered)
        self.assertIn("pleaded role basis", lowered)
        self.assertIn("notice-defendant", lowered)
        self.assertIn("not optional", lowered)
        self.assertIn("cannot be omitted for brevity", lowered)

        # Requirement must follow concision/materiality system guidance and
        # evidence serialization in the exact final model prompt.
        system = de.RECORD_ANALYSIS_SYSTEM_PROMPT.lower()
        self.assertIn("concise practical attorney work product", system)
        self.assertIn("materially useful", system)
        evidence_json = de._stable_json(packet)
        self.assertIn(evidence_json, prompt)
        self.assertGreater(
            prompt.lower().rfind("party-role drafting requirement"),
            prompt.find(evidence_json),
        )
        # Trailing instruction is after the serialized evidence block.
        self.assertTrue(
            prompt.rstrip().endswith(
                "Do not invent attributes absent from the evidence."
            )
        )

    def test_complete_initial_response_causes_no_retry(self):
        calls = []

        def _model(system_prompt, user_prompt):
            calls.append(user_prompt)
            packet = de.build_evidence_packet(self.party_question, self.retrieval)
            return self._complete_payload(packet)

        result = de.answer_attorney_record_question(
            self.party_question,
            self.retrieval,
            model_call=_model,
        )
        self.assertEqual(result["status"], de.STATUS_READY)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["audit"].get("party_role_provider_calls"), 1)
        self.assertFalse(result["audit"].get("party_role_repair_attempted"))
        self.assertIn("PARTY-ROLE DRAFTING REQUIREMENT", calls[0])

    def test_incomplete_triggers_one_repair_then_success(self):
        calls = []

        def _model(system_prompt, user_prompt):
            calls.append(user_prompt)
            packet = de.build_evidence_packet(self.party_question, self.retrieval)
            if len(calls) == 1:
                return self._incomplete_payload(packet)
            return self._complete_payload(packet)

        result = de.answer_attorney_record_question(
            self.party_question,
            self.retrieval,
            model_call=_model,
        )
        self.assertEqual(result["status"], de.STATUS_READY)
        self.assertEqual(len(calls), 2)
        self.assertTrue(result["audit"].get("party_role_repair_attempted"))
        self.assertEqual(result["audit"].get("party_role_provider_calls"), 2)
        repair = calls[1].lower()
        self.assertIn("missing required attributes", repair)
        self.assertIn("original question", repair)
        self.assertIn("evidence packet", repair)
        self.assertIn("current draft", repair)
        self.assertIn("cedar ridge logistics llc", repair)
        # Repair prompt includes only permitted context — no protected refs.
        self.assertNotIn("provisional_should_not_appear", repair)
        self.assertNotIn("gold_should_not_appear", repair)
        self.assertNotIn("feedback_should_not_appear", repair)
        self.assertNotIn("attorney_feedback", repair)
        self.assertIn("domestic corporation", result["proposed_answer"].lower())

    def test_failed_repair_is_generation_failure_without_second_retry(self):
        calls = []

        def _model(system_prompt, user_prompt):
            calls.append(user_prompt)
            packet = de.build_evidence_packet(self.party_question, self.retrieval)
            return self._incomplete_payload(packet)

        result = de.answer_attorney_record_question(
            self.party_question,
            self.retrieval,
            model_call=_model,
        )
        self.assertEqual(result["status"], de.STATUS_NOT_READY)
        self.assertEqual(len(calls), 2)
        self.assertTrue(result["audit"].get("party_role_completeness_failed"))
        self.assertTrue(result["audit"].get("party_role_repair_attempted"))
        self.assertEqual(result["audit"].get("party_role_provider_calls"), 2)
        self.assertTrue(result["audit"].get("missing_party_role_attributes"))
        self.assertEqual(result["proposed_answer"], "")
        self.assertEqual(result["propositions"], [])

    def test_non_party_questions_skip_instruction_and_repair(self):
        motion_hit = {
            "result_id": "m1",
            "page_id": "nyscef-811-p1",
            "nyscef_document_number": 811,
            "pdf_page": 1,
            "source_filename": "nyscef_doc_no_811_notice_of_motion.pdf",
            "document_type": "motion",
            "excerpt": (
                "Notice of Motion for Summary Judgment returnable June 1, 2024. "
                "Movant seeks dismissal of the complaint."
            ),
            "classifications": ["motion"],
            "assertion_kind": "unknown",
            "score": 8.0,
        }
        retrieval = {
            "query": self.motion_question,
            "results": [motion_hit],
            "provisional_answer": "PROVISIONAL_SHOULD_NOT_APPEAR",
            "gold_answer": "GOLD_SHOULD_NOT_APPEAR",
        }
        calls = []

        def _model(system_prompt, user_prompt):
            calls.append(user_prompt)
            return {
                "proposed_answer": "Movant seeks dismissal of the complaint.",
                "propositions": [
                    {
                        "proposition_id": "P1",
                        "text": "Movant seeks dismissal of the complaint.",
                        "classification": "verified_record_fact",
                        "nyscef_document_number": 811,
                        "page_id": "nyscef-811-p1",
                        "pdf_page": 1,
                        "source_excerpt": "Movant seeks dismissal of the complaint.",
                        "confidence": 0.8,
                        "rationale": "Motion relief.",
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
                    "review_notes": "Motion relief.",
                    "legal_conclusions_labeled": True,
                    "coverage_conclusion": None,
                },
                "review_scope": {
                    "completeness": "not_established",
                    "qualification": "Motion packet.",
                },
            }

        result = de.answer_attorney_record_question(
            self.motion_question,
            retrieval,
            model_call=_model,
        )
        self.assertEqual(result["status"], de.STATUS_READY)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("PARTY-ROLE DRAFTING REQUIREMENT", calls[0])
        self.assertNotIn("party_role_repair_attempted", result["audit"])
        self.assertNotIn("party_role_provider_calls", result["audit"])

    def test_protected_references_never_loaded_into_prompts(self):
        calls = []

        def _model(system_prompt, user_prompt):
            calls.append(system_prompt + "\n" + user_prompt)
            packet = de.build_evidence_packet(self.party_question, self.retrieval)
            if "Missing required attributes" in user_prompt:
                return self._complete_payload(packet)
            return self._incomplete_payload(packet)

        result = de.answer_attorney_record_question(
            self.party_question,
            self.retrieval,
            model_call=_model,
        )
        self.assertEqual(result["status"], de.STATUS_READY)
        self.assertEqual(len(calls), 2)
        for blob in calls:
            lowered = blob.lower()
            self.assertNotIn("provisional_should_not_appear", lowered)
            self.assertNotIn("gold_should_not_appear", lowered)
            self.assertNotIn("feedback_should_not_appear", lowered)
            self.assertNotIn("provisional_answer", lowered)
            self.assertNotIn("gold_answer", lowered)
            self.assertNotIn("attorney_feedback", lowered)

    def test_expected_attribute_extraction_is_generic_and_ocr_tolerant(self):
        packet = {
            "question": self.party_question,
            "retrieval_hits": [
                {
                    "excerpt": (
                        "1. Plaintiff Ortov Lighting Inc. is a domesti c corporation.\n"
                        "2. Ortov Lighting Inc. maintained a principal place of business "
                        "in Albany.\n"
                        "3. Defendant Meadow Bridge Repair Inc. is a notice defendant.\n"
                    )
                }
            ],
        }
        expected = de.extract_party_role_expected_attributes(packet)
        by_name = {
            de.normalize_citation_text(item["identity"]): item for item in expected
        }
        self.assertIn("ortov lighting inc", by_name)
        ortov = by_name["ortov lighting inc"]
        self.assertEqual(ortov["procedural_role"], "plaintiff")
        self.assertEqual(ortov["entity_type"], "domestic corporation")
        self.assertIn("principal place of business", ortov["residence_or_ppb"].lower())
        meadow = by_name.get("meadow bridge repair inc")
        self.assertIsNotNone(meadow)
        self.assertEqual(meadow["procedural_role"], "defendant")
        self.assertEqual(meadow["pleaded_role_basis"], "notice defendant")

        # Validation tolerates OCR surface forms without accepting missing values.
        draft = {
            "proposed_answer": (
                "Plaintiff Ortov Lighting Inc. is a domestic corporation; "
                "principal place of business in Albany. "
                "Defendant Meadow Bridge Repair Inc. is a notice defendant."
            ),
            "propositions": [],
        }
        self.assertEqual(de.find_missing_party_role_attributes(draft, expected), [])
        incomplete = {
            "proposed_answer": "Ortov Lighting Inc. is plaintiff.",
            "propositions": [],
        }
        missing = de.find_missing_party_role_attributes(incomplete, expected)
        categories = {item["category"] for item in missing}
        self.assertIn("entity_type", categories)
        self.assertIn("residence_or_ppb", categories)
        self.assertTrue(
            any(
                "Meadow Bridge Repair Inc" in (m.get("party") or "")
                for m in missing
            )
        )


class PartyRoleDiscreteProtectionAndJurisdictionTests(unittest.TestCase):
    """Discrete intro/PARTIES protection and named-party jurisdiction filtering."""

    def setUp(self):
        self.party_query = (
            "Who are the parties and what are their roles in this action?"
        )
        self.motion_query = (
            "What relief does the notice of motion for summary judgment seek?"
        )
        self.caption = (
            "SUPREME COURT OF THE STATE OF NEW YORK\n"
            "COUNTY OF KINGS\n"
            "Harbor Mill Carrier Inc.,\n"
            "Plaintiff,\n"
            "-against-\n"
            "Northshore Logistics LP,\n"
            "Defendant.\n"
        )

    def _hit(self, doc_no, page, doc_type, text, *, expanded=False, score=1.0):
        value = {
            "result_id": f"r-{doc_no}-{page}",
            "page_id": f"nyscef-{doc_no}-page-{page:04d}",
            "nyscef_document_number": doc_no,
            "pdf_page": page,
            "source_filename": f"filing_{doc_no}_{doc_type}.pdf",
            "document_type": doc_type,
            "excerpt": text,
            "page_text": text,
            "classifications": ["party_identity"],
            "assertion_kind": "verified_record_fact",
            "score": score,
        }
        if expanded:
            value["party_role_section_expanded"] = True
        return value

    def test_intro_and_parties_protected_separately(self):
        hits = [
            self._hit(
                601,
                1,
                "complaint",
                self.caption
                + "INTRODUCTION\n"
                + "1. This is an action for breach of a freight contract.\n",
                expanded=True,
                score=30.0,
            ),
            self._hit(
                601,
                2,
                "complaint",
                "2. The opening section continues without warehouse chronology.\n",
                expanded=True,
                score=29.0,
            ),
            self._hit(
                601,
                3,
                "complaint",
                "JURISDICTION AND VENUE\n"
                "1. This Court has personal jurisdiction over the Defendants.\n",
                score=28.0,
            ),
            self._hit(
                601,
                4,
                "complaint",
                "PARTIES\n"
                "1. Plaintiff Harbor Mill Carrier Inc. is a domestic corporation.\n",
                expanded=True,
                score=27.0,
            ),
            self._hit(
                601,
                5,
                "complaint",
                "2. Defendant Northshore Logistics LP is a limited liability "
                "partnership.\n",
                expanded=True,
                score=26.0,
            ),
        ]
        marked = de._mark_controlling_party_role_group(hits)
        by_page = {item["pdf_page"]: item for item in marked}
        self.assertTrue(by_page[1].get("controlling_party_role_pleading"))
        self.assertTrue(by_page[2].get("controlling_party_role_pleading"))
        self.assertFalse(by_page[3].get("controlling_party_role_pleading"))
        self.assertTrue(by_page[4].get("controlling_party_role_pleading"))
        self.assertTrue(by_page[5].get("controlling_party_role_pleading"))
        self.assertEqual(
            de._hit_party_role_protected_section_kind(by_page[1]), "intro"
        )
        self.assertEqual(
            de._hit_party_role_protected_section_kind(by_page[4]), "parties"
        )

    def test_intervening_pages_not_force_retained(self):
        hits = [
            self._hit(
                602,
                1,
                "complaint",
                self.caption
                + "NATURE OF THE ACTION\n"
                + "1. This is an action for breach of a freight contract.\n",
                expanded=True,
                score=30.0,
            ),
            self._hit(
                602,
                2,
                "complaint",
                "CALENDAR NOTE\nUnrelated procedural calendar filler without roles.\n",
                score=5.0,
            ),
            self._hit(
                602,
                3,
                "complaint",
                "PARTIES\n"
                "1. Plaintiff Harbor Mill Carrier Inc. is a domestic corporation.\n",
                expanded=True,
                score=28.0,
            ),
        ]
        marked = de._mark_controlling_party_role_group(hits)
        by_page = {item["pdf_page"]: item for item in marked}
        self.assertTrue(by_page[1].get("controlling_party_role_pleading"))
        self.assertFalse(by_page[2].get("controlling_party_role_pleading"))
        self.assertTrue(by_page[3].get("controlling_party_role_pleading"))
        selected, meta = de.apply_party_role_packet_budget(hits, max_hits=4)
        selected_pages = {item["pdf_page"] for item in selected}
        self.assertIn(1, selected_pages)
        self.assertIn(3, selected_pages)
        self.assertNotIn(2, selected_pages)
        self.assertEqual(meta["protected_hit_count"], 2)

    def test_continuation_within_intro_and_parties(self):
        doc = _normalized(
            _doc(
                603,
                "complaint",
                [
                    self.caption
                    + "Plaintiffs allege as follows: INTRODUCTION\n"
                    + "1. This is an action for breach of a freight contract.\n",
                    "2. The opening section continues with a concise statement of "
                    "the commercial carriage dispute.\n",
                    "PARTIES\n"
                    "3. Plaintiff Harbor Mill Carrier Inc. is a domestic corporation.\n",
                    "4. Defendant Northshore Logistics LP is a limited liability "
                    "partnership.\n",
                    "FACTUAL BACKGROUND\n"
                    "5. On March 1, 2024, cargo was diverted through warehouses.\n",
                ],
                filename="nyscef_doc_no_603_complaint.pdf",
            )
        )
        lookup = mb._page_lookup_from_documents([doc])
        intro_ids, intro_continuations = mb._collect_intro_section_page_ids(lookup)
        parties_ids = mb._collect_parties_section_page_ids(lookup)
        intro_pages = [lookup[pid]["page"]["page_number"] for pid in intro_ids]
        parties_pages = [lookup[pid]["page"]["page_number"] for pid in parties_ids]
        self.assertEqual(intro_pages, [1, 2])
        self.assertEqual(len(intro_continuations), 1)
        self.assertEqual(parties_pages, [3, 4])
        self.assertNotIn(5, intro_pages)
        self.assertNotIn(5, parties_pages)

        # Unmarked intra-PARTIES pages between expanded endpoints stay protected.
        parties_hits = [
            self._hit(
                604,
                1,
                "complaint",
                self.caption,
                score=30.0,
            ),
            self._hit(
                604,
                2,
                "complaint",
                "PARTIES\n1. Harbor Mill Carrier Inc. is the Plaintiff.",
                expanded=True,
                score=29.0,
            ),
            self._hit(
                604,
                3,
                "complaint",
                "2. Northshore Logistics LP is the Defendant.",
                score=28.0,
            ),
            self._hit(
                604,
                4,
                "complaint",
                "3. West Field LP is joined as a necessary party.",
                expanded=True,
                score=27.0,
            ),
        ]
        marked = de._mark_controlling_party_role_group(parties_hits)
        for page in (1, 2, 3, 4):
            self.assertTrue(
                next(h for h in marked if h["pdf_page"] == page).get(
                    "controlling_party_role_pleading"
                ),
                msg=f"page {page} should remain protected within PARTIES",
            )

    def test_generic_personal_jurisdiction_excluded(self):
        for unit in (
            "9. This Court has personal jurisdiction over the Defendants.",
            "9. This Court has personal jurisdiction over the Defendants "
            "pursuant to CPLR 301.",
            "9. This Court has jurisdiction over this action pursuant to CPLR 301.",
        ):
            self.assertTrue(mb._party_role_unit_is_generic_jurisdiction(unit))
            self.assertFalse(mb._party_role_unit_in_evidence_scope(unit))

    def test_bare_collective_role_rejected_as_identity_signal(self):
        for unit in (
            "5. The Defendants transacted business in the State of New York.",
            "6. Venue is proper because the Defendants reside in Kings County.",
            "The Petitioners conducted business in this state.",
            "Respondents reside in Kings County.",
            "6. Venue is proper.",
        ):
            self.assertFalse(
                mb._party_role_unit_has_named_party_identity(unit),
                msg=unit,
            )
            self.assertFalse(
                mb._party_role_unit_in_evidence_scope(unit),
                msg=unit,
            )

    def test_named_party_jurisdiction_business_allegations_retained(self):
        units = (
            "5. Defendant Northshore Logistics LP transacted business in the "
            "State of New York and within this County.",
            "5. That at all times mentioned herein, Northshore Logistics LP "
            "transacted business in the State of New York.",
            "6. Venue is proper because Defendant Northshore Logistics LP "
            "resides in Kings County.",
            "2. Alpha Carrier LP maintained a principal place of business "
            "located at 10 Harbor Way, Buffalo, NY 14201.",
        )
        for unit in units:
            self.assertTrue(
                mb._party_role_unit_has_named_party_identity(unit),
                msg=unit,
            )
            self.assertTrue(
                mb._party_role_unit_in_evidence_scope(unit),
                msg=unit,
            )

    def test_caption_parties_and_inventory_preserved(self):
        page_text = (
            self.caption
            + "NATURE OF THE ACTION\n"
            + "1. This is an action for breach of a freight contract.\n"
            + "\n"
            + "PARTIES\n"
            + "2. Plaintiff Harbor Mill Carrier Inc. is a domestic corporation "
            + "authorized to do business in this state.\n"
            + "3. Defendant Northshore Logistics LP is a limited liability "
            + "partnership.\n"
            + "4. Venue is proper because Defendant Northshore Logistics LP "
            + "resides in Kings County.\n"
            + "5. This Court has personal jurisdiction over the Defendants.\n"
        )
        doc = _normalized(
            _doc(
                605,
                "complaint",
                [page_text],
                filename="nyscef_doc_no_605_complaint.pdf",
            )
        )
        entry = {
            "page": doc["pages"][0],
            "document": doc,
            "nyscef_document_number": 605,
            "filename": doc["filename"],
            "document_type": "complaint",
            "segment": None,
        }
        excerpt = mb._party_role_evidence_excerpt(entry, doc["pages"][0]["text"])
        self.assertIn("Harbor Mill Carrier Inc.", excerpt)
        self.assertIn("Northshore Logistics LP", excerpt)
        self.assertIn("Plaintiff", excerpt)
        self.assertIn("Defendant", excerpt)
        self.assertIn("PARTIES", excerpt)
        self.assertIn("domestic corporation", excerpt)
        self.assertIn("Venue is proper", excerpt)
        self.assertNotIn("personal jurisdiction over the Defendants", excerpt)

        inventory = de.extract_party_role_expected_attributes(
            {
                "retrieval_hits": [
                    {
                        "result_id": "r-605-1",
                        "page_id": doc["pages"][0]["page_id"],
                        "nyscef_document_number": 605,
                        "pdf_page": 1,
                        "source_filename": doc["filename"],
                        "document_type": "complaint",
                        "excerpt": excerpt,
                        "classifications": ["party_identity"],
                        "assertion_kind": "party_allegation",
                        "score": 10.0,
                    }
                ]
            }
        )
        names = " ".join(
            str(item.get("identity") or "") for item in inventory
        ).lower()
        self.assertIn("harbor mill carrier", names)
        self.assertIn("northshore logistics", names)

    def test_non_party_behavior_unchanged(self):
        docs = [
            _normalized(
                _doc(
                    606,
                    "complaint",
                    [
                        self.caption
                        + "INTRODUCTION\n"
                        + "1. This is an action for breach.\n"
                        + "PARTIES\n"
                        + "2. Plaintiff Harbor Mill Carrier Inc. is a domestic "
                        + "corporation.\n"
                    ],
                    filename="nyscef_doc_no_606_complaint.pdf",
                )
            ),
            _normalized(
                _doc(
                    607,
                    "motion",
                    [
                        "Notice of Motion for Summary Judgment returnable June 1, 2024. "
                        "Harbor Mill Carrier Inc. are Plaintiffs. Northshore Logistics LP "
                        "are Defendants. Movant seeks dismissal on the procedural calendar."
                    ],
                    filename="nyscef_doc_no_607_notice_of_motion.pdf",
                )
            ),
        ]
        motion_result = mb.retrieve_canonical_records(
            docs, self.motion_query, top_k=5
        )
        motion_packet = de.build_evidence_packet(
            self.motion_query, motion_result
        )
        self.assertNotIn("materiality_filter", motion_packet)
        for hit in motion_packet["retrieval_hits"]:
            self.assertNotIn("party_role_section_expanded", hit)

        noise = {
            "result_id": "m1",
            "page_id": "nyscef-607-p1",
            "nyscef_document_number": 607,
            "pdf_page": 1,
            "source_filename": "nyscef_doc_no_607_notice_of_motion.pdf",
            "document_type": "motion",
            "excerpt": "Notice of Motion for Summary Judgment.",
            "page_text": docs[1]["pages"][0]["text"],
            "classifications": [],
            "assertion_kind": "unknown",
        }
        self.assertFalse(de.hit_is_material_for_party_role_question(noise))


if __name__ == "__main__":
    unittest.main()
