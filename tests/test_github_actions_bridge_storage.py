from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

from github_actions_bridge.storage_policy import (
    CASE00_PREFIXES,
    MAX_REVIEW_PACKET_BYTES,
    REVIEW_PACKET_MANIFEST_FILENAME,
    archive_create_only_put_params,
    assert_archive_objects_absent,
    build_attorney_review_archive,
    build_review_packet_archive,
    decode_review_packet_docx_base64,
    inventory_prefix,
    map_archive_put_precondition_failure,
    normalize_review_packet_recipient,
    validate_docx_bytes,
)

_BRIDGE_DIR = Path(__file__).resolve().parent.parent / "github_actions_bridge"
_BRIDGE_SERVER_ENV = {
    "GITHUB_OAUTH_CLIENT_ID": "test-client-id",
    "GITHUB_OAUTH_CLIENT_SECRET": "test-client-secret",
    "REDIS_HOST": "127.0.0.1",
    "REDIS_PORT": "6379",
    "STORAGE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "JWT_SIGNING_KEY": "test-jwt-signing-key-for-bridge",
}


def _import_bridge_server():
    """Load server the same way the container does (sibling storage_policy import)."""
    for key, value in _BRIDGE_SERVER_ENV.items():
        os.environ.setdefault(key, value)
    bridge_dir = str(_BRIDGE_DIR)
    if bridge_dir not in sys.path:
        sys.path.insert(0, bridge_dir)
    import server as bridge_server

    return bridge_server


def _minimal_docx_bytes(*, document_xml: bool = True) -> bytes:
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
        if document_xml:
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
        else:
            bundle.writestr("word/styles.xml", "<styles/>")
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
        self.assertEqual(manifest["recipient"], "attorney@example.com")
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

    def test_missing_word_document_xml_rejected(self) -> None:
        payload = _minimal_docx_bytes(document_xml=False)
        with self.assertRaises(ValueError) as ctx:
            validate_docx_bytes(payload)
        self.assertIn("word/document.xml", str(ctx.exception))
        with self.assertRaises(ValueError):
            build_review_packet_archive(
                **_review_packet_kwargs(
                    docx_base64=base64.b64encode(payload).decode("ascii")
                )
            )

    def test_invalid_recipient_rejected(self) -> None:
        for recipient in (
            "not-an-email",
            "attorney@",
            "@example.com",
            "attorney@example",
            "attorney example@example.com",
            "a" * (129),
        ):
            with self.assertRaises(ValueError) as ctx:
                build_review_packet_archive(
                    **_review_packet_kwargs(recipient=recipient)
                )
            message = str(ctx.exception).lower()
            self.assertTrue(
                "valid" in message or "exceeds" in message,
                msg=message,
            )
            self.assertNotIn("allowlisted", message)

    def test_recipient_case_normalization(self) -> None:
        self.assertEqual(
            normalize_review_packet_recipient("Attorney@Example.COM"),
            "attorney@example.com",
        )
        lower_id, lower_items = build_review_packet_archive(
            **_review_packet_kwargs(recipient="attorney@example.com")
        )
        mixed_id, mixed_items = build_review_packet_archive(
            **_review_packet_kwargs(recipient="Attorney@Example.COM")
        )
        self.assertEqual(lower_id, mixed_id)
        self.assertEqual(
            [item["object_key"] for item in lower_items],
            [item["object_key"] for item in mixed_items],
        )
        manifest = json.loads(mixed_items[1]["payload"].decode("utf-8"))
        self.assertEqual(manifest["recipient"], "attorney@example.com")

    def test_metadata_validation_allowlists(self) -> None:
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
        # Partial prior write (DOCX only) must fail closed on rerun.
        with self.assertRaises(ValueError) as partial_ctx:
            assert_archive_objects_absent(
                items, object_exists=lambda key: key == items[0]["object_key"]
            )
        self.assertIn("already exists", str(partial_ctx.exception))

    def test_atomic_conditional_put_params_and_precondition_errors(self) -> None:
        self.assertEqual(
            archive_create_only_put_params(),
            {"IfNoneMatch": "*"},
        )
        key = "Benchmarks/Case-00-Triborough/derived/attorney-feedback-eval/attorney-review-packets/packet-q1/x.docx"
        for code, status in (
            ("PreconditionFailed", 412),
            ("412", None),
            ("ConditionNotMet", 409),
            ("AccessDenied", 412),
        ):
            mapped = map_archive_put_precondition_failure(
                object_key=key,
                error_code=code,
                http_status_code=status,
            )
            self.assertIsInstance(mapped, ValueError)
            self.assertIn("already exists", str(mapped))
            self.assertIn(key, str(mapped))
        self.assertIsNone(
            map_archive_put_precondition_failure(
                object_key=key,
                error_code="InternalError",
                http_status_code=500,
            )
        )


class BridgeOperationalIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _import_bridge_server()

    def test_deployed_commit_sha_prefers_explicit_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {self.server.DEPLOYED_COMMIT_SHA_ENV: "abc123def456"},
            clear=False,
        ):
            self.assertEqual(self.server.get_deployed_commit_sha(), "abc123def456")

    def test_deployed_commit_sha_unknown_without_explicit_env(self) -> None:
        env = os.environ.copy()
        env.pop(self.server.DEPLOYED_COMMIT_SHA_ENV, None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                self.server.get_deployed_commit_sha(),
                self.server.UNKNOWN_DEPLOYED_COMMIT_SHA,
            )

    def test_deployed_commit_sha_blank_env_is_unknown(self) -> None:
        with mock.patch.dict(
            os.environ,
            {self.server.DEPLOYED_COMMIT_SHA_ENV: "  "},
            clear=False,
        ):
            self.assertEqual(
                self.server.get_deployed_commit_sha(),
                self.server.UNKNOWN_DEPLOYED_COMMIT_SHA,
            )

    def test_required_tools_include_archive_review_packet(self) -> None:
        self.assertIn(
            "archive_case00_review_packet",
            self.server.REQUIRED_PRODUCTION_TOOLS,
        )

    def test_missing_required_tools_detection(self) -> None:
        registered = set(self.server.REQUIRED_PRODUCTION_TOOLS) - {
            "archive_case00_review_packet"
        }
        self.assertEqual(
            self.server.missing_required_production_tools(registered),
            ["archive_case00_review_packet"],
        )
        self.server.assert_required_production_tools(
            self.server.REQUIRED_PRODUCTION_TOOLS | {"harmless_extra_tool"}
        )
        with self.assertRaises(RuntimeError) as ctx:
            self.server.assert_required_production_tools(registered)
        self.assertIn("archive_case00_review_packet", str(ctx.exception))

    def test_registered_tools_match_fastmcp_supported_api(self) -> None:
        names = asyncio.run(self.server.list_registered_tool_names())
        self.assertEqual(names, sorted(names))
        self.assertTrue(
            self.server.REQUIRED_PRODUCTION_TOOLS.issubset(names),
            msg=f"missing={self.server.missing_required_production_tools(names)}",
        )
        asyncio.run(self.server.validate_required_production_tools())

    def test_health_reports_commit_sha_and_sorted_tools(self) -> None:
        expected_sha = "deadbeefcafebabe0123456789abcdef01234567"
        with mock.patch.dict(
            os.environ,
            {self.server.DEPLOYED_COMMIT_SHA_ENV: expected_sha},
            clear=False,
        ):
            response = asyncio.run(self.server.health(mock.Mock()))
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload["service"], "hal-github-actions-bridge")
        self.assertEqual(payload["deployed_commit_sha"], expected_sha)
        self.assertEqual(
            payload["registered_tools"],
            sorted(payload["registered_tools"]),
        )
        self.assertTrue(
            self.server.REQUIRED_PRODUCTION_TOOLS.issubset(
                payload["registered_tools"]
            )
        )
        self.assertIn("archive_case00_review_packet", payload["registered_tools"])


if __name__ == "__main__":
    unittest.main()
