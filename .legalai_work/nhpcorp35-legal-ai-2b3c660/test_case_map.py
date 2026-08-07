"""Focused tests for citation-grounded litigation case mapping."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

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


def _normalized(doc, segments=True):
    return mb.normalize_document(doc, include_exhibit_segments=segments)


class CaseMapNodeCategoryTests(unittest.TestCase):
    def setUp(self):
        self.complaint = _normalized(
            _doc(
                10,
                "complaint",
                [
                    "Acme Holdings LLC v. Beta Insurance Co. "
                    "Plaintiff alleges premium payment was completed. "
                    "FIRST CAUSE OF ACTION for breach of contract. "
                    "Policy No. POL-998877 governs coverage. "
                    "The occurrence was filed on January 15, 2024.",
                    "EXHIBIT A",
                    "Lease agreement body continuing without label " + ("x" * 80),
                ],
            )
        )
        self.answer = _normalized(
            _doc(
                11,
                "answer",
                [
                    "Defendant alleges premium payment was never completed. "
                    "FIRST AFFIRMATIVE DEFENSE of failure to perform. "
                    "Notice of Motion is not in this pleading.",
                ],
            )
        )
        self.motion = _normalized(
            _doc(
                12,
                "motion",
                [
                    "Notice of Motion for Summary Judgment returnable March 1, 2024. "
                    "Movant respectfully seeks dismissal.",
                ],
            )
        )
        self.order = _normalized(
            _doc(
                13,
                "order",
                [
                    "Decision and Order. IT IS HEREBY ORDERED that the motion is held.",
                ],
            )
        )
        self.case_map = mb.build_case_map_from_documents(
            [self.complaint, self.answer, self.motion, self.order]
        )

    def test_all_node_categories_populated(self):
        for collection in mb.CASE_MAP_NODE_COLLECTIONS:
            self.assertTrue(
                self.case_map[collection],
                msg=f"expected nodes in {collection}",
            )
        self.assertTrue(self.case_map["relationships"])
        self.assertTrue(self.case_map["review_candidates"])

    def test_deterministic_ids(self):
        again = mb.build_case_map_from_documents(
            [self.complaint, self.answer, self.motion, self.order]
        )
        ids_a = sorted(node["id"] for _, node in mb.iter_case_map_nodes(self.case_map))
        ids_b = sorted(node["id"] for _, node in mb.iter_case_map_nodes(again))
        self.assertEqual(ids_a, ids_b)
        rel_a = sorted(rel["id"] for rel in self.case_map["relationships"])
        rel_b = sorted(rel["id"] for rel in again["relationships"])
        self.assertEqual(rel_a, rel_b)
        for node_id in ids_a:
            self.assertTrue(node_id.startswith("cmap-"))

    def test_record_citations_on_substantive_nodes(self):
        for _, node in mb.iter_case_map_nodes(self.case_map):
            if node["assertion_kind"] == "unknown":
                continue
            self.assertTrue(node.get("record_support"))
            for support in node["record_support"]:
                self.assertIsInstance(support.get("nyscef_document_number"), int)
                self.assertTrue(support.get("page_ids"))
                for page_id in support["page_ids"]:
                    self.assertRegex(page_id, r"^nyscef-\d{3}-page-\d{4}$")
                self.assertIn("excerpt", support)

    def test_allegation_vs_fact_separation(self):
        for allegation in self.case_map["allegations"]:
            self.assertEqual(allegation["assertion_kind"], "party_allegation")
            self.assertNotEqual(allegation["assertion_kind"], "verified_record_fact")
        for claim in self.case_map["claims"]:
            self.assertEqual(claim["assertion_kind"], "party_allegation")
        for defense in self.case_map["defenses"]:
            self.assertEqual(defense["assertion_kind"], "legal_position")

    def test_conflicting_claims_coexist_and_link(self):
        allegations = self.case_map["allegations"]
        self.assertGreaterEqual(len(allegations), 2)
        linked = [
            node
            for node in allegations
            if node.get("conflicts_with")
        ]
        self.assertTrue(linked)
        conflict_rels = [
            rel
            for rel in self.case_map["relationships"]
            if rel["relation_type"] == "conflicts_with"
        ]
        self.assertTrue(conflict_rels)
        # Both sides remain present; nothing is dropped/reconciled.
        labels = " ".join(a["label"].lower() for a in allegations)
        self.assertIn("completed", labels)
        self.assertIn("never", labels)

    def test_unknowns_not_fabricated(self):
        sparse = _normalized(
            _doc(40, "other", ["Calendar notice without party caption text " + ("z" * 40)])
        )
        case_map = mb.build_case_map_from_documents([sparse])
        self.assertTrue(case_map["parties"])
        unknown_parties = [
            p for p in case_map["parties"] if p["assertion_kind"] == "unknown"
        ]
        self.assertTrue(unknown_parties)
        self.assertEqual(unknown_parties[0]["status"], "unknown")
        self.assertEqual(unknown_parties[0]["label"], "")

    def test_exhibit_provenance_on_evidence(self):
        exhibits = [
            node
            for node in self.case_map["evidence"]
            if (node.get("label") or "").startswith("Exhibit ")
        ]
        self.assertTrue(exhibits)
        exhibit = exhibits[0]
        support = exhibit["record_support"][0]
        self.assertEqual(support["nyscef_document_number"], 10)
        self.assertIn("nyscef-010-page-0002", support["page_ids"])
        self.assertEqual(support.get("exhibit_label"), "A")
        self.assertTrue(support.get("segment_id", "").startswith("nyscef-010-segment-"))
        attached = [
            rel
            for rel in self.case_map["relationships"]
            if rel["relation_type"] == "attached_as_exhibit"
        ]
        self.assertTrue(attached)


class CaseMapMergeAndValidationTests(unittest.TestCase):
    def test_incremental_merge_preserves_prior_provenance(self):
        first = mb.build_case_map_from_documents(
            [
                _normalized(
                    _doc(
                        21,
                        "complaint",
                        [
                            "Gamma LLC v. Delta LLC. "
                            "FIRST CAUSE OF ACTION for negligence. "
                            "Plaintiff Gamma LLC alleges roof damage occurred."
                        ],
                    )
                )
            ]
        )
        second = mb.build_case_map_from_documents(
            [
                _normalized(
                    _doc(
                        22,
                        "answer",
                        [
                            "Defendant Delta LLC alleges roof damage never occurred. "
                            "FIRST AFFIRMATIVE DEFENSE of comparative fault."
                        ],
                    )
                )
            ]
        )
        prior_support = copy.deepcopy(first["claims"][0]["record_support"])
        merged = mb.merge_case_maps(first, second)
        self.assertTrue(merged["claims"])
        self.assertTrue(merged["defenses"])
        claim = next(c for c in merged["claims"] if c["id"] == first["claims"][0]["id"])
        self.assertEqual(claim["record_support"], prior_support)
        # Second filing provenance also present elsewhere in the map.
        all_nyscef = {
            support.get("nyscef_document_number")
            for _, node in mb.iter_case_map_nodes(merged)
            for support in node.get("record_support") or []
        }
        self.assertIn(21, all_nyscef)
        self.assertIn(22, all_nyscef)

    def test_validator_flags_invalid_page_ids(self):
        case_map = mb.empty_case_map()
        bad = mb.build_case_map_node(
            "claim",
            "Invented claim",
            nyscef_document_number=5,
            page_ids=["not-a-page-id"],
            assertion_kind="party_allegation",
            stable_key="bad-page",
            excerpt="no grounding",
            requires_review=True,
        )
        case_map["claims"].append(bad)
        docs = [_normalized(_doc(5, "complaint", ["FIRST CAUSE OF ACTION for fraud."]))]
        result = mb.validate_case_map(case_map, docs)
        self.assertFalse(result["ok"])
        codes = {err["code"] for err in result["errors"]}
        self.assertIn("invalid_page_id", codes)

    def test_validator_flags_dangling_relationships(self):
        docs = [_normalized(_doc(6, "motion", ["Notice of Motion for Summary Judgment."]))]
        case_map = mb.build_case_map_from_documents(docs)
        case_map["relationships"].append(
            mb.build_case_map_relationship(
                "raises_issue",
                case_map["motions"][0]["id"],
                "cmap-allegation-nyscef-006-missing",
                nyscef_document_number=6,
                page_ids=[docs[0]["pages"][0]["page_id"]],
                assertion_kind="inference",
                excerpt="dangling",
            )
        )
        result = mb.validate_case_map(case_map, docs)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any(err["code"] == "dangling_relationship" for err in result["errors"])
        )

    def test_validator_flags_duplicate_ids(self):
        docs = [_normalized(_doc(7, "order", ["IT IS HEREBY ORDERED that discovery continue."]))]
        case_map = mb.build_case_map_from_documents(docs)
        duplicate = copy.deepcopy(case_map["court_orders"][0])
        case_map["motions"].append(duplicate)
        result = mb.validate_case_map(case_map, docs)
        self.assertFalse(result["ok"])
        self.assertTrue(any(err["code"] == "duplicate_id" for err in result["errors"]))

    def test_validator_flags_unsupported_substantive_assertion(self):
        case_map = mb.empty_case_map()
        node = mb.build_case_map_node(
            "claim",
            "Unsupported",
            nyscef_document_number=8,
            page_ids=["nyscef-008-page-0001"],
            assertion_kind="party_allegation",
            stable_key="unsupported",
        )
        node["record_support"] = []
        case_map["claims"].append(node)
        result = mb.validate_case_map(case_map)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any(err["code"] == "unsupported_assertion" for err in result["errors"])
        )

    def test_validator_flags_provenance_mismatch(self):
        case_map = mb.empty_case_map()
        node = mb.build_case_map_node(
            "evidence",
            "Mismatched",
            nyscef_document_number=9,
            page_ids=["nyscef-009-page-0001"],
            assertion_kind="verified_record_fact",
            stable_key="mismatch",
            excerpt="x",
        )
        # Force page id from a different filing number.
        node["record_support"][0]["page_ids"] = ["nyscef-099-page-0001"]
        case_map["evidence"].append(node)
        result = mb.validate_case_map(case_map)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any(err["code"] == "provenance_mismatch" for err in result["errors"])
        )

    def test_validator_rejects_allegation_promoted_to_fact(self):
        case_map = mb.empty_case_map()
        node = mb.build_case_map_node(
            "allegation",
            "Plaintiff alleges payment",
            nyscef_document_number=3,
            page_ids=["nyscef-003-page-0001"],
            assertion_kind="party_allegation",
            stable_key="promoted",
            excerpt="Plaintiff alleges payment",
        )
        node["assertion_kind"] = "verified_record_fact"
        case_map["allegations"].append(node)
        result = mb.validate_case_map(case_map)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any(err["code"] == "allegation_promoted_to_fact" for err in result["errors"])
        )


class CaseMapBackwardCompatibilityTests(unittest.TestCase):
    def test_get_matter_default_omits_case_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = mb.get_matter(
                documents=[
                    _doc(
                        15,
                        "motion",
                        ["Notice of Motion for Summary Judgment " + ("m" * 40)],
                    )
                ],
                matter_folder=tmp,
            )
        self.assertNotIn("case_map", result)
        for document in result["documents"]:
            self.assertNotIn("exhibit_segments", document)

    def test_get_matter_opt_in_adds_case_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = mb.get_matter(
                documents=[
                    _doc(
                        16,
                        "complaint",
                        [
                            "North LLC v. South LLC. "
                            "FIRST CAUSE OF ACTION for conversion. "
                            "Plaintiff North LLC alleges conversion of goods. "
                            "EXHIBIT A",
                            "Invoice body " + ("i" * 90),
                        ],
                    )
                ],
                matter_folder=tmp,
                include_case_map=True,
            )
        self.assertIn("case_map", result)
        self.assertIn("claims", result["case_map"])
        self.assertTrue(result["case_map"]["validation"]["ok"])
        # Opt-in path may attach exhibit segments for provenance; core pages remain.
        for document in result["documents"]:
            if document.get("pages"):
                self.assertTrue(document["pages"][0]["page_id"].startswith("nyscef-"))

    def test_default_matter_keys_unchanged_without_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "note.txt").write_text("Legacy note without pages.", encoding="utf-8")
            result = mb.get_matter(matter_folder=tmp)
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
        self.assertEqual(set(result.keys()), expected_keys)

    def test_normalize_document_structure_unchanged_by_default(self):
        document = _doc(
            18,
            "motion",
            [
                "Parent affirmation text " + ("p" * 80),
                "EXHIBIT A",
                "Body " + ("b" * 80),
            ],
        )
        normalized = mb.normalize_document(document)
        self.assertNotIn("exhibit_segments", normalized)
        self.assertEqual(normalized["page_count"], 3)
        self.assertEqual(len(normalized["pages"]), 3)
        self.assertEqual(normalized["pages"][0]["page_id"], "nyscef-018-page-0001")


if __name__ == "__main__":
    unittest.main()
