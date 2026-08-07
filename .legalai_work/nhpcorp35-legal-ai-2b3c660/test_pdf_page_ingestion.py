"""Regression tests for page-preserving LegalAI PDF ingestion."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pypdf import PdfWriter

import matter_builder as mb


ADEQUATE_TEXT = "A" * (mb.OCR_MIN_TEXT_LENGTH + 20)
SHORT_TEXT = "short native"


def _write_blank_pdf(path: Path, page_count: int) -> Path:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def _mock_reader(page_texts):
    reader = MagicMock()
    pages = []
    for text in page_texts:
        page = MagicMock()
        page.extract_text.return_value = text
        pages.append(page)
    reader.pages = pages
    return reader


class ParseNyscefDocumentNumberTests(unittest.TestCase):
    def test_benchmark_filename_does_not_invent_document_number(self):
        name = "2018-03730__decision__2026-03-10.pdf"
        self.assertIsNone(mb.parse_nyscef_document_number_from_filename(name))

    def test_explicit_nyscef_filename_patterns(self):
        self.assertEqual(
            mb.parse_nyscef_document_number_from_filename("nyscef_doc_no_45_motion.pdf"),
            45,
        )
        self.assertEqual(
            mb.parse_nyscef_document_number_from_filename("Doc_No_7_Affirmation.pdf"),
            7,
        )
        self.assertEqual(
            mb.parse_nyscef_document_number_from_filename("nyscef-12-complaint.pdf"),
            12,
        )

    def test_explicit_metadata_preferred(self):
        self.assertEqual(
            mb.resolve_nyscef_document_number(
                {"nyscef_document_number": "9", "filename": "nyscef_doc_no_45.pdf"}
            ),
            9,
        )


class PagePreservationTests(unittest.TestCase):
    def test_pdfs_longer_than_20_pages_retain_every_page(self):
        page_texts = [f"Page {i} content with enough body text for native keep." for i in range(1, 26)]
        # Ensure adequate length
        page_texts = [t + (" x" * 40) for t in page_texts]

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "nyscef_doc_no_3_long.pdf"
            _write_blank_pdf(pdf_path, 25)

            with patch.object(mb, "PdfReader", return_value=_mock_reader(page_texts)):
                result = mb.extract_pdf_document(pdf_path)

        self.assertEqual(result["page_count"], 25)
        self.assertEqual(len(result["pages"]), 25)
        self.assertEqual([p["page_number"] for p in result["pages"]], list(range(1, 26)))
        self.assertTrue(all(p["extraction_method"] == "native" for p in result["pages"]))

    def test_deterministic_one_based_page_ids(self):
        page_texts = [ADEQUATE_TEXT, ADEQUATE_TEXT]

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "Doc_No_15_motion.pdf"
            _write_blank_pdf(pdf_path, 2)

            with patch.object(mb, "PdfReader", return_value=_mock_reader(page_texts)):
                result = mb.extract_pdf_document(pdf_path)

        self.assertEqual(result["nyscef_document_number"], 15)
        self.assertEqual(result["pages"][0]["page_id"], "nyscef-015-page-0001")
        self.assertEqual(result["pages"][1]["page_id"], "nyscef-015-page-0002")
        self.assertEqual(result["pages"][0]["page_number"], 1)
        self.assertEqual(result["pages"][1]["page_number"], 2)

    def test_empty_pages_remain_represented(self):
        page_texts = [ADEQUATE_TEXT, "", ADEQUATE_TEXT]

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "nyscef_doc_no_2_mixed.pdf"
            _write_blank_pdf(pdf_path, 3)

            with patch.object(mb, "PdfReader", return_value=_mock_reader(page_texts)):
                with patch.object(mb, "extract_pdf_ocr_page", return_value="") as ocr:
                    result = mb.extract_pdf_document(pdf_path)

        self.assertEqual(result["page_count"], 3)
        self.assertEqual(result["pages"][1]["text"], "")
        self.assertEqual(result["pages"][1]["extraction_method"], "empty")
        self.assertEqual(result["pages"][1]["page_number"], 2)
        self.assertEqual(result["pages"][1]["page_id"], "nyscef-002-page-0002")
        # OCR attempted only for the inadequate/empty middle page
        ocr.assert_called_once_with(pdf_path, 2)

    def test_page_specific_ocr_fallback(self):
        page_texts = [ADEQUATE_TEXT, SHORT_TEXT, ""]
        ocr_by_page = {
            2: "OCR recovered page two text " + ("y" * 100),
            3: "",
        }

        def fake_ocr(path, page_number):
            return ocr_by_page.get(page_number, "")

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "nyscef_doc_no_8_ocr.pdf"
            _write_blank_pdf(pdf_path, 3)

            with patch.object(mb, "PdfReader", return_value=_mock_reader(page_texts)):
                with patch.object(mb, "extract_pdf_ocr_page", side_effect=fake_ocr) as ocr:
                    result = mb.extract_pdf_document(pdf_path)

        self.assertEqual(
            [p["extraction_method"] for p in result["pages"]],
            ["native", "ocr", "empty"],
        )
        self.assertEqual(result["pages"][0]["page_number"], 1)
        self.assertEqual(result["pages"][1]["page_number"], 2)
        self.assertEqual(result["pages"][2]["page_number"], 3)
        self.assertIn("OCR recovered page two text", result["pages"][1]["text"])
        self.assertEqual(ocr.call_count, 2)

    def test_aggregate_text_is_ordered_concatenation(self):
        page_texts = [
            "First page body " + ("a" * 120),
            "Second page body " + ("b" * 120),
            "Third page body " + ("c" * 120),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "nyscef_doc_no_1_agg.pdf"
            _write_blank_pdf(pdf_path, 3)

            with patch.object(mb, "PdfReader", return_value=_mock_reader(page_texts)):
                result = mb.extract_pdf_document(pdf_path)

        expected = mb.aggregate_page_text(result["pages"])
        self.assertEqual(result["text"], expected)
        self.assertLess(result["text"].index("First page body"), result["text"].index("Second page body"))
        self.assertLess(result["text"].index("Second page body"), result["text"].index("Third page body"))

    def test_unknown_document_number_uses_stable_page_id_fallback(self):
        page_texts = [ADEQUATE_TEXT]

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "2018-03730__decision__2026-03-10.pdf"
            _write_blank_pdf(pdf_path, 1)

            with patch.object(mb, "PdfReader", return_value=_mock_reader(page_texts)):
                result = mb.extract_pdf_document(pdf_path)

        self.assertIsNone(result["nyscef_document_number"])
        self.assertEqual(result["pages"][0]["page_id"], "nyscef-000-page-0001")


class NormalizeCompatibilityTests(unittest.TestCase):
    def test_documents_without_pages_remain_backward_compatible(self):
        document = {
            "filename": "manual_note.txt",
            "text": "Legacy document text for engines.",
            "type": "other",
            "source": "manual",
        }

        normalized = mb.normalize_document(document)

        self.assertEqual(normalized["text"], "Legacy document text for engines.")
        self.assertEqual(normalized["preview"], normalized["text"][:800])
        self.assertNotIn("pages", normalized)
        self.assertNotIn("page_count", normalized)

    def test_page_metadata_survives_normalize_document(self):
        document = {
            "filename": "nyscef_doc_no_4.pdf",
            "nyscef_document_number": 4,
            "type": "motion",
            "pages": [
                {
                    "page_number": 1,
                    "page_id": "nyscef-004-page-0001",
                    "text": "Page one " + ("z" * 100),
                    "extraction_method": "native",
                },
                {
                    "page_number": 2,
                    "page_id": "nyscef-004-page-0002",
                    "text": "",
                    "extraction_method": "empty",
                },
            ],
            "page_count": 2,
            "text": "ignored aggregate",
        }

        normalized = mb.normalize_document(document)

        self.assertEqual(normalized["nyscef_document_number"], 4)
        self.assertEqual(normalized["page_count"], 2)
        self.assertEqual(len(normalized["pages"]), 2)
        self.assertEqual(normalized["pages"][1]["extraction_method"], "empty")
        self.assertEqual(normalized["pages"][0]["page_id"], "nyscef-004-page-0001")
        self.assertEqual(normalized["text"], mb.aggregate_page_text(normalized["pages"]))
        self.assertEqual(normalized["preview"], normalized["text"][:800])

    def test_normalize_preserves_page_boundaries(self):
        document = {
            "filename": "Doc_No_11.pdf",
            "nyscef_document_number": 11,
            "pages": [
                {"page_number": 1, "text": "Alpha\n\nline", "extraction_method": "native"},
                {"page_number": 2, "text": "Beta\n\nline", "extraction_method": "native"},
            ],
            "page_count": 2,
        }

        normalized = mb.normalize_document(document)

        self.assertEqual(len(normalized["pages"]), 2)
        self.assertEqual(normalized["pages"][0]["text"], "Alpha line")
        self.assertEqual(normalized["pages"][1]["text"], "Beta line")
        self.assertNotEqual(normalized["pages"][0]["page_id"], normalized["pages"][1]["page_id"])


class RealBlankPdfTests(unittest.TestCase):
    def test_blank_pdf_over_20_pages_keeps_page_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "nyscef_doc_no_21_blank.pdf"
            _write_blank_pdf(pdf_path, 22)

            with patch.object(mb, "extract_pdf_ocr_page", return_value=""):
                result = mb.extract_pdf_document(pdf_path)

        self.assertEqual(result["page_count"], 22)
        self.assertEqual(len(result["pages"]), 22)
        self.assertEqual(result["pages"][-1]["page_number"], 22)
        self.assertEqual(result["pages"][-1]["page_id"], "nyscef-021-page-0022")
        self.assertTrue(all(p["extraction_method"] == "empty" for p in result["pages"]))


if __name__ == "__main__":
    unittest.main()
