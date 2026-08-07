"""Regression tests for embedded exhibit segmentation during LegalAI ingestion."""

from __future__ import annotations

import copy
import unittest

import matter_builder as mb


def _page(page_number, text, nyscef_document_number=7, extraction_method="native"):
    return mb.build_page_record(
        page_number,
        text,
        extraction_method,
        nyscef_document_number,
    )


def _filing_pages(nyscef_document_number, texts):
    return [
        _page(i, text, nyscef_document_number=nyscef_document_number)
        for i, text in enumerate(texts, start=1)
    ]


def _all_page_ids(segments):
    return [page_id for seg in segments for page_id in seg["page_ids"]]


class NoExhibitFilingTests(unittest.TestCase):
    def test_no_exhibit_filing_yields_single_parent_segment(self):
        pages = _filing_pages(
            12,
            [
                "Notice of Motion returnable January 12 " + ("x" * 80),
                "Supporting affirmation paragraph one " + ("y" * 80),
            ],
        )

        result = mb.segment_embedded_exhibits(pages, 12)

        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(result["uncertain_boundaries"], [])
        segment = result["segments"][0]
        self.assertEqual(segment["segment_type"], "parent")
        self.assertIsNone(segment["exhibit_label"])
        self.assertEqual(segment["start_page"], 1)
        self.assertEqual(segment["end_page"], 2)
        self.assertEqual(
            segment["page_ids"],
            ["nyscef-012-page-0001", "nyscef-012-page-0002"],
        )
        self.assertEqual(segment["nyscef_document_number"], 12)
        self.assertEqual(segment["boundary_confidence"], "high")


class SingleExhibitTests(unittest.TestCase):
    def test_one_embedded_exhibit_with_parent_prefix(self):
        pages = _filing_pages(
            5,
            [
                "Attorney affirmation introducing the annexed records " + ("a" * 80),
                "EXHIBIT A",
                "Lease agreement body continuing without a repeated label " + ("b" * 80),
            ],
        )

        result = mb.segment_embedded_exhibits(pages, 5)
        segments = result["segments"]

        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["segment_type"], "parent")
        self.assertEqual(segments[0]["start_page"], 1)
        self.assertEqual(segments[0]["end_page"], 1)
        self.assertEqual(segments[1]["segment_type"], "exhibit")
        self.assertEqual(segments[1]["exhibit_label"], "A")
        self.assertEqual(segments[1]["start_page"], 2)
        self.assertEqual(segments[1]["end_page"], 3)
        self.assertIn(segments[1]["boundary_confidence"], {"high", "medium"})
        self.assertTrue(segments[1]["boundary_evidence"])


class MultipleExhibitTests(unittest.TestCase):
    def test_multiple_exhibits_with_labels_and_titles(self):
        pages = _filing_pages(
            9,
            [
                "Main affirmation text before exhibits " + ("m" * 80),
                "EXHIBIT A - Deed",
                "Deed recording continuation page " + ("d" * 80),
                "EXHIBIT B - Lease Agreement",
                "Lease continuation page one " + ("l" * 80),
                "EXHIBIT 1",
                "Numeric exhibit body page " + ("n" * 80),
            ],
        )

        result = mb.segment_embedded_exhibits(pages, 9)
        segments = result["segments"]

        self.assertEqual(
            [s["segment_type"] for s in segments],
            ["parent", "exhibit", "exhibit", "exhibit"],
        )
        self.assertEqual(
            [s.get("exhibit_label") for s in segments],
            [None, "A", "B", "1"],
        )
        self.assertEqual(segments[1]["exhibit_title"], "Deed")
        self.assertEqual(segments[2]["exhibit_title"], "Lease Agreement")
        self.assertEqual(segments[1]["start_page"], 2)
        self.assertEqual(segments[1]["end_page"], 3)
        self.assertEqual(segments[2]["start_page"], 4)
        self.assertEqual(segments[2]["end_page"], 5)
        self.assertEqual(segments[3]["start_page"], 6)
        self.assertEqual(segments[3]["end_page"], 7)


class ContinuationPageTests(unittest.TestCase):
    def test_continuation_pages_stay_with_prior_exhibit(self):
        pages = _filing_pages(
            3,
            [
                "EXHIBIT A",
                "First continuation without label " + ("c" * 90),
                "Second continuation still unlabeled " + ("c" * 90),
                "EXHIBIT B",
                "Exhibit B body " + ("b" * 90),
            ],
        )

        segments = mb.segment_embedded_exhibits(pages, 3)["segments"]

        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["exhibit_label"], "A")
        self.assertEqual(segments[0]["page_ids"], [
            "nyscef-003-page-0001",
            "nyscef-003-page-0002",
            "nyscef-003-page-0003",
        ])
        self.assertEqual(segments[1]["exhibit_label"], "B")
        self.assertEqual(segments[1]["start_page"], 4)
        self.assertEqual(segments[1]["end_page"], 5)


class UncertainBoundaryTests(unittest.TestCase):
    def test_uncertain_boundary_is_not_silently_asserted(self):
        pages = _filing_pages(
            18,
            [
                "Affirmation discussing damages calculations " + ("a" * 100),
                (
                    "The damages table references Exhibit Q among other items "
                    "and continues with substantial analysis of invoices "
                    + ("z" * 120)
                ),
                "Closing prayer for relief and signature block " + ("c" * 100),
            ],
        )

        result = mb.segment_embedded_exhibits(pages, 18)

        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(result["segments"][0]["segment_type"], "parent")
        self.assertTrue(result["uncertain_boundaries"])
        uncertain = result["uncertain_boundaries"][0]
        self.assertEqual(uncertain["page_number"], 2)
        self.assertEqual(uncertain["exhibit_label"], "Q")
        self.assertEqual(uncertain["boundary_confidence"], "low")
        self.assertTrue(uncertain["boundary_evidence"])

    def test_prose_see_exhibit_reference_is_ignored(self):
        pages = _filing_pages(
            18,
            [
                "Plaintiff will see Exhibit A attached hereto for the lease "
                + ("p" * 100),
                "More affirmation text without a cover sheet " + ("p" * 100),
            ],
        )

        result = mb.segment_embedded_exhibits(pages, 18)

        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(result["segments"][0]["segment_type"], "parent")
        self.assertEqual(result["uncertain_boundaries"], [])


class DeterminismAndCoverageTests(unittest.TestCase):
    def test_segment_ids_are_deterministic(self):
        pages = _filing_pages(
            21,
            [
                "Parent filing preface " + ("p" * 80),
                "EXHIBIT A",
                "Body " + ("b" * 80),
                "EXHIBIT B - Invoice",
                "Invoice body " + ("i" * 80),
            ],
        )

        first = mb.segment_embedded_exhibits(pages, 21)
        second = mb.segment_embedded_exhibits(copy.deepcopy(pages), 21)

        self.assertEqual(
            [s["segment_id"] for s in first["segments"]],
            [s["segment_id"] for s in second["segments"]],
        )
        self.assertEqual(
            [s["segment_id"] for s in first["segments"]],
            [
                "nyscef-021-segment-0001",
                "nyscef-021-segment-0002",
                "nyscef-021-segment-0003",
            ],
        )

    def test_no_missing_or_duplicated_pages(self):
        pages = _filing_pages(
            4,
            [
                "Intro " + ("i" * 80),
                "EXHIBIT A",
                "Cont 1 " + ("c" * 80),
                "Cont 2 " + ("c" * 80),
                "EXHIBIT B",
                "Cont B " + ("c" * 80),
            ],
        )

        segments = mb.segment_embedded_exhibits(pages, 4)["segments"]
        assigned = _all_page_ids(segments)
        expected = [p["page_id"] for p in pages]

        self.assertEqual(assigned, expected)
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertEqual(len(assigned), len(pages))


class CitationProvenanceTests(unittest.TestCase):
    def test_one_based_nyscef_page_ids_and_parent_filing_number(self):
        pages = _filing_pages(
            45,
            [
                "Motion preface " + ("m" * 80),
                "EXHIBIT A",
                "Exhibit body " + ("e" * 80),
            ],
        )

        segments = mb.segment_embedded_exhibits(pages, 45)["segments"]

        self.assertEqual(pages[0]["page_number"], 1)
        self.assertEqual(pages[0]["page_id"], "nyscef-045-page-0001")
        for segment in segments:
            self.assertEqual(segment["nyscef_document_number"], 45)
            self.assertTrue(segment["segment_id"].startswith("nyscef-045-segment-"))
            for page_id in segment["page_ids"]:
                self.assertTrue(page_id.startswith("nyscef-045-page-"))
                self.assertNotIn("page-0000", page_id)


class BackwardCompatibilityTests(unittest.TestCase):
    def test_normalize_document_omits_segments_by_default(self):
        document = {
            "filename": "nyscef_doc_no_4.pdf",
            "nyscef_document_number": 4,
            "type": "motion",
            "pages": _filing_pages(
                4,
                [
                    "Parent text " + ("p" * 80),
                    "EXHIBIT A",
                    "Body " + ("b" * 80),
                ],
            ),
            "page_count": 3,
        }

        normalized = mb.normalize_document(document)

        self.assertNotIn("exhibit_segments", normalized)
        self.assertNotIn("uncertain_exhibit_boundaries", normalized)
        self.assertEqual(len(normalized["pages"]), 3)
        self.assertEqual(normalized["pages"][0]["page_id"], "nyscef-004-page-0001")
        self.assertEqual(normalized["nyscef_document_number"], 4)

    def test_normalize_document_opt_in_adds_segments(self):
        document = {
            "filename": "nyscef_doc_no_4.pdf",
            "nyscef_document_number": 4,
            "type": "motion",
            "pages": _filing_pages(
                4,
                [
                    "Parent text " + ("p" * 80),
                    "EXHIBIT A",
                    "Body " + ("b" * 80),
                ],
            ),
            "page_count": 3,
        }

        normalized = mb.normalize_document(document, include_exhibit_segments=True)

        self.assertIn("exhibit_segments", normalized)
        self.assertEqual(len(normalized["exhibit_segments"]), 2)
        self.assertEqual(normalized["exhibit_segments"][1]["exhibit_label"], "A")
        # Core page structure remains intact for existing consumers.
        self.assertEqual(len(normalized["pages"]), 3)
        self.assertEqual(normalized["page_count"], 3)

    def test_document_flag_opt_in(self):
        document = {
            "filename": "Doc_No_8.pdf",
            "nyscef_document_number": 8,
            "include_exhibit_segments": True,
            "pages": _filing_pages(8, ["Only parent material " + ("p" * 90)]),
            "page_count": 1,
        }

        normalized = mb.normalize_document(document)

        self.assertEqual(len(normalized["exhibit_segments"]), 1)
        self.assertEqual(normalized["exhibit_segments"][0]["segment_type"], "parent")

    def test_legacy_documents_without_pages_unchanged(self):
        document = {
            "filename": "manual_note.txt",
            "text": "Legacy document text for engines.",
            "type": "other",
            "source": "manual",
        }

        normalized = mb.normalize_document(
            document,
            include_exhibit_segments=True,
        )

        self.assertEqual(normalized["text"], "Legacy document text for engines.")
        self.assertNotIn("pages", normalized)
        self.assertNotIn("exhibit_segments", normalized)


if __name__ == "__main__":
    unittest.main()
