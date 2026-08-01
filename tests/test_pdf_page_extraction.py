"""Regression tests for complete PDF page extraction in matter_builder."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import matter_builder


PAGE_COUNT = 25


def build_multipage_pdf(path: Path, page_count: int = PAGE_COUNT) -> list[str]:
    """Write a PDF with ``page_count`` pages and return the expected page markers."""
    markers = []
    pdf = canvas.Canvas(str(path))

    for index in range(1, page_count + 1):
        marker = f"PAGE_MARKER_{index:02d}"
        markers.append(marker)
        pdf.drawString(72, 720, marker)
        pdf.showPage()

    pdf.save()
    return markers


class PdfPageExtractionTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmpdir.name)
        self.pdf_path = self.folder / "long_exhibit.pdf"
        self.markers = build_multipage_pdf(self.pdf_path, PAGE_COUNT)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_native_extraction_processes_more_than_twenty_pages(self):
        reader = PdfReader(str(self.pdf_path))
        self.assertGreater(len(reader.pages), 20)
        self.assertEqual(len(reader.pages), PAGE_COUNT)

        text, pages = matter_builder.extract_pdf_native(self.pdf_path)

        self.assertEqual(len(pages), PAGE_COUNT)

        # Former 20-page cap would drop markers 21–25.
        for marker in self.markers:
            self.assertIn(marker, text)

        for marker in self.markers[20:]:
            self.assertIn(marker, text)

    def test_ordered_page_collection_preserves_boundaries(self):
        _text, pages = matter_builder.extract_pdf_native(self.pdf_path)

        self.assertEqual(len(pages), PAGE_COUNT)
        for index, page in enumerate(pages, start=1):
            self.assertIsInstance(page, dict)
            self.assertEqual(page["page_number"], index)
            self.assertIn(self.markers[index - 1], page["text"])

        # Boundaries retained: each marker appears on its own page entry only.
        for index, marker in enumerate(self.markers, start=1):
            owning = [p for p in pages if marker in p["text"]]
            self.assertEqual(len(owning), 1)
            self.assertEqual(owning[0]["page_number"], index)

    def test_combined_text_field_remains_complete_and_ordered(self):
        text, pages = matter_builder.extract_pdf(self.pdf_path)

        self.assertIsInstance(text, str)
        self.assertTrue(text)

        positions = [text.find(marker) for marker in self.markers]
        self.assertTrue(all(pos >= 0 for pos in positions))
        self.assertEqual(positions, sorted(positions))

        rebuilt = matter_builder.clean_text(
            matter_builder.combined_text_from_pages(pages)
        )
        self.assertEqual(text, rebuilt)

    def test_read_matter_folder_document_dict_is_backward_compatible(self):
        documents = matter_builder.read_matter_folder(self.folder)
        self.assertEqual(len(documents), 1)

        document = documents[0]
        self.assertIn("text", document)
        self.assertIn("pages", document)
        self.assertIn("preview", document)
        self.assertIn("filename", document)

        self.assertEqual(len(document["pages"]), PAGE_COUNT)
        for marker in self.markers:
            self.assertIn(marker, document["text"])

        normalized = matter_builder.normalize_document(document)
        self.assertIn("text", normalized)
        self.assertIn("pages", normalized)
        self.assertEqual(len(normalized["pages"]), PAGE_COUNT)
        self.assertEqual(normalized["text"], document["text"])

        # Existing consumers that only read ``text`` remain valid.
        consumer_text = normalized["text"]
        self.assertIsInstance(consumer_text, str)
        self.assertGreater(len(consumer_text), 0)

    def test_extract_text_api_still_returns_combined_string(self):
        result = matter_builder.extract_text(self.pdf_path)
        self.assertIsInstance(result, str)
        for marker in self.markers:
            self.assertIn(marker, result)

    def test_no_twenty_page_cap_remains_in_native_path(self):
        source = Path(matter_builder.__file__).read_text(encoding="utf-8")
        self.assertNotIn("pages[:20]", source)
        self.assertNotIn("OCR_MAX_PAGES", source)


if __name__ == "__main__":
    unittest.main()
