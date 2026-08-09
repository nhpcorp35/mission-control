from __future__ import annotations

import base64
import io
import json
import unittest
import zipfile

from github_actions_bridge.storage_policy import (
    CASE00_PREFIXES,
    MAX_REVIEW_PACKET_BYTES,
    REVIEW_PACKET_MANIFEST_FILENAME,
    assert_archive_objects_absent,
    build_attorney_review_archive,
    build_review_packet_archive,
    decode_review_packet_docx_base64,
    inventory_prefix,
    validate_docx_bytes,
)


def _minimal_docx_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(
            "[Content_Types].xml",
            (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'wordprocessingml.document.main+xml"/>'
                "</Types>"
            ),
        )
        bundle.writestr(
            "word/document.xml",
            (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
                "Case-00 review packet fixture"
                "</w:t></w:r></w:p></w:body></w:document>"
            ),
        )
    return buffer.getvalue()


def _review_packet_kwargs(**overrides: object) -> dict[str, object]:
    docx_bytes = _minimal_docx_bytes()
    values: dict[str, object] = {
        "docx_base64": base64.b64encode(docx_bytes).decode("ascii"),
        "recipient": "attorney@example.com",
        "question_id": "Q1",
        "sent_at": "2026-08-02T15:30:00Z",
        "original_filename": "Case00-Q1-Review-Packet.docx",
        "archived_by": "nhpcorp35",
    }
    values.update(overrides)
    return values


class Case00StoragePolicyTests(unittest.TestCase):
    def test_inventory_prefix_is_allowlisted(self) -> None:
        self.assertEqual(
            inventory_prefix("attorney_reviews"), CASE00_PREFIXES["attorney_reviews"]
        )
        self.assertEqual(
            inventory_prefix("attorney_review_packets"),
            CASE00_PREFIXES["attorney_review_packets"],
        )
        with self.assertRaises(ValueError):
            inventory_prefix("../../other-bucket")

    def test_archive_is_deterministic_and_confined(self) -> None:
        kwargs = {
            "evaluation_date": "2026-08-02",
            "original_packet_md": "# Packet",
            "feedback_email_md": "# Feedback",
            "structured_evaluation_json": json.dumps({"Q1": "incorrect"}),
            "archived_by": "nhpcorp35",
        }
        first_id, first_items = build_attorney_review_archive(**kwargs)
        second_id, second_items = build_attorney_review_archive(**kwargs)
        self.assertEqual(first_id, second_id)
        self.assertEqual(
            [item["object_key"] for item in first_items],
            [item["object_key"] for item in second_items],
        )
        self.assertEqual(len(first_items), 4)
        for item in first_items:
            self.assertTrue(
                item["object_key"].startswith(CASE00_PREFIXES["attorney_reviews"])
            )

    def test_archive_rejects_invalid_inputs(self) -> None:
        with self.assertRaises(ValueError):
            build_attorney_review_archive(
                evaluation_date="2026-02-30",
                original_packet_md="packet",
                feedback_email_md="feedback",
                structured_evaluation_json="{}",
                archived_by="nhpcorp35",
            )
        with self.assertRaises(ValueError):
            build_attorney_review_archive(
                evaluation_date="2026-08-02",
                original_packet_md="packet",
                feedback_email_md="feedback",
                structured_evaluation_json="[]",
                archived_by="nhpcorp35",
            )


class Case00ReviewPacketArchiveTests(unittest.TestCase):
    def test_valid_construction_preserves_docx_bytes(self) -> None:
        docx_bytes = _minimal_docx_bytes()
        kwargs = _review_packet_kwargs(
            docx_base64=base64.b64encode(docx_bytes).decode("ascii")
        )
        archive_id, items = build_review_packet_archive(**kwargs)
        self.assertTrue(archive_id.startswith("packet-q1-20260802-"))
        self.assertEqual(len(items), 2)
        docx_item = items[0]
        manifest_item = items[1]
        self.assertEqual(docx_item["payload"], docx_bytes)
        self.assertEqual(docx_item["filename"], kwargs["original_filename"])
        self.assertEqual(manifest_item["filename"], REVIEW_PACKET_MANIFEST_FILENAME)
        manifest = json.loads(manifest_item["payload"].decode("utf-8"))
        self.assertEqual(manifest["archive_id"], archive_id)
        self.assertEqual(manifest["recipient"], kwargs["recipient"])
        self.assertEqual(manifest["question_id"], kwargs["question_id"])
        self.assertEqual(manifest["original_filename"], kwargs["original_filename"])
        self.assertNotIn("docx_base64", manifest)

    def test_archive_id_and_keys_are_deterministic(self) -> None:
        kwargs = _review_packet_kwargs()
        first_id, first_items = build_review_packet_archive(**kwargs)
        second_id, second_items = build_review_packet_archive(**kwargs)
        self.assertEqual(first_id, second_id)
        self.assertEqual(
            [item["object_key"] for item in first_items],
            [item["object_key"] for item in second_items],
        )

    def test_path_confinement_under_review_packets_prefix(self) -> None:
        _, items = build_review_packet_archive(**_review_packet_kwargs())
        prefix = CASE00_PREFIXES["attorney_review_packets"]
        for item in items:
            self.assertTrue(item["object_key"].startswith(prefix))
            self.assertNotIn("..", item["object_key"])
            self.assertNotIn("\\", item["object_key"])

    def test_strict_base64_rejects_whitespace_and_corrupt_input(self) -> None:
        valid = base64.b64encode(_minimal_docx_bytes()).decode("ascii")
        with self.assertRaises(ValueError):
            decode_review_packet_docx_base64(valid + "\n")
        with self.assertRaises(ValueError):
            decode_review_packet_docx_base64(valid[:-1] + "!")
        with self.assertRaises(ValueError):
            decode_review_packet_docx_base64("")

    def test_invalid_docx_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_docx_bytes(b"not-a-docx")
        empty_zip = io.BytesIO()
        with zipfile.ZipFile(empty_zip, "w") as bundle:
            bundle.writestr("readme.txt", "no ooxml")
        with self.assertRaises(ValueError):
            validate_docx_bytes(empty_zip.getvalue())
        with self.assertRaises(ValueError):
            build_review_packet_archive(
                **_review_packet_kwargs(
                    docx_base64=base64.b64encode(b"PK\x03\x04notzip").decode("ascii")
                )
            )

    def test_metadata_validation_allowlists(self) -> None:
        with self.assertRaises(ValueError):
            build_review_packet_archive(**_review_packet_kwargs(recipient="not-an-email"))
        with self.assertRaises(ValueError):
            build_review_packet_archive(**_review_packet_kwargs(question_id="Q99"))
        with self.assertRaises(ValueError):
            build_review_packet_archive(
                **_review_packet_kwargs(sent_at="2026-08-02 15:30:00")
            )
        with self.assertRaises(ValueError):
            build_review_packet_archive(
                **_review_packet_kwargs(original_filename="packet.pdf")
            )
        with self.assertRaises(ValueError):
            build_review_packet_archive(
                **_review_packet_kwargs(original_filename="../escape.docx")
            )

    def test_size_limit_enforced(self) -> None:
        oversized = b"a" * (MAX_REVIEW_PACKET_BYTES + 1)
        with self.assertRaises(ValueError):
            decode_review_packet_docx_base64(
                base64.b64encode(oversized).decode("ascii")
            )

    def test_collision_overwrite_rejected(self) -> None:
        _, items = build_review_packet_archive(**_review_packet_kwargs())
        assert_archive_objects_absent(items, object_exists=lambda _key: False)
        with self.assertRaises(ValueError) as ctx:
            assert_archive_objects_absent(items, object_exists=lambda _key: True)
        self.assertIn("already exists", str(ctx.exception))
        existing = {items[0]["object_key"]}
        with self.assertRaises(ValueError):
            assert_archive_objects_absent(
                items, object_exists=lambda key: key in existing
            )


if __name__ == "__main__":
    unittest.main()
