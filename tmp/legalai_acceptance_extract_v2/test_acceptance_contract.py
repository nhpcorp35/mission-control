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


def _load_view(doc: dict[str, Any] | None = None) -> ac.ContractEvaluationView:
    document = doc or _valid_contract()
    raw = json.dumps(document, sort_keys=True).encode("utf-8")
    result = ac.load_acceptance_contract_from_bytes(
        raw,
        object_key=_object_key(),
        expected_identity=_identity(),
        expected_content_sha256=document["content_sha256"],
    )
    assert result.ok and result.evaluation is not None
    return result.evaluation


def _answer_covering(view: ac.ContractEvaluationView, *, omit: str | None = None) -> str:
    parts: list[str] = []
    for spec in view.criteria:
        if omit and spec.id == omit:
            continue
        chunk = " ".join(
            list(spec.presence_phrases)
            + list(spec.evidence_phrases)
            + list(spec.semantic_required_phrases)
        )
        parts.append(chunk + ".")
    return " ".join(parts)


class Phase2ValidationTests(unittest.TestCase):
    def test_complete_pass(self) -> None:
        view = _load_view()
        answer = _answer_covering(view)
        result = ac.validate_final_answer_against_contract(answer, view)
        self.assertTrue(result.ok)
        self.assertEqual(
            {c.result_code for c in result.criterion_results}, {ac.CRIT_PASS}
        )
        for c in result.criterion_results:
            self.assertEqual(c.presence, ac.PRESENCE_PRESENT)
            self.assertEqual(c.evidence, ac.EVIDENCE_SUPPORTED)
            self.assertEqual(c.semantic, ac.SEMANTIC_PRESERVED)
        self.assertIn(result.duplication_result, {ac.DUP_OK, ac.DUP_REPAIRED})

    def test_missing_criterion_fail(self) -> None:
        view = _load_view()
        omit = view.required_criterion_ids[0]
        # Answer covers others but omits one; disable fallback so absence sticks.
        answer = _answer_covering(view, omit=omit)
        result = ac.validate_final_answer_against_contract(
            answer, view, apply_fallback=False
        )
        self.assertFalse(result.ok)
        by_id = {c.criterion_id: c for c in result.criterion_results}
        self.assertEqual(by_id[omit].result_code, ac.CRIT_FAIL_MISSING)
        self.assertEqual(by_id[omit].presence, ac.PRESENCE_ABSENT)

    def test_unsupported_criterion_fail(self) -> None:
        view = _load_view()
        # Presence + semantic tokens only — omit evidence phrases.
        parts: list[str] = []
        for spec in view.criteria:
            parts.append(
                " ".join(
                    list(spec.presence_phrases) + list(spec.semantic_required_phrases)
                )
                + "."
            )
        result = ac.validate_final_answer_against_contract(
            " ".join(parts), view, apply_fallback=False
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            all(c.result_code == ac.CRIT_FAIL_UNSUPPORTED for c in result.criterion_results)
        )
        self.assertTrue(
            all(c.evidence == ac.EVIDENCE_UNSUPPORTED for c in result.criterion_results)
        )


class Phase2FallbackAndDuplicationTests(unittest.TestCase):
    def test_equivalent_fallback_not_duplicated(self) -> None:
        view = _load_view()
        spec = view.criteria[0]
        # Seed answer with equivalent fallback prose already present.
        seeded = spec.fallback_text.strip()
        out, actions = ac.apply_idempotent_contract_fallback(
            seeded, view, missing_ids=[spec.id]
        )
        self.assertEqual(actions[spec.id], ac.FALLBACK_SKIPPED_EQUIVALENT)
        self.assertEqual(out.count(spec.fallback_text.strip()), 1)

    def test_missing_fallback_inserted_exactly_once(self) -> None:
        view = _load_view()
        spec = view.criteria[0]
        out, actions = ac.apply_idempotent_contract_fallback(
            "Unrelated preamble.", view, missing_ids=[spec.id]
        )
        self.assertEqual(actions[spec.id], ac.FALLBACK_INSERTED)
        self.assertEqual(out.count(spec.fallback_text.strip()), 1)
        # Idempotent second application.
        out2, actions2 = ac.apply_idempotent_contract_fallback(
            out, view, missing_ids=[spec.id]
        )
        self.assertEqual(actions2[spec.id], ac.FALLBACK_SKIPPED_EQUIVALENT)
        self.assertEqual(out2.count(spec.fallback_text.strip()), 1)

    def test_remaining_duplication_repairs_or_fails(self) -> None:
        view = _load_view()
        # Near-identical sentences should repair by dropping duplicates.
        prose = (
            "Alpha synthetic clause about venue bearing on service. "
            "Alpha synthetic clause about venue bearing on service. "
            "Distinct closing remark for the record."
        )
        repaired, status, diags = ac.apply_duplication_gate(
            prose, view.duplication_rules, repair=True
        )
        self.assertIn(status, {ac.DUP_REPAIRED, ac.DUP_FAIL})
        if status == ac.DUP_REPAIRED:
            self.assertLess(
                repaired.lower().count("alpha synthetic clause"),
                prose.lower().count("alpha synthetic clause"),
            )
        else:
            self.assertTrue(diags)

        # Irreducible duplication with repair disabled fails closed.
        _, status2, diags2 = ac.apply_duplication_gate(
            prose, view.duplication_rules, repair=False
        )
        self.assertEqual(status2, ac.DUP_FAIL)
        self.assertTrue(diags2)


class Phase2StructureAndProvenanceTests(unittest.TestCase):
    def test_structure_range_retained(self) -> None:
        import complaint_structure as cs

        doc = _valid_contract()
        doc["structure_requirements"] = {
            "required_kinds": ["factual_layout", "overview"],
            "required_ranges": [
                {"kind": "factual_layout", "start": 40, "end": 55, "category": "roadmap"},
                {"kind": "overview", "start": 1, "end": 3},
            ],
            "required_categories": ["complaint_roadmap"],
        }
        doc["content_sha256"] = ac.compute_content_sha256(doc)
        view = _load_view(doc)
        # Existing context without the factual_layout range.
        base = {
            "note": "synthetic",
            "schema_version": cs.SCHEMA_VERSION,
            "documents": [
                {
                    "document_id": "synth-doc",
                    "sections": [
                        {
                            "heading": "Overview",
                            "kind": "overview",
                            "paragraph_numbers": [1, 2, 3],
                            "paragraph_range": {
                                "start": 1,
                                "end": 3,
                                "contiguous": True,
                            },
                            "page_ids": [],
                            "page_numbers": [],
                            "uncertainty": [],
                            "provenance": {},
                        }
                    ],
                }
            ],
        }
        merged = cs.merge_contract_structure_requirements(
            base, view.structure_requirements.as_safe_dict()
        )
        assert merged is not None
        sections = merged["documents"][0]["sections"]
        kinds = [s.get("kind") for s in sections]
        self.assertIn("factual_layout", kinds)
        factual = next(s for s in sections if s.get("kind") == "factual_layout")
        self.assertEqual(factual["paragraph_range"]["start"], 40)
        self.assertEqual(factual["paragraph_range"]["end"], 55)
        self.assertIn("complaint_roadmap", merged["contract_required_categories"])

    def test_audit_manifest_provenance(self) -> None:
        view = _load_view()
        answer = _answer_covering(view)
        validation = ac.validate_final_answer_against_contract(answer, view)
        prov = ac.safe_provenance_record(
            load_status=ac.LOAD_OK, view=view, validation=validation
        )
        block = prov["acceptance_contract"]
        self.assertEqual(block["contract_id"], "contract-synth-alpha-q01")
        self.assertEqual(block["version"], "1.0.0")
        self.assertEqual(block["object_key"], _object_key())
        self.assertEqual(block["content_sha256"], view.content_sha256)
        self.assertEqual(block["load_status"], ac.LOAD_OK)
        self.assertTrue(block["validation_ok"])
        self.assertEqual(len(block["criterion_results"]), 3)
        self.assertIn(block["duplication_result"], {ac.DUP_OK, ac.DUP_REPAIRED})
        # Never embed private criterion prose / fallback text.
        blob = json.dumps(prov)
        for spec in view.criteria:
            if spec.fallback_text:
                self.assertNotIn(spec.fallback_text, blob)
            for phrase in spec.presence_phrases:
                self.assertNotIn(phrase, blob)

    def test_loader_evaluation_repr_excludes_prose(self) -> None:
        view = _load_view()
        rendered = repr(view)
        for spec in view.criteria:
            self.assertNotIn(spec.fallback_text, rendered)
            for phrase in spec.presence_phrases:
                self.assertNotIn(phrase, rendered)


if __name__ == "__main__":
    unittest.main()
