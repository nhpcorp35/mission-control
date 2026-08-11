"""Synthetic regression tests for acceptance-contract schema + private B2 loader.

Uses wholly generic fixtures only — no Case-00 / private benchmark content.
"""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

import acceptance_contract as ac
from acceptance_contract.loader import (
 ERROR_B2_READ,
 fetch_b2_object_bytes,
)


def _identity() -> ac.ContractIdentity:
 return ac.ContractIdentity(
 benchmark_id="synth-benchmark-alpha",
 question_id="Q-SYNTH-01",
 )


def _object_key() -> str:
 return "Contracts/synthetic/alpha/Q-SYNTH-01.acceptance_contract.json"


def _valid_contract() -> dict[str, Any]:
 return ac.build_synthetic_contract(
 contract_id="contract-synth-alpha-q01",
 version="1.0.0",
 benchmark_id="synth-benchmark-alpha",
 question_id="Q-SYNTH-01",
 object_key=_object_key(),
 required_criterion_ids=["crit-presence", "crit-negation", "crit-roles"],
 )


def _client_error(http_status: int, code: str, operation: str = "GetObject") -> ClientError:
 return ClientError(
 {
 "Error": {"Code": code, "Message": f"status {http_status}"},
 "ResponseMetadata": {"HTTPStatusCode": http_status, "HTTPHeaders": {}},
 },
 operation,
 )


class SchemaValidationTests(unittest.TestCase):
 def test_schema_version_constant(self) -> None:
 self.assertEqual(ac.SCHEMA_VERSION, "acceptance_contract.v1")
 self.assertIn(ac.SCHEMA_VERSION, ac.SUPPORTED_SCHEMA_VERSIONS)

 def test_valid_document_has_no_diagnostics(self) -> None:
 doc = _valid_contract()
 self.assertEqual(ac.validate_acceptance_contract_schema(doc), [])

 def test_malformed_schema_extra_property(self) -> None:
 doc = _valid_contract()
 doc["secret_prose"] = "must-not-appear-in-diagnostics"
 diags = ac.validate_acceptance_contract_schema(doc)
 self.assertTrue(diags)
 joined = " ".join(diags)
 self.assertIn("unexpected properties", joined)
 # Property names may appear; values must never appear in diagnostics.
 self.assertNotIn("must-not-appear-in-diagnostics", joined)


class LoaderBytesTests(unittest.TestCase):
 def test_valid_load_returns_safe_metadata_only(self) -> None:
 doc = _valid_contract()
 raw = json.dumps(doc, sort_keys=True).encode("utf-8")
 result = ac.load_acceptance_contract_from_bytes(
 raw,
 object_key=_object_key(),
 expected_identity=_identity(),
 expected_content_sha256=doc["content_sha256"],
 )
 self.assertTrue(result.ok)
 self.assertIsNone(result.error_code)
 self.assertIsNotNone(result.metadata)
 assert result.metadata is not None
 self.assertEqual(result.metadata.contract_id, "contract-synth-alpha-q01")
 self.assertEqual(result.metadata.version, "1.0.0")
 self.assertEqual(result.metadata.schema_version, ac.SCHEMA_VERSION)
 self.assertEqual(result.metadata.benchmark_id, "synth-benchmark-alpha")
 self.assertEqual(result.metadata.question_id, "Q-SYNTH-01")
 self.assertEqual(
 list(result.metadata.required_criterion_ids),
 ["crit-presence", "crit-negation", "crit-roles"],
 )
 self.assertEqual(result.metadata.object_key, _object_key())
 self.assertEqual(result.metadata.content_sha256, doc["content_sha256"])
 # Result / repr must not embed full document JSON body.
 rendered = repr(result)
 self.assertNotIn("allowed_source_types", rendered)
 self.assertNotIn('"semantic_preservation"', rendered)

 def test_malformed_json(self) -> None:
 result = ac.load_acceptance_contract_from_bytes(
 b"{not-json",
 object_key=_object_key(),
 expected_identity=_identity(),
 )
 self.assertFalse(result.ok)
 self.assertEqual(result.error_code, ac.ERROR_MALFORMED_JSON)
 self.assertTrue(result.diagnostics)

 def test_missing_version_fail_closed(self) -> None:
 doc = _valid_contract()
 del doc["version"]
 del doc["schema_version"]
 raw = json.dumps(doc).encode("utf-8")
 result = ac.load_acceptance_contract_from_bytes(
 raw,
 object_key=_object_key(),
 expected_identity=_identity(),
 )
 self.assertFalse(result.ok)
 self.assertEqual(result.error_code, ac.ERROR_MISSING_VERSION)

 def test_unsupported_schema_version(self) -> None:
 doc = _valid_contract()
 doc["schema_version"] = "acceptance_contract.v0-legacy"
 doc["content_sha256"] = ac.compute_content_sha256(doc)
 raw = json.dumps(doc).encode("utf-8")
 result = ac.load_acceptance_contract_from_bytes(
 raw,
 object_key=_object_key(),
 expected_identity=_identity(),
 )
 self.assertFalse(result.ok)
 self.assertEqual(result.error_code, ac.ERROR_UNSUPPORTED_VERSION)

 def test_schema_invalid_missing_required_field(self) -> None:
 doc = _valid_contract()
 del doc["duplication_rules"]
 doc["content_sha256"] = ac.compute_content_sha256(doc)
 raw = json.dumps(doc).encode("utf-8")
 result = ac.load_acceptance_contract_from_bytes(
 raw,
 object_key=_object_key(),
 expected_identity=_identity(),
 )
 self.assertFalse(result.ok)
 self.assertEqual(result.error_code, ac.ERROR_SCHEMA_INVALID)

 def test_identity_mismatch(self) -> None:
 doc = _valid_contract()
 raw = json.dumps(doc).encode("utf-8")
 result = ac.load_acceptance_contract_from_bytes(
 raw,
 object_key=_object_key(),
 expected_identity=ac.ContractIdentity(
 benchmark_id="synth-benchmark-alpha",
 question_id="Q-OTHER",
 ),
 )
 self.assertFalse(result.ok)
 self.assertEqual(result.error_code, ac.ERROR_IDENTITY_MISMATCH)

 def test_hash_mismatch_declared(self) -> None:
 doc = _valid_contract()
 doc["content_sha256"] = "0" * 64
 raw = json.dumps(doc).encode("utf-8")
 result = ac.load_acceptance_contract_from_bytes(
 raw,
 object_key=_object_key(),
 expected_identity=_identity(),
 )
 self.assertFalse(result.ok)
 self.assertEqual(result.error_code, ac.ERROR_HASH_MISMATCH)
 self.assertIsNotNone(result.computed_content_sha256)

 def test_hash_mismatch_expected_provenance(self) -> None:
 doc = _valid_contract()
 raw = json.dumps(doc).encode("utf-8")
 result = ac.load_acceptance_contract_from_bytes(
 raw,
 object_key=_object_key(),
 expected_identity=_identity(),
 expected_content_sha256="f" * 64,
 )
 self.assertFalse(result.ok)
 self.assertEqual(result.error_code, ac.ERROR_HASH_MISMATCH)

 def test_object_key_mismatch(self) -> None:
 doc = _valid_contract()
 raw = json.dumps(doc).encode("utf-8")
 result = ac.load_acceptance_contract_from_bytes(
 raw,
 object_key="Contracts/synthetic/other/key.json",
 expected_identity=_identity(),
 )
 self.assertFalse(result.ok)
 self.assertEqual(result.error_code, ac.ERROR_OBJECT_KEY_MISMATCH)


class LoaderB2Tests(unittest.TestCase):
 def test_valid_b2_load(self) -> None:
 doc = _valid_contract()
 raw = json.dumps(doc, sort_keys=True).encode("utf-8")
 body = MagicMock()
 body.read.return_value = raw
 client = MagicMock()
 client.get_object.return_value = {"Body": body}

 result = ac.load_acceptance_contract_from_b2(
 client=client,
 bucket="synthetic-bucket",
 object_key=_object_key(),
 expected_identity=_identity(),
 expected_content_sha256=doc["content_sha256"],
 call_with_retry=lambda op, **_kwargs: op(),
 )
 self.assertTrue(result.ok)
 self.assertIsNotNone(result.metadata)
 client.get_object.assert_called_once_with(
 Bucket="synthetic-bucket", Key=_object_key()
 )

 def test_missing_object(self) -> None:
 client = MagicMock()
 client.get_object.side_effect = _client_error(404, "NoSuchKey")
 result = ac.load_acceptance_contract_from_b2(
 client=client,
 bucket="synthetic-bucket",
 object_key=_object_key(),
 expected_identity=_identity(),
 call_with_retry=lambda op, **_kwargs: op(),
 )
 self.assertFalse(result.ok)
 self.assertEqual(result.error_code, ac.ERROR_MISSING_OBJECT)
 self.assertIsNone(result.metadata)

 def test_b2_permanent_error_fail_closed(self) -> None:
 client = MagicMock()
 client.get_object.side_effect = _client_error(403, "AccessDenied")
 result = ac.load_acceptance_contract_from_b2(
 client=client,
 bucket="synthetic-bucket",
 object_key=_object_key(),
 expected_identity=_identity(),
 call_with_retry=lambda op, **_kwargs: op(),
 )
 self.assertFalse(result.ok)
 self.assertEqual(result.error_code, ERROR_B2_READ)

 def test_fetch_bytes_uses_retry_helper(self) -> None:
 doc = _valid_contract()
 raw = json.dumps(doc).encode("utf-8")
 body = MagicMock()
 body.read.return_value = raw
 client = MagicMock()
 client.get_object.return_value = {"Body": body}
 calls = {"n": 0}

 def retry(op, **_kwargs):
 calls["n"] += 1
 return op()

 data = fetch_b2_object_bytes(
 client, "synthetic-bucket", _object_key(), call_with_retry=retry
 )
 self.assertEqual(data, raw)
 self.assertEqual(calls["n"], 1)

 def test_error_repr_excludes_body_details(self) -> None:
 err = ac.AcceptanceContractError(
 "boom",
 error_code=ac.ERROR_MALFORMED_JSON,
 object_key=_object_key(),
 diagnostics=["json decode failed at line 1"],
 body={"secret": "nope"},
 content="private-text",
 )
 rendered = repr(err)
 self.assertNotIn("private-text", rendered)
 self.assertNotIn("nope", rendered)
 self.assertNotIn("secret", rendered)
 self.assertEqual(err.details, {})


if __name__ == "__main__":
 unittest.main()
