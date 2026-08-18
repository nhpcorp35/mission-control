from __future__ import annotations

import asyncio
import base64
import hashlib
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
    ACCEPTANCE_CONTRACT_PREFIX,
    ACCEPTANCE_CONTRACT_SCHEMA,
    AcceptanceContractValidationError,
    CANONICAL_LEGALAI_BUCKET,
    CASE00_PREFIXES,
    MAX_REVIEW_PACKET_BYTES,
    REVIEW_PACKET_MANIFEST_FILENAME,
    archive_create_only_put_params,
    assert_archive_objects_absent,
    assert_canonical_legalai_bucket,
    build_acceptance_contract_archive,
    build_acceptance_contract_template,
    build_attorney_review_archive,
    build_review_packet_archive,
    build_synthetic_acceptance_contract,
    compute_acceptance_contract_sha256,
    compute_acceptance_object_sha256,
    canonical_acceptance_contract_object_key,
    canonical_acceptance_contract_sha256,
    decode_review_packet_docx_base64,
    inventory_prefix,
    map_archive_put_precondition_failure,
    normalize_review_packet_recipient,
    resolve_acceptance_contract_retrieval_key,
    serialize_acceptance_contract_stored_bytes,
    validate_acceptance_contract_object_key,
    validate_docx_bytes,
    verify_retrieved_acceptance_contract,
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

    def test_b2_put_params_omit_unsupported_conditional_headers(self) -> None:
        params = archive_create_only_put_params()
        self.assertEqual(params, {})
        banned = {
            "IfNoneMatch",
            "IfMatch",
            "IfModifiedSince",
            "IfUnmodifiedSince",
        }
        self.assertTrue(banned.isdisjoint(params))
        self.assertTrue(
            banned.isdisjoint({str(key) for key in params}),
        )

    def test_put_archive_object_does_not_send_if_none_match(self) -> None:
        server = _import_bridge_server()
        _, items = build_review_packet_archive(**_review_packet_kwargs())
        item = items[0]
        client = mock.Mock()
        server._put_archive_object_create_only(client, item)
        client.put_object.assert_called_once()
        kwargs = client.put_object.call_args.kwargs
        self.assertNotIn("IfNoneMatch", kwargs)
        self.assertEqual(kwargs["Key"], item["object_key"])
        self.assertEqual(kwargs["Body"], item["payload"])
        self.assertEqual(kwargs["Metadata"], {"sha256": item["sha256"]})

    def test_archive_review_packet_preflight_fails_closed_without_put(self) -> None:
        server = _import_bridge_server()
        build_kwargs = _review_packet_kwargs()
        _, items = build_review_packet_archive(**build_kwargs)
        existing_key = items[0]["object_key"]
        tool_kwargs = {
            key: value
            for key, value in build_kwargs.items()
            if key != "archived_by"
        }
        client = mock.Mock()
        client.head_object.return_value = {
            "ContentLength": 1,
            "Metadata": {"sha256": "x"},
        }
        with mock.patch.object(server, "_require_allowed_user", return_value="tester"):
            with mock.patch.object(server, "_b2_client", return_value=client):
                with self.assertRaises(ValueError) as ctx:
                    asyncio.run(
                        server.archive_case00_review_packet.fn(**tool_kwargs)
                    )
        self.assertIn("already exists", str(ctx.exception))
        self.assertIn(existing_key, str(ctx.exception))
        client.put_object.assert_not_called()

    def test_archive_review_packet_head_verifies_size_and_sha256(self) -> None:
        server = _import_bridge_server()
        build_kwargs = _review_packet_kwargs()
        _, expected_items = build_review_packet_archive(
            **{**build_kwargs, "archived_by": "tester"}
        )
        tool_kwargs = {
            key: value
            for key, value in build_kwargs.items()
            if key != "archived_by"
        }
        client = mock.Mock()
        written: dict[str, dict[str, object]] = {}

        def _put_object(**kwargs: object) -> dict[str, object]:
            key = str(kwargs["Key"])
            self.assertNotIn("IfNoneMatch", kwargs)
            body = kwargs["Body"]
            assert isinstance(body, (bytes, bytearray))
            metadata = kwargs["Metadata"]
            assert isinstance(metadata, dict)
            written[key] = {
                "payload": bytes(body),
                "sha256": metadata["sha256"],
            }
            return {}

        def _head_object(*, Bucket: str, Key: str) -> dict[str, object]:
            del Bucket
            stored = written.get(Key)
            if stored is None:
                raise server.ClientError(
                    {"Error": {"Code": "404", "Message": "Not Found"}},
                    "HeadObject",
                )
            payload = stored["payload"]
            assert isinstance(payload, bytes)
            return {
                "ContentLength": len(payload),
                "ETag": '"etag-value"',
                "Metadata": {"sha256": stored["sha256"]},
            }

        client.put_object.side_effect = _put_object
        client.head_object.side_effect = _head_object
        with mock.patch.object(server, "_require_allowed_user", return_value="tester"):
            with mock.patch.object(server, "_b2_client", return_value=client):
                result = asyncio.run(
                    server.archive_case00_review_packet.fn(**tool_kwargs)
                )
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(len(result["objects"]), 2)
        self.assertEqual(
            [obj["object_key"] for obj in result["objects"]],
            [item["object_key"] for item in expected_items],
        )
        self.assertEqual(
            [call.kwargs["Key"] for call in client.put_object.call_args_list],
            [item["object_key"] for item in expected_items],
        )
        for obj in result["objects"]:
            stored = written[obj["object_key"]]
            self.assertEqual(obj["size"], len(stored["payload"]))
            self.assertEqual(obj["sha256"], stored["sha256"])

    def test_precondition_failure_mapping_still_fail_closed(self) -> None:
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


class Case00QuestionRetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _import_bridge_server()

    def _read_question(self, question_id: str, packet: bytes):
        class FakeBody:
            def read(self, _n: int = -1) -> bytes:
                return packet

            def close(self) -> None:
                return None

        client = mock.Mock()
        client.head_object.return_value = {"ContentLength": len(packet)}
        client.get_object.return_value = {"Body": FakeBody()}
        with mock.patch.object(
            self.server, "_require_allowed_user", return_value="nhpcorp35"
        ), mock.patch.object(self.server, "_b2_client", return_value=client), mock.patch.object(
            self.server,
            "CANONICAL_CASE00_ATTORNEY_PACKET_SIZE",
            len(packet),
        ), mock.patch.object(
            self.server,
            "CANONICAL_CASE00_ATTORNEY_PACKET_SHA256",
            hashlib.sha256(packet).hexdigest(),
        ):
            return asyncio.run(self.server.get_case00_question.fn(question_id))

    def test_returns_only_requested_verified_heading(self) -> None:
        packet = b"# Packet\n\n## Q2. What relief is requested?\n\nprivate body\n\n## Q3. What occurred next?\n"
        result = self._read_question("Q3", packet)
        self.assertTrue(result["ok"])
        self.assertEqual(result["question_id"], "Q3")
        self.assertEqual(result["question_text"], "What occurred next?")
        self.assertNotIn("private body", result["question_text"])

    def test_missing_question_is_safe_and_non_mutating(self) -> None:
        result = self._read_question("Q3", b"## Q2. What relief is requested?\n")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "question_not_found")

    def test_invalid_question_id_fails_before_b2_access(self) -> None:
        with (
            mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ),
            mock.patch.object(self.server, "_b2_client") as b2,
        ):
            with self.assertRaises(ValueError):
                asyncio.run(self.server.get_case00_question.fn("../Q3"))
        b2.assert_not_called()


class Case00RefResolutionTests(unittest.TestCase):
    """Case-00 submit ref alias + SHA preflight against configured LegalAI repo."""

    LEGALAI_SHA = "49f6881c08e7e4fdf76d8500d52a27d057c0804b"
    WRONG_REPO_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _import_bridge_server()

    def setUp(self) -> None:
        self.dispatches: list[dict] = []
        self._orig_repo = self.server.REPOSITORY
        self._orig_branch = self.server.CASE00_WORKFLOW_BRANCH
        self._orig_workflow = self.server.CASE00_WORKFLOW
        self.server.REPOSITORY = "nhpcorp35/legal-ai"
        self.server.CASE00_WORKFLOW_BRANCH = "main"
        self.server.CASE00_WORKFLOW = "hal-case00-q1.yml"

    def tearDown(self) -> None:
        self.server.REPOSITORY = self._orig_repo
        self.server.CASE00_WORKFLOW_BRANCH = self._orig_branch
        self.server.CASE00_WORKFLOW = self._orig_workflow

    def _submit(self):
        tool = self.server.submit_case00_q1
        return getattr(tool, "fn", tool)

    def _tool_error_payload(self, exc: Exception) -> dict:
        from fastmcp.exceptions import ToolError

        self.assertIsInstance(exc, ToolError)
        payload = json.loads(str(exc))
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get("ok"), False)
        return payload

    def _patch_github_json(self, handler):
        return mock.patch.object(self.server, "_github_json", side_effect=handler)

    async def _fake_github_json(self, method, path, **kwargs):
        class Resp:
            def __init__(self, status_code: int):
                self.status_code = status_code

        if method == "GET" and path.endswith(f"/commits/{self.server.CASE00_WORKFLOW_BRANCH}"):
            return Resp(200), {"sha": self.LEGALAI_SHA}, None
        if method == "GET" and path.endswith(f"/commits/{self.LEGALAI_SHA}"):
            return Resp(200), {"sha": self.LEGALAI_SHA}, None
        if method == "GET" and path.endswith(f"/commits/{self.WRONG_REPO_SHA}"):
            return Resp(404), {"message": "Not Found"}, None
        if method == "POST" and path.endswith("/dispatches"):
            self.dispatches.append({"path": path, "json": kwargs.get("json")})
            return Resp(204), None, None
        return Resp(500), {"message": "unexpected"}, None

    async def _absent_sha_github_json(self, status_code: int, method, path, **kwargs):
        class Resp:
            def __init__(self, code: int):
                self.status_code = code

        if method == "GET" and path.endswith(f"/commits/{self.WRONG_REPO_SHA}"):
            message = "Not Found" if status_code == 404 else "Validation Failed"
            return Resp(status_code), {"message": message}, None
        if method == "POST" and path.endswith("/dispatches"):
            self.dispatches.append({"path": path, "json": kwargs.get("json")})
            return Resp(204), None, None
        return Resp(500), {"message": "unexpected"}, None

    def test_main_resolves_from_configured_legalai_repo_and_dispatches_sha(self) -> None:
        submit = self._submit()

        async def run():
            with mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ), self._patch_github_json(self._fake_github_json):
                return await submit(
                    ref="main",
                    authorization_confirmed=True,
                    mission_id="mission-main-alias",
                )

        result = asyncio.run(run())
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["requested_ref"], "main")
        self.assertEqual(result["resolved_ref"], self.LEGALAI_SHA)
        self.assertEqual(result["repository"], "nhpcorp35/legal-ai")
        self.assertEqual(result["workflow"], "hal-case00-q1.yml")
        self.assertEqual(len(self.dispatches), 1)
        payload = self.dispatches[0]["json"]
        self.assertEqual(payload["ref"], "main")
        self.assertEqual(payload["inputs"]["legalai_ref"], self.LEGALAI_SHA)
        self.assertNotIn("GITHUB_TOKEN", json.dumps(result))
        self.assertNotIn("bearer", json.dumps(result).lower())

    def test_explicit_valid_sha_succeeds(self) -> None:
        submit = self._submit()

        async def run():
            with mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ), self._patch_github_json(self._fake_github_json):
                return await submit(
                    ref=self.LEGALAI_SHA,
                    authorization_confirmed=True,
                    mission_id="mission-explicit-sha",
                )

        result = asyncio.run(run())
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["requested_ref"], self.LEGALAI_SHA)
        self.assertEqual(result["resolved_ref"], self.LEGALAI_SHA)
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(
            self.dispatches[0]["json"]["inputs"]["legalai_ref"], self.LEGALAI_SHA
        )

    def test_wrong_repository_sha_fails_before_dispatch(self) -> None:
        """Absent explicit SHA: GitHub 404 → ref_not_in_repository, no dispatch."""
        self._assert_absent_sha_classified(404)

    def test_absent_sha_http_422_fails_before_dispatch(self) -> None:
        """Absent explicit SHA: GitHub 422 → ref_not_in_repository, no dispatch."""
        self._assert_absent_sha_classified(422)

    def _assert_absent_sha_classified(self, status_code: int) -> None:
        submit = self._submit()

        async def handler(method, path, **kwargs):
            return await self._absent_sha_github_json(
                status_code, method, path, **kwargs
            )

        async def run():
            with mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ), self._patch_github_json(handler):
                return await submit(
                    ref=self.WRONG_REPO_SHA,
                    authorization_confirmed=True,
                    mission_id=f"mission-absent-sha-{status_code}",
                )

        with self.assertRaises(Exception) as ctx:
            asyncio.run(run())
        result = self._tool_error_payload(ctx.exception)
        self.assertEqual(result["error_code"], self.server.ERROR_REF_NOT_IN_REPOSITORY)
        self.assertIn("nhpcorp35/legal-ai", result["message"])
        self.assertIn(self.WRONG_REPO_SHA, result["message"])
        self.assertEqual(self.dispatches, [])

    def test_arbitrary_branch_tag_and_malformed_refs_fail_before_dispatch(self) -> None:
        from fastmcp.exceptions import ToolError

        submit = self._submit()
        bad_refs = [
            "develop",
            "refs/tags/v1.0.0",
            "v1.0.0",
            "49f6881",  # abbreviated
            self.LEGALAI_SHA.upper(),  # uppercase
            "MAIN",
            "not a ref",
            "",
            "g" * 40,  # non-hex
        ]

        async def run(ref: str):
            with mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ), self._patch_github_json(self._fake_github_json):
                return await submit(
                    ref=ref,
                    authorization_confirmed=True,
                    mission_id="mission-bad-ref",
                )

        for ref in bad_refs:
            with self.assertRaises(ToolError) as ctx:
                asyncio.run(run(ref))
            result = self._tool_error_payload(ctx.exception)
            self.assertEqual(
                result["error_code"],
                self.server.ERROR_REF_INVALID,
                msg=f"ref={ref!r}",
            )
        self.assertEqual(self.dispatches, [])

    def test_github_api_resolution_failure_is_structured(self) -> None:
        submit = self._submit()

        async def boom(method, path, **kwargs):
            return None, None, "ConnectError"

        async def run():
            with mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ), self._patch_github_json(boom):
                return await submit(
                    ref="main",
                    authorization_confirmed=True,
                    mission_id="mission-resolve-fail",
                )

        with self.assertRaises(Exception) as ctx:
            asyncio.run(run())
        result = self._tool_error_payload(ctx.exception)
        self.assertEqual(result["error_code"], self.server.ERROR_REF_RESOLUTION_FAILED)
        self.assertEqual(self.dispatches, [])
        blob = json.dumps(result).lower()
        self.assertNotIn("bearer", blob)
        self.assertNotIn("github_token", blob)

    def test_dispatch_failure_is_structured_and_redacted(self) -> None:
        submit = self._submit()

        async def handler(method, path, **kwargs):
            class Resp:
                def __init__(self, status_code: int):
                    self.status_code = status_code

            if method == "GET":
                return Resp(200), {"sha": self.LEGALAI_SHA}, None
            return Resp(403), {"message": "Resource not accessible by integration"}, None

        async def run():
            with mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ), self._patch_github_json(handler):
                return await submit(
                    ref=self.LEGALAI_SHA,
                    authorization_confirmed=True,
                    mission_id="mission-dispatch-fail",
                )

        with self.assertRaises(Exception) as ctx:
            asyncio.run(run())
        result = self._tool_error_payload(ctx.exception)
        self.assertEqual(result["error_code"], self.server.ERROR_DISPATCH_FAILED)
        self.assertIn("HTTP 403", result["message"])
        self.assertNotIn("token_scopes", result["message"])
        self.assertNotIn("Bearer", json.dumps(result))

    def test_authorization_confirmed_still_required(self) -> None:
        submit = self._submit()

        async def run():
            with mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ), self._patch_github_json(self._fake_github_json):
                return await submit(
                    ref="main",
                    authorization_confirmed=False,
                )

        with self.assertRaises(ValueError) as ctx:
            asyncio.run(run())
        self.assertIn("authorization_confirmed", str(ctx.exception))
        self.assertEqual(self.dispatches, [])


class Case00GenericWorkflowTests(unittest.TestCase):
    """Question-agnostic Case-00 submit/status/artifact/cancel validation + routing."""

    LEGALAI_SHA = "49f6881c08e7e4fdf76d8500d52a27d057c0804b"
    BENCHMARK_ID = "Case-00-Triborough"
    QUESTION_ID = "Q1"

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _import_bridge_server()

    def setUp(self) -> None:
        self.dispatches: list[dict] = []
        self._orig_repo = self.server.REPOSITORY
        self._orig_branch = self.server.CASE00_WORKFLOW_BRANCH
        self._orig_workflow = self.server.CASE00_WORKFLOW
        self.server.REPOSITORY = "nhpcorp35/legal-ai"
        self.server.CASE00_WORKFLOW_BRANCH = "main"
        self.server.CASE00_WORKFLOW = "hal-case00-q1.yml"

    def tearDown(self) -> None:
        self.server.REPOSITORY = self._orig_repo
        self.server.CASE00_WORKFLOW_BRANCH = self._orig_branch
        self.server.CASE00_WORKFLOW = self._orig_workflow

    def _tool(self, name: str):
        tool = getattr(self.server, name)
        return getattr(tool, "fn", tool)

    def _tool_error_payload(self, exc: Exception) -> dict:
        from fastmcp.exceptions import ToolError

        self.assertIsInstance(exc, ToolError)
        payload = json.loads(str(exc))
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get("ok"), False)
        return payload

    def _patch_github_json(self, handler):
        return mock.patch.object(self.server, "_github_json", side_effect=handler)

    async def _fake_github_json(self, method, path, **kwargs):
        class Resp:
            def __init__(self, status_code: int):
                self.status_code = status_code

        if method == "GET" and path.endswith(f"/commits/{self.LEGALAI_SHA}"):
            return Resp(200), {"sha": self.LEGALAI_SHA}, None
        if method == "GET" and path.endswith(f"/commits/{self.server.CASE00_WORKFLOW_BRANCH}"):
            return Resp(200), {"sha": self.LEGALAI_SHA}, None
        if method == "POST" and path.endswith("/dispatches"):
            self.dispatches.append({"path": path, "json": kwargs.get("json")})
            return Resp(204), None, None
        return Resp(500), {"message": "unexpected"}, None

    def test_valid_generic_submission_schema_and_routing(self) -> None:
        submit = self._tool("submit_case00")

        async def run():
            with mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ), self._patch_github_json(self._fake_github_json):
                return await submit(
                    commit_sha=self.LEGALAI_SHA,
                    benchmark_id=self.BENCHMARK_ID,
                    question_id=self.QUESTION_ID,
                    authorization_confirmed=True,
                    mission_id="mission-generic-valid",
                )

        result = asyncio.run(run())
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["mission_id"], "mission-generic-valid")
        self.assertEqual(result["benchmark_id"], self.BENCHMARK_ID)
        self.assertEqual(result["question_id"], self.QUESTION_ID)
        self.assertEqual(result["requested_ref"], self.LEGALAI_SHA)
        self.assertEqual(result["resolved_ref"], self.LEGALAI_SHA)
        self.assertEqual(result["workflow"], "hal-case00-q1.yml")
        self.assertEqual(len(self.dispatches), 1)
        payload = self.dispatches[0]["json"]
        self.assertEqual(payload["ref"], "main")
        self.assertEqual(payload["inputs"]["legalai_ref"], self.LEGALAI_SHA)
        self.assertEqual(payload["inputs"]["mission_id"], "mission-generic-valid")
        self.assertEqual(payload["inputs"]["authorization_confirmed"], "true")
        self.assertEqual(payload["inputs"]["benchmark_id"], self.BENCHMARK_ID)
        self.assertEqual(payload["inputs"]["question_id"], self.QUESTION_ID)
        self.assertTrue(
            self.dispatches[0]["path"].endswith("/actions/workflows/hal-case00-q1.yml/dispatches")
        )
        self.assertEqual(
            self.server.case00_run_marker(self.QUESTION_ID, "mission-generic-valid"),
            "hal-case00-q1-mission-generic-valid",
        )
        self.assertEqual(
            self.server.case00_result_filename(self.QUESTION_ID),
            "case00-q1-result.json",
        )

    def test_q2_accepted_and_forwarded_unchanged(self) -> None:
        submit = self._tool("submit_case00")

        async def run():
            with mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ), self._patch_github_json(self._fake_github_json):
                return await submit(
                    commit_sha=self.LEGALAI_SHA,
                    benchmark_id=self.BENCHMARK_ID,
                    question_id="Q2",
                    authorization_confirmed=True,
                    mission_id="mission-generic-q2",
                )

        result = asyncio.run(run())
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["benchmark_id"], self.BENCHMARK_ID)
        self.assertEqual(result["question_id"], "Q2")
        self.assertEqual(len(self.dispatches), 1)
        inputs = self.dispatches[0]["json"]["inputs"]
        self.assertEqual(inputs["benchmark_id"], self.BENCHMARK_ID)
        self.assertEqual(inputs["question_id"], "Q2")
        self.assertEqual(inputs["legalai_ref"], self.LEGALAI_SHA)
        self.assertEqual(
            self.server.case00_run_marker("Q2", "mission-generic-q2"),
            "hal-case00-q2-mission-generic-q2",
        )
        self.assertEqual(
            self.server.case00_result_filename("Q2"),
            "case00-q2-result.json",
        )
        self.assertEqual(
            self.server.parse_case00_question_token(
                "hal-case00-q2-mission-generic-q2", "mission-generic-q2"
            ),
            "q2",
        )

    def test_run_marker_correlation_normalizes_question_casing(self) -> None:
        """Uppercase workflow titles correlate; artifact names stay lowercase."""
        mission_q1 = "mission-corr-q1"
        mission_q2 = "mission-corr-q2"
        # Markers / artifact filenames remain lowercase at runtime.
        self.assertEqual(
            self.server.case00_run_marker("Q1", mission_q1),
            f"hal-case00-q1-{mission_q1}",
        )
        self.assertEqual(
            self.server.case00_run_marker("Q2", mission_q2),
            f"hal-case00-q2-{mission_q2}",
        )
        self.assertEqual(
            self.server.case00_result_filename("Q2"),
            "case00-q2-result.json",
        )

        # Workflow display titles keep submitted Q casing; parser normalizes.
        self.assertEqual(
            self.server.parse_case00_question_token(
                f"hal-case00-Q1-{mission_q1}", mission_q1
            ),
            "q1",
        )
        self.assertEqual(
            self.server.parse_case00_question_token(
                f"hal-case00-Q2-{mission_q2}", mission_q2
            ),
            "q2",
        )
        self.assertEqual(
            self.server.parse_case00_question_token(
                f"hal-case00-q1-{mission_q1}", mission_q1
            ),
            "q1",
        )
        self.assertEqual(
            self.server.parse_case00_question_token(
                f"hal-case00-q2-{mission_q2}", mission_q2
            ),
            "q2",
        )
        # Lowercase artifact-style names still parse.
        self.assertEqual(
            self.server.parse_case00_question_token(
                f"hal-case00-q2-{mission_q2}-artifacts", mission_q2
            ),
            "q2",
        )

        # Wrong mission IDs and malformed question tokens fail closed.
        self.assertIsNone(
            self.server.parse_case00_question_token(
                f"hal-case00-Q2-{mission_q2}", "mission-other"
            )
        )
        self.assertIsNone(
            self.server.parse_case00_question_token(
                f"hal-case00-Q2-{mission_q1}", mission_q2
            )
        )
        for bad in (
            f"hal-case00-Q0-{mission_q2}",
            f"hal-case00-Q02-{mission_q2}",
            f"hal-case00-Q-{mission_q2}",
            f"hal-case00-2-{mission_q2}",
            f"hal-case00-qx-{mission_q2}",
            f"prefix-hal-case00-Q2x-{mission_q2}",
        ):
            self.assertIsNone(
                self.server.parse_case00_question_token(bad, mission_q2),
                msg=bad,
            )

        q2_run = {
            "id": 42,
            "status": "in_progress",
            "conclusion": None,
            "display_title": f"hal-case00-Q2-{mission_q2}",
            "head_sha": self.LEGALAI_SHA,
            "html_url": "https://github.com/example/actions/runs/42",
        }
        q1_run = {
            "id": 41,
            "status": "completed",
            "conclusion": "success",
            "display_title": f"hal-case00-Q1-{mission_q1}",
            "head_sha": self.LEGALAI_SHA,
            "html_url": "https://github.com/example/actions/runs/41",
        }

        async def fake_github(method, path, **kwargs):
            self.assertEqual(method, "GET")
            self.assertIn("/actions/workflows/", path)
            response = mock.Mock()
            response.json.return_value = {"workflow_runs": [q2_run, q1_run]}
            return response

        async def resolve_uppercase_q2():
            with mock.patch.object(self.server, "_github", side_effect=fake_github):
                return await self.server._resolve_case00_run(mission_q2, "Q2")

        async def resolve_q2_without_question():
            with mock.patch.object(self.server, "_github", side_effect=fake_github):
                return await self.server._resolve_case00_run(mission_q2)

        async def resolve_q1():
            with mock.patch.object(self.server, "_github", side_effect=fake_github):
                return await self.server._resolve_case00_run(mission_q1, "Q1")

        async def resolve_wrong_mission():
            with mock.patch.object(self.server, "_github", side_effect=fake_github):
                return await self.server._resolve_case00_run("mission-other", "Q2")

        self.assertEqual(asyncio.run(resolve_uppercase_q2())["id"], 42)
        self.assertEqual(asyncio.run(resolve_q2_without_question())["id"], 42)
        self.assertEqual(asyncio.run(resolve_q1())["id"], 41)
        self.assertIsNone(asyncio.run(resolve_wrong_mission()))

    def test_mutable_ref_rejected_before_dispatch(self) -> None:
        from fastmcp.exceptions import ToolError

        submit = self._tool("submit_case00")
        bad_refs = ["main", "develop", "HEAD", "49f6881", self.LEGALAI_SHA.upper()]

        async def run(commit_sha: str):
            with mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ), self._patch_github_json(self._fake_github_json):
                return await submit(
                    commit_sha=commit_sha,
                    benchmark_id=self.BENCHMARK_ID,
                    question_id=self.QUESTION_ID,
                    authorization_confirmed=True,
                    mission_id="mission-mutable-ref",
                )

        for commit_sha in bad_refs:
            with self.assertRaises(ToolError) as ctx:
                asyncio.run(run(commit_sha))
            result = self._tool_error_payload(ctx.exception)
            self.assertEqual(
                result["error_code"],
                self.server.ERROR_REF_INVALID,
                msg=f"commit_sha={commit_sha!r}",
            )
        self.assertEqual(self.dispatches, [])

    def test_authorization_rejection(self) -> None:
        submit = self._tool("submit_case00")

        async def run():
            with mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ), self._patch_github_json(self._fake_github_json):
                return await submit(
                    commit_sha=self.LEGALAI_SHA,
                    benchmark_id=self.BENCHMARK_ID,
                    question_id=self.QUESTION_ID,
                    authorization_confirmed=False,
                )

        with self.assertRaises(ValueError) as ctx:
            asyncio.run(run())
        self.assertIn("authorization_confirmed", str(ctx.exception))
        self.assertEqual(self.dispatches, [])

    def test_q1_specific_tools_remain_backward_compatible(self) -> None:
        submit = self._tool("submit_case00_q1")

        async def run():
            with mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ), self._patch_github_json(self._fake_github_json):
                return await submit(
                    ref="main",
                    authorization_confirmed=True,
                    mission_id="mission-q1-compat",
                )

        result = asyncio.run(run())
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["requested_ref"], "main")
        self.assertEqual(result["resolved_ref"], self.LEGALAI_SHA)
        self.assertNotIn("benchmark_id", result)
        self.assertEqual(len(self.dispatches), 1)
        inputs = self.dispatches[0]["json"]["inputs"]
        self.assertEqual(inputs["mission_id"], "mission-q1-compat")
        self.assertEqual(inputs["legalai_ref"], self.LEGALAI_SHA)
        self.assertEqual(inputs["authorization_confirmed"], "true")
        # Legacy Q1 path omits identity inputs so workflow defaults stay intact.
        self.assertNotIn("benchmark_id", inputs)
        self.assertNotIn("question_id", inputs)

    def test_malformed_question_and_wrong_benchmark_fail_closed(self) -> None:
        from fastmcp.exceptions import ToolError

        submit = self._tool("submit_case00")
        rejected = [
            (self.BENCHMARK_ID, "Q0"),
            (self.BENCHMARK_ID, "q2"),
            (self.BENCHMARK_ID, "Q01"),
            (self.BENCHMARK_ID, "Q"),
            (self.BENCHMARK_ID, "1"),
            (self.BENCHMARK_ID, "Q-2"),
            (self.BENCHMARK_ID, ""),
            ("Case-00-Other", "Q1"),
            ("Case-00-Other", "Q2"),
            ("", "Q1"),
        ]

        async def run(benchmark_id: str, question_id: str):
            with mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ), self._patch_github_json(self._fake_github_json):
                return await submit(
                    commit_sha=self.LEGALAI_SHA,
                    benchmark_id=benchmark_id,
                    question_id=question_id,
                    authorization_confirmed=True,
                    mission_id="mission-unsupported",
                )

        for benchmark_id, question_id in rejected:
            with self.assertRaises(ToolError) as ctx:
                asyncio.run(run(benchmark_id, question_id))
            result = self._tool_error_payload(ctx.exception)
            self.assertEqual(
                result["error_code"],
                self.server.ERROR_UNSUPPORTED_BENCHMARK_QUESTION,
                msg=f"{benchmark_id!r}/{question_id!r}",
            )
            self.assertIn("Case-00-Triborough", result["message"])
        self.assertEqual(self.dispatches, [])

        # Direct validator accepts Q1/Q2 and rejects malformed ids.
        self.assertEqual(
            self.server.validate_case00_benchmark_question(
                self.BENCHMARK_ID, "Q1"
            ),
            (self.BENCHMARK_ID, "Q1"),
        )
        self.assertEqual(
            self.server.validate_case00_benchmark_question(
                self.BENCHMARK_ID, "Q2"
            ),
            (self.BENCHMARK_ID, "Q2"),
        )
        with self.assertRaises(ToolError):
            self.server.validate_case00_benchmark_question(
                self.BENCHMARK_ID, "Q0"
            )
    def test_status_artifact_cancel_routing(self) -> None:
        status = self._tool("get_case00_run")
        cancel = self._tool("cancel_case00_run")
        artifacts = self._tool("get_case00_artifacts")
        run_payload = {
            "id": 31404004716,
            "status": "completed",
            "conclusion": "success",
            "head_sha": self.LEGALAI_SHA,
            "html_url": "https://github.com/example/actions/runs/1",
        }

        async def run_status():
            with mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ), mock.patch.object(
                self.server, "_resolve_case00_run", return_value=run_payload
            ):
                return await status(mission_id="mission-status-1")

        async def run_cancel():
            with mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ), mock.patch.object(
                self.server, "_resolve_case00_run", return_value=run_payload
            ), mock.patch.object(
                self.server, "_github", new_callable=mock.AsyncMock
            ) as github:
                github.return_value = mock.Mock(status_code=202)
                result = await cancel(mission_id="mission-cancel-1")
                github.assert_awaited()
                return result

        async def run_artifacts():
            with mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ), mock.patch.object(
                self.server,
                "_verify_case00_artifacts",
                new_callable=mock.AsyncMock,
                return_value={
                    "ok": True,
                    "mission_id": "mission-artifacts-1",
                    "verified": True,
                    "objects": [],
                },
            ) as verify:
                result = await artifacts(mission_id="mission-artifacts-1")
                verify.assert_awaited_once_with("mission-artifacts-1")
                return result

        status_result = asyncio.run(run_status())
        self.assertEqual(status_result["ok"], True)
        self.assertEqual(status_result["mission_id"], "mission-status-1")
        self.assertEqual(status_result["run_id"], run_payload["id"])
        self.assertEqual(status_result["status"], "completed")

        cancel_result = asyncio.run(run_cancel())
        self.assertEqual(cancel_result["ok"], True)
        self.assertEqual(cancel_result["mission_id"], "mission-cancel-1")
        self.assertEqual(cancel_result["status"], "cancellation_requested")

        artifacts_result = asyncio.run(run_artifacts())
        self.assertEqual(artifacts_result["ok"], True)
        self.assertEqual(artifacts_result["mission_id"], "mission-artifacts-1")

    def test_unified_gateway_registry_routes_generic_case_tools(self) -> None:
        registry_path = (
            Path(__file__).resolve().parent.parent
            / "hal_legalai_gateway"
            / "registry.json"
        )
        document = json.loads(registry_path.read_text(encoding="utf-8"))
        expected = {
            "case.submit": "submit_case00",
            "case.status": "get_case00_run",
            "case.cancel": "cancel_case00_run",
            "case.list_artifacts": "get_case00_artifacts",
            "case.submit_case00_q1": "submit_case00_q1",
        }
        by_tool = {
            item["tool"]: item["downstream_tool"]
            for item in document["tool_bindings"]
            if item["namespace"] == "case"
        }
        for gateway_tool, downstream in expected.items():
            self.assertEqual(by_tool[gateway_tool], downstream)
            self.assertIn(gateway_tool, document["namespaces"]["case"]["tools"])
        # Q1-specific tools remain registered alongside the generic surface.
        self.assertIn("case.get_case00_q1_run", by_tool)
        self.assertIn("case.cancel_case00_q1_run", by_tool)
        self.assertIn("case.get_case00_q1_artifacts", by_tool)

    def test_allowed_case_artifact_filenames_are_question_scoped(self) -> None:
        self.assertEqual(
            self.server.allowed_case_artifact_filenames("q1"),
            frozenset(
                {
                    "Q1_candidate_answer.json",
                    "Q1_candidate_answer.md",
                    "generation_manifest.json",
                    "model_input_audit.json",
                    "case00_attorney_review_packet.md",
                }
            ),
        )
        self.assertEqual(
            self.server.allowed_case_artifact_filenames("q2"),
            frozenset(
                {
                    "Q2_candidate_answer.json",
                    "Q2_candidate_answer.md",
                    "generation_manifest.json",
                    "model_input_audit.json",
                    "case00_attorney_review_packet.md",
                }
            ),
        )
        self.assertNotIn(
            "Q1_candidate_answer.json",
            self.server.allowed_case_artifact_filenames("q2"),
        )
        with self.assertRaises(ValueError):
            self.server.assert_safe_case_artifact_basename("../Q2_candidate_answer.json")
        with self.assertRaises(ValueError):
            self.server.assert_safe_case_artifact_basename("q2/Q2_candidate_answer.json")
        with self.assertRaises(ValueError):
            self.server.assert_safe_case_artifact_basename("")

    def test_case00_durable_objects_require_exact_allowlisted_set(self) -> None:
        filenames = sorted(self.server.allowed_case_artifact_filenames("q1"))
        objects = [{"filename": filename} for filename in filenames]

        self.assertTrue(
            self.server.case00_durable_objects_complete(objects, "q1")
        )
        self.assertFalse(
            self.server.case00_durable_objects_complete(objects[:-1], "q1")
        )
        self.assertFalse(
            self.server.case00_durable_objects_complete(
                objects[:-1] + [{"filename": filenames[0]}], "q1"
            )
        )

    def _synthetic_case_artifact_bundle(self, question_id: str, mission_id: str):
        """Synthetic Bridge zip + B2 bodies — no private benchmark content."""
        token = question_id.lower()
        prefix = (
            "Benchmarks/Case-00-Triborough/derived/"
            "attorney-feedback-eval/candidate-answers/"
            f"synth-{token}-candidate/"
        )
        files = [
            (f"{question_id}_candidate_answer.json", b'{"synthetic":true,"ok":true}'),
            (f"{question_id}_candidate_answer.md", b"# synthetic candidate\n"),
            ("generation_manifest.json", b'{"synthetic_manifest":true}'),
            ("model_input_audit.json", b'{"synthetic_audit":true}'),
            (
                "case00_attorney_review_packet.md",
                b"# Synthetic attorney review packet\n",
            ),
        ]
        objects = []
        bodies: dict[str, bytes] = {}
        for name, body in files:
            key = f"{prefix}{name}"
            objects.append(
                {
                    "filename": name,
                    "object_key": key,
                    "size": len(body),
                    "etag": f"etag-{name}",
                }
            )
            bodies[key] = body
        result_payload = {
            "ok": True,
            "durable_artifacts": {
                "bucket": self.server.B2_BUCKET,
                "objects": objects,
            },
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr(
                f"case00-{token}-result.json",
                json.dumps(result_payload),
            )
        artifact_name = f"hal-case00-{token}-{mission_id}"
        return {
            "token": token,
            "objects": objects,
            "bodies": bodies,
            "zip_bytes": buffer.getvalue(),
            "artifact_name": artifact_name,
            "run": {
                "id": 31415926535,
                "status": "completed",
                "conclusion": "success",
                "head_sha": self.LEGALAI_SHA,
                "html_url": "https://github.com/example/actions/runs/31415926535",
            },
        }

    def _run_get_case_artifact(
        self,
        *,
        mission_id: str,
        filename: str,
        question_id: str,
        resolve_proof_run=None,
    ):
        get_artifact = self._tool("get_case_artifact")
        bundle = self._synthetic_case_artifact_bundle(question_id, mission_id)
        bodies = bundle["bodies"]

        class FakeBody:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self, _n: int = -1) -> bytes:
                return self._data

            def close(self) -> None:
                return None

        class FakeB2:
            def head_object(self, Bucket, Key):  # noqa: N803
                body = bodies[Key]
                name = Key.rsplit("/", 1)[-1]
                return {"ContentLength": len(body), "ETag": f'"etag-{name}"'}

            def get_object(self, Bucket, Key):  # noqa: N803
                return {"Body": FakeBody(bodies[Key])}

        async def fake_github(method, path, **kwargs):
            if method == "GET" and path.endswith(
                f"/actions/runs/{bundle['run']['id']}/artifacts"
            ):
                return mock.Mock(
                    json=lambda: {
                        "artifacts": [
                            {"id": 99, "name": bundle["artifact_name"]},
                        ]
                    }
                )
            if method == "GET" and path.endswith("/actions/artifacts/99/zip"):
                return mock.Mock(content=bundle["zip_bytes"])
            raise AssertionError(f"unexpected github call {method} {path}")

        async def run():
            with mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ), mock.patch.object(
                self.server,
                "_resolve_case00_run",
                new_callable=mock.AsyncMock,
                return_value=bundle["run"],
            ), mock.patch.object(
                self.server,
                "_github",
                new_callable=mock.AsyncMock,
                side_effect=fake_github,
            ), mock.patch.object(
                self.server, "_b2_client", return_value=FakeB2()
            ), mock.patch.object(
                self.server,
                "_resolve_run",
                new_callable=mock.AsyncMock,
                return_value=resolve_proof_run,
            ):
                return await get_artifact(mission_id=mission_id, filename=filename)

        return asyncio.run(run()), bundle

    def test_get_case_artifact_q1_compatibility(self) -> None:
        result, bundle = self._run_get_case_artifact(
            mission_id="mission-artifact-q1",
            filename="Q1_candidate_answer.json",
            question_id="Q1",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["filename"], "Q1_candidate_answer.json")
        self.assertEqual(result["question_id"], "Q1")
        self.assertEqual(result["run_id"], bundle["run"]["id"])
        self.assertEqual(result["content"], {"synthetic": True, "ok": True})
        shared, _ = self._run_get_case_artifact(
            mission_id="mission-artifact-q1-shared",
            filename="generation_manifest.json",
            question_id="Q1",
        )
        self.assertTrue(shared["ok"])
        self.assertEqual(shared["filename"], "generation_manifest.json")
        packet, _ = self._run_get_case_artifact(
            mission_id="mission-artifact-q1-packet",
            filename="case00_attorney_review_packet.md",
            question_id="Q1",
        )
        self.assertTrue(packet["ok"])
        self.assertEqual(packet["content_type"], "text/markdown")
        self.assertIn("Synthetic attorney review packet", packet["content"])

    def test_get_case_artifact_q2_retrieval(self) -> None:
        result, _bundle = self._run_get_case_artifact(
            mission_id="mission-artifact-q2",
            filename="Q2_candidate_answer.md",
            question_id="Q2",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["filename"], "Q2_candidate_answer.md")
        self.assertEqual(result["question_id"], "Q2")
        self.assertEqual(result["content_type"], "text/markdown")
        self.assertIn("synthetic candidate", result["content"])
        self.assertTrue(result["object_key"].endswith("/Q2_candidate_answer.md"))
        audit, _ = self._run_get_case_artifact(
            mission_id="mission-artifact-q2-audit",
            filename="model_input_audit.json",
            question_id="Q2",
        )
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["content"], {"synthetic_audit": True})

    def test_get_case_artifact_rejects_cross_question_and_traversal(self) -> None:
        with self.assertRaises(ValueError) as cross:
            self._run_get_case_artifact(
                mission_id="mission-artifact-q2-cross",
                filename="Q1_candidate_answer.json",
                question_id="Q2",
            )
        self.assertIn("allowlisted", str(cross.exception))

        with self.assertRaises(ValueError) as traversal:
            self._run_get_case_artifact(
                mission_id="mission-artifact-q2-trav",
                filename="../Q2_candidate_answer.json",
                question_id="Q2",
            )
        self.assertIn("basename", str(traversal.exception))

        with self.assertRaises(ValueError) as arbitrary:
            self._run_get_case_artifact(
                mission_id="mission-artifact-q2-arb",
                filename="secrets.env",
                question_id="Q2",
            )
        self.assertIn("allowlisted", str(arbitrary.exception))

    def test_get_case_artifact_uses_bridge_identity_not_proof_registry(self) -> None:
        """Case-00 Bridge missions work even when generic get_artifacts has no run."""
        mission_id = "case00-q2-sharedvalidator-synth-01"
        result, bundle = self._run_get_case_artifact(
            mission_id=mission_id,
            filename="Q2_candidate_answer.json",
            question_id="Q2",
            resolve_proof_run=None,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["mission_id"], mission_id)
        self.assertEqual(result["run_id"], bundle["run"]["id"])

        get_proof = self._tool("get_artifacts")

        async def run_proof():
            with mock.patch.object(
                self.server, "_require_allowed_user", return_value="nhpcorp35"
            ), mock.patch.object(
                self.server,
                "_resolve_run",
                new_callable=mock.AsyncMock,
                return_value=None,
            ):
                return await get_proof(mission_id=mission_id)

        proof = asyncio.run(run_proof())
        self.assertEqual(
            proof,
            {"ok": False, "mission_id": mission_id, "error": "run_not_found"},
        )


def _synthetic_acceptance_contract(**overrides: object) -> dict[str, object]:
    """Cross-interface fixture matching LegalAI ``build_synthetic_contract``."""
    object_key = (
        f"{ACCEPTANCE_CONTRACT_PREFIX}"
        "synth-benchmark-alpha/Q-SYNTH-01/contract-synth-alpha-q01/"
        "v1.0.0/acceptance_contract.json"
    )
    document = build_synthetic_acceptance_contract(
        contract_id="contract-synth-alpha-q01",
        version="1.0.0",
        benchmark_id="synth-benchmark-alpha",
        question_id="Q-SYNTH-01",
        object_key=object_key,
        required_criterion_ids=["crit-presence", "crit-negation", "crit-roles"],
    )
    if overrides:
        document = dict(document)
        document.update(overrides)
        if "content_sha256" not in overrides and isinstance(document, dict):
            # Keep digest coherent unless the test intentionally tampers with it.
            try:
                document["content_sha256"] = compute_acceptance_contract_sha256(document)
            except Exception:
                pass
    return document


def _acceptance_archive_kwargs(
    document: dict[str, object] | None = None, **overrides: object
) -> dict[str, object]:
    payload_doc = document if document is not None else _synthetic_acceptance_contract()
    payload = json.dumps(payload_doc, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    identity = payload_doc.get("identity")
    if isinstance(identity, dict):
        benchmark_id = str(identity.get("benchmark_id") or "synth-benchmark-alpha")
        question_id = str(identity.get("question_id") or "Q-SYNTH-01")
        object_key = str(
            payload_doc.get("object_key")
            or (
                f"{ACCEPTANCE_CONTRACT_PREFIX}{benchmark_id}/{question_id}/"
                f"{payload_doc.get('contract_id')}/v{payload_doc.get('version')}/"
                "acceptance_contract.json"
            )
        )
    else:
        benchmark_id = str(payload_doc.get("benchmark_id") or "synth-benchmark")
        question_id = str(payload_doc.get("question_id") or "Q-synth")
        object_key = (
            f"{ACCEPTANCE_CONTRACT_PREFIX}{benchmark_id}/{question_id}/"
            f"{payload_doc.get('contract_id')}/v{payload_doc.get('version')}/"
            "acceptance_contract.json"
        )
    try:
        contract_digest = compute_acceptance_contract_sha256(payload_doc)
    except Exception:
        contract_digest = "0" * 64
    object_digest = compute_acceptance_object_sha256(payload)
    values: dict[str, object] = {
        "contract_json_base64": base64.b64encode(payload).decode("ascii"),
        "expected_object_key": object_key,
        "expected_benchmark_id": benchmark_id,
        "expected_question_id": question_id,
        "expected_contract_id": payload_doc["contract_id"],
        "expected_version": payload_doc["version"],
        "expected_contract_sha256": contract_digest,
        "expected_sha256": contract_digest,
    }
    values.update(overrides)
    values["_object_sha256"] = object_digest
    values["_contract_sha256"] = contract_digest
    values["_size"] = len(payload)
    return values


class AcceptanceContractStoragePolicyTests(unittest.TestCase):
    def test_prefix_and_bucket_are_canonical(self) -> None:
        self.assertEqual(
            ACCEPTANCE_CONTRACT_PREFIX, "Benchmarks/acceptance-contracts/"
        )
        self.assertEqual(
            assert_canonical_legalai_bucket(CANONICAL_LEGALAI_BUCKET),
            "legalai-corpus",
        )
        with self.assertRaises(ValueError):
            assert_canonical_legalai_bucket("other-bucket")

    def test_object_key_rejects_traversal_and_wrong_prefix(self) -> None:
        validate_acceptance_contract_object_key(
            f"{ACCEPTANCE_CONTRACT_PREFIX}synth/Q1/c1/v1/acceptance_contract.json"
        )
        with self.assertRaises(ValueError):
            validate_acceptance_contract_object_key(
                "Benchmarks/other/acceptance_contract.json"
            )
        with self.assertRaises(ValueError):
            validate_acceptance_contract_object_key(
                f"{ACCEPTANCE_CONTRACT_PREFIX}../escape.json"
            )
        with self.assertRaises(ValueError):
            validate_acceptance_contract_object_key(
                f"/{ACCEPTANCE_CONTRACT_PREFIX}x.json"
            )

    def test_cross_interface_synthetic_matches_legalai_shape_and_hash(self) -> None:
        doc = _synthetic_acceptance_contract()
        self.assertEqual(doc["schema_version"], ACCEPTANCE_CONTRACT_SCHEMA)
        self.assertIn("identity", doc)
        self.assertNotIn("schema", doc)
        self.assertNotIn("benchmark_id", doc)
        self.assertEqual(
            doc["content_sha256"],
            "c43d54b3923c519fbca52d6127dc5b76fb3cbd9bc12a40b018aa461e284d4b4f",
        )
        # Canonical contract hash is stable across whitespace / key ordering.
        variants = [
            json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8"),
            json.dumps(doc, sort_keys=False, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            ),
            json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            ),
        ]
        digests = {
            compute_acceptance_contract_sha256(json.loads(raw.decode("utf-8")))
            for raw in variants
        }
        self.assertEqual(digests, {doc["content_sha256"]})
        object_digests = {compute_acceptance_object_sha256(raw) for raw in variants}
        self.assertEqual(len(object_digests), 3)
        # Alias still derives contract digest from object bytes.
        self.assertEqual(
            canonical_acceptance_contract_sha256(variants[0]), doc["content_sha256"]
        )

    def test_rejects_old_flat_schema_shape(self) -> None:
        flat = {
            "schema": ACCEPTANCE_CONTRACT_SCHEMA,
            "contract_id": "synth-contract-01",
            "version": "1.0.0",
            "benchmark_id": "synth-benchmark",
            "question_id": "Q-synth",
            "criteria": [{"id": "c1", "status": "pending"}],
        }
        kwargs = _acceptance_archive_kwargs(flat)
        with self.assertRaises(ValueError) as ctx:
            build_acceptance_contract_archive(
                **{
                    k: v
                    for k, v in kwargs.items()
                    if not str(k).startswith("_")
                }
            )
        self.assertIn("flat", str(ctx.exception).lower())

    def test_build_validates_identity_and_hash(self) -> None:
        kwargs = _acceptance_archive_kwargs()
        build_kwargs = {
            k: v for k, v in kwargs.items() if not str(k).startswith("_")
        }
        item = build_acceptance_contract_archive(**build_kwargs)
        self.assertEqual(item["schema"], ACCEPTANCE_CONTRACT_SCHEMA)
        self.assertEqual(item["schema_version"], ACCEPTANCE_CONTRACT_SCHEMA)
        self.assertEqual(item["contract_sha256"], kwargs["expected_contract_sha256"])
        self.assertEqual(item["object_sha256"], kwargs["_object_sha256"])
        self.assertEqual(item["content_sha256"], kwargs["_contract_sha256"])
        self.assertTrue(item["object_key"].startswith(ACCEPTANCE_CONTRACT_PREFIX))
        self.assertEqual(item["contract_id"], "contract-synth-alpha-q01")
        self.assertNotIn("criteria", item)
        self.assertEqual(
            item["b2_metadata"]["contract_sha256"], item["contract_sha256"]
        )
        self.assertEqual(item["b2_metadata"]["object_sha256"], item["object_sha256"])

        bad_hash = dict(build_kwargs)
        bad_hash["expected_contract_sha256"] = "0" * 64
        bad_hash["expected_sha256"] = "0" * 64
        with self.assertRaises(ValueError) as ctx:
            build_acceptance_contract_archive(**bad_hash)
        self.assertIn("contract_sha256", str(ctx.exception))

        bad_id = dict(build_kwargs)
        bad_id["expected_contract_id"] = "other-id"
        with self.assertRaises(ValueError):
            build_acceptance_contract_archive(**bad_id)

    def test_rejects_wrong_schema_and_malformed_base64(self) -> None:
        bad_schema = _acceptance_archive_kwargs(
            _synthetic_acceptance_contract(schema_version="acceptance_contract.v0")
        )
        build_kwargs = {
            k: v for k, v in bad_schema.items() if not str(k).startswith("_")
        }
        with self.assertRaises(ValueError) as ctx:
            build_acceptance_contract_archive(**build_kwargs)
        self.assertIn("schema_version", str(ctx.exception))

        kwargs = _acceptance_archive_kwargs()
        build_kwargs = {k: v for k, v in kwargs.items() if not str(k).startswith("_")}
        build_kwargs["contract_json_base64"] = "not%%base64"
        with self.assertRaises(ValueError):
            build_acceptance_contract_archive(**build_kwargs)

    def test_archive_and_verify_head_round_trip(self) -> None:
        server = _import_bridge_server()
        kwargs = _acceptance_archive_kwargs()
        build_kwargs = {
            k: v for k, v in kwargs.items() if not str(k).startswith("_")
        }
        item = build_acceptance_contract_archive(**build_kwargs)
        client = mock.Mock()
        written: dict[str, dict[str, object]] = {}

        def _put_object(**put_kwargs: object) -> dict[str, object]:
            key = str(put_kwargs["Key"])
            body = put_kwargs["Body"]
            assert isinstance(body, (bytes, bytearray))
            metadata = put_kwargs["Metadata"]
            assert isinstance(metadata, dict)
            written[key] = {
                "payload": bytes(body),
                "metadata": dict(metadata),
            }
            return {}

        def _head_object(*, Bucket: str, Key: str) -> dict[str, object]:
            del Bucket
            stored = written.get(Key)
            if stored is None:
                raise server.ClientError(
                    {"Error": {"Code": "404", "Message": "Not Found"}},
                    "HeadObject",
                )
            payload = stored["payload"]
            assert isinstance(payload, bytes)
            return {
                "ContentLength": len(payload),
                "ETag": '"etag-synth"',
                "Metadata": stored["metadata"],
            }

        client.put_object.side_effect = _put_object
        client.head_object.side_effect = _head_object
        client.list_objects_v2.return_value = {
            "Contents": [
                {
                    "Key": item["object_key"],
                    "Size": item["size"],
                    "ETag": '"etag-synth"',
                    "LastModified": mock.Mock(
                        isoformat=lambda: "2026-08-11T00:00:00+00:00"
                    ),
                }
            ],
            "IsTruncated": False,
        }
        with mock.patch.object(server, "B2_BUCKET", CANONICAL_LEGALAI_BUCKET):
            with mock.patch.object(
                server, "_require_allowed_user", return_value="tester"
            ):
                with mock.patch.object(server, "_b2_client", return_value=client):
                    archived = asyncio.run(
                        server.archive_acceptance_contract.fn(**build_kwargs)
                    )
                    verified = asyncio.run(
                        server.verify_acceptance_contract.fn(
                            object_key=str(kwargs["expected_object_key"]),
                            expected_contract_sha256=str(item["contract_sha256"]),
                            expected_object_sha256=str(item["object_sha256"]),
                            expected_size=item["size"],
                        )
                    )
                    listed = asyncio.run(
                        server.list_acceptance_contracts.fn(max_keys=50)
                    )

        self.assertTrue(archived["ok"])
        self.assertTrue(archived["verified"])
        self.assertFalse(archived["already_present"])
        self.assertEqual(archived["contract_sha256"], item["contract_sha256"])
        self.assertEqual(archived["object_sha256"], item["object_sha256"])
        self.assertNotIn("criteria", archived)
        self.assertNotIn("contract_json", archived)
        self.assertTrue(verified["verified"])
        self.assertTrue(verified["size_match"])
        self.assertTrue(verified["contract_sha256_match"])
        self.assertTrue(verified["object_sha256_match"])
        self.assertEqual(listed["prefix"], ACCEPTANCE_CONTRACT_PREFIX)
        self.assertEqual(listed["schema_version"], ACCEPTANCE_CONTRACT_SCHEMA)
        client.put_object.assert_called_once()

    def test_rejects_overwrite_with_different_content(self) -> None:
        server = _import_bridge_server()
        kwargs = _acceptance_archive_kwargs()
        build_kwargs = {
            k: v for k, v in kwargs.items() if not str(k).startswith("_")
        }
        item = build_acceptance_contract_archive(**build_kwargs)
        client = mock.Mock()
        client.head_object.return_value = {
            "ContentLength": item["size"] + 1,
            "ETag": '"other"',
            "Metadata": {
                "contract_sha256": "1" * 64,
                "object_sha256": "2" * 64,
                "sha256": "2" * 64,
            },
        }
        with mock.patch.object(server, "B2_BUCKET", CANONICAL_LEGALAI_BUCKET):
            with mock.patch.object(
                server, "_require_allowed_user", return_value="tester"
            ):
                with mock.patch.object(server, "_b2_client", return_value=client):
                    with self.assertRaises(ValueError) as ctx:
                        asyncio.run(
                            server.archive_acceptance_contract.fn(**build_kwargs)
                        )
        self.assertIn("different content", str(ctx.exception))
        client.put_object.assert_not_called()

    def test_idempotent_when_same_content_already_present(self) -> None:
        server = _import_bridge_server()
        kwargs = _acceptance_archive_kwargs()
        build_kwargs = {
            k: v for k, v in kwargs.items() if not str(k).startswith("_")
        }
        item = build_acceptance_contract_archive(**build_kwargs)
        client = mock.Mock()
        client.head_object.return_value = {
            "ContentLength": item["size"],
            "ETag": '"same"',
            "Metadata": {
                "contract_sha256": item["contract_sha256"],
                "object_sha256": item["object_sha256"],
                "sha256": item["object_sha256"],
            },
        }
        with mock.patch.object(server, "B2_BUCKET", CANONICAL_LEGALAI_BUCKET):
            with mock.patch.object(
                server, "_require_allowed_user", return_value="tester"
            ):
                with mock.patch.object(server, "_b2_client", return_value=client):
                    result = asyncio.run(
                        server.archive_acceptance_contract.fn(**build_kwargs)
                    )
        self.assertTrue(result["verified"])
        self.assertTrue(result["already_present"])
        self.assertEqual(result["contract_sha256"], item["contract_sha256"])
        self.assertEqual(result["object_sha256"], item["object_sha256"])
        client.put_object.assert_not_called()

    def test_required_tools_include_acceptance_contract_ops(self) -> None:
        server = _import_bridge_server()
        for name in (
            "archive_acceptance_contract",
            "verify_acceptance_contract",
            "list_acceptance_contracts",
            "get_acceptance_contract_template",
            "get_acceptance_contract",
        ):
            self.assertIn(name, server.REQUIRED_PRODUCTION_TOOLS)
        names = asyncio.run(server.list_registered_tool_names())
        self.assertTrue(
            {
                "archive_acceptance_contract",
                "verify_acceptance_contract",
                "list_acceptance_contracts",
                "get_acceptance_contract_template",
                "get_acceptance_contract",
            }.issubset(names)
        )

    def test_template_returns_schema_hashing_and_synthetic_example(self) -> None:
        template = build_acceptance_contract_template()
        self.assertTrue(template["ok"])
        self.assertEqual(template["schema_version"], ACCEPTANCE_CONTRACT_SCHEMA)
        schema = template["json_schema"]
        self.assertEqual(schema["properties"]["identity"]["required"], [
            "benchmark_id",
            "question_id",
        ])
        hashing = template["canonical_hashing"]
        self.assertEqual(
            hashing["contract_sha256"]["excludes_field"], "content_sha256"
        )
        self.assertIn("object_sha256", hashing)
        example = template["example"]
        self.assertEqual(
            example["object_key"],
            canonical_acceptance_contract_object_key(
                benchmark_id=str(example["identity"]["benchmark_id"]),
                question_id=str(example["identity"]["question_id"]),
                contract_id=str(example["contract_id"]),
                version=str(example["version"]),
            ),
        )
        self.assertEqual(
            example["content_sha256"],
            compute_acceptance_contract_sha256(example),
        )
        self.assertNotIn("Case-00", json.dumps(example))
        prep = template["archive_preparation"]
        self.assertEqual(prep["preferred_field"], "contract")
        self.assertTrue(prep["pass_example_directly"])

    def test_structured_contract_template_example_archive_preparation(self) -> None:
        """Template example archives directly as contract with no client encoding."""
        template = build_acceptance_contract_template()
        example = template["example"]
        assert isinstance(example, dict)

        # Unstable key order / whitespace must not affect server-side result.
        shuffled = json.loads(json.dumps(example, sort_keys=False))
        item_a = build_acceptance_contract_archive(contract=shuffled)
        item_b = build_acceptance_contract_archive(contract=dict(reversed(list(example.items()))))

        expected_key = canonical_acceptance_contract_object_key(
            benchmark_id=str(example["identity"]["benchmark_id"]),
            question_id=str(example["identity"]["question_id"]),
            contract_id=str(example["contract_id"]),
            version=str(example["version"]),
        )
        expected_payload = serialize_acceptance_contract_stored_bytes(example)
        expected_contract = compute_acceptance_contract_sha256(example)
        expected_object = compute_acceptance_object_sha256(expected_payload)

        for item in (item_a, item_b):
            self.assertEqual(item["object_key"], expected_key)
            self.assertEqual(item["payload"], expected_payload)
            self.assertEqual(item["size"], len(expected_payload))
            self.assertEqual(item["contract_sha256"], expected_contract)
            self.assertEqual(item["object_sha256"], expected_object)
            self.assertEqual(item["content_sha256"], expected_contract)

        # No client Base64 / expected digest / identity fields required.
        self.assertEqual(item_a["contract_sha256"], item_b["contract_sha256"])
        self.assertEqual(item_a["object_sha256"], item_b["object_sha256"])
        self.assertEqual(item_a["object_key"], item_b["object_key"])

    def test_structured_contract_server_round_trip_and_zero_write(self) -> None:
        server = _import_bridge_server()
        example = build_acceptance_contract_template()["example"]
        assert isinstance(example, dict)
        item = build_acceptance_contract_archive(contract=example)
        client = mock.Mock()
        written: dict[str, dict[str, object]] = {}

        def _put_object(**put_kwargs: object) -> dict[str, object]:
            key = str(put_kwargs["Key"])
            body = put_kwargs["Body"]
            assert isinstance(body, (bytes, bytearray))
            metadata = put_kwargs["Metadata"]
            assert isinstance(metadata, dict)
            written[key] = {
                "payload": bytes(body),
                "metadata": dict(metadata),
            }
            return {}

        def _head_object(*, Bucket: str, Key: str) -> dict[str, object]:
            del Bucket
            stored = written.get(Key)
            if stored is None:
                raise server.ClientError(
                    {"Error": {"Code": "404", "Message": "Not Found"}},
                    "HeadObject",
                )
            payload = stored["payload"]
            assert isinstance(payload, bytes)
            return {
                "ContentLength": len(payload),
                "ETag": '"etag-struct"',
                "Metadata": stored["metadata"],
            }

        client.put_object.side_effect = _put_object
        client.head_object.side_effect = _head_object
        with mock.patch.object(server, "B2_BUCKET", CANONICAL_LEGALAI_BUCKET):
            with mock.patch.object(
                server, "_require_allowed_user", return_value="tester"
            ):
                with mock.patch.object(server, "_b2_client", return_value=client):
                    archived = asyncio.run(
                        server.archive_acceptance_contract.fn(contract=example)
                    )

        self.assertTrue(archived["ok"])
        self.assertTrue(archived["verified"])
        self.assertEqual(archived["object_key"], item["object_key"])
        self.assertEqual(archived["size"], item["size"])
        self.assertEqual(archived["contract_sha256"], item["contract_sha256"])
        self.assertEqual(archived["object_sha256"], item["object_sha256"])
        self.assertEqual(written[item["object_key"]]["payload"], item["payload"])
        client.put_object.assert_called_once()

        bad_client = mock.Mock()
        with mock.patch.object(server, "B2_BUCKET", CANONICAL_LEGALAI_BUCKET):
            with mock.patch.object(
                server, "_require_allowed_user", return_value="tester"
            ):
                with mock.patch.object(server, "_b2_client", return_value=bad_client):
                    with self.assertRaises(ValueError):
                        asyncio.run(
                            server.archive_acceptance_contract.fn(
                                contract={
                                    "schema_version": "acceptance_contract.v0",
                                    "contract_id": "x",
                                }
                            )
                        )
        bad_client.put_object.assert_not_called()
        bad_client.head_object.assert_not_called()

    def test_legacy_base64_path_still_accepted(self) -> None:
        kwargs = _acceptance_archive_kwargs()
        build_kwargs = {
            k: v for k, v in kwargs.items() if not str(k).startswith("_")
        }
        legacy = build_acceptance_contract_archive(**build_kwargs)
        structured = build_acceptance_contract_archive(
            contract=_synthetic_acceptance_contract()
        )
        self.assertEqual(legacy["object_key"], structured["object_key"])
        self.assertEqual(legacy["contract_sha256"], structured["contract_sha256"])
        # Legacy stores caller-provided bytes; structured stores canonical bytes.
        self.assertEqual(
            structured["payload"],
            serialize_acceptance_contract_stored_bytes(_synthetic_acceptance_contract()),
        )
        self.assertEqual(
            structured["object_sha256"],
            compute_acceptance_object_sha256(structured["payload"]),
        )

    def test_deterministic_key_generation_and_optional_legacy_key(self) -> None:
        key = canonical_acceptance_contract_object_key(
            benchmark_id="synth-benchmark-alpha",
            question_id="Q-SYNTH-01",
            contract_id="contract-synth-alpha-q01",
            version="1.0.0",
        )
        self.assertEqual(
            key,
            f"{ACCEPTANCE_CONTRACT_PREFIX}"
            "synth-benchmark-alpha/Q-SYNTH-01/contract-synth-alpha-q01/"
            "v1.0.0/acceptance_contract.json",
        )
        kwargs = _acceptance_archive_kwargs()
        build_kwargs = {
            k: v for k, v in kwargs.items() if not str(k).startswith("_")
        }
        # Server generates key; omitting expected_object_key still works.
        without_key = dict(build_kwargs)
        without_key.pop("expected_object_key", None)
        item = build_acceptance_contract_archive(**without_key)
        self.assertEqual(item["object_key"], key)

        # Matching optional legacy key is accepted.
        matched = build_acceptance_contract_archive(**build_kwargs)
        self.assertEqual(matched["object_key"], key)

        mismatched = dict(without_key)
        mismatched["expected_object_key"] = (
            f"{ACCEPTANCE_CONTRACT_PREFIX}other/Q1/c1/v1.0.0/acceptance_contract.json"
        )
        with self.assertRaises(AcceptanceContractValidationError) as ctx:
            build_acceptance_contract_archive(**mismatched)
        self.assertEqual(ctx.exception.path, "expected_object_key")
        self.assertIn("generated key", ctx.exception.constraint)
        self.assertEqual(ctx.exception.received_type, "string")

    def test_malformed_and_path_unsafe_identity_errors(self) -> None:
        kwargs = _acceptance_archive_kwargs()
        build_kwargs = {
            k: v for k, v in kwargs.items() if not str(k).startswith("_")
        }
        bad = dict(build_kwargs)
        bad["expected_benchmark_id"] = "../escape"
        with self.assertRaises(AcceptanceContractValidationError) as ctx:
            build_acceptance_contract_archive(**bad)
        self.assertEqual(ctx.exception.path, "expected_benchmark_id")
        self.assertIn("identity shape", ctx.exception.constraint)
        self.assertEqual(ctx.exception.received_type, "string")
        self.assertIn("../escape", ctx.exception.received_value)

        bad_type = dict(build_kwargs)
        bad_type["expected_question_id"] = 123
        with self.assertRaises(AcceptanceContractValidationError) as ctx:
            build_acceptance_contract_archive(**bad_type)
        self.assertEqual(ctx.exception.path, "expected_question_id")
        self.assertEqual(ctx.exception.received_type, "integer")
        self.assertEqual(ctx.exception.received_value, "123")

        unsafe_doc = dict(_synthetic_acceptance_contract())
        unsafe_doc["identity"] = {
            "benchmark_id": "bad/id",
            "question_id": "Q-SYNTH-01",
        }
        unsafe_doc["content_sha256"] = compute_acceptance_contract_sha256(unsafe_doc)
        payload = json.dumps(
            unsafe_doc, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        payload_build = dict(build_kwargs)
        payload_build["contract_json_base64"] = base64.b64encode(payload).decode(
            "ascii"
        )
        with self.assertRaises(AcceptanceContractValidationError) as ctx:
            build_acceptance_contract_archive(**payload_build)
        self.assertEqual(ctx.exception.path, "$.identity.benchmark_id")
        self.assertNotIn("fallback_text", str(ctx.exception))
        self.assertNotIn("presence_phrases", str(ctx.exception))

    def test_zero_write_on_validation_failure(self) -> None:
        server = _import_bridge_server()
        kwargs = _acceptance_archive_kwargs()
        build_kwargs = {
            k: v for k, v in kwargs.items() if not str(k).startswith("_")
        }
        build_kwargs["expected_benchmark_id"] = "../escape"
        client = mock.Mock()
        with mock.patch.object(server, "B2_BUCKET", CANONICAL_LEGALAI_BUCKET):
            with mock.patch.object(
                server, "_require_allowed_user", return_value="tester"
            ):
                with mock.patch.object(server, "_b2_client", return_value=client):
                    with self.assertRaises(ValueError) as ctx:
                        asyncio.run(
                            server.archive_acceptance_contract.fn(**build_kwargs)
                        )
        self.assertIn("expected_benchmark_id", str(ctx.exception))
        client.put_object.assert_not_called()
        client.head_object.assert_not_called()


class AcceptanceContractRetrievalTests(unittest.TestCase):
    """Focused security/regression coverage for get_acceptance_contract."""

    def _identity(self) -> dict[str, str]:
        return {
            "benchmark_id": "synth-benchmark-alpha",
            "question_id": "Q-SYNTH-01",
            "contract_id": "contract-synth-alpha-q01",
            "version": "1.0.0",
        }

    def _valid_payload_and_meta(self) -> tuple[dict[str, object], bytes, dict[str, str]]:
        doc = _synthetic_acceptance_contract()
        item = build_acceptance_contract_archive(contract=doc)
        payload = item["payload"]
        assert isinstance(payload, bytes)
        meta = {
            "contract_sha256": str(item["contract_sha256"]),
            "object_sha256": str(item["object_sha256"]),
            "sha256": str(item["object_sha256"]),
        }
        return doc, payload, meta

    def test_valid_retrieval_returns_metadata_and_contract(self) -> None:
        doc, payload, meta = self._valid_payload_and_meta()
        identity = self._identity()
        result = verify_retrieved_acceptance_contract(
            payload=payload,
            expected_size=len(payload),
            stored_contract_sha256=meta["contract_sha256"],
            stored_object_sha256=meta["object_sha256"],
            **identity,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["object_key"], doc["object_key"])
        self.assertEqual(result["contract_sha256"], meta["contract_sha256"])
        self.assertEqual(result["object_sha256"], meta["object_sha256"])
        self.assertEqual(result["contract"]["contract_id"], identity["contract_id"])
        self.assertIn("required_criterion_ids", result["contract"])

        server = _import_bridge_server()
        client = mock.Mock()
        client.head_object.return_value = {
            "ContentLength": len(payload),
            "ETag": '"etag-get"',
            "Metadata": meta,
        }

        class _Body:
            def read(self, _n: int) -> bytes:
                return payload

            def close(self) -> None:
                return None

        client.get_object.return_value = {"Body": _Body()}
        with mock.patch.object(server, "B2_BUCKET", CANONICAL_LEGALAI_BUCKET):
            with mock.patch.object(
                server, "_require_allowed_user", return_value="tester"
            ):
                with mock.patch.object(server, "_b2_client", return_value=client):
                    fetched = asyncio.run(
                        server.get_acceptance_contract.fn(**identity)
                    )
        self.assertTrue(fetched["ok"])
        self.assertTrue(fetched["verified"])
        self.assertEqual(fetched["b2_bucket"], CANONICAL_LEGALAI_BUCKET)
        self.assertEqual(fetched["contract"]["schema_version"], ACCEPTANCE_CONTRACT_SCHEMA)
        client.get_object.assert_called_once_with(
            Bucket=CANONICAL_LEGALAI_BUCKET,
            Key=str(doc["object_key"]),
        )

    def test_traversal_and_arbitrary_key_rejected_by_schema(self) -> None:
        with self.assertRaises(AcceptanceContractValidationError) as ctx:
            resolve_acceptance_contract_retrieval_key(
                benchmark_id="../escape",
                question_id="Q1",
                contract_id="c1",
                version="1.0.0",
            )
        self.assertEqual(ctx.exception.path, "benchmark_id")

        with self.assertRaises(AcceptanceContractValidationError):
            resolve_acceptance_contract_retrieval_key(
                benchmark_id="ok",
                question_id="Q1/../../etc",
                contract_id="c1",
                version="1.0.0",
            )

        # Tool surface accepts only identity fields — no object_key / bucket / URL.
        server = _import_bridge_server()
        tool = server.get_acceptance_contract
        params = getattr(tool, "parameters", None) or {}
        if isinstance(params, dict) and params:
            self.assertNotIn("object_key", params)
            self.assertNotIn("bucket", params)
            self.assertNotIn("url", params)
            self.assertNotIn("prefix", params)
        for bad in ("object_key", "bucket", "url", "prefix", "path"):
            with self.assertRaises(TypeError):
                asyncio.run(
                    server.get_acceptance_contract.fn(
                        benchmark_id="synth-benchmark-alpha",
                        question_id="Q-SYNTH-01",
                        contract_id="contract-synth-alpha-q01",
                        version="1.0.0",
                        **{bad: "Benchmarks/other/secret.json"},
                    )
                )

    def test_identity_mismatch_fail_closed(self) -> None:
        _doc, payload, meta = self._valid_payload_and_meta()
        with self.assertRaises(AcceptanceContractValidationError) as ctx:
            verify_retrieved_acceptance_contract(
                payload=payload,
                benchmark_id="synth-benchmark-alpha",
                question_id="Q-SYNTH-01",
                contract_id="contract-other-id",
                version="1.0.0",
                expected_size=len(payload),
                stored_contract_sha256=meta["contract_sha256"],
                stored_object_sha256=meta["object_sha256"],
            )
        # Different contract_id yields a different canonical key than embedded.
        self.assertIn(ctx.exception.path, {"$.object_key", "$.contract_id"})

    def test_contract_hash_mismatch_fail_closed(self) -> None:
        doc = _synthetic_acceptance_contract()
        doc = dict(doc)
        doc["content_sha256"] = "a" * 64
        payload = serialize_acceptance_contract_stored_bytes(doc)
        object_digest = compute_acceptance_object_sha256(payload)
        with self.assertRaises(AcceptanceContractValidationError) as ctx:
            verify_retrieved_acceptance_contract(
                payload=payload,
                expected_size=len(payload),
                stored_contract_sha256="a" * 64,
                stored_object_sha256=object_digest,
                **self._identity(),
            )
        self.assertEqual(ctx.exception.path, "$.content_sha256")

    def test_legacy_missing_digest_metadata_is_verified_from_payload(self) -> None:
        doc, payload, _meta = self._valid_payload_and_meta()
        result = verify_retrieved_acceptance_contract(
            payload=payload,
            expected_size=len(payload),
            stored_contract_sha256=None,
            stored_object_sha256=None,
            **self._identity(),
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["object_key"], doc["object_key"])

    def test_present_object_metadata_mismatch_still_fails_closed(self) -> None:
        _doc, payload, meta = self._valid_payload_and_meta()
        with self.assertRaises(AcceptanceContractValidationError) as ctx:
            verify_retrieved_acceptance_contract(
                payload=payload,
                expected_size=len(payload),
                stored_contract_sha256=meta["contract_sha256"],
                stored_object_sha256="0" * 64,
                **self._identity(),
            )
        self.assertEqual(ctx.exception.path, "object_sha256")

    def test_present_contract_metadata_mismatch_still_fails_closed(self) -> None:
        _doc, payload, meta = self._valid_payload_and_meta()
        with self.assertRaises(AcceptanceContractValidationError) as ctx:
            verify_retrieved_acceptance_contract(
                payload=payload,
                expected_size=len(payload),
                stored_contract_sha256="0" * 64,
                stored_object_sha256=meta["object_sha256"],
                **self._identity(),
            )
        self.assertEqual(ctx.exception.path, "contract_sha256")

    def test_case_variant_benchmark_resolves_unique_legacy_key(self) -> None:
        server = _import_bridge_server()
        identity = self._identity()
        doc, payload, _meta = self._valid_payload_and_meta()
        object_key = str(doc["object_key"])
        client = mock.Mock()

        def _head_object(*, Bucket: str, Key: str) -> dict[str, object]:
            del Bucket
            if Key != object_key:
                raise server.ClientError(
                    {"Error": {"Code": "404", "Message": "Not Found"}},
                    "HeadObject",
                )
            return {
                "ContentLength": len(payload),
                "ETag": '"legacy"',
                "Metadata": {},
            }

        class _Body:
            def read(self, _n: int) -> bytes:
                return payload

            def close(self) -> None:
                return None

        client.head_object.side_effect = _head_object
        client.list_objects_v2.return_value = {
            "Contents": [{"Key": object_key}],
            "IsTruncated": False,
        }
        client.get_object.return_value = {"Body": _Body()}
        requested = {**identity, "benchmark_id": identity["benchmark_id"].upper()}
        with mock.patch.object(server, "B2_BUCKET", CANONICAL_LEGALAI_BUCKET):
            with mock.patch.object(server, "_require_allowed_user", return_value="tester"):
                with mock.patch.object(server, "_b2_client", return_value=client):
                    result = asyncio.run(server.get_acceptance_contract.fn(**requested))
        self.assertTrue(result["verified"])
        self.assertEqual(result["object_key"], object_key)
        client.get_object.assert_called_once_with(
            Bucket=CANONICAL_LEGALAI_BUCKET, Key=object_key
        )

    def test_case_variant_benchmark_ambiguity_fails_closed(self) -> None:
        server = _import_bridge_server()
        identity = self._identity()
        requested = {**identity, "benchmark_id": identity["benchmark_id"].upper()}
        key = canonical_acceptance_contract_object_key(**identity)
        client = mock.Mock()
        client.head_object.side_effect = server.ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}},
            "HeadObject",
        )
        client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": key},
                {
                    "Key": key.replace(
                        identity["benchmark_id"],
                        identity["benchmark_id"].upper(),
                    )
                },
            ],
            "IsTruncated": False,
        }
        with mock.patch.object(server, "B2_BUCKET", CANONICAL_LEGALAI_BUCKET):
            with mock.patch.object(
                server, "_require_allowed_user", return_value="tester"
            ):
                with mock.patch.object(server, "_b2_client", return_value=client):
                    with self.assertRaisesRegex(ValueError, "ambiguous"):
                        asyncio.run(server.get_acceptance_contract.fn(**requested))
        client.get_object.assert_not_called()

    def test_object_corruption_hash_mismatch_fail_closed(self) -> None:
        _doc, payload, meta = self._valid_payload_and_meta()
        corrupted = payload + b" "
        with self.assertRaises(AcceptanceContractValidationError) as ctx:
            verify_retrieved_acceptance_contract(
                payload=corrupted,
                expected_size=len(corrupted),
                stored_contract_sha256=meta["contract_sha256"],
                stored_object_sha256=meta["object_sha256"],
                **self._identity(),
            )
        self.assertEqual(ctx.exception.path, "object_sha256")

    def test_missing_object_returns_not_found(self) -> None:
        server = _import_bridge_server()
        identity = self._identity()
        client = mock.Mock()

        def _head_object(*, Bucket: str, Key: str) -> dict[str, object]:
            del Bucket, Key
            raise server.ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}},
                "HeadObject",
            )

        client.head_object.side_effect = _head_object
        with mock.patch.object(server, "B2_BUCKET", CANONICAL_LEGALAI_BUCKET):
            with mock.patch.object(
                server, "_require_allowed_user", return_value="tester"
            ):
                with mock.patch.object(server, "_b2_client", return_value=client):
                    result = asyncio.run(
                        server.get_acceptance_contract.fn(**identity)
                    )
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["error"], "object_not_found")
        self.assertEqual(
            result["object_key"],
            canonical_acceptance_contract_object_key(**identity),
        )
        client.get_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
