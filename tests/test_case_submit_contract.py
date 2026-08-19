"""Contract suite for gateway-native direct-GitHub case.submit."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
from cryptography.fernet import Fernet

from hal_legalai_gateway.case_submit import (
    ALLOWED_QUESTION_IDS,
    BENCHMARK_ID,
    CASE00_WORKFLOW,
    CONTRACT_SCHEMA_RELATIVE,
    ERROR_COMMIT_NOT_FOUND,
    ERROR_DISPATCH_FAILED,
    ERROR_INVALID_INPUT,
    ERROR_UNSUPPORTED_BENCHMARK_QUESTION,
    FIXED_REPOSITORY,
    GATEWAY_TOOL,
    LEGACY_PUBLIC_CASE_SUBMISSION_ROUTES,
    SUCCESS_KEYS,
    CaseSubmitContractError,
    assert_legacy_case_submission_routes_absent,
    case00_run_marker,
    contract_schema_path,
    contract_violation_response,
    dispatch_case00_workflow,
    find_workflow_run_id,
    load_case_submit_contract,
    load_contract_document,
    resolve_registry_case_submit_binding,
    run_id_for_idempotency_key,
    submit_case00_direct,
    validate_contract_schema_document,
    validate_public_input,
    validate_public_output,
    verify_immutable_commit_sha,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "hal_legalai_gateway" / "registry.json"

VALID_SHA = "49f6881c08e7e4fdf76d8500d52a27d057c0804b"
MISSING_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
IDEMPOTENCY_KEY = "case00-q1-smoke-01"
RUN_ID = run_id_for_idempotency_key(IDEMPOTENCY_KEY)
WORKFLOW_RUN_ID = 4242


class _MockTransport:
    def __init__(self, routes: dict[tuple[str, str], tuple[int, dict[str, Any] | None]]):
        self.routes = routes
        self.requests: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = (request.method.upper(), request.url.path)
        self.requests.append(key)
        status, body = self.routes.get(key, (500, {"message": "unexpected"}))
        return httpx.Response(status, json=body)


def _client_factory(routes: dict[tuple[str, str], tuple[int, dict[str, Any] | None]]):
    transport = _MockTransport(routes)

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(transport.handler))

    return factory, transport


def _valid_registry_document() -> dict[str, Any]:
    raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    contract = load_case_submit_contract()
    case_tools = [
        tool
        for tool in raw["namespaces"]["case"]["tools"]
        if tool not in LEGACY_PUBLIC_CASE_SUBMISSION_ROUTES
    ]
    if GATEWAY_TOOL not in case_tools:
        case_tools.insert(1, GATEWAY_TOOL)
    raw["namespaces"]["case"]["tools"] = case_tools
    bindings = [
        item
        for item in raw["tool_bindings"]
        if item.get("tool") not in LEGACY_PUBLIC_CASE_SUBMISSION_ROUTES
    ]
    bindings = [
        item for item in bindings if item.get("tool") != GATEWAY_TOOL
    ]
    bindings.insert(
        1,
        {
            "tool": GATEWAY_TOOL,
            "namespace": contract.namespace,
            "downstream_service": contract.downstream_service,
            "downstream_tool": contract.downstream_tool,
            "contract_schema": CONTRACT_SCHEMA_RELATIVE,
            "notes": "Gateway-native direct GitHub workflow_dispatch; does not forward downstream.",
        },
    )
    raw["tool_bindings"] = bindings
    return raw


class CaseSubmitSchemaTests(unittest.TestCase):
    def test_schema_path_version_and_identifier(self) -> None:
        contract = load_case_submit_contract()
        self.assertTrue(contract_schema_path().is_file())
        self.assertEqual(
            contract.schema_id,
            "https://hal.nhpcorp.dev/mcp/contracts/case.submit.v1.json",
        )
        self.assertEqual(contract.version, "1.0.0")
        self.assertEqual(contract.gateway_tool, GATEWAY_TOOL)
        self.assertEqual(contract.downstream_tool, "case.submit")
        self.assertEqual(contract.benchmark_id, BENCHMARK_ID)

    def test_schema_document_is_valid(self) -> None:
        document = load_contract_document()
        validate_contract_schema_document(document)
        success_props = document["$defs"]["success_output"]["properties"]
        self.assertEqual(set(success_props), set(SUCCESS_KEYS))
        self.assertNotIn("mission_id", success_props)
        self.assertNotIn("status", success_props)


class CaseSubmitRegistryTests(unittest.TestCase):
    def test_resolve_registry_binding_contract(self) -> None:
        resolve_registry_case_submit_binding(_valid_registry_document())

    def test_legacy_submission_routes_must_be_absent(self) -> None:
        document = _valid_registry_document()
        document["namespaces"]["case"]["tools"].append("case.submit_case00_q1")
        with self.assertRaises(RuntimeError):
            assert_legacy_case_submission_routes_absent(document)

    def test_registry_binding_requires_contract_schema(self) -> None:
        document = _valid_registry_document()
        binding = next(
            item for item in document["tool_bindings"] if item["tool"] == GATEWAY_TOOL
        )
        binding.pop("contract_schema")
        with self.assertRaises(RuntimeError):
            resolve_registry_case_submit_binding(document)


class CaseSubmitValidationTests(unittest.TestCase):
    def test_valid_input_accepts_q1_q2_q3(self) -> None:
        for question_id in sorted(ALLOWED_QUESTION_IDS):
            with self.subTest(question_id=question_id):
                validated = validate_public_input(
                    {
                        "commit_sha": VALID_SHA,
                        "benchmark_id": BENCHMARK_ID,
                        "question_id": question_id,
                        "idempotency_key": IDEMPOTENCY_KEY,
                        "authorization_confirmed": True,
                    }
                )
                self.assertEqual(validated["question_id"], question_id)

    def test_q4_and_mission_control_fields_rejected(self) -> None:
        with self.assertRaises(CaseSubmitContractError):
            validate_public_input(
                {
                    "commit_sha": VALID_SHA,
                    "benchmark_id": BENCHMARK_ID,
                    "question_id": "Q4",
                    "idempotency_key": IDEMPOTENCY_KEY,
                    "authorization_confirmed": True,
                }
            )
        with self.assertRaises(CaseSubmitContractError):
            validate_public_input(
                {
                    "commit_sha": VALID_SHA,
                    "benchmark_id": BENCHMARK_ID,
                    "question_id": "Q1",
                    "idempotency_key": IDEMPOTENCY_KEY,
                    "authorization_confirmed": True,
                    "mission_yaml": "version: '1.0'",
                }
            )

    def test_success_response_exact_keys_and_values(self) -> None:
        payload = {
            "ok": True,
            "run_id": RUN_ID,
            "workflow_run_id": WORKFLOW_RUN_ID,
            "commit_sha": VALID_SHA,
            "question_id": "Q1",
            "idempotency_key": IDEMPOTENCY_KEY,
        }
        validated = validate_public_output(payload)
        self.assertEqual(set(validated.keys()), SUCCESS_KEYS)
        self.assertEqual(validated["run_id"], RUN_ID)
        self.assertEqual(validated["workflow_run_id"], WORKFLOW_RUN_ID)

    def test_negative_drift_detects_extra_response_field(self) -> None:
        drifted = {
            "ok": True,
            "run_id": RUN_ID,
            "workflow_run_id": WORKFLOW_RUN_ID,
            "commit_sha": VALID_SHA,
            "question_id": "Q1",
            "idempotency_key": IDEMPOTENCY_KEY,
            "mission_id": "legacy",
        }
        with self.assertRaises(CaseSubmitContractError):
            validate_public_output(drifted)


class CaseSubmitGitHubDispatchTests(unittest.TestCase):
    def _dispatch_routes(
        self,
        *,
        include_existing_run: bool = False,
    ) -> dict[tuple[str, str], tuple[int, dict[str, Any] | None]]:
        marker = case00_run_marker("Q1", RUN_ID)
        runs: list[dict[str, Any]] = []
        if include_existing_run:
            runs.append(
                {
                    "id": WORKFLOW_RUN_ID,
                    "display_title": marker,
                }
            )
        return {
            ("GET", f"/repos/{FIXED_REPOSITORY}/commits/{VALID_SHA}"): (
                200,
                {"sha": VALID_SHA},
            ),
            (
                "GET",
                f"/repos/{FIXED_REPOSITORY}/actions/workflows/{CASE00_WORKFLOW}/runs",
            ): (
                200,
                {"workflow_runs": runs},
            ),
            (
                "POST",
                f"/repos/{FIXED_REPOSITORY}/actions/workflows/{CASE00_WORKFLOW}/dispatches",
            ): (
                204,
                None,
            ),
        }

    def test_verify_immutable_commit_sha(self) -> None:
        routes = {
            ("GET", f"/repos/{FIXED_REPOSITORY}/commits/{VALID_SHA}"): (
                200,
                {"sha": VALID_SHA},
            )
        }
        factory, _ = _client_factory(routes)
        verified, error = asyncio.run(
            verify_immutable_commit_sha(VALID_SHA, client_factory=factory)
        )
        self.assertTrue(verified)
        self.assertIsNone(error)

    def test_missing_commit_maps_to_not_found(self) -> None:
        routes = {
            ("GET", f"/repos/{FIXED_REPOSITORY}/commits/{MISSING_SHA}"): (
                422,
                {"message": "No commit found for SHA"},
            )
        }
        factory, _ = _client_factory(routes)
        verified, error = asyncio.run(
            verify_immutable_commit_sha(MISSING_SHA, client_factory=factory)
        )
        self.assertFalse(verified)
        self.assertIsNone(error)

    def test_dispatch_inputs_use_run_id_not_mission_control(self) -> None:
        routes = self._dispatch_routes()
        factory, transport = _client_factory(routes)

        async def run() -> None:
            accepted, error = await dispatch_case00_workflow(
                run_id=RUN_ID,
                commit_sha=VALID_SHA,
                question_id="Q1",
                client_factory=factory,
            )
            self.assertTrue(accepted)
            self.assertIsNone(error)

        asyncio.run(run())
        post_calls = [
            key
            for key in transport.requests
            if key[0] == "POST" and key[1].endswith("/dispatches")
        ]
        self.assertEqual(len(post_calls), 1)

    def test_submit_success_returns_exact_public_response(self) -> None:
        routes = self._dispatch_routes()
        routes[
            (
                "GET",
                f"/repos/{FIXED_REPOSITORY}/actions/workflows/{CASE00_WORKFLOW}/runs",
            )
        ] = (
            200,
            {
                "workflow_runs": [
                    {
                        "id": WORKFLOW_RUN_ID,
                        "display_title": case00_run_marker("Q1", RUN_ID),
                    }
                ]
            },
        )
        factory, _ = _client_factory(routes)

        async def run() -> dict[str, Any]:
            return await submit_case00_direct(
                {
                    "commit_sha": VALID_SHA,
                    "benchmark_id": BENCHMARK_ID,
                    "question_id": "Q1",
                    "idempotency_key": IDEMPOTENCY_KEY,
                    "authorization_confirmed": True,
                },
                client_factory=factory,
                poll_attempts=1,
                poll_delay_seconds=0,
            )

        payload = asyncio.run(run())
        self.assertEqual(set(payload.keys()), SUCCESS_KEYS)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["run_id"], RUN_ID)
        self.assertEqual(payload["workflow_run_id"], WORKFLOW_RUN_ID)
        self.assertEqual(payload["commit_sha"], VALID_SHA)
        self.assertEqual(payload["question_id"], "Q1")
        self.assertEqual(payload["idempotency_key"], IDEMPOTENCY_KEY)

    def test_idempotent_replay_skips_redispatch(self) -> None:
        routes = self._dispatch_routes(include_existing_run=True)
        factory, transport = _client_factory(routes)

        async def run() -> dict[str, Any]:
            return await submit_case00_direct(
                {
                    "commit_sha": VALID_SHA,
                    "benchmark_id": BENCHMARK_ID,
                    "question_id": "Q1",
                    "idempotency_key": IDEMPOTENCY_KEY,
                    "authorization_confirmed": True,
                },
                client_factory=factory,
                poll_attempts=1,
                poll_delay_seconds=0,
            )

        payload = asyncio.run(run())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["question_id"], "Q1")
        post_calls = [key for key in transport.requests if key[0] == "POST"]
        self.assertEqual(post_calls, [])

    def test_invalid_input_fail_closed_before_github(self) -> None:
        routes = self._dispatch_routes()
        factory, transport = _client_factory(routes)

        async def run() -> dict[str, Any]:
            return await submit_case00_direct(
                {
                    "commit_sha": VALID_SHA,
                    "benchmark_id": BENCHMARK_ID,
                    "question_id": "Q9",
                    "idempotency_key": IDEMPOTENCY_KEY,
                    "authorization_confirmed": True,
                },
                client_factory=factory,
            )

        payload = asyncio.run(run())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], ERROR_INVALID_INPUT)
        self.assertEqual(transport.requests, [])

    def test_missing_commit_fail_closed(self) -> None:
        routes = {
            ("GET", f"/repos/{FIXED_REPOSITORY}/commits/{MISSING_SHA}"): (
                404,
                {"message": "Not Found"},
            )
        }
        factory, _ = _client_factory(routes)

        async def run() -> dict[str, Any]:
            return await submit_case00_direct(
                {
                    "commit_sha": MISSING_SHA,
                    "benchmark_id": BENCHMARK_ID,
                    "question_id": "Q1",
                    "idempotency_key": "missing-commit-key",
                    "authorization_confirmed": True,
                },
                client_factory=factory,
            )

        payload = asyncio.run(run())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], ERROR_COMMIT_NOT_FOUND)

    def test_dispatch_failure_is_bounded(self) -> None:
        routes = {
            ("GET", f"/repos/{FIXED_REPOSITORY}/commits/{VALID_SHA}"): (
                200,
                {"sha": VALID_SHA},
            ),
            (
                "POST",
                f"/repos/{FIXED_REPOSITORY}/actions/workflows/{CASE00_WORKFLOW}/dispatches",
            ): (
                500,
                {"message": "token ghp_secret leaked"},
            ),
        }
        factory, _ = _client_factory(routes)

        async def run() -> dict[str, Any]:
            return await submit_case00_direct(
                {
                    "commit_sha": VALID_SHA,
                    "benchmark_id": BENCHMARK_ID,
                    "question_id": "Q1",
                    "idempotency_key": "dispatch-fail-key",
                    "authorization_confirmed": True,
                },
                client_factory=factory,
            )

        payload = asyncio.run(run())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], ERROR_DISPATCH_FAILED)
        self.assertNotIn("ghp_secret", json.dumps(payload))


class CaseSubmitDiscoveryGateTests(unittest.TestCase):
    def test_deploy_time_contract_load_gate(self) -> None:
        contract = load_case_submit_contract()
        self.assertEqual(contract.gateway_tool, GATEWAY_TOOL)
        validate_contract_schema_document(contract.document)

    def test_contract_violation_response_is_bounded(self) -> None:
        payload = contract_violation_response(
            question_id="Q1",
            idempotency_key=IDEMPOTENCY_KEY,
            stage=ERROR_UNSUPPORTED_BENCHMARK_QUESTION,
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], ERROR_UNSUPPORTED_BENCHMARK_QUESTION)
        self.assertEqual(payload["question_id"], "Q1")


class CaseSubmitLiveSmokeTests(unittest.TestCase):
    """Post-deploy public MCP discovery (env-gated; never submits Q3)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.gateway_url = (
            os.environ.get("HAL_LEGALAI_GATEWAY_URL")
            or os.environ.get("GATEWAY_PUBLIC_URL")
            or ""
        ).rstrip("/")
        cls.gateway_token = os.environ.get("GATEWAY_LIVE_OAUTH_TOKEN", "").strip()
        cls.run_live = bool(cls.gateway_url and cls.gateway_token)

    def test_live_catalog_lists_case_submit_without_legacy_routes(self) -> None:
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
                "clientInfo": {"name": "case-submit-contract-smoke", "version": "1"},
            },
        }
        tools_list = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
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
            for legacy in LEGACY_PUBLIC_CASE_SUBMISSION_ROUTES:
                self.assertNotIn(legacy, names)

        health = httpx.get(f"{self.gateway_url}/health", timeout=30.0)
        self.assertEqual(health.status_code, 200, health.text[:500])
        registered = health.json().get("registered_tools", [])
        self.assertIn(GATEWAY_TOOL, registered)
        for legacy in LEGACY_PUBLIC_CASE_SUBMISSION_ROUTES:
            self.assertNotIn(legacy, registered)


if __name__ == "__main__":
    unittest.main()
