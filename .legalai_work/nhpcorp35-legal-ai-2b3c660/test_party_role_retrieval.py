"""Synthetic regressions for party-role retrieval and extraction corrections."""

from __future__ import annotations

import unittest

import matter_builder as mb


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


def _party_role_corpus():
    """Complaint with late PARTIES section vs answer/motion/RJI fillers."""
    filler = "Procedural calendar notation without role assignments. " + ("z" * 60)
    complaint = _normalized(
        _doc(
            101,
            "complaint",
            [
                "SUPREME COURT OF THE STATE OF NEW YORK\n"
                "Northshore Logistics LP v. Harbor Mill Carrier Inc.\n"
                "Summons with notice. Index No. 123456/2024.",
                filler,
                filler,
                filler,
                "PARTIES\n"
                "1. Plaintiff Northshore Logistics LP is a limited liability partnership "
                "authorized to do business in this state.\n"
                "2. Defendant Harbor Mill Carrier Inc. is a domestic corporation.\n"
                "3. Gamma Trailer Repair LLC, third-party defendant, was joined herein "
                "as a necessary party.\n"
                "4. Delta Freight Appeal Fund, appellant, seeks review.\n"
                "5. Harbor Mill Carrier Inc., respondent on appeal, opposes.",
            ],
            filename="nyscef_doc_no_101_summons_complaint.pdf",
        )
    )
    answer = _normalized(
        _doc(
            102,
            "answer",
            [
                "Defendant Harbor Mill Carrier Inc. answers the complaint and denies "
                "knowledge. FIRST AFFIRMATIVE DEFENSE of failure to state a claim. "
                "This answer mentions parties only in passing.",
            ],
            filename="nyscef_doc_no_102_verified_answer.pdf",
        )
    )
    motion = _normalized(
        _doc(
            103,
            "motion",
            [
                "Notice of Motion for Summary Judgment returnable June 1, 2024. "
                "Movant seeks dismissal. The motion papers name the parties in the caption "
                "only: Northshore Logistics LP against Harbor Mill Carrier Inc.",
            ],
            filename="nyscef_doc_no_103_notice_of_motion.pdf",
        )
    )
    rji = _normalized(
        _doc(
            104,
            "other",
            [
                "Request for Judicial Intervention. RJI addendum lists the caption "
                "Northshore Logistics LP v. Harbor Mill Carrier Inc. without a PARTIES "
                "section explaining roles.",
            ],
            filename="nyscef_doc_no_104_rji.pdf",
            title="RJI",
        )
    )
    return [complaint, answer, motion, rji]


class PartyRoleRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.docs = _party_role_corpus()
        self.case_map = mb.build_case_map_from_documents(self.docs)
        self.party_query = (
            "Who are the parties and what are their roles in this action?"
        )

    def test_complaint_parties_section_outranks_answer_motion_rji(self):
        result = mb.retrieve_canonical_records(
            self.docs,
            self.party_query,
            case_map=self.case_map,
            top_k=8,
        )
        self.assertTrue(result["results"])
        top = result["results"][0]
        self.assertEqual(top["nyscef_document_number"], 101)
        self.assertEqual(top["document_type"], "complaint")
        self.assertGreaterEqual(top["pdf_page"], 5)
        self.assertIn("PARTIES", top["excerpt"].upper())

        top_filing_scores = {}
        for hit in result["results"]:
            doc_no = hit["nyscef_document_number"]
            score = hit.get("diversity_adjusted_score", hit["score"])
            top_filing_scores[doc_no] = max(top_filing_scores.get(doc_no, 0.0), score)

        self.assertIn(101, top_filing_scores)
        for other in (102, 103, 104):
            self.assertIn(other, top_filing_scores)
            self.assertGreater(top_filing_scores[101], top_filing_scores[other])

    def test_incidental_answer_word_does_not_outrank_complaint(self):
        result = mb.retrieve_canonical_records(
            self.docs,
            "Please answer: identify the parties and their roles from the record",
            case_map=self.case_map,
            top_k=5,
            include_diagnostics=True,
        )
        hints = result["diagnostics"]["query_hints"]
        self.assertTrue(hints.get("party_role_intent"))
        self.assertNotIn("answer", hints.get("document_types") or [])
        self.assertEqual(result["results"][0]["nyscef_document_number"], 101)
        self.assertNotEqual(result["results"][0]["document_type"], "answer")

    def test_role_bearing_non_caption_pages_are_retrieved(self):
        result = mb.retrieve_canonical_records(
            self.docs,
            self.party_query,
            case_map=self.case_map,
            top_k=10,
        )
        complaint_pages = {
            hit["pdf_page"]
            for hit in result["results"]
            if hit["nyscef_document_number"] == 101
        }
        self.assertTrue(complaint_pages.intersection({5}))
        parties_hit = next(
            hit
            for hit in result["results"]
            if hit["nyscef_document_number"] == 101 and hit["pdf_page"] == 5
        )
        self.assertGreater(parties_hit["component_scores"]["party_role_pleading"], 0.0)
        self.assertTrue(parties_hit.get("page_id"))
        self.assertIsInstance(parties_hit.get("excerpt"), str)

    def test_motion_query_keeps_motion_priority(self):
        result = mb.retrieve_canonical_records(
            self.docs,
            "Notice of Motion for Summary Judgment relief sought by movant",
            case_map=self.case_map,
            top_k=5,
            include_diagnostics=True,
        )
        hints = result["diagnostics"]["query_hints"]
        self.assertFalse(hints.get("party_role_intent"))
        self.assertEqual(result["results"][0]["nyscef_document_number"], 103)
        self.assertEqual(result["results"][0]["document_type"], "motion")
        for hit in result["results"]:
            self.assertEqual(hit["component_scores"]["party_role_pleading"], 0.0)


class PartyRoleExtractionTests(unittest.TestCase):
    def test_supported_roles_extracted_from_later_parties_section(self):
        docs = _party_role_corpus()
        case_map = mb.build_case_map_from_documents(docs)
        roles = {
            (party.get("role"), party.get("label"))
            for party in case_map["parties"]
            if party.get("role")
        }
        self.assertIn(("plaintiff", "Northshore Logistics LP"), roles)
        self.assertIn(("defendant", "Harbor Mill Carrier Inc"), roles)
        self.assertIn(("third-party defendant", "Gamma Trailer Repair LLC"), roles)
        self.assertIn(("appellant", "Delta Freight Appeal Fund"), roles)
        self.assertIn(("respondent on appeal", "Harbor Mill Carrier Inc"), roles)

        later = [
            party
            for party in case_map["parties"]
            if party.get("procedural_posture") == "parties_section"
        ]
        self.assertTrue(later)
        for party in later:
            support = party["record_support"][0]
            self.assertTrue(support.get("page_ids"))
            self.assertEqual(support.get("nyscef_document_number"), 101)
            self.assertEqual(party.get("assertion_kind"), "verified_record_fact")

    def test_unclear_roles_remain_unassigned(self):
        docs = [
            _normalized(
                _doc(
                    201,
                    "complaint",
                    [
                        "Caption page without usable party lines.",
                        "Mystery Holdings appears somewhere in the narrative "
                        "without a clear procedural designation.",
                    ],
                )
            )
        ]
        case_map = mb.build_case_map_from_documents(docs)
        # No invented procedural role for an untagged name fragment.
        invented = [
            party
            for party in case_map["parties"]
            if "Mystery" in (party.get("label") or "") and party.get("role")
        ]
        self.assertEqual(invented, [])

    def test_petitioner_respondent_and_third_party_plaintiff(self):
        docs = [
            _normalized(
                _doc(
                    202,
                    "complaint",
                    [
                        "Special proceeding caption page.",
                        "PARTIES\n"
                        "Petitioner Oak Valley Trust is a domestic trust.\n"
                        "Respondent Pine County Clerk is a municipal officer.\n"
                        "Riverbank Guaranty Co., third-party plaintiff, asserts indemnity.",
                    ],
                    filename="nyscef_doc_no_202_petition.pdf",
                )
            )
        ]
        case_map = mb.build_case_map_from_documents(docs)
        by_role = {
            party.get("role"): party.get("label")
            for party in case_map["parties"]
            if party.get("role")
        }
        self.assertEqual(by_role.get("petitioner"), "Oak Valley Trust")
        self.assertEqual(by_role.get("respondent"), "Pine County Clerk")
        self.assertEqual(by_role.get("third-party plaintiff"), "Riverbank Guaranty Co")

    def test_amended_and_original_pleadings_remain_distinguishable(self):
        original = _normalized(
            _doc(
                301,
                "complaint",
                [
                    "Original caption.",
                    "PARTIES\nPlaintiff Cedar Supply Co. is a corporation.\n"
                    "Defendant Maple Depot LLC is a limited liability company.",
                ],
                filename="nyscef_doc_no_301_complaint.pdf",
            )
        )
        amended = _normalized(
            _doc(
                302,
                "complaint",
                [
                    "Amended caption.",
                    "PARTIES\nPlaintiff Cedar Supply Co. is a corporation.\n"
                    "Defendant Maple Depot LLC is a limited liability company.\n"
                    "Birch Indemnity Inc., third-party defendant, is joined.",
                ],
                filename="nyscef_doc_no_302_amended_complaint.pdf",
            )
        )
        case_map = mb.build_case_map_from_documents([original, amended])
        third_party = [
            party
            for party in case_map["parties"]
            if party.get("role") == "third-party defendant"
        ]
        self.assertEqual(len(third_party), 1)
        self.assertEqual(
            third_party[0]["record_support"][0]["nyscef_document_number"], 302
        )
        cedar_nodes = [
            party
            for party in case_map["parties"]
            if party.get("role") == "plaintiff"
            and "Cedar Supply" in (party.get("label") or "")
        ]
        source_docs = {
            node["record_support"][0]["nyscef_document_number"] for node in cedar_nodes
        }
        self.assertEqual(source_docs, {301, 302})


class PartyRoleIntentHelperTests(unittest.TestCase):
    def test_normalize_party_role_vocabulary(self):
        self.assertEqual(mb._normalize_party_role("Plaintiffs"), "plaintiff")
        self.assertEqual(mb._normalize_party_role("Defendants"), "defendant")
        self.assertEqual(mb._normalize_party_role("Petitioner"), "petitioner")
        self.assertEqual(mb._normalize_party_role("Respondent"), "respondent")
        self.assertEqual(
            mb._normalize_party_role("third-party plaintiff"), "third-party plaintiff"
        )
        self.assertEqual(
            mb._normalize_party_role("third party defendant"), "third-party defendant"
        )
        self.assertEqual(mb._normalize_party_role("Appellant"), "appellant")
        self.assertEqual(
            mb._normalize_party_role("respondent on appeal"), "respondent on appeal"
        )
        self.assertIsNone(mb._normalize_party_role("interested spectator"))
        self.assertIsNone(mb._normalize_party_role(""))


if __name__ == "__main__":
    unittest.main()
