import io
import json
import unittest
import zipfile
from unittest.mock import patch
from github_actions_bridge.verified_case_index import build_ocr_page_records, build_page_records, diagnose_page_record


class _Body(io.BytesIO):
    def close(self): pass
class _Client:
    def __init__(self, data): self.data=data
    def head_object(self, **_): return {"ContentLength": len(self.data)}
    def get_object(self, **kw):
        s,e=map(int,kw["Range"].removeprefix("bytes=").split("-")); return {"Body":_Body(self.data[s:e+1])}


class IndexTests(unittest.TestCase):
    @patch("github_actions_bridge.verified_case_index.PdfReader")
    def test_indexes_manifest_pdfs_only(self, reader):
        reader.return_value.pages=[type("P",(),{"extract_text":lambda _: "hello"})()]
        blob=io.BytesIO()
        with zipfile.ZipFile(blob,"w") as z: z.writestr("Doc.pdf",b"x")
        rows=build_page_records(_Client(blob.getvalue()),"b","k",{"files":[{"filename":"Doc.pdf"},{"filename":"x.txt"}]})
        self.assertEqual(json.loads(rows)["filename"],"Doc.pdf")

    @patch("github_actions_bridge.verified_case_index.PdfReader")
    def test_indexes_pdf_when_manifest_uses_archive_directory(self, reader):
        reader.return_value.pages=[type("P",(),{"extract_text":lambda _: "hello"})()]
        blob=io.BytesIO()
        with zipfile.ZipFile(blob,"w") as z: z.writestr("SZYMCZYK case/Doc.pdf",b"x")
        rows=build_page_records(_Client(blob.getvalue()),"b","k",{"files":[{"filename":"SZYMCZYK case/Doc.pdf"}]})
        self.assertEqual(json.loads(rows)["filename"],"Doc.pdf")

    @patch("github_actions_bridge.verified_case_index.pytesseract.image_to_string", return_value="TEMPORARILY RESTRAINED AND ENJOINED")
    @patch("github_actions_bridge.verified_case_index.Image.open")
    @patch("github_actions_bridge.verified_case_index.subprocess.run")
    @patch("github_actions_bridge.verified_case_index.PdfReader")
    def test_ocr_index_enriches_sparse_scanned_page(self, reader, run, image_open, ocr):
        reader.return_value.pages = [type("P", (), {"extract_text": lambda _: "NYSCEF DOC. NO. 32"})()]
        image_open.return_value.__enter__.return_value = object()
        blob = io.BytesIO()
        with zipfile.ZipFile(blob, "w") as z:
            z.writestr("Order.pdf", b"x")

        rows = build_ocr_page_records(
            _Client(blob.getvalue()), "b", "k", {"files": [{"filename": "Order.pdf"}]}
        )

        row = json.loads(rows)
        self.assertEqual(row["filename"], "Order.pdf")
        self.assertIn("TEMPORARILY RESTRAINED AND ENJOINED", row["text"])
        run.assert_called_once()
        ocr.assert_called_once()


    def test_page_diagnostic_reports_exact_match_without_text(self):
        records = (json.dumps({"filename": "Answer.pdf", "page_number": 17, "text": "First affirmative defense"}) + "\n").encode()
        result = diagnose_page_record(
            records,
            filename="Answer.pdf",
            page_number=17,
            extracted_text="First affirmative defense",
        )
        self.assertTrue(result["indexed_record_present"])
        self.assertTrue(result["matches"])
        self.assertEqual(result["direct_text_length"], 25)
        self.assertNotIn("text", result)

    def test_page_diagnostic_reports_missing_index_record(self):
        result = diagnose_page_record(
            b"",
            filename="Answer.pdf",
            page_number=17,
            extracted_text="First affirmative defense",
        )
        self.assertFalse(result["indexed_record_present"])
        self.assertFalse(result["matches"])
        self.assertIsNone(result["indexed_text_sha256"])
