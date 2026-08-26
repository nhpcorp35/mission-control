from __future__ import annotations

import base64
import hashlib
import io
import unittest

from github_actions_bridge.case_intake import decode_base64_upload, intake_keys, verify_object


class _FakeB2Client:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def head_object(self, **_: object) -> dict[str, object]:
        return {"ContentLength": len(self.payload), "ETag": '"test-etag"'}

    def get_object(self, **_: object) -> dict[str, object]:
        return {"Body": io.BytesIO(self.payload)}


class _NotFoundError(Exception):
    response = {"Error": {"Code": "404"}}


class _MissingB2Client:
    def head_object(self, **_: object) -> dict[str, object]:
        raise _NotFoundError()

    def list_objects_v2(self, **_: object) -> dict[str, object]:
        return {
            "Contents": [{"Key": "cases/NY-Nassau-613561-2026-Rennick/intake/other.zip", "Size": 7}],
            "IsTruncated": False,
        }


class CaseIntakeTests(unittest.TestCase):
    def test_decode_base64_upload_is_bounded_and_strict(self) -> None:
        self.assertEqual(
            decode_base64_upload(
                base64.b64encode(b"payload").decode("ascii"),
                label="payload",
                max_size=7,
            ),
            b"payload",
        )
        with self.assertRaises(ValueError):
            decode_base64_upload("not-base64", label="payload", max_size=7)

    def test_intake_keys_are_confined_to_the_case_intake_prefix(self) -> None:
        source, manifest = intake_keys(
            "NY-Nassau-613561-2026-Rennick",
            "Rennick_Case_Source_2026-08-26.zip",
            "Rennick_Case_Intake_Manifest_2026-08-26.json",
        )
        self.assertEqual(
            source,
            "cases/NY-Nassau-613561-2026-Rennick/intake/"
            "Rennick_Case_Source_2026-08-26.zip",
        )
        self.assertTrue(manifest.endswith("Rennick_Case_Intake_Manifest_2026-08-26.json"))

    def test_intake_keys_reject_path_traversal(self) -> None:
        with self.assertRaises(ValueError):
            intake_keys(
                "NY-Nassau-613561-2026-Rennick",
                "../source.zip",
                "manifest.json",
            )

    def test_verify_object_requires_exact_size_and_digest(self) -> None:
        payload = b"verified intake payload"
        result = verify_object(
            _FakeB2Client(payload),
            bucket="legalai-corpus",
            object_key="cases/NY-Nassau-613561-2026-Rennick/intake/source.zip",
            expected_size=len(payload),
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            max_size=1024,
        )
        self.assertEqual(result["size"], len(payload))
        self.assertEqual(result["etag"], "test-etag")

    def test_verify_object_reports_only_the_constrained_prefix_on_not_found(self) -> None:
        result = verify_object(
            _MissingB2Client(),
            bucket="legalai-corpus",
            object_key="cases/NY-Nassau-613561-2026-Rennick/intake/source.zip",
            expected_size=7,
            expected_sha256=hashlib.sha256(b"payload").hexdigest(),
            max_size=1024,
        )
        self.assertFalse(result["verified"])
        self.assertEqual(result["error"], "object_not_found")
        self.assertEqual(
            result["observed_prefix"],
            "cases/NY-Nassau-613561-2026-Rennick/intake/",
        )
        self.assertEqual(result["observed_objects"][0]["object_key"], result["observed_prefix"] + "other.zip")

    def test_verify_object_rejects_bad_digest(self) -> None:
        payload = b"verified intake payload"
        with self.assertRaises(ValueError):
            verify_object(
                _FakeB2Client(payload),
                bucket="legalai-corpus",
                object_key="cases/NY-Nassau-613561-2026-Rennick/intake/source.zip",
                expected_size=len(payload),
                expected_sha256="0" * 64,
                max_size=1024,
            )

