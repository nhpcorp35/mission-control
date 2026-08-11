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
    validate_acceptance_contract_object_key,
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
        ):
            self.assertIn(name, server.REQUIRED_PRODUCTION_TOOLS)
        names = asyncio.run(server.list_registered_tool_names())
        self.assertTrue(
            {
                "archive_acceptance_contract",
                "verify_acceptance_contract",
                "list_acceptance_contracts",
                "get_acceptance_contract_template",
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


if __name__ == "__main__":
    unittest.main()
