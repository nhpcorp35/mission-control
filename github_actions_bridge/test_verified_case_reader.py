import io
import unittest
from github_actions_bridge.verified_case_reader import RangeObjectReader, canonical_source_prefix, source_set_key, validate_page_request, validate_source_set


class _Body(io.BytesIO):
    def close(self) -> None:
        pass


class _RangeClient:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.ranges: list[str] = []

    def get_object(self, **kwargs):
        requested = kwargs["Range"]
        self.ranges.append(requested)
        start, end = (int(value) for value in requested.removeprefix("bytes=").split("-"))
        return {"Body": _Body(self.data[start:end + 1])}


class VerifiedCaseReaderTests(unittest.TestCase):
    def test_builds_only_canonical_source_prefix(self):
        self.assertEqual(canonical_source_prefix("NY-Nassau-613561-2026-Desousa-v-Rennick", "a" * 64), "cases/NY-Nassau-613561-2026-Desousa-v-Rennick/intake/source/" + "a" * 64 + "/")

    def test_validates_additive_immutable_source_set(self):
        case_id = "NY-Nassau-613561-2026-Desousa-v-Rennick"
        self.assertEqual(
            validate_source_set(case_id, {"schema_version": "verified-case-source-set.v1", "case_id": case_id, "sources": [{"source_sha256": "a" * 64}, {"source_sha256": "b" * 64}]}),
            ["a" * 64, "b" * 64],
        )
        self.assertEqual(source_set_key(case_id), f"cases/{case_id}/intake/source_set.json")

    def test_refuses_duplicate_or_cross_case_source_set(self):
        case_id = "NY-Nassau-613561-2026-Desousa-v-Rennick"
        with self.assertRaises(ValueError):
            validate_source_set(case_id, {"schema_version": "verified-case-source-set.v1", "case_id": case_id, "sources": [{"source_sha256": "a" * 64}, {"source_sha256": "a" * 64}]})
        with self.assertRaises(ValueError):
            validate_source_set(case_id, {"schema_version": "verified-case-source-set.v1", "case_id": "NY-NewYork-158068-2018-Szymczyk-v-Hudson-36-37", "sources": [{"source_sha256": "a" * 64}]})

    def test_limits_and_normalizes_page_requests(self):
        self.assertEqual(validate_page_request("Doc 2.pdf", [4, 2, 4]), ("Doc 2.pdf", [2, 4]))

    def test_refuses_paths_and_unbounded_pages(self):
        with self.assertRaises(ValueError): validate_page_request("../secret.pdf", [1])
        with self.assertRaises(ValueError): validate_page_request("Doc.pdf", list(range(1, 12)))

    def test_range_reader_only_requests_bounded_blocks(self):
        client = _RangeClient(b"abcdef" * 400_000)
        reader = RangeObjectReader(client, "bucket", "source.zip", len(client.data))
        reader.seek(1_100_000)
        self.assertEqual(reader.read(3), b"cde")
        self.assertEqual(len(client.ranges), 1)
        self.assertTrue(client.ranges[0].startswith("bytes=1048576-"))
