import io
import json
import unittest
import zipfile
from unittest.mock import patch
from github_actions_bridge.verified_case_index import build_page_records


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
