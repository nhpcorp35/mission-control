"""Synthetic tests for production acceptance-contract B2 / Q1 wiring.

Wholly generic fixtures only — no Case-00 content, real B2 keys, or private
benchmark hashes. Covers CLI/runtime B2 client materialization, required Q1
workflow configuration, and fail-closed pre-generation behavior.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import acceptance_contract as ac


def _load_module(name: str, relative: str):
    path = Path(__file__).resolve().parent / relative
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    repo_root = Path(__file__).resolve().parent
    if str(repo_root) not in os.sys.path:
        os.sys.path.insert(0, str(repo_root))
    os.sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


GEN = _load_module(
    "generate_attorney_feedback_candidate_wiring",
    "scripts/generate_attorney_feedback_candidate.py",
)
Q1 = _load_module("run_case00_b2_q1_wiring", "scripts/run_case00_b2_q1.py")


def _synth_object_key() -> str:
    return "Contracts/synthetic/wiring/Q-SYNTH-WIRE.acceptance_contract.json"


def _synth_identity() -> ac.ContractIdentity:
    return ac.ContractIdentity(
        benchmark_id="synth-benchmark-wiring",
        question_id="Q-SYNTH-WIRE",
    )


def _synth_contract() -> dict[str, Any]:
    return ac.build_synthetic_contract(
        contract_id="contract-synth-wiring-01",
        version="1.0.0",
        benchmark_id="synth-benchmark-wiring",
        question_id="Q-SYNTH-WIRE",
        object_key=_synth_object_key(),
        required_criterion_ids=["crit-wire-a", "crit-wire-b"],
    )


def _b2_env() -> dict[str, str]:
    return {
        "B2_KEY_ID": "key-id-secret-value",
        "B2_APPLICATION_KEY": "app-key-secret-value",
        "B2_BUCKET": "synthetic-bucket",
        "B2_ENDPOINT": "https://s3.example.test",
        "B2_REGION": "us-test-1",
    }


def _acceptance_env(doc: dict[str, Any] | None = None) -> dict[str, str]:
    contract = doc or _synth_contract()
    return {
        GEN.ACCEPTANCE_CONTRACT_OBJECT_KEY_ENV: _synth_object_key(),
        GEN.ACCEPTANCE_CONTRACT_CONTENT_SHA256_ENV: contract["content_sha256"],
        GEN.ACCEPTANCE_CONTRACT_BENCHMARK_ID_ENV: "synth-benchmark-wiring",
    }


class MaterializeB2TransportTests(unittest.TestCase):
    def test_cli_metadata_only_config_attaches_authenticated_client(self) -> None:
        doc = _synth_contract()
        config = {
            "object_key": _synth_object_key(),
            "benchmark_id": "synth-benchmark-wiring",
            "question_id": "Q-SYNTH-WIRE",
            "content_sha256": doc["content_sha256"],
        }
        fake_client = MagicMock(name="b2-client")
        with patch.object(
            GEN.rebuild_cli, "create_b2_client", return_value=fake_client
        ) as create:
            transport, err = GEN.materialize_acceptance_contract_b2_transport(
                config, environ=_b2_env()
            )
        self.assertIsNone(err)
        assert transport is not None
        self.assertIs(transport["client"], fake_client)
        self.assertEqual(transport["bucket"], "synthetic-bucket")
        self.assertIs(
            transport["call_with_retry"], GEN.rebuild_cli.call_b2_with_read_retry
        )
        create.assert_called_once()
        # Credentials must never be copied onto the load config.
        blob = repr(transport)
        self.assertNotIn("key-id-secret-value", blob)
        self.assertNotIn("app-key-secret-value", blob)
        for key in transport:
            self.assertNotIn(key, {"key_id", "application_key", "secret"})

    def test_missing_b2_env_fail_closed_without_secret_leak(self) -> None:
        config = {
            "object_key": _synth_object_key(),
            "benchmark_id": "synth-benchmark-wiring",
            "question_id": "Q-SYNTH-WIRE",
            "content_sha256": "a" * 64,
        }
        transport, err = GEN.materialize_acceptance_contract_b2_transport(
            config, environ={"B2_KEY_ID": "key-id-secret-value"}
        )
        self.assertIsNone(transport)
        self.assertEqual(err, "b2_read_error")

    def test_load_configured_uses_materialized_client_for_b2_object(self) -> None:
        doc = _synth_contract()
        raw = json.dumps(doc, sort_keys=True).encode("utf-8")
        body = MagicMock()
        body.read.return_value = raw
        fake_client = MagicMock()
        fake_client.get_object.return_value = {"Body": body}

        config = {
            "object_key": _synth_object_key(),
            "benchmark_id": "synth-benchmark-wiring",
            "question_id": "Q-SYNTH-WIRE",
            "content_sha256": doc["content_sha256"],
        }
        with patch.object(
            GEN.rebuild_cli, "create_b2_client", return_value=fake_client
        ):
            status, view, err, prov = GEN.load_configured_acceptance_contract(
                config, environ=_b2_env()
            )
        self.assertEqual(status, ac.LOAD_OK)
        self.assertIsNone(err)
        self.assertIsNotNone(view)
        fake_client.get_object.assert_called_once_with(
            Bucket="synthetic-bucket", Key=_synth_object_key()
        )
        block = prov["acceptance_contract"]
        self.assertEqual(block["load_status"], ac.LOAD_OK)
        self.assertEqual(block["object_key"], _synth_object_key())
        self.assertEqual(block["content_sha256"], doc["content_sha256"])
        # Safe provenance only — no criterion phrases / body.
        rendered = json.dumps(prov)
        self.assertNotIn("presence_phrases", rendered)
        self.assertNotIn("fallback_text", rendered)
        self.assertNotIn("key-id-secret-value", rendered)


class ResolveGeneratorConfigTests(unittest.TestCase):
    def test_resolve_from_env_pins_without_cli_key(self) -> None:
        doc = _synth_contract()
        env = {**_b2_env(), **_acceptance_env(doc)}
        config = GEN.resolve_acceptance_contract_config(
            question_id="Q-SYNTH-WIRE",
            environ=env,
        )
        assert config is not None
        self.assertEqual(config["object_key"], _synth_object_key())
        self.assertEqual(config["benchmark_id"], "synth-benchmark-wiring")
        self.assertEqual(config["question_id"], "Q-SYNTH-WIRE")
        self.assertEqual(config["content_sha256"], doc["content_sha256"])

    def test_unconfigured_returns_none(self) -> None:
        self.assertIsNone(
            GEN.resolve_acceptance_contract_config(
                question_id="Q-SYNTH-WIRE",
                environ=_b2_env(),
            )
        )

    def test_b2_path_without_sha_fail_closed_before_read(self) -> None:
        config = {
            "object_key": _synth_object_key(),
            "benchmark_id": "synth-benchmark-wiring",
            "question_id": "Q-SYNTH-WIRE",
            "content_sha256": None,
        }
        fake_client = MagicMock()
        with patch.object(
            GEN.rebuild_cli, "create_b2_client", return_value=fake_client
        ):
            status, view, err, prov = GEN.load_configured_acceptance_contract(
                config, environ=_b2_env()
            )
        self.assertEqual(status, ac.LOAD_INVALID)
        self.assertEqual(err, ac.ERROR_SCHEMA_INVALID)
        self.assertIsNone(view)
        fake_client.get_object.assert_not_called()
        self.assertEqual(
            prov["acceptance_contract"]["load_error_code"], ac.ERROR_SCHEMA_INVALID
        )


class Q1WorkflowConfigTests(unittest.TestCase):
    def test_requires_object_key_sha_and_benchmark(self) -> None:
        with self.assertRaises(Q1.AcceptanceContractConfigError) as ctx:
            Q1.resolve_production_acceptance_contract(
                question_id="Q-SYNTH-WIRE",
                environ={},
            )
        self.assertIn("object_key", ctx.exception.details["missing"])
        self.assertIn("content_sha256", ctx.exception.details["missing"])
        self.assertIn("benchmark_id", ctx.exception.details["missing"])

    def test_resolves_explicit_identities_from_env(self) -> None:
        doc = _synth_contract()
        resolved = Q1.resolve_production_acceptance_contract(
            question_id="Q-SYNTH-WIRE",
            environ=_acceptance_env(doc),
        )
        self.assertEqual(resolved["object_key"], _synth_object_key())
        self.assertEqual(resolved["content_sha256"], doc["content_sha256"])
        self.assertEqual(resolved["benchmark_id"], "synth-benchmark-wiring")
        self.assertEqual(resolved["question_id"], "Q-SYNTH-WIRE")

    def test_cli_overrides_env(self) -> None:
        doc = _synth_contract()
        resolved = Q1.resolve_production_acceptance_contract(
            question_id="Q-SYNTH-WIRE",
            object_key="Contracts/synthetic/wiring/override.json",
            content_sha256="b" * 64,
            benchmark_id="synth-benchmark-override",
            environ=_acceptance_env(doc),
        )
        self.assertEqual(
            resolved["object_key"], "Contracts/synthetic/wiring/override.json"
        )
        self.assertEqual(resolved["content_sha256"], "b" * 64)
        self.assertEqual(resolved["benchmark_id"], "synth-benchmark-override")

    def test_wrapper_main_fails_closed_when_pins_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            captured = io.StringIO()
            with patch.dict(os.environ, {}, clear=False):
                # Ensure acceptance pins are absent even if the host exported them.
                cleared = {
                    Q1.ACCEPTANCE_CONTRACT_OBJECT_KEY_ENV: "",
                    Q1.ACCEPTANCE_CONTRACT_CONTENT_SHA256_ENV: "",
                    Q1.ACCEPTANCE_CONTRACT_BENCHMARK_ID_ENV: "",
                }
                with patch.dict(os.environ, cleared, clear=False):
                    with patch("sys.stdout", captured):
                        code = Q1.main(
                            [
                                "--case-root",
                                tmp,
                                "--question-id",
                                "Q-SYNTH-WIRE",
                                "--required-commit",
                                "c" * 40,
                                "--candidate-output-root",
                                tmp,
                                "--authorization-confirmed",
                                "--generation-only",
                            ]
                        )
            self.assertEqual(code, 1)
            payload = json.loads(captured.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["phase"], "acceptance_contract")
            self.assertIn("missing", payload)


class FailClosedPreGenerationTests(unittest.TestCase):
    def test_hash_mismatch_raises_before_model_call(self) -> None:
        doc = _synth_contract()
        raw = json.dumps(doc, sort_keys=True).encode("utf-8")
        body = MagicMock()
        body.read.return_value = raw
        fake_client = MagicMock()
        fake_client.get_object.return_value = {"Body": body}

        config = {
            "object_key": _synth_object_key(),
            "benchmark_id": "synth-benchmark-wiring",
            "question_id": "Q-SYNTH-WIRE",
            "content_sha256": "f" * 64,
            "client": fake_client,
            "bucket": "synthetic-bucket",
            "call_with_retry": lambda op, **_k: op(),
        }
        model = MagicMock(side_effect=AssertionError("model must not be called"))
        with tempfile.TemporaryDirectory() as tmp:
            case_root = Path(tmp) / "case"
            case_root.mkdir()
            out = Path(tmp) / "out"
            out.mkdir()
            with self.assertRaises(GEN.GenerationError) as ctx:
                GEN.run_generation(
                    case_root=case_root,
                    question_id="Q-SYNTH-WIRE",
                    required_commit="d" * 40,
                    candidate_output_root=out,
                    authorization_acknowledgement=GEN.AUTHORIZATION_ACK,
                    generation_only=True,
                    skip_commit_check=True,
                    model_call=model,
                    acceptance_contract_config=config,
                )
        self.assertEqual(
            ctx.exception.details.get("acceptance_contract_load_status"),
            ac.LOAD_INVALID,
        )
        self.assertEqual(
            ctx.exception.details.get("acceptance_contract_error_code"),
            ac.ERROR_HASH_MISMATCH,
        )
        model.assert_not_called()
        # Provenance must stay safe.
        prov = ctx.exception.details.get("acceptance_contract") or {}
        rendered = json.dumps(prov)
        self.assertNotIn("crit-wire-a", rendered)
        self.assertNotIn("presence_phrases", rendered)

    def test_identity_mismatch_raises_before_model_call(self) -> None:
        doc = _synth_contract()
        config = {
            "object_key": _synth_object_key(),
            "benchmark_id": "synth-benchmark-OTHER",
            "question_id": "Q-SYNTH-WIRE",
            "content_sha256": doc["content_sha256"],
            "raw_bytes": json.dumps(doc, sort_keys=True).encode("utf-8"),
        }
        model = MagicMock(side_effect=AssertionError("model must not be called"))
        with tempfile.TemporaryDirectory() as tmp:
            case_root = Path(tmp) / "case"
            case_root.mkdir()
            out = Path(tmp) / "out"
            out.mkdir()
            with self.assertRaises(GEN.GenerationError) as ctx:
                GEN.run_generation(
                    case_root=case_root,
                    question_id="Q-SYNTH-WIRE",
                    required_commit="e" * 40,
                    candidate_output_root=out,
                    authorization_acknowledgement=GEN.AUTHORIZATION_ACK,
                    generation_only=True,
                    skip_commit_check=True,
                    model_call=model,
                    acceptance_contract_config=config,
                )
        self.assertEqual(
            ctx.exception.details.get("acceptance_contract_error_code"),
            ac.ERROR_IDENTITY_MISMATCH,
        )
        model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
