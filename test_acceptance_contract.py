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
        # Evidence phrases must already be present; fallback may only add framing.
        seeded = f"Unrelated preamble with evidence:{spec.id}."
        out, actions = ac.apply_idempotent_contract_fallback(
            seeded, view, missing_ids=[spec.id]
        )
        self.assertEqual(actions[spec.id], ac.FALLBACK_INSERTED)
        self.assertEqual(out.count(spec.fallback_text.strip()), 1)
        # Idempotent second application.
        out2, actions2 = ac.apply_idempotent_contract_fallback(
            out, view, missing_ids=[spec.id]
        )
        self.assertEqual(actions2[spec.id], ac.FALLBACK_SKIPPED_EQUIVALENT)
        self.assertEqual(out2.count(spec.fallback_text.strip()), 1)

    def test_fallback_skips_when_evidence_unsupported(self) -> None:
        view = _load_view()
        spec = view.criteria[0]
        # Presence missing and evidence phrases absent — fail closed, no insert.
        out, actions = ac.apply_idempotent_contract_fallback(
            "Unrelated preamble without evidence linkage.",
            view,
            missing_ids=[spec.id],
        )
        self.assertEqual(actions[spec.id], ac.FALLBACK_SKIPPED_UNSUPPORTED)
        self.assertNotIn(spec.fallback_text.strip(), out)
        result = ac.validate_final_answer_against_contract(
            "Unrelated preamble without evidence linkage.",
            view,
            apply_fallback=True,
        )
        self.assertFalse(result.ok)
        self.assertEqual(
            result.fallback_actions.get(spec.id), ac.FALLBACK_SKIPPED_UNSUPPORTED
        )
        by_id = {c.criterion_id: c for c in result.criterion_results}
        self.assertEqual(by_id[spec.id].result_code, ac.CRIT_FAIL_MISSING)

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


# ---------------------------------------------------------------------------
# Synthetic Q2-shaped relief criteria (no private Case-00 contract/source text)
# ---------------------------------------------------------------------------

_Q2_CRIT_RESCISSION = "q2-rescission-void-ab-initio"
_Q2_CRIT_NO_DEFENSE = "q2-no-defense-or-indemnity"
_Q2_CRIT_PLEADED = "q2-pleaded-relief-not-adjudication"
_Q2_CRIT_CATCH_ALL = "q2-catch-all-relief"


def _q2_shaped_contract() -> dict[str, Any]:
    """Wholly synthetic Q2-shaped criteria — mirrors ids/shapes, not private prose."""
    return ac.build_synthetic_contract(
        contract_id="contract-synth-q2-relief",
        version="1.0.0",
        benchmark_id="synth-benchmark-q2",
        question_id="Q2",
        object_key="Contracts/synthetic/q2/Q2.acceptance_contract.json",
        required_criterion_ids=[
            _Q2_CRIT_RESCISSION,
            _Q2_CRIT_NO_DEFENSE,
            _Q2_CRIT_PLEADED,
            _Q2_CRIT_CATCH_ALL,
        ],
        criteria=[
            {
                "id": _Q2_CRIT_RESCISSION,
                "presence_phrases": ["rescission", "void ab initio"],
                "evidence_phrases": ["synth wherefore void ab initio excerpt"],
                "semantic_required_phrases": [],
                "semantic_forbidden_phrases": [],
                "fallback_text": (
                    "Fallback rescission and void ab initio framing with "
                    "synth wherefore void ab initio excerpt."
                ),
                "category": "relief",
            },
            {
                "id": _Q2_CRIT_NO_DEFENSE,
                "presence_phrases": ["no defense or indemnity"],
                "evidence_phrases": ["synth no duty to defend or indemnify excerpt"],
                "semantic_required_phrases": [],
                "semantic_forbidden_phrases": [],
                "fallback_text": (
                    "Fallback no defense or indemnity framing with "
                    "synth no duty to defend or indemnify excerpt."
                ),
                "category": "relief",
            },
            {
                "id": _Q2_CRIT_PLEADED,
                "presence_phrases": [
                    "pleaded requested relief",
                    "not a judicial determination",
                ],
                "evidence_phrases": [],
                "semantic_required_phrases": ["pleaded"],
                "semantic_forbidden_phrases": [
                    "court has ruled",
                    "established entitlement",
                ],
                "fallback_text": (
                    "This answer describes pleaded requested relief in the "
                    "complaint, not a judicial determination."
                ),
                "category": "relief",
            },
            {
                "id": _Q2_CRIT_CATCH_ALL,
                "presence_phrases": ["such other and further relief"],
                "evidence_phrases": ["synth such other and further relief excerpt"],
                "semantic_required_phrases": [],
                "semantic_forbidden_phrases": [],
                "fallback_text": (
                    "Fallback catch-all framing with "
                    "synth such other and further relief excerpt."
                ),
                "category": "relief",
            },
        ],
    )


def _q2_view() -> ac.ContractEvaluationView:
    doc = _q2_shaped_contract()
    raw = json.dumps(doc, sort_keys=True).encode("utf-8")
    loaded = ac.load_acceptance_contract_from_bytes(
        raw,
        object_key=doc["object_key"],
        expected_identity=ac.ContractIdentity(
            benchmark_id="synth-benchmark-q2",
            question_id="Q2",
        ),
        expected_content_sha256=doc["content_sha256"],
    )
    assert loaded.ok and loaded.evaluation is not None
    return loaded.evaluation


def _q2_grounded_answer() -> str:
    return (
        "This answer describes pleaded requested relief in the complaint, "
        "not a judicial determination. "
        "The complaint requests rescission and void ab initio treatment "
        "(synth wherefore void ab initio excerpt). "
        "It also seeks no defense or indemnity "
        "(synth no duty to defend or indemnify excerpt). "
        "The WHEREFORE includes such other and further relief "
        "(synth such other and further relief excerpt)."
    )


class Q2ShapedReliefCriterionTests(unittest.TestCase):
    def test_all_four_criteria_pass_when_evidence_linked(self) -> None:
        view = _q2_view()
        result = ac.validate_final_answer_against_contract(
            _q2_grounded_answer(), view, apply_fallback=True
        )
        self.assertTrue(result.ok)
        by_id = {c.criterion_id: c for c in result.criterion_results}
        for cid in (
            _Q2_CRIT_RESCISSION,
            _Q2_CRIT_NO_DEFENSE,
            _Q2_CRIT_PLEADED,
            _Q2_CRIT_CATCH_ALL,
        ):
            self.assertEqual(by_id[cid].result_code, ac.CRIT_PASS)
            self.assertEqual(by_id[cid].evidence, ac.EVIDENCE_SUPPORTED)

    def test_presence_without_evidence_is_unsupported(self) -> None:
        view = _q2_view()
        # Mentions relief concepts but omits cited evidence phrases.
        answer = (
            "This answer describes pleaded requested relief in the complaint, "
            "not a judicial determination. "
            "Plaintiff seeks rescission and void ab initio treatment plus "
            "no defense or indemnity and such other and further relief."
        )
        result = ac.validate_final_answer_against_contract(
            answer, view, apply_fallback=True
        )
        self.assertFalse(result.ok)
        by_id = {c.criterion_id: c for c in result.criterion_results}
        self.assertEqual(by_id[_Q2_CRIT_RESCISSION].result_code, ac.CRIT_FAIL_UNSUPPORTED)
        self.assertEqual(by_id[_Q2_CRIT_NO_DEFENSE].result_code, ac.CRIT_FAIL_UNSUPPORTED)
        self.assertEqual(by_id[_Q2_CRIT_CATCH_ALL].result_code, ac.CRIT_FAIL_UNSUPPORTED)
        # Pleaded distinction has no evidence phrases — presence alone passes.
        self.assertEqual(by_id[_Q2_CRIT_PLEADED].result_code, ac.CRIT_PASS)

    def test_pleaded_versus_adjudicated_language_required(self) -> None:
        view = _q2_view()
        answer = (
            "The complaint requests rescission and void ab initio "
            "(synth wherefore void ab initio excerpt). "
            "It seeks no defense or indemnity "
            "(synth no duty to defend or indemnify excerpt). "
            "It includes such other and further relief "
            "(synth such other and further relief excerpt)."
        )
        result = ac.validate_final_answer_against_contract(
            answer, view, apply_fallback=False
        )
        self.assertFalse(result.ok)
        by_id = {c.criterion_id: c for c in result.criterion_results}
        self.assertEqual(by_id[_Q2_CRIT_PLEADED].result_code, ac.CRIT_FAIL_MISSING)
        self.assertEqual(by_id[_Q2_CRIT_PLEADED].presence, ac.PRESENCE_ABSENT)

    def test_fallback_inserts_pleaded_distinction_when_evidence_ready(self) -> None:
        view = _q2_view()
        # Evidence-linked relief present; pleaded distinction missing → fallback ok.
        answer = (
            "The complaint requests rescission and void ab initio "
            "(synth wherefore void ab initio excerpt). "
            "It seeks no defense or indemnity "
            "(synth no duty to defend or indemnify excerpt). "
            "It includes such other and further relief "
            "(synth such other and further relief excerpt)."
        )
        result = ac.validate_final_answer_against_contract(
            answer, view, apply_fallback=True
        )
        self.assertTrue(result.ok)
        self.assertEqual(
            result.fallback_actions.get(_Q2_CRIT_PLEADED), ac.FALLBACK_INSERTED
        )
        self.assertIn("not a judicial determination", result.final_answer.lower())
        self.assertIn("pleaded", result.final_answer.lower())

    def test_fallback_cannot_manufacture_unsupported_relief_claims(self) -> None:
        view = _q2_view()
        # Missing rescission entirely (no presence, no evidence) — must not insert.
        answer = (
            "This answer describes pleaded requested relief in the complaint, "
            "not a judicial determination. "
            "It seeks no defense or indemnity "
            "(synth no duty to defend or indemnify excerpt). "
            "It includes such other and further relief "
            "(synth such other and further relief excerpt)."
        )
        result = ac.validate_final_answer_against_contract(
            answer, view, apply_fallback=True
        )
        self.assertFalse(result.ok)
        self.assertEqual(
            result.fallback_actions.get(_Q2_CRIT_RESCISSION),
            ac.FALLBACK_SKIPPED_UNSUPPORTED,
        )
        by_id = {c.criterion_id: c for c in result.criterion_results}
        self.assertEqual(by_id[_Q2_CRIT_RESCISSION].result_code, ac.CRIT_FAIL_MISSING)
        # Fallback prose must not appear when support was absent.
        rescission_spec = view.criterion_by_id()[_Q2_CRIT_RESCISSION]
        self.assertNotIn(rescission_spec.fallback_text.strip(), result.final_answer)


class Q2ReliefSynthesisAssemblyTests(unittest.TestCase):
    """Evidence-grounded relief synthesis (synthetic complaint excerpts only)."""

    def _packet(self, excerpt: str) -> dict:
        return {
            "question": "What relief is requested in the WHEREFORE clause?",
            "retrieval_hit_count": 1,
            "retrieval_hits": [
                {
                    "result_id": "hit-wherefore",
                    "page_id": "nyscef-900-page-0004",
                    "nyscef_document_number": 900,
                    "pdf_page": 4,
                    "document_type": "complaint",
                    "excerpt": excerpt,
                    "classifications": ["legal_position"],
                }
            ],
        }

    def test_synthesis_grounds_supported_categories_and_pleaded_distinction(self) -> None:
        from engines import drafting_engine as de

        excerpt = (
            "WHEREFORE Plaintiff demands judgment declaring the policy void ab initio "
            "and for rescission of the same; declaring that there is no duty to defend "
            "or indemnify Defendants; and for such other and further relief as the "
            "Court deems just and proper."
        )
        packet = self._packet(excerpt)
        self.assertTrue(
            de.detect_relief_question_intent(packet["question"])
        )
        supported = de.extract_supported_complaint_relief(packet)
        self.assertTrue(supported["rescission_void_ab_initio"]["supported"])
        self.assertTrue(supported["no_defense_or_indemnity"]["supported"])
        self.assertTrue(supported["catch_all_relief"]["supported"])

        assembled = de.apply_evidence_grounded_relief_synthesis(
            {
                "proposed_answer": "Partial draft omitting required relief detail.",
                "propositions": [],
                "audit": {},
            },
            packet,
        )
        answer = assembled["proposed_answer"].lower()
        self.assertIn("not a judicial determination", answer)
        self.assertIn("pleaded", answer)
        self.assertIn("pleaded requested relief", answer)
        self.assertIn("void ab initio", answer)
        self.assertIn("rescission", answer)
        self.assertIn("no defense or indemnity", answer)
        self.assertIn("catch-all", answer)
        # Evidence snippets from the complaint must remain linked.
        self.assertIn("void ab initio", assembled["proposed_answer"])
        self.assertTrue(assembled["audit"].get("relief_synthesis_applied"))
        cats = set(assembled["audit"].get("relief_supported_categories") or [])
        self.assertEqual(
            cats,
            {
                "rescission_void_ab_initio",
                "no_defense_or_indemnity",
                "catch_all_relief",
            },
        )

    def test_synthesis_rejects_absent_support(self) -> None:
        from engines import drafting_engine as de

        packet = self._packet(
            "WHEREFORE Plaintiff demands costs and disbursements of this action."
        )
        supported = de.extract_supported_complaint_relief(packet)
        self.assertFalse(supported["rescission_void_ab_initio"]["supported"])
        self.assertFalse(supported["no_defense_or_indemnity"]["supported"])
        self.assertFalse(supported["catch_all_relief"]["supported"])

        assembled = de.apply_evidence_grounded_relief_synthesis(
            {
                "proposed_answer": "No supported relief categories in this excerpt.",
                "propositions": [],
                "audit": {},
            },
            packet,
        )
        answer = assembled["proposed_answer"].lower()
        self.assertNotIn("void ab initio", answer)
        self.assertNotIn("no defense or indemnity", answer)
        self.assertFalse(assembled["audit"].get("relief_synthesis_applied"))


class Q2ReliefRoutingProductionShapedTests(unittest.TestCase):
    """
    Production-shaped routing: truncated retrieval is repaired via structure-
    backed WHEREFORE page selection so synthesis sees cited relief records.
    Synthetic non-private evidence only.
    """

    QUESTION = (
        "What relief does the complaint request in the WHEREFORE / "
        "requested-relief section?"
    )

    _WHEREFORE_TEXT = (
        "WHEREFORE\n"
        "Plaintiff demands judgment: (a) declaring the subject "
        "coverage void ab initio and for rescission; (b) declaring that there "
        "is no duty to defend or indemnify the named defendants under the "
        "policy; and (c) awarding such other and further relief as the Court "
        "deems just and proper."
    )

    def _synthetic_corpus(self, *, include_wherefore: bool = True) -> tuple:
        import complaint_structure as cs
        from engines import drafting_engine as de

        noise = (
            "INTRODUCTION\n"
            "1. This is a synthetic coverage dispute pleading.\n"
            "PARTIES\n"
            "2. Plaintiff Synthetic Carrier LLC is a domestic company.\n"
            "3. Defendant Harbor Logistics Inc. is a domestic corporation.\n"
            "FACTS\n"
            "4. The parties dispute coverage under a commercial policy.\n"
        )
        wherefore_page = (
            self._WHEREFORE_TEXT
            if include_wherefore
            else (
                "WHEREFORE\n"
                "Plaintiff demands costs and disbursements only."
            )
        )
        pages = [
            {
                "nyscef_document_number": 940,
                "page_number": 1,
                "page_id": "nyscef-940-page-0001",
                "text": noise,
                "document_type": "complaint",
                "document_classification": "complaint",
                "source_filename": "synth_complaint_940.pdf",
            },
            {
                "nyscef_document_number": 940,
                "page_number": 2,
                "page_id": "nyscef-940-page-0002",
                "text": wherefore_page,
                "document_type": "complaint",
                "document_classification": "complaint",
                "source_filename": "synth_complaint_940.pdf",
            },
            {
                "nyscef_document_number": 941,
                "page_number": 1,
                "page_id": "nyscef-941-page-0001",
                "text": (
                    "STIPULATION unrelated to requested relief or WHEREFORE demands."
                ),
                "document_type": "stipulation",
                "document_classification": "stipulation",
                "source_filename": "synth_stip_941.pdf",
            },
        ]
        structure_map = cs.build_complaint_structure_map({"pages": pages})
        documents = [
            {
                "filename": "synth_complaint_940.pdf",
                "nyscef_document_number": 940,
                "type": "complaint",
                "document_type": "complaint",
                "pages": [p for p in pages if p["nyscef_document_number"] == 940],
            },
            {
                "filename": "synth_stip_941.pdf",
                "nyscef_document_number": 941,
                "type": "stipulation",
                "document_type": "stipulation",
                "pages": [p for p in pages if p["nyscef_document_number"] == 941],
            },
        ]
        # Simulate ordinary retrieval that missed the WHEREFORE page and only
        # returned a truncated intro snippet plus unrelated noise.
        retrieval = {
            "query": self.QUESTION,
            "results": [
                {
                    "result_id": "hit-intro-truncated",
                    "page_id": "nyscef-940-page-0001",
                    "nyscef_document_number": 940,
                    "pdf_page": 1,
                    "document_type": "complaint",
                    "excerpt": "This is a synthetic coverage dispute pleading.",
                    "classifications": ["party_allegation"],
                    "score": 0.4,
                },
                {
                    "result_id": "hit-stip",
                    "page_id": "nyscef-941-page-0001",
                    "nyscef_document_number": 941,
                    "pdf_page": 1,
                    "document_type": "stipulation",
                    "excerpt": "STIPULATION unrelated to requested relief.",
                    "classifications": ["unknown"],
                    "score": 0.3,
                },
            ],
            "complaint_structure_map": structure_map,
        }
        return structure_map, documents, retrieval, de

    def test_routing_selects_wherefore_and_grounds_all_three_relief_items(self) -> None:
        structure_map, documents, retrieval, de = self._synthetic_corpus(
            include_wherefore=True
        )
        self.assertIn(
            "nyscef-940-page-0002",
            __import__(
                "complaint_structure"
            ).collect_complaint_relief_page_ids(structure_map),
        )
        routed = de.route_complaint_relief_evidence(
            retrieval,
            question=self.QUESTION,
            documents=documents,
            complaint_structure_map=structure_map,
        )
        self.assertTrue((routed.get("complaint_relief_routing") or {}).get("applied"))
        page_ids = [h.get("page_id") for h in (routed.get("results") or [])]
        self.assertEqual(page_ids, ["nyscef-940-page-0002"])
        excerpt = (routed["results"][0].get("excerpt") or "").lower()
        self.assertIn("void ab initio", excerpt)
        self.assertIn("no duty to defend", excerpt)
        self.assertIn("such other and further relief", excerpt)

        packet = de.build_evidence_packet(
            self.QUESTION,
            routed,
            complaint_structure_map=structure_map,
            documents=documents,
        )
        # Party-role roadmap must not pollute relief packets.
        self.assertNotIn("complaint_structure_context", packet)
        supported = de.extract_supported_complaint_relief(packet)
        self.assertTrue(supported["rescission_void_ab_initio"]["supported"])
        self.assertTrue(supported["no_defense_or_indemnity"]["supported"])
        self.assertTrue(supported["catch_all_relief"]["supported"])

        assembled = de.apply_evidence_grounded_relief_synthesis(
            {
                "proposed_answer": "Draft lacking grounded relief detail.",
                "propositions": [],
                "audit": {},
            },
            packet,
        )
        answer = assembled["proposed_answer"].lower()
        self.assertIn("not a judicial determination", answer)
        self.assertIn("pleaded", answer)
        self.assertIn("void ab initio", answer)
        self.assertIn("no defense or indemnity", answer)
        self.assertIn("catch-all", answer)
        self.assertTrue(assembled["audit"].get("relief_synthesis_applied"))

    def test_routing_missing_relief_evidence_still_fails_closed(self) -> None:
        structure_map, documents, retrieval, de = self._synthetic_corpus(
            include_wherefore=False
        )
        routed = de.route_complaint_relief_evidence(
            retrieval,
            question=self.QUESTION,
            documents=documents,
            complaint_structure_map=structure_map,
        )
        packet = de.build_evidence_packet(
            self.QUESTION,
            routed,
            complaint_structure_map=structure_map,
            documents=documents,
        )
        supported = de.extract_supported_complaint_relief(packet)
        self.assertFalse(supported["rescission_void_ab_initio"]["supported"])
        self.assertFalse(supported["no_defense_or_indemnity"]["supported"])
        self.assertFalse(supported["catch_all_relief"]["supported"])
        assembled = de.apply_evidence_grounded_relief_synthesis(
            {
                "proposed_answer": "No grounded relief categories available.",
                "propositions": [],
                "audit": {},
            },
            packet,
        )
        answer = assembled["proposed_answer"].lower()
        self.assertNotIn("void ab initio", answer)
        self.assertNotIn("no defense or indemnity", answer)
        self.assertFalse(assembled["audit"].get("relief_synthesis_applied"))


class Q2EvidenceProvenanceLinkageTests(unittest.TestCase):
    """
    Production-shaped provenance: clause-bounded citations, presence-without-
    evidence repair, and multi-page WHEREFORE continuation routing.
    Synthetic non-private evidence only — never private Case-00 complaint text.
    """

    QUESTION = (
        "What relief does the complaint request in the WHEREFORE / "
        "requested-relief section?"
    )

    # Long enumerated rescission clause — evidence_phrase must survive span trim.
    _RESCISSION_CLAUSE = (
        "(a) declaring that the subject commercial coverage form issued to "
        "Harbor Logistics Inc. is void ab initio because of material "
        "misrepresentations in the application and for rescission of the same"
    )
    _NO_DEFENSE_CLAUSE = (
        "(b) declaring that Plaintiff owes neither a duty to defend nor a "
        "duty to indemnify the named defendants under the policy"
    )
    _CATCH_ALL_CLAUSE = (
        "(c) awarding such other and further relief as the Court deems just "
        "and proper"
    )

    def _long_wherefore(self) -> str:
        return (
            "WHEREFORE\n"
            "Plaintiff demands judgment: "
            + self._RESCISSION_CLAUSE
            + "; "
            + self._NO_DEFENSE_CLAUSE
            + "; and "
            + self._CATCH_ALL_CLAUSE
            + "."
        )

    def test_clause_bounded_span_preserves_long_evidence_phrase(self) -> None:
        from engines import drafting_engine as de

        excerpt = self._long_wherefore()
        packet = {
            "question": self.QUESTION,
            "retrieval_hit_count": 1,
            "retrieval_hits": [
                {
                    "result_id": "hit-wherefore-long",
                    "page_id": "nyscef-950-page-0003",
                    "nyscef_document_number": 950,
                    "pdf_page": 3,
                    "document_type": "complaint",
                    "excerpt": excerpt,
                    "classifications": ["legal_position"],
                }
            ],
        }
        supported = de.extract_supported_complaint_relief(packet)
        self.assertTrue(supported["rescission_void_ab_initio"]["supported"])
        rescission_snip = supported["rescission_void_ab_initio"]["evidence_snippet"]
        # Contract-style evidence phrase equals the enumerated demand item.
        self.assertIn(
            "void ab initio because of material misrepresentations",
            rescission_snip.lower(),
        )
        self.assertIn("for rescission of the same", rescission_snip.lower())
        self.assertNotIn("...", rescission_snip)

        self.assertTrue(supported["no_defense_or_indemnity"]["supported"])
        indemnity_snip = supported["no_defense_or_indemnity"]["evidence_snippet"]
        self.assertIn("neither a duty to defend nor a duty to indemnify", indemnity_snip.lower())

    def test_presence_without_evidence_gets_source_excerpt_linked(self) -> None:
        """Mirrors Q2 failure: rescission present in draft, evidence unsupported."""
        from engines import drafting_engine as de

        excerpt = self._long_wherefore()
        packet = {
            "question": self.QUESTION,
            "retrieval_hit_count": 1,
            "retrieval_hits": [
                {
                    "result_id": "hit-wherefore-long",
                    "page_id": "nyscef-950-page-0003",
                    "nyscef_document_number": 950,
                    "pdf_page": 3,
                    "document_type": "complaint",
                    "excerpt": excerpt,
                    "classifications": ["legal_position"],
                }
            ],
        }
        # Draft already has presence language but omits cited source excerpts.
        draft = (
            "The complaint requests rescission and void ab initio treatment. "
            "It also mentions catch-all relief in general terms."
        )
        assembled = de.apply_evidence_grounded_relief_synthesis(
            {"proposed_answer": draft, "propositions": [], "audit": {}},
            packet,
        )
        answer = assembled["proposed_answer"]
        answer_l = answer.lower()
        self.assertIn("not a judicial determination", answer_l)
        self.assertIn("void ab initio because of material misrepresentations", answer_l)
        self.assertIn("for rescission of the same", answer_l)
        self.assertIn("neither a duty to defend nor a duty to indemnify", answer_l)
        self.assertIn("such other and further relief", answer_l)
        self.assertTrue(assembled["audit"].get("relief_synthesis_applied"))

        # Wire through a synthetic Q2 contract whose evidence phrases match
        # this production-shaped excerpt (not private Case-00 prose).
        base = _q2_shaped_contract()
        by_id = {c["id"]: c for c in base["criteria"]}
        by_id[_Q2_CRIT_RESCISSION]["evidence_phrases"] = [
            "void ab initio because of material misrepresentations in the application "
            "and for rescission of the same"
        ]
        by_id[_Q2_CRIT_NO_DEFENSE]["evidence_phrases"] = [
            "neither a duty to defend nor a duty to indemnify the named defendants"
        ]
        by_id[_Q2_CRIT_CATCH_ALL]["evidence_phrases"] = [
            "such other and further relief as the Court deems just and proper"
        ]
        contract = ac.build_synthetic_contract(
            contract_id=base["contract_id"],
            version=base["version"],
            benchmark_id=base["identity"]["benchmark_id"],
            question_id=base["identity"]["question_id"],
            object_key=base["object_key"],
            required_criterion_ids=list(base["required_criterion_ids"]),
            criteria=list(base["criteria"]),
        )
        loaded = ac.load_acceptance_contract_from_bytes(
            json.dumps(contract, sort_keys=True).encode("utf-8"),
            object_key=contract["object_key"],
            expected_identity=ac.ContractIdentity(
                benchmark_id="synth-benchmark-q2",
                question_id="Q2",
            ),
            expected_content_sha256=contract["content_sha256"],
        )
        self.assertTrue(loaded.ok)
        view = loaded.evaluation
        result = ac.validate_final_answer_against_contract(
            answer, view, apply_fallback=True
        )
        self.assertTrue(result.ok, result.as_safe_dict())
        by_crit = {c.criterion_id: c for c in result.criterion_results}
        for cid in (
            _Q2_CRIT_RESCISSION,
            _Q2_CRIT_NO_DEFENSE,
            _Q2_CRIT_PLEADED,
            _Q2_CRIT_CATCH_ALL,
        ):
            self.assertEqual(by_crit[cid].result_code, ac.CRIT_PASS)
            self.assertEqual(by_crit[cid].evidence, ac.EVIDENCE_SUPPORTED)

    def test_multipage_wherefore_continuation_routes_no_defense(self) -> None:
        """Structure lists continuation page lacking WHEREFORE heading."""
        import complaint_structure as cs
        from engines import drafting_engine as de

        page1 = (
            "WHEREFORE\n"
            "Plaintiff demands judgment: "
            + self._RESCISSION_CLAUSE
            + ";"
        )
        page2 = (
            self._NO_DEFENSE_CLAUSE
            + "; and "
            + self._CATCH_ALL_CLAUSE
            + "."
        )
        pages = [
            {
                "nyscef_document_number": 960,
                "page_number": 1,
                "page_id": "nyscef-960-page-0001",
                "text": "INTRODUCTION\n1. Synthetic coverage dispute.",
                "document_type": "complaint",
                "document_classification": "complaint",
                "source_filename": "synth_complaint_960.pdf",
            },
            {
                "nyscef_document_number": 960,
                "page_number": 2,
                "page_id": "nyscef-960-page-0002",
                "text": page1,
                "document_type": "complaint",
                "document_classification": "complaint",
                "source_filename": "synth_complaint_960.pdf",
            },
            {
                "nyscef_document_number": 960,
                "page_number": 3,
                "page_id": "nyscef-960-page-0003",
                "text": page2,
                "document_type": "complaint",
                "document_classification": "complaint",
                "source_filename": "synth_complaint_960.pdf",
            },
        ]
        structure_map = cs.build_complaint_structure_map({"pages": pages})
        # Force both relief pages into the structure selection even if the
        # builder only tagged the heading page — mirrors production multi-page
        # WHEREFORE provenance records.
        relief_ids = cs.collect_complaint_relief_page_ids(structure_map)
        if "nyscef-960-page-0003" not in relief_ids:
            # Inject continuation page_id into the observed wherefore section.
            for doc in structure_map.get("documents") or []:
                for sec in doc.get("sections") or []:
                    match_key = str(sec.get("match_key") or "").lower()
                    if match_key in {"wherefore", "prayer_for_relief"}:
                        ids = list(sec.get("page_ids") or [])
                        if "nyscef-960-page-0003" not in ids:
                            ids.append("nyscef-960-page-0003")
                        sec["page_ids"] = ids
        self.assertIn(
            "nyscef-960-page-0003",
            cs.collect_complaint_relief_page_ids(structure_map),
        )

        documents = [
            {
                "filename": "synth_complaint_960.pdf",
                "nyscef_document_number": 960,
                "type": "complaint",
                "document_type": "complaint",
                "pages": [p for p in pages if p["nyscef_document_number"] == 960],
            }
        ]
        retrieval = {
            "query": self.QUESTION,
            "results": [
                {
                    "result_id": "hit-intro",
                    "page_id": "nyscef-960-page-0001",
                    "nyscef_document_number": 960,
                    "pdf_page": 1,
                    "document_type": "complaint",
                    "excerpt": "Synthetic coverage dispute.",
                    "classifications": ["party_allegation"],
                    "score": 0.4,
                }
            ],
            "complaint_structure_map": structure_map,
        }
        routed = de.route_complaint_relief_evidence(
            retrieval,
            question=self.QUESTION,
            documents=documents,
            complaint_structure_map=structure_map,
        )
        page_ids = [h.get("page_id") for h in (routed.get("results") or [])]
        self.assertIn("nyscef-960-page-0002", page_ids)
        self.assertIn("nyscef-960-page-0003", page_ids)

        packet = de.build_evidence_packet(
            self.QUESTION,
            routed,
            complaint_structure_map=structure_map,
            documents=documents,
        )
        # Provenance: relief packets retain page_text for clause citation.
        for hit in packet["retrieval_hits"]:
            if hit.get("page_id") in {
                "nyscef-960-page-0002",
                "nyscef-960-page-0003",
            }:
                self.assertTrue(hit.get("page_text") or hit.get("excerpt"))

        supported = de.extract_supported_complaint_relief(packet)
        self.assertTrue(supported["rescission_void_ab_initio"]["supported"])
        self.assertTrue(
            supported["no_defense_or_indemnity"]["supported"],
            "continuation-page no-defense language must be selected when "
            "present in the canonical source",
        )
        self.assertTrue(supported["catch_all_relief"]["supported"])

        assembled = de.apply_evidence_grounded_relief_synthesis(
            {
                "proposed_answer": "Draft omitting grounded relief citations.",
                "propositions": [],
                "audit": {},
            },
            packet,
        )
        answer = assembled["proposed_answer"].lower()
        self.assertIn("not a judicial determination", answer)
        self.assertIn("void ab initio", answer)
        self.assertIn("no defense or indemnity", answer)
        self.assertIn("neither a duty to defend nor a duty to indemnify", answer)
        self.assertIn("such other and further relief", answer)

    def test_no_defense_absent_from_source_fails_closed(self) -> None:
        """If canonical source lacks no-defense relief, never invent it."""
        from engines import drafting_engine as de

        excerpt = (
            "WHEREFORE Plaintiff demands judgment declaring the policy void ab "
            "initio and for rescission of the same, and for such other and "
            "further relief as the Court deems just and proper."
        )
        packet = {
            "question": self.QUESTION,
            "retrieval_hit_count": 1,
            "retrieval_hits": [
                {
                    "result_id": "hit-wherefore-no-indemnity",
                    "page_id": "nyscef-970-page-0002",
                    "nyscef_document_number": 970,
                    "pdf_page": 2,
                    "document_type": "complaint",
                    "excerpt": excerpt,
                    "classifications": ["legal_position"],
                }
            ],
        }
        supported = de.extract_supported_complaint_relief(packet)
        self.assertTrue(supported["rescission_void_ab_initio"]["supported"])
        self.assertFalse(supported["no_defense_or_indemnity"]["supported"])
        self.assertTrue(supported["catch_all_relief"]["supported"])
        assembled = de.apply_evidence_grounded_relief_synthesis(
            {
                "proposed_answer": "Partial draft.",
                "propositions": [],
                "audit": {},
            },
            packet,
        )
        answer = assembled["proposed_answer"].lower()
        self.assertNotIn("no defense or indemnity", answer)
        cats = set(assembled["audit"].get("relief_supported_categories") or [])
        self.assertNotIn("no_defense_or_indemnity", cats)


class Q2EmptyStructurePriorPageRoutingTests(unittest.TestCase):
    """
    Production-shaped: empty structure map + WHEREFORE page with prior-page
    no-defense support. Synthetic text only — mirrors collapsed-OCR caches
    that lack WHEREFORE section provenance.
    """

    QUESTION = (
        "What relief does the complaint request in the WHEREFORE / "
        "requested-relief section?"
    )

    def _empty_selected_structure(self, nyscef: int = 980) -> dict:
        import complaint_structure as cs

        return {
            "schema_version": cs.SCHEMA_VERSION,
            "selection": {
                "status": cs.SELECTION_STATUS_SELECTED,
                "reason": None,
                "controlling_nyscef_document_number": nyscef,
                "candidate_nyscef_document_numbers": [nyscef],
                "excluded_nyscef_document_numbers": [],
            },
            "documents": [
                {
                    "document_id": f"nyscef-{nyscef:03d}",
                    "nyscef_document_number": nyscef,
                    "source_pages": [
                        {
                            "page_id": f"nyscef-{nyscef}-page-0001",
                            "page_number": 1,
                            "nyscef_document_number": nyscef,
                        },
                        {
                            "page_id": f"nyscef-{nyscef}-page-0002",
                            "page_number": 2,
                            "nyscef_document_number": nyscef,
                        },
                    ],
                    "section_headings": [],
                    "paragraph_numbers": [],
                    "sections": [],
                    "contiguous_ranges": [],
                    "missing_paragraph_numbers": [],
                    "noncontiguous_sequences": [],
                    "uncertainties": [],
                }
            ],
        }

    def test_empty_structure_routes_wherefore_plus_prior_no_defense_page(self) -> None:
        import complaint_structure as cs
        from engines import drafting_engine as de

        prior_page = (
            "Count II further seeks a declaration that Plaintiff owes neither "
            "a duty to defend nor a duty to indemnify the named defendants."
        )
        wherefore_page = (
            "26 WHEREFORE Plaintiff demands judgment declaring coverage void "
            "ab initio and for rescission, and awarding costs."
        )
        pages = [
            {
                "nyscef_document_number": 980,
                "page_number": 1,
                "page_id": "nyscef-980-page-0001",
                "text": prior_page,
                "document_type": "complaint",
                "document_classification": "complaint",
                "source_filename": "synth_complaint_980.pdf",
            },
            {
                "nyscef_document_number": 980,
                "page_number": 2,
                "page_id": "nyscef-980-page-0002",
                "text": wherefore_page,
                "document_type": "complaint",
                "document_classification": "complaint",
                "source_filename": "synth_complaint_980.pdf",
            },
        ]
        structure_map = self._empty_selected_structure(980)
        self.assertTrue(cs.controlling_complaint_structure_is_empty(structure_map))
        self.assertEqual(cs.collect_complaint_relief_page_ids(structure_map), [])

        documents = [
            {
                "filename": "synth_complaint_980.pdf",
                "nyscef_document_number": 980,
                "type": "complaint",
                "document_type": "complaint",
                "pages": pages,
            }
        ]
        retrieval = {
            "query": self.QUESTION,
            "results": [
                {
                    "result_id": "hit-noise",
                    "page_id": "nyscef-980-page-0001",
                    "nyscef_document_number": 980,
                    "pdf_page": 1,
                    "document_type": "complaint",
                    "excerpt": "Count II further seeks a declaration.",
                    "classifications": ["party_allegation"],
                    "score": 0.3,
                }
            ],
            "complaint_structure_map": structure_map,
        }
        routed = de.route_complaint_relief_evidence(
            retrieval,
            question=self.QUESTION,
            documents=documents,
            complaint_structure_map=structure_map,
        )
        page_ids = [h.get("page_id") for h in (routed.get("results") or [])]
        # Narrow set only: prior support page + WHEREFORE page — not broad retrieval.
        self.assertEqual(
            page_ids,
            ["nyscef-980-page-0001", "nyscef-980-page-0002"],
        )

        packet = de.build_evidence_packet(
            self.QUESTION,
            routed,
            complaint_structure_map=structure_map,
            documents=documents,
        )
        supported = de.extract_supported_complaint_relief(packet)
        self.assertTrue(supported["no_defense_or_indemnity"]["supported"])
        self.assertTrue(supported["rescission_void_ab_initio"]["supported"])

        assembled = de.apply_evidence_grounded_relief_synthesis(
            {
                "proposed_answer": "Draft omitting grounded relief citations.",
                "propositions": [],
                "audit": {},
            },
            packet,
        )
        answer = assembled["proposed_answer"].lower()
        self.assertIn("pleaded", answer)
        self.assertIn("not a judicial determination", answer)
        self.assertIn("no defense or indemnity", answer)
        self.assertIn("neither a duty to defend nor a duty to indemnify", answer)

    def test_collapsed_ocr_wherefore_populates_structure_relief_page_ids(self) -> None:
        """Mid-line WHEREFORE after page chrome must yield relief page_ids."""
        import complaint_structure as cs

        pages = [
            {
                "nyscef_document_number": 981,
                "page_number": 1,
                "page_id": "nyscef-981-page-0001",
                "text": (
                    "1 Plaintiff Synthetic Carrier LLC brings this coverage action."
                ),
                "document_type": "complaint",
                "document_classification": "complaint",
                "source_filename": "synth_complaint_981.pdf",
            },
            {
                "nyscef_document_number": 981,
                "page_number": 2,
                "page_id": "nyscef-981-page-0002",
                # Collapsed OCR: page number glued onto WHEREFORE (no newlines).
                "text": (
                    "26 WHEREFORE Plaintiff demands judgment declaring the policy "
                    "void ab initio and for rescission of the same."
                ),
                "document_type": "complaint",
                "document_classification": "complaint",
                "source_filename": "synth_complaint_981.pdf",
            },
        ]
        structure_map = cs.build_complaint_structure_map({"pages": pages})
        self.assertFalse(cs.controlling_complaint_structure_is_empty(structure_map))
        relief_ids = cs.collect_complaint_relief_page_ids(structure_map)
        self.assertEqual(relief_ids, ["nyscef-981-page-0002"])
        doc = structure_map["documents"][0]
        self.assertTrue(doc.get("section_headings"))
        self.assertTrue(doc.get("sections"))
        match_keys = [
            str(sec.get("match_key") or "").lower()
            for sec in (doc.get("sections") or [])
        ]
        self.assertIn("wherefore", match_keys)

    def test_pleaded_semantic_requires_pleaded_token(self) -> None:
        view = _q2_view()
        # Presence-like language without the required "pleaded" semantic token.
        answer = (
            "This answer describes relief requested in the complaint, "
            "not a judicial determination. "
            "The complaint requests rescission and void ab initio "
            "(synth wherefore void ab initio excerpt). "
            "It seeks no defense or indemnity "
            "(synth no duty to defend or indemnify excerpt). "
            "It includes such other and further relief "
            "(synth such other and further relief excerpt)."
        )
        result = ac.validate_final_answer_against_contract(
            answer, view, apply_fallback=False
        )
        self.assertFalse(result.ok)
        by_id = {c.criterion_id: c for c in result.criterion_results}
        pleaded = by_id[_Q2_CRIT_PLEADED]
        # Missing "pleaded requested relief" presence → absent, or semantic fail
        # if presence somehow matches. Either way the criterion must not pass.
        self.assertNotEqual(pleaded.result_code, ac.CRIT_PASS)

    def test_rescission_unsupported_when_evidence_phrase_absent(self) -> None:
        """Fail-closed: do not invent unsupported rescission evidence phrases."""
        view = _q2_view()
        # Presence ok for rescission; evidence phrase deliberately absent.
        # Catch-all and no-defense remain evidence-linked; pleaded uses stock.
        answer = (
            "This answer describes pleaded requested relief in the complaint, "
            "not a judicial determination. "
            "The complaint requests rescission and void ab initio treatment. "
            "It also seeks no defense or indemnity "
            "(synth no duty to defend or indemnify excerpt). "
            "The WHEREFORE includes such other and further relief "
            "(synth such other and further relief excerpt)."
        )
        result = ac.validate_final_answer_against_contract(
            answer, view, apply_fallback=True
        )
        self.assertFalse(result.ok)
        by_id = {c.criterion_id: c for c in result.criterion_results}
        self.assertEqual(
            by_id[_Q2_CRIT_RESCISSION].result_code, ac.CRIT_FAIL_UNSUPPORTED
        )
        # Catch-all remains supported / unchanged.
        self.assertEqual(by_id[_Q2_CRIT_CATCH_ALL].result_code, ac.CRIT_PASS)
        self.assertEqual(by_id[_Q2_CRIT_NO_DEFENSE].result_code, ac.CRIT_PASS)
        self.assertEqual(by_id[_Q2_CRIT_PLEADED].result_code, ac.CRIT_PASS)
        # Must not invent judicial-rescission style phrasing.
        self.assertNotIn("judicial rescission", result.final_answer.lower())
        rescission_spec = view.criterion_by_id()[_Q2_CRIT_RESCISSION]
        self.assertNotIn(rescission_spec.fallback_text.strip(), result.final_answer)

    def test_catch_all_behavior_unchanged_when_supported(self) -> None:
        from engines import drafting_engine as de

        excerpt = (
            "WHEREFORE Plaintiff demands such other and further relief as the "
            "Court deems just and proper."
        )
        packet = {
            "question": self.QUESTION,
            "retrieval_hit_count": 1,
            "retrieval_hits": [
                {
                    "result_id": "hit-catch-all-only",
                    "page_id": "nyscef-982-page-0001",
                    "nyscef_document_number": 982,
                    "pdf_page": 1,
                    "document_type": "complaint",
                    "excerpt": excerpt,
                    "classifications": ["legal_position"],
                }
            ],
        }
        supported = de.extract_supported_complaint_relief(packet)
        self.assertTrue(supported["catch_all_relief"]["supported"])
        self.assertFalse(supported["rescission_void_ab_initio"]["supported"])
        self.assertFalse(supported["no_defense_or_indemnity"]["supported"])
        assembled = de.apply_evidence_grounded_relief_synthesis(
            {
                "proposed_answer": "Partial draft.",
                "propositions": [],
                "audit": {},
            },
            packet,
        )
        answer = assembled["proposed_answer"].lower()
        self.assertIn("catch-all", answer)
        self.assertIn("such other and further relief", answer)
        self.assertIn("pleaded", answer)
        self.assertNotIn("no defense or indemnity", answer)
        self.assertNotIn("void ab initio", answer)

    def test_rebuilt_cache_routes_catch_all_continuation_and_preserves_passes(
        self,
    ) -> None:
        """
        Production-shaped rebuilt structure tags only the WHEREFORE heading
        page. Adjacent expansion must preserve prior-page no-defense and
        next-page catch-all without broad retrieval. Synthetic text only.
        """
        import complaint_structure as cs
        from engines import drafting_engine as de

        prior_page = (
            "Count II further seeks a declaration that Plaintiff owes neither "
            "a duty to defend nor a duty to indemnify the named defendants."
        )
        wherefore_page = (
            "26 WHEREFORE Plaintiff demands judgment declaring coverage void "
            "ab initio and for rescission of the same;"
        )
        continuation_page = (
            "and awarding such other and further relief as the Court deems "
            "just and proper."
        )
        pages = [
            {
                "nyscef_document_number": 990,
                "page_number": 1,
                "page_id": "nyscef-990-page-0001",
                "text": prior_page,
                "document_type": "complaint",
                "document_classification": "complaint",
                "source_filename": "synth_complaint_990.pdf",
            },
            {
                "nyscef_document_number": 990,
                "page_number": 2,
                "page_id": "nyscef-990-page-0002",
                "text": wherefore_page,
                "document_type": "complaint",
                "document_classification": "complaint",
                "source_filename": "synth_complaint_990.pdf",
            },
            {
                "nyscef_document_number": 990,
                "page_number": 3,
                "page_id": "nyscef-990-page-0003",
                "text": continuation_page,
                "document_type": "complaint",
                "document_classification": "complaint",
                "source_filename": "synth_complaint_990.pdf",
            },
        ]
        structure_map = cs.build_complaint_structure_map({"pages": pages})
        # Rebuilt cache: heading page only (no continuation provenance).
        relief_ids = cs.collect_complaint_relief_page_ids(structure_map)
        self.assertEqual(relief_ids, ["nyscef-990-page-0002"])

        documents = [
            {
                "filename": "synth_complaint_990.pdf",
                "nyscef_document_number": 990,
                "type": "complaint",
                "document_type": "complaint",
                "pages": pages,
            }
        ]
        retrieval = {
            "query": self.QUESTION,
            "results": [
                {
                    "result_id": "hit-noise",
                    "page_id": "nyscef-990-page-0001",
                    "nyscef_document_number": 990,
                    "pdf_page": 1,
                    "document_type": "complaint",
                    "excerpt": "Count II further seeks a declaration.",
                    "classifications": ["party_allegation"],
                    "score": 0.2,
                }
            ],
            "complaint_structure_map": structure_map,
        }
        routed = de.route_complaint_relief_evidence(
            retrieval,
            question=self.QUESTION,
            documents=documents,
            complaint_structure_map=structure_map,
        )
        page_ids = [h.get("page_id") for h in (routed.get("results") or [])]
        self.assertEqual(
            page_ids,
            [
                "nyscef-990-page-0001",
                "nyscef-990-page-0002",
                "nyscef-990-page-0003",
            ],
        )

        packet = de.build_evidence_packet(
            self.QUESTION,
            routed,
            complaint_structure_map=structure_map,
            documents=documents,
        )
        supported = de.extract_supported_complaint_relief(packet)
        self.assertTrue(supported["no_defense_or_indemnity"]["supported"])
        self.assertTrue(supported["catch_all_relief"]["supported"])
        self.assertTrue(supported["rescission_void_ab_initio"]["supported"])

        assembled = de.apply_evidence_grounded_relief_synthesis(
            {
                "proposed_answer": "Draft omitting grounded relief citations.",
                "propositions": [],
                "audit": {},
            },
            packet,
        )
        answer = assembled["proposed_answer"].lower()
        # Preserve the two previously passing criteria.
        self.assertIn("pleaded", answer)
        self.assertIn("not a judicial determination", answer)
        self.assertIn("no defense or indemnity", answer)
        self.assertIn("neither a duty to defend nor a duty to indemnify", answer)
        # Catch-all must remain source-backed after rebuilt-cache routing.
        self.assertIn("catch-all", answer)
        self.assertIn("such other and further relief", answer)
        cats = set(assembled["audit"].get("relief_supported_categories") or [])
        self.assertIn("no_defense_or_indemnity", cats)
        self.assertIn("catch_all_relief", cats)
        self.assertTrue(assembled["audit"].get("relief_synthesis_applied"))

    def test_truncated_wherefore_excerpt_preserves_same_page_catch_all(self) -> None:
        """Bounded WHEREFORE excerpt must not drop same-page catch-all text."""
        from engines import drafting_engine as de

        filler = "Synthetic enumerated demand clause. " * 120
        full_page = (
            "WHEREFORE Plaintiff demands judgment declaring the policy void "
            "ab initio and for rescission; "
            + filler
            + "and for such other and further relief as the Court deems just "
            "and proper."
        )
        # Mimic routed truncated excerpt that omits the trailing catch-all.
        truncated = de._complaint_relief_excerpt_from_page_text(full_page)
        self.assertLessEqual(len(truncated), de._RELIEF_EXCERPT_MAX)
        packet = {
            "question": self.QUESTION,
            "retrieval_hit_count": 1,
            "retrieval_hits": [
                {
                    "result_id": "hit-truncated-wherefore",
                    "page_id": "nyscef-991-page-0001",
                    "nyscef_document_number": 991,
                    "pdf_page": 1,
                    "document_type": "complaint",
                    "excerpt": truncated,
                    "page_text": full_page,
                    "classifications": ["legal_position"],
                }
            ],
        }
        supported = de.extract_supported_complaint_relief(packet)
        self.assertTrue(supported["rescission_void_ab_initio"]["supported"])
        self.assertTrue(
            supported["catch_all_relief"]["supported"],
            "same-page catch-all must survive rebuilt excerpt truncation",
        )

    def test_restored_cache_source_catch_all_wording_preserves_passes(self) -> None:
        """
        Restored-cache WHEREFORE heading page with full page_text using
        source-equivalent catch-all wording (not “such other and further”).
        Routing -> packet -> supported relief -> synthesis; synthetic only.
        """
        import complaint_structure as cs
        from engines import drafting_engine as de

        # Equivalent to “any other relief that this court deems just and
        # equitable” — must match without broadening retrieval.
        catch_all_source = (
            "any other relief that this court deems just and equitable"
        )
        full_page = (
            "WHEREFORE Plaintiff demands judgment declaring the policy void "
            "ab initio and for rescission of the same; declaring that "
            "Plaintiff owes neither a duty to defend nor a duty to indemnify "
            "the named defendants; and for "
            + catch_all_source
            + "."
        )
        pages = [
            {
                "nyscef_document_number": 992,
                "page_number": 1,
                "page_id": "nyscef-992-page-0001",
                "text": full_page,
                "document_type": "complaint",
                "document_classification": "complaint",
                "source_filename": "synth_complaint_992.pdf",
            },
        ]
        structure_map = cs.build_complaint_structure_map({"pages": pages})
        relief_ids = cs.collect_complaint_relief_page_ids(structure_map)
        self.assertEqual(relief_ids, ["nyscef-992-page-0001"])

        documents = [
            {
                "filename": "synth_complaint_992.pdf",
                "nyscef_document_number": 992,
                "type": "complaint",
                "document_type": "complaint",
                "pages": pages,
            }
        ]
        # Bounded excerpt may omit the trailing catch-all; restored page_text
        # retains the full WHEREFORE page (production restored-cache shape).
        truncated = de._complaint_relief_excerpt_from_page_text(full_page)
        retrieval = {
            "query": self.QUESTION,
            "results": [
                {
                    "result_id": "hit-restored-wherefore",
                    "page_id": "nyscef-992-page-0001",
                    "nyscef_document_number": 992,
                    "pdf_page": 1,
                    "document_type": "complaint",
                    "excerpt": truncated,
                    "page_text": full_page,
                    "classifications": ["legal_position"],
                    "score": 0.9,
                }
            ],
            "complaint_structure_map": structure_map,
        }
        routed = de.route_complaint_relief_evidence(
            retrieval,
            question=self.QUESTION,
            documents=documents,
            complaint_structure_map=structure_map,
        )
        page_ids = [h.get("page_id") for h in (routed.get("results") or [])]
        self.assertIn("nyscef-992-page-0001", page_ids)

        packet = de.build_evidence_packet(
            self.QUESTION,
            routed,
            complaint_structure_map=structure_map,
            documents=documents,
        )
        # Restored-cache provenance: WHEREFORE hit keeps full page_text.
        wherefore_hits = [
            h
            for h in packet["retrieval_hits"]
            if h.get("page_id") == "nyscef-992-page-0001"
        ]
        self.assertTrue(wherefore_hits)
        self.assertTrue(
            any(h.get("page_text") for h in wherefore_hits),
            "restored-cache packet must retain full page_text",
        )

        supported = de.extract_supported_complaint_relief(packet)
        self.assertTrue(supported["rescission_void_ab_initio"]["supported"])
        self.assertTrue(supported["no_defense_or_indemnity"]["supported"])
        self.assertTrue(
            supported["catch_all_relief"]["supported"],
            "source-equivalent catch-all wording must be recognized",
        )

        assembled = de.apply_evidence_grounded_relief_synthesis(
            {
                "proposed_answer": "Draft omitting grounded relief citations.",
                "propositions": [],
                "audit": {},
            },
            packet,
        )
        answer = assembled["proposed_answer"].lower()
        # Preserve the three currently passing Q2 criteria.
        self.assertIn("void ab initio", answer)
        self.assertIn("no defense or indemnity", answer)
        self.assertIn("neither a duty to defend nor a duty to indemnify", answer)
        self.assertIn("pleaded", answer)
        self.assertIn("not a judicial determination", answer)
        # Catch-all recognized from source-equivalent wording.
        self.assertIn("catch-all", answer)
        self.assertIn("any other relief that this court deems", answer)
        cats = set(assembled["audit"].get("relief_supported_categories") or [])
        self.assertIn("rescission_void_ab_initio", cats)
        self.assertIn("no_defense_or_indemnity", cats)
        self.assertIn("catch_all_relief", cats)
        self.assertTrue(assembled["audit"].get("relief_synthesis_applied"))


class ValidatedEvidenceBindingRegressionTests(unittest.TestCase):
    def test_validated_proposition_evidence_authorizes_missing_fallback(self) -> None:
        view = _load_view()
        spec = view.criteria[0]
        evidence = " ".join(spec.evidence_phrases)
        out, actions = ac.apply_idempotent_contract_fallback(
            "Unrelated attorney-facing preamble.",
            view,
            missing_ids=[spec.id],
            validated_evidence_text=evidence,
        )
        self.assertEqual(actions[spec.id], ac.FALLBACK_INSERTED)
        self.assertIn(spec.fallback_text.strip(), out)

    def test_explicit_validated_evidence_channel_cannot_be_bypassed_by_answer(self) -> None:
        view = _load_view()
        spec = view.criteria[0]
        answer = " ".join(spec.evidence_phrases)
        out, actions = ac.apply_idempotent_contract_fallback(
            answer,
            view,
            missing_ids=[spec.id],
            validated_evidence_text="validated proposition without required support",
        )
        self.assertEqual(actions[spec.id], ac.FALLBACK_SKIPPED_UNSUPPORTED)
        self.assertNotIn(spec.fallback_text.strip(), out)

    def test_final_validation_uses_same_validated_evidence_on_both_passes(self) -> None:
        view = _load_view()
        answer = _answer_covering(view)
        evidence = " ".join(
            phrase
            for spec in view.criteria
            for phrase in spec.evidence_phrases
        )
        result = ac.validate_final_answer_against_contract(
            answer,
            view,
            apply_fallback=False,
            validated_evidence_text=evidence,
        )
        self.assertTrue(result.ok)
        self.assertTrue(
            all(
                row.evidence == ac.EVIDENCE_SUPPORTED
                for row in result.criterion_results
            )
        )

    def test_phrase_coverage_reports_indices_without_contract_prose(self) -> None:
        spec = ac.CriterionEvalSpec(
            id="synthetic-diagnostic",
            presence_phrases=("presence-token-one", "presence-token-two"),
            evidence_phrases=("evidence-token-one", "evidence-token-two"),
            semantic_required_phrases=(),
            semantic_forbidden_phrases=(),
            fallback_text="",
        )
        result = ac.evaluate_criterion(
            "presence-token-one",
            spec,
            semantic_preservation={},
            validated_evidence_text="evidence-token-one",
        )
        self.assertEqual(
            result.phrase_coverage["presence"],
            {
                "phrase_count": 2,
                "matched_indices": [1],
                "missing_indices": [2],
            },
        )
        self.assertEqual(
            result.phrase_coverage["evidence"],
            {
                "phrase_count": 2,
                "matched_indices": [1],
                "missing_indices": [2],
            },
        )
        safe = json.dumps(result.as_safe_dict(), sort_keys=True)
        self.assertNotIn("presence-token", safe)
        self.assertNotIn("evidence-token", safe)


if __name__ == "__main__":
    unittest.main()
