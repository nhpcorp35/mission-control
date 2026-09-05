import hashlib
import io
import json
import unittest
import zipfile

from github_actions_bridge.case_intake import normalized_generic_contents_manifest


class GenericSourceManifestTests(unittest.TestCase):
    case_id = "NY-Suffolk-600371-2021-DeSousa-v-Calvagno-II-Karcher"

    def _manifest(self, files):
        return json.dumps({"case_id": self.case_id, "documents": [
            {"filename": name, "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
            for name, body in files.items()
        ]}, sort_keys=True).encode()

    def _zip(self, files):
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, "w") as archive:
            for name, body in files.items():
                archive.writestr(name, body)
        return bundle.getvalue()

    def test_preserves_hash_verified_pdf_image_and_audio_source_files(self):
        files = {"pleading.pdf": b"%PDF-1.4", "photo.jpg": b"\xff\xd8photo", "recording.wav": b"RIFFaudio"}
        normalized = json.loads(normalized_generic_contents_manifest(self.case_id, self._zip(files), self._manifest(files)))
        self.assertEqual({item["filename"] for item in normalized["files"]}, set(files))

    def test_rejects_unsupported_file_type(self):
        files = {"notes.txt": b"not an allowed source"}
        with self.assertRaisesRegex(ValueError, "unsupported"):
            normalized_generic_contents_manifest(self.case_id, self._zip(files), self._manifest(files))

    def test_rejects_hash_mismatch(self):
        expected = {"pleading.pdf": b"expected"}
        actual = {"pleading.pdf": b"altered!"}
        with self.assertRaisesRegex(ValueError, "hash"):
            normalized_generic_contents_manifest(self.case_id, self._zip(actual), self._manifest(expected))
