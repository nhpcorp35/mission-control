"""Contract suite for storage.get_case00_question (schema, gateway, CI gates)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_DIR = REPO_ROOT / "hal_legalai_gateway"
BRIDGE_DIR = REPO_ROOT / "github_actions_bridge"
_BRIDGE_SERVER_ENV = {
    "GITHUB_OAUTH_CLIENT_ID": "test-client-id",
    "GITHUB_OAUTH_CLIENT_SECRET": "test-client-secret",
    "REDIS_HOST": "127.0.0.1",
    "REDIS_PORT": "6379",
    "STORAGE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "JWT_SIGNING_KEY": "test-jwt-signing-key-for-bridge",
}

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

from hal_legalai_gateway.case00_question_contract import (  # noqa: E402
    BENCHMARK_ID,
    CONTRACT_SCHEMA_RELATIVE,
    GATEWAY_TOOL,
    Case00QuestionContractError,
    contract_schema_path,
    contract_violation_response,
    load_case00_question_contract,
    load_contract_document,
    public_metadata_summary,
    validate_contract_schema_document,
    validate_public_input,
    validate_public_output,
)
from hal_legalai_gateway.config import load_settings  # noqa: E402
from hal_legalai_gateway.mcp_server import (  # noqa: E402
    create_mcp_server,
    list_registered_tool_names,
    register_forwarding_tools,
)
from hal_legalai_gateway.registry import load_registry  # noqa: E402

REGISTRY_PATH = GATEWAY_DIR / "registry.json"
CANONICAL_SHA256 = (
    "ce7e3a25b22ec23822aec4dcd317b1df38ce6c85b59f684f45f3bdb811316d86"
)
CANONICAL_SOURCE_KEY = (
    "Benchmarks/Case-00-Triborough/derived/attorney-feedback-eval/"
    "attorney-reviews/review-20260802-2122f82dafe3/"
    "attorney_review_packet_02-original.md"
)
FORBIDDEN_DERIVED_PREFIX = (
    "Benchmarks/Case-00-Triborough/derived/question-text/"
)

REQUIRED_SECRETS = {
    "GITHUB_OAUTH_CLIENT_ID": "test-gateway-client-id",
    "GITHUB_OAUTH_CLIENT_SECRET": "test-gateway-client-secret",
    "JWT_SIGNING_KEY": "test-jwt-signing-key",
    "STORAGE_ENCRYPTION_KEY": "dGVzdC1zdG9yYWdlLWVuY3J5cHRpb24ta2V5MDEyMzQ1Ng==",
    "REDIS_HOST": "127.0.0.1",
    "REDIS_PORT": "6379",
    "GATEWAY_BRIDGE_AUTHORIZATION": "test-bridge-service-token",
    "GATEWAY_PUBLIC_URL": "https://gateway.example",
}


def _gateway_settings():
    environ = {
        **REQUIRED_SECRETS,
        "GATEWAY_BRIDGE_URL": "https://bridge.example",
        "GATEWAY_STORAGE_URL": "https://storage.example",
        "GATEWAY_MISSION_CONTROL_URL": "https://mission.example",
        "GATEWAY_ARTIFACTS_URL": "https://artifacts.example",
    }
    return load_settings(environ=environ, registry=load_registry(REGISTRY_PATH))


def _import_bridge_server():
    for key, value in _BRIDGE_SERVER_ENV.items():
        os.environ.setdefault(key, value)
    bridge_dir = str(BRIDGE_DIR)
    if bridge_dir not in sys.path:
        sys.path.insert(0, bridge_dir)
    import server as bridge_server  # type: ignore[import-not-found]

    return bridge_server


class Case00QuestionSchemaTests(unittest.TestCase):
    def test_schema_path_version_and_identifier(self) -> None:
        contract = load_case00_question_contract()
        self.assertTrue(contract_schema_path().is_file())
        self.assertEqual(
            contract.schema_id,
            "https://hal.nhpcorp.dev/mcp/contracts/storage.get_case00_question.v1.json",
        )
        self.assertEqual(contract.version, "1.0.0")
        self.assertEqual(contract.gateway_tool, GATEWAY_TOOL)
        self.assertEqual(contract.downstream_tool, "get_case00_question")
        self.assertEqual(contract.benchmark_id, BENCHMARK_ID)

    def test_schema_document_is_valid(self) -> None:
        document = load_contract_document()
        validate_contract_schema_document(document)
        self.assertEqual(document["x-hal-contract-version"], "1.0.0")
        success_props = document["$defs"]["success_output"]["properties"]
        self.assertNotIn("object_key", success_props)
        self.assertNotIn("size", success_props)


class Case00QuestionRegistryTests(unittest.TestCase):
    def test_registry_registers_from_schema_not_duplicated_metadata(self) -> None:
        registry = load_registry(REGISTRY_PATH)
        contract = load_case00_question_contract()
        binding = next(
            b for b in registry.tool_bindings if b.gateway_tool == GATEWAY_TOOL
        )
        self.assertEqual(binding.description, contract.description)
        self.assertEqual(binding.downstream_tool, contract.downstream_tool)
        raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        entry = next(
            item for item in raw["tool_bindings"] if item["tool"] == GATEWAY_TOOL
        )
        self.assertEqual(entry["contract_schema"], CONTRACT_SCHEMA_RELATIVE)
        self.assertNotIn("description", entry)

    def test_gateway_discovers_registered_public_tool(self) -> None:
        settings = _gateway_settings()
        mcp = create_mcp_server(settings, auth=mock.Mock())
        names = asyncio.run(list_registered_tool_names(mcp))
        self.assertIn(GATEWAY_TOOL, names)


class Case00QuestionValidationTests(unittest.TestCase):
    def test_q3_success_response_exact_keys_and_values(self) -> None:
        payload = {
            "ok": True,
            "benchmark_id": BENCHMARK_ID,
            "question_id": "Q3",
            "question_text": "## Q3. Which insurance policies are at issue?\n\nbody",
            "source_object_key": CANONICAL_SOURCE_KEY,
            "sha256": CANONICAL_SHA256,
        }
        validated = validate_public_output(payload)
        self.assertEqual(set(validated.keys()), set(payload.keys()))
        self.assertEqual(validated["question_id"], "Q3")
        self.assertEqual(validated["benchmark_id"], BENCHMARK_ID)
        self.assertEqual(validated["sha256"], CANONICAL_SHA256)

    def test_not_found_response_shape(self) -> None:
        payload = {
            "ok": False,
            "benchmark_id": BENCHMARK_ID,
            "question_id": "Q3",
            "error": "not_found",
            "source_object_key": CANONICAL_SOURCE_KEY,
            "sha256": CANONICAL_SHA256,
        }
        validated = validate_public_output(payload)
        self.assertFalse(validated["ok"])
        self.assertEqual(validated["error"], "not_found")
        self.assertNotIn("question_text", validated)

    def test_invalid_id_rejected_before_downstream(self) -> None:
        with self.assertRaises(Case00QuestionContractError):
            validate_public_input({"question_id": "../Q3"})

    def test_negative_drift_detects_extra_response_field(self) -> None:
        drifted = {
            "ok": True,
            "benchmark_id": BENCHMARK_ID,
            "question_id": "Q3",
            "question_text": "## Q3. title\n",
            "source_object_key": CANONICAL_SOURCE_KEY,
            "sha256": CANONICAL_SHA256,
            "object_key": "Benchmarks/secret",
        }
        with self.assertRaises(Case00QuestionContractError) as ctx:
            validate_public_output(drifted)
        self.assertIn("undocumented", str(ctx.exception).lower())

    def test_negative_drift_detects_wrong_benchmark(self) -> None:
        drifted = {
            "ok": True,
            "benchmark_id": "Case-99",
            "question_id": "Q3",
            "question_text": "## Q3. title\n",
            "source_object_key": CANONICAL_SOURCE_KEY,
            "sha256": CANONICAL_SHA256,
        }
        with self.assertRaises(Case00QuestionContractError):
            validate_public_output(drifted)


class Case00QuestionBridgeSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _import_bridge_server()

    def test_canonical_source_key_not_derived_question_text_prefix(self) -> None:
        self.assertEqual(
            self.server.CANONICAL_CASE00_ATTORNEY_PACKET_KEY,
            CANONICAL_SOURCE_KEY,
        )
        self.assertNotIn(
            FORBIDDEN_DERIVED_PREFIX,
            self.server.CANONICAL_CASE00_ATTORNEY_PACKET_KEY,
        )

    def test_q3_from_verified_canonical_packet(self) -> None:
        packet = (
            b"# Packet\n\n"
            b"## Q2. What relief is requested?\n\nprivate q2 body\n\n"
            b"## Q3. Which insurance policies are at issue?\n\n"
            b"private q3 body\n\n"
        )

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
            result = asyncio.run(self.server.get_case00_question.fn("Q3"))
        validate_public_output(result)
        self.assertTrue(result["ok"])
        self.assertIn("private q3 body", result["question_text"])
        self.assertNotIn("private q2 body", result["question_text"])


class Case00QuestionGatewayGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = _gateway_settings()

    def _collect_tool(self) -> Any:
        collector: dict[str, Any] = {}

        class _Mcp:
            def tool(self, *args: Any, **kwargs: Any):
                def decorator(fn: Any) -> Any:
                    name = kwargs.get("name") or (args[0] if args else None)
                    if name == GATEWAY_TOOL:
                        collector["fn"] = fn
                    return fn

                return decorator

        register_forwarding_tools(
            _Mcp(),  # type: ignore[arg-type]
            self.settings,
            load_registry(REGISTRY_PATH).tool_bindings,
        )
        return collector["fn"]

    def test_malformed_bridge_response_fails_closed(self) -> None:
        tool = self._collect_tool()

        async def run():
            with mock.patch(
                "hal_legalai_gateway.mcp_server._require_gateway_principal",
                return_value="nhpcorp35",
            ), mock.patch(
                "hal_legalai_gateway.mcp_server.forward_mcp_tool",
                return_value={
                    "ok": True,
                    "result": {
                        "ok": True,
                        "benchmark_id": BENCHMARK_ID,
                        "question_id": "Q3",
                        "question_text": "## Q3. leaked\n",
                        "source_object_key": CANONICAL_SOURCE_KEY,
                        "sha256": CANONICAL_SHA256,
                        "size": 999,
                    },
                },
            ):
                return await tool("Q3")

        payload = asyncio.run(run())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "contract_violation")
        self.assertNotIn("question_text", payload)
        self.assertNotIn("size", payload)

    def test_invalid_input_rejected_before_forward(self) -> None:
        tool = self._collect_tool()

        async def run():
            with mock.patch(
                "hal_legalai_gateway.mcp_server._require_gateway_principal",
                return_value="nhpcorp35",
            ), mock.patch(
                "hal_legalai_gateway.mcp_server.forward_mcp_tool",
            ) as forward:
                result = await tool("Q0")
                forward.assert_not_called()
                return result

        payload = asyncio.run(run())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "invalid_input")


class Case00QuestionLiveSmokeTests(unittest.TestCase):
    """Post-deploy public MCP discovery + Q3 invocation (env-gated)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.gateway_url = (
            os.environ.get("HAL_LEGALAI_GATEWAY_URL")
            or os.environ.get("GATEWAY_PUBLIC_URL")
            or ""
        ).rstrip("/")
        cls.gateway_token = os.environ.get("GATEWAY_LIVE_OAUTH_TOKEN", "").strip()
        cls.run_live = bool(cls.gateway_url and cls.gateway_token)

    def test_live_public_q3_smoke(self) -> None:
        if not self.run_live:
            self.skipTest(
                "set HAL_LEGALAI_GATEWAY_URL and GATEWAY_LIVE_OAUTH_TOKEN for live smoke"
            )

        headers = {
            "Authorization": f"Bearer {self.gateway_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        mcp_url = f"{self.gateway_url}/mcp"
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "case00-contract-smoke", "version": "1"},
            },
        }
        tools_list = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
        call_q3 = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": GATEWAY_TOOL,
                "arguments": {"question_id": "Q3"},
            },
        }

        with httpx.Client(timeout=60.0) as client:
            init_resp = client.post(mcp_url, headers=headers, json=init)
            self.assertEqual(init_resp.status_code, 200, init_resp.text[:500])
            list_resp = client.post(mcp_url, headers=headers, json=tools_list)
            self.assertEqual(list_resp.status_code, 200, list_resp.text[:500])
            list_payload = list_resp.json()
            tools = list_payload.get("result", {}).get("tools", [])
            names = {item.get("name") for item in tools if isinstance(item, dict)}
            self.assertIn(GATEWAY_TOOL, names)
            call_resp = client.post(mcp_url, headers=headers, json=call_q3)
            self.assertEqual(call_resp.status_code, 200, call_resp.text[:500])
            call_payload = call_resp.json()
            structured = call_payload.get("result", {}).get("structuredContent")
            if not isinstance(structured, dict):
                content = call_payload.get("result", {}).get("content")
                if isinstance(content, list) and content:
                    text = content[0].get("text") if isinstance(content[0], dict) else None
                    structured = json.loads(text) if isinstance(text, str) else {}
            self.assertIsInstance(structured, dict)
            validated = validate_public_output(structured)
            summary = public_metadata_summary(validated)
            print(
                json.dumps(
                    {
                        "live_smoke": "storage.get_case00_question.Q3",
                        "gateway_url": self.gateway_url,
                        "schema_id": summary["schema_id"],
                        "contract_version": summary["contract_version"],
                        "response_metadata": summary,
                    },
                    sort_keys=True,
                )
            )
            self.assertTrue(validated["ok"])
            self.assertEqual(validated["question_id"], "Q3")
            self.assertEqual(validated["benchmark_id"], BENCHMARK_ID)
            self.assertEqual(validated["sha256"], CANONICAL_SHA256)


if __name__ == "__main__":
    unittest.main()
