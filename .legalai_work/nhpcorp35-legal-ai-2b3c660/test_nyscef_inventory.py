"""Tests for Case-00 Triborough NYSCEF inventory and verified page IDs."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

import matter_builder as mb


INVENTORY_PATH = Path("data/case-00-triborough/nyscef_filing_inventory.json")
MOUNTED_CORPUS = Path(
    "/app/data/case-00-triborough/source-pdfs/original:/Tribrough Full Docket"
)

ADEQUATE_TEXT = "A" * (mb.OCR_MIN_TEXT_LENGTH + 20)


def _write_blank_pdf(path: Path, page_count: int = 1) -> Path:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class InventorySchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = json.loads(INVENTORY_PATH.read_text())
        cls.filings = cls.inventory["filings"]

    def test_physical_and_canonical_counts(self):
        self.assertEqual(self.inventory["physical_pdf_count"], 102)
        self.assertEqual(self.inventory["canonical_filing_count"], 97)
        self.assertEqual(len(self.filings), 102)
        canonical = [f for f in self.filings if f.get("ingest_canonical")]
        self.assertEqual(len(canonical), 97)

    def test_filings_1_through_97_with_only_expected_duplicates(self):
        numbers = [f["nyscef_document_number"] for f in self.filings]
        self.assertEqual(min(numbers), 1)
        self.assertEqual(max(numbers), 97)

        from collections import Counter

        counts = Counter(numbers)
        self.assertEqual(sorted(counts), list(range(1, 98)))
        repeated = sorted(n for n, c in counts.items() if c > 1)
        self.assertEqual(repeated, [4, 6, 7, 8, 9])
        self.assertEqual(self.inventory["duplicate_filing_numbers"], [4, 6, 7, 8, 9])
        for n in repeated:
            self.assertEqual(counts[n], 2)

    def test_duplicate_pairs_share_hash_and_single_canonical(self):
        from collections import defaultdict

        by_num = defaultdict(list)
        for entry in self.filings:
            by_num[entry["nyscef_document_number"]].append(entry)

        for n in [4, 6, 7, 8, 9]:
            pair = by_num[n]
            self.assertEqual(len(pair), 2)
            self.assertEqual(pair[0]["sha256"], pair[1]["sha256"])
            canonical = [e for e in pair if e.get("ingest_canonical")]
            duplicates = [e for e in pair if not e.get("ingest_canonical")]
            self.assertEqual(len(canonical), 1)
            self.assertEqual(len(duplicates), 1)
            self.assertFalse(canonical[0]["duplicate_marker"])
            self.assertTrue(duplicates[0]["duplicate_marker"])
            self.assertEqual(canonical[0]["duplicate_group_id"], f"nyscef-{n:03d}")
            self.assertEqual(duplicates[0]["duplicate_group_id"], f"nyscef-{n:03d}")

    def test_page_count_metadata(self):
        self.assertEqual(self.inventory["physical_page_count"], 937)
        self.assertEqual(self.inventory["canonical_page_count"], 932)
        physical = sum(f["page_count"] for f in self.filings)
        canonical = sum(
            f["page_count"] for f in self.filings if f.get("ingest_canonical")
        )
        self.assertEqual(physical, 937)
        self.assertEqual(canonical, 932)


class InventoryLookupTests(unittest.TestCase):
    def test_verified_lookup_passes_number_to_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            pdf_path = folder / "nyscef_doc_no_45_motion.pdf"
            _write_blank_pdf(pdf_path, 2)
            digest = _sha256(pdf_path)

            inventory = {
                "filings": [
                    {
                        "filename": pdf_path.name,
                        "nyscef_document_number": 45,
                        "page_count": 2,
                        "sha256": digest,
                        "size_bytes": pdf_path.stat().st_size,
                        "duplicate_marker": False,
                        "duplicate_relationship": None,
                        "duplicate_group_id": None,
                        "ingest_canonical": True,
                    }
                ]
            }
            inv_path = folder / "inventory.json"
            inv_path.write_text(json.dumps(inventory))

            with patch.object(mb, "extract_pdf_ocr_page", return_value=""):
                with patch.object(
                    mb,
                    "extract_pdf_document",
                    wraps=mb.extract_pdf_document,
                ) as wrapped:
                    docs = mb.read_matter_folder(folder, inventory_path=inv_path)

            self.assertEqual(len(docs), 1)
            self.assertEqual(docs[0]["nyscef_document_number"], 45)
            self.assertEqual(docs[0]["nyscef_provenance_status"], "verified")
            self.assertEqual(docs[0]["pages"][0]["page_id"], "nyscef-045-page-0001")
            wrapped.assert_called()
            _, kwargs = wrapped.call_args
            self.assertEqual(kwargs.get("nyscef_document_number"), 45)

    def test_hash_mismatch_never_assigns_nyscef_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            pdf_path = folder / "nyscef_doc_no_45_motion.pdf"
            _write_blank_pdf(pdf_path, 1)

            inventory = {
                "filings": [
                    {
                        "filename": pdf_path.name,
                        "nyscef_document_number": 45,
                        "page_count": 1,
                        "sha256": "0" * 64,
                        "size_bytes": 1,
                        "duplicate_marker": False,
                        "duplicate_relationship": None,
                        "duplicate_group_id": None,
                        "ingest_canonical": True,
                    }
                ]
            }
            inv_path = folder / "inventory.json"
            inv_path.write_text(json.dumps(inventory))

            with patch.object(mb, "extract_pdf_ocr_page", return_value=""):
                docs = mb.read_matter_folder(folder, inventory_path=inv_path)

            self.assertEqual(len(docs), 1)
            self.assertIsNone(docs[0]["nyscef_document_number"])
            self.assertEqual(docs[0]["nyscef_provenance_status"], "hash_mismatch")
            self.assertEqual(docs[0]["pages"][0]["page_id"], "nyscef-000-page-0001")

    def test_missing_inventory_entry_never_assigns_guessed_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            pdf_path = folder / "nyscef_doc_no_45_motion.pdf"
            _write_blank_pdf(pdf_path, 1)

            inv_path = folder / "inventory.json"
            inv_path.write_text(json.dumps({"filings": []}))

            with patch.object(mb, "extract_pdf_ocr_page", return_value=""):
                docs = mb.read_matter_folder(folder, inventory_path=inv_path)

            self.assertEqual(len(docs), 1)
            self.assertIsNone(docs[0]["nyscef_document_number"])
            self.assertEqual(docs[0]["nyscef_provenance_status"], "missing")
            self.assertEqual(docs[0]["pages"][0]["page_id"], "nyscef-000-page-0001")

    def test_non_canonical_duplicate_excluded_from_ingestion(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            keep = folder / "filing_4.pdf"
            skip = folder / "filing_4(1).pdf"
            _write_blank_pdf(keep, 1)
            shutil.copyfile(keep, skip)
            digest = _sha256(keep)

            inventory = {
                "filings": [
                    {
                        "filename": keep.name,
                        "nyscef_document_number": 4,
                        "page_count": 1,
                        "sha256": digest,
                        "size_bytes": keep.stat().st_size,
                        "duplicate_marker": False,
                        "duplicate_relationship": "canonical_original",
                        "duplicate_group_id": "nyscef-004",
                        "ingest_canonical": True,
                    },
                    {
                        "filename": skip.name,
                        "nyscef_document_number": 4,
                        "page_count": 1,
                        "sha256": digest,
                        "size_bytes": skip.stat().st_size,
                        "duplicate_marker": True,
                        "duplicate_relationship": "duplicate_copy",
                        "duplicate_group_id": "nyscef-004",
                        "ingest_canonical": False,
                    },
                ]
            }
            inv_path = folder / "inventory.json"
            inv_path.write_text(json.dumps(inventory))

            with patch.object(mb, "extract_pdf_ocr_page", return_value=""):
                docs = mb.read_matter_folder(folder, inventory_path=inv_path)

            self.assertEqual(len(docs), 1)
            self.assertEqual(docs[0]["filename"], keep.name)
            self.assertEqual(docs[0]["nyscef_document_number"], 4)


class LegacyCompatibilityTests(unittest.TestCase):
    def test_folder_without_inventory_remains_backward_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            pdf_path = folder / "nyscef_doc_no_12_motion.pdf"
            _write_blank_pdf(pdf_path, 1)
            txt_path = folder / "note.txt"
            txt_path.write_text("legacy note")

            with patch.dict(mb.os.environ, {}, clear=False):
                mb.os.environ.pop(mb.LEGALAI_MATTER_FOLDER_ENV, None)
                mb.os.environ.pop(mb.LEGALAI_NYSCEF_INVENTORY_PATH_ENV, None)
                with patch.object(mb, "extract_pdf_ocr_page", return_value=""):
                    docs = mb.read_matter_folder(folder)

            by_name = {d["filename"]: d for d in docs}
            self.assertEqual(by_name[pdf_path.name]["nyscef_document_number"], 12)
            self.assertNotIn("nyscef_provenance_status", by_name[pdf_path.name])
            self.assertEqual(by_name[txt_path.name]["text"], "legacy note")
            self.assertNotIn("pages", by_name[txt_path.name])

    def test_default_matter_folder_without_env(self):
        with patch.dict(mb.os.environ, {}, clear=False):
            mb.os.environ.pop(mb.LEGALAI_MATTER_FOLDER_ENV, None)
            mb.os.environ.pop(mb.LEGALAI_NYSCEF_INVENTORY_PATH_ENV, None)
            self.assertEqual(mb.resolve_matter_folder(), mb.DEFAULT_MATTER_FOLDER)
            self.assertIsNone(mb.resolve_inventory_path())


@unittest.skipUnless(MOUNTED_CORPUS.is_dir(), "Mounted Triborough corpus unavailable")
class MountedCorpusVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = json.loads(INVENTORY_PATH.read_text())

    def test_physical_pdf_and_page_counts_with_pypdf(self):
        from pypdf import PdfReader

        pdfs = sorted(MOUNTED_CORPUS.glob("*.pdf"))
        self.assertEqual(len(pdfs), 102)
        self.assertTrue((MOUNTED_CORPUS / "Archive.zip").exists())

        physical_pages = 0
        for pdf in pdfs:
            physical_pages += len(PdfReader(str(pdf)).pages)
        self.assertEqual(physical_pages, 937)

        by_name = {f["filename"]: f for f in self.inventory["filings"]}
        canonical_pages = 0
        for pdf in pdfs:
            entry = by_name[pdf.name]
            pages = len(PdfReader(str(pdf)).pages)
            self.assertEqual(pages, entry["page_count"])
            self.assertEqual(_sha256(pdf), entry["sha256"])
            if entry.get("ingest_canonical"):
                canonical_pages += pages
        self.assertEqual(canonical_pages, 932)

    def test_canonical_ingestion_yields_filings_1_to_97_without_nyscef_000(self):
        with patch.object(mb, "extract_pdf_ocr_page", return_value=""):
            docs = mb.read_matter_folder(
                MOUNTED_CORPUS,
                inventory_path=INVENTORY_PATH,
            )

        self.assertEqual(len(docs), 97)
        numbers = sorted(d["nyscef_document_number"] for d in docs)
        self.assertEqual(numbers, list(range(1, 98)))
        self.assertEqual(len(set(numbers)), 97)

        page_ids = []
        total_pages = 0
        for doc in docs:
            self.assertEqual(doc["nyscef_provenance_status"], "verified")
            self.assertIsNotNone(doc["nyscef_document_number"])
            self.assertNotEqual(doc["nyscef_document_number"], 0)
            total_pages += doc["page_count"]
            for page in doc["pages"]:
                page_id = page["page_id"]
                self.assertFalse(page_id.startswith("nyscef-000-"))
                self.assertTrue(
                    page_id.startswith(
                        f"nyscef-{doc['nyscef_document_number']:03d}-page-"
                    )
                )
                page_ids.append(page_id)

        self.assertEqual(total_pages, 932)
        self.assertEqual(len(page_ids), 932)
        self.assertEqual(len(set(page_ids)), 932)


class PypdfDependencyTests(unittest.TestCase):
    def test_pypdf_importable_and_declared(self):
        import pypdf  # noqa: F401

        req = Path("requirements.txt").read_text().lower()
        self.assertIn("pypdf", req)


if __name__ == "__main__":
    unittest.main()
