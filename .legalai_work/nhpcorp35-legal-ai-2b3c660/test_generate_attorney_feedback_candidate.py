"""Focused tests for scripts/generate_attorney_feedback_candidate.py."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import matter_builder as mb
from engines import drafting_engine as de


def _load_cli():
    path = (
        Path(__file__).resolve().parent
        / "scripts"
        / "generate_attorney_feedback_candidate.py"
    )
    spec = importlib.util.spec_from_file_location(
        "generate_attorney_feedback_candidate", path
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


CLI = _load_cli()


def _page(page_number: int, text: str, nyscef: int) -> dict:
    record = mb.build_page_record(
        page_number, text, "native", nyscef_document_number=nyscef
    )
    record.update(
        {
            "nyscef_document_number": nyscef,
            "pdf_page_number": page_number,
            "source_filename": f"nyscef_doc_no_{nyscef}_complaint.pdf",
            "source_path": f"/tmp/synthetic/nyscef_doc_no_{nyscef}_complaint.pdf",
        }
    )
    return record


def _synthetic_case(root: Path) -> None:
    """Build a minimal permitted corpus (no gold/feedback/eval artifacts)."""
    nyscef = 101
    pages = [
        _page(
            1,
            (
                "PARTIES\n"
                "Cedar Ridge Logistics LLC is a domestic corporation duly "
                "authorized to do business in this state and is the plaintiff.\n"
                "Meadow Bridge Repair Inc. is a domestic corporation with its "
                "principal place of business in Albany and is a defendant.\n"
            ),
            nyscef,
        ),
        _page(
            2,
            (
                "Cedar Ridge Logistics LLC plaintiff caption. "
                "Meadow Bridge Repair Inc. defendant notice defendant."
            ),
            nyscef,
        ),
    ]
    (root / "derived" / "page-extraction").mkdir(parents=True)
    (root / "derived" / "exhibit-segmentation").mkdir(parents=True)
    (root / "derived" / "case-map").mkdir(parents=True)
    (root / "derived" / "question-text").mkdir(parents=True)

    (root / "derived" / "page-extraction" / "canonical_page_records.json").write_text(
        json.dumps({"pages": pages}, indent=2) + "\n", encoding="utf-8"
    )
    (root / "derived" / "exhibit-segmentation" / "filing_exhibit_map.json").write_text(
        json.dumps(
            {
                "filings": [
                    {
                        "nyscef_document_number": nyscef,
                        "segments": [],
                        "uncertain_boundaries": [],
                    }
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "derived" / "case-map" / "case_map.json").write_text(
        json.dumps({"case_map": mb.empty_case_map()}, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / "derived" / "question-text" / "questions.json").write_text(
        json.dumps(
            {
                "Q1": (
                    "Who are the parties, and what role does each party allegedly "
                    "have in the underlying insurance dispute?"
                )
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _inventory(path: Path, nyscef: int = 101) -> Path:
    payload = {
        "filings": [
            {
                "nyscef_document_number": nyscef,
                "filename": f"nyscef_doc_no_{nyscef}_complaint.pdf",
                "ingest_canonical": True,
                "sha256": "a" * 64,
            }
        ]
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _packet_from_user_prompt(user_prompt: str) -> dict:
    """Recover the exact evidence packet serialized into the production prompt."""
    marker = "Analyze the attorney question using only this evidence packet.\n"
    marker += "Return the required JSON object and nothing else.\n\n"
    if not user_prompt.startswith(marker):
        # Repair prompts embed the packet under a different header.
        key = "Evidence packet:\n"
        idx = user_prompt.find(key)
        if idx < 0:
            raise AssertionError("user_prompt missing evidence packet")
        rest = user_prompt[idx + len(key) :]
        # Packet JSON ends before the next blank-line section header.
        end = rest.find("\n\n")
        blob = rest if end < 0 else rest[:end]
        return json.loads(blob)
    rest = user_prompt[len(marker) :]
    instruction = "\n\n" + de.PARTY_ROLE_DRAFTING_COMPLETENESS_INSTRUCTION
    if instruction in rest:
        rest = rest.split(instruction, 1)[0]
    return json.loads(rest)


def _complete_payload_from_prompt(user_prompt: str) -> dict:
    packet = _packet_from_user_prompt(user_prompt)
    hit = (packet.get("retrieval_hits") or [{}])[0]
    expected = de.extract_party_role_expected_attributes(packet)
    bits = []
    for party in expected:
        bit = f"{party.get('procedural_role')} {party.get('identity')}"
        if party.get("entity_type"):
            bit += f" is a {party['entity_type']}"
        if party.get("residence_or_ppb"):
            bit += f"; {party['residence_or_ppb']}"
        if party.get("pleaded_role_basis"):
            bit += f" ({party['pleaded_role_basis']})"
        bits.append(bit + ".")
    answer = " ".join(bits) or "Parties are identified in the record."
    return {
        "proposed_answer": answer,
        "propositions": [
            {
                "proposition_id": "P1",
                "text": answer,
                "classification": "party_allegation",
                "nyscef_document_number": hit.get("nyscef_document_number") or 101,
                "page_id": hit.get("page_id") or "nyscef-101-page-0001",
                "pdf_page": hit.get("pdf_page") or 1,
                "source_excerpt": "Cedar Ridge Logistics LLC is a domestic corporation",
                "confidence": 0.9,
                "rationale": "Party roster from pleading.",
                "polarity": "supporting",
            }
        ],
        "supporting_evidence": [],
        "contrary_evidence": [],
        "unresolved_questions": [],
        "documents_pages_reviewed": [],
        "confidence": 0.9,
        "attorney_review": {"requires_attorney_review": True},
    }


def _incomplete_payload_from_prompt(user_prompt: str) -> dict:
    packet = _packet_from_user_prompt(user_prompt)
    hit = (packet.get("retrieval_hits") or [{}])[0]
    return {
        "proposed_answer": "Cedar Ridge Logistics LLC is the plaintiff.",
        "propositions": [
            {
                "proposition_id": "P1",
                "text": "Cedar Ridge Logistics LLC is the plaintiff.",
                "classification": "party_allegation",
                "nyscef_document_number": hit.get("nyscef_document_number") or 101,
                "page_id": hit.get("page_id") or "nyscef-101-page-0001",
                "pdf_page": hit.get("pdf_page") or 1,
                "source_excerpt": "Cedar Ridge Logistics LLC",
                "confidence": 0.4,
                "rationale": "Incomplete roster.",
                "polarity": "supporting",
            }
        ],
        "supporting_evidence": [],
        "contrary_evidence": [],
        "unresolved_questions": [],
        "documents_pages_reviewed": [],
        "confidence": 0.4,
        "attorney_review": {"requires_attorney_review": True},
    }


class GenerateAttorneyFeedbackCandidateCLITests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.case_root = self.root / "case"
        self.out_root = self.root / "candidates"
        self.case_root.mkdir()
        self.out_root.mkdir()
        _synthetic_case(self.case_root)
        self.inventory = _inventory(self.root / "inventory.json")
        self.required_commit = "95407c73201ca375b7f824d8cbcbe06ed598405c"
        self.question = (
            "Who are the parties, and what role does each party allegedly "
            "have in the underlying insurance dispute?"
        )

    def tearDown(self):
        self._tmpdir.cleanup()

    def _run(self, **kwargs):
        params = dict(
            case_root=self.case_root,
            question_id="Q1",
            required_commit=self.required_commit,
            candidate_output_root=self.out_root,
            authorization_acknowledgement=CLI.AUTHORIZATION_ACK,
            generation_only=True,
            inventory_path=self.inventory,
            skip_commit_check=True,
        )
        params.update(kwargs)
        return CLI.run_generation(**params)

    def test_args_exposed(self):
        parser = CLI.build_parser()
        actions = {a.dest for a in parser._actions}
        self.assertIn("case_root", actions)
        self.assertIn("question_id", actions)
        self.assertIn("required_commit", actions)
        self.assertIn("candidate_output_root", actions)
        self.assertIn("authorization_acknowledgement", actions)
        self.assertIn("generation_only", actions)

    def test_auth_mandatory(self):
        with self.assertRaises(CLI.GenerationError) as ctx:
            self._run(authorization_acknowledgement="not-authorized")
        self.assertIn("authorization", ctx.exception.blocker.lower())

    def test_protected_isolation_refuses_gold_inputs(self):
        gold = (
            self.case_root
            / "derived"
            / "attorney-gold-benchmark-01"
            / "attorney_gold_labels_01.json"
        )
        gold.parent.mkdir(parents=True)
        gold.write_text("{}", encoding="utf-8")
        # Point page records path resolution at a protected tree by replacing
        # the page-records file with a symlink into the gold tree.
        page_path = (
            self.case_root
            / "derived"
            / "page-extraction"
            / "canonical_page_records.json"
        )
        page_path.unlink()
        page_path.symlink_to(gold)
        with self.assertRaises(CLI.GenerationError) as ctx:
            self._run(model_call=lambda s, u: {})
        self.assertIn("protected", ctx.exception.blocker.lower())

    def test_protected_paths_never_opened_during_success(self):
        opened: list[str] = []
        real_load = CLI._load_json

        def tracking_load(path, *, role="input"):
            opened.append(str(path))
            return real_load(path, role=role)

        calls = []

        def model(system_prompt, user_prompt):
            calls.append(user_prompt)
            return _complete_payload_from_prompt(user_prompt)

        with mock.patch.object(CLI, "_load_json", side_effect=tracking_load):
            result = self._run(model_call=model, top_k=5)
        self.assertTrue(result["ok"])
        joined = "\n".join(opened).lower()
        self.assertNotIn("attorney-gold-benchmark", joined)
        self.assertNotIn("provisional-gold", joined)
        self.assertNotIn("attorney_gold_labels", joined)
        self.assertNotIn("case00_attorney_feedback_eval", joined)
        for prompt in calls:
            lower = prompt.lower()
            self.assertNotIn("attorney_feedback", lower)
            self.assertNotIn("provisional_should_not_appear", lower)
            self.assertNotIn("gold_should_not_appear", lower)

    def test_production_functions_invoked_not_duplicated(self):
        source = Path(CLI.__file__).read_text(encoding="utf-8")
        # Must not embed the ad-hoc Case-00 benchmark hard-stop tables.
        self.assertNotIn("CAPTION_IDENTITIES_18", source)
        self.assertNotIn("EXPECTED_RETAINED_PAGES", source)
        self.assertNotIn("EXPECTED_CANONICAL_PAGES", source)
        self.assertNotIn("Leavitt Manor", source)

        with mock.patch.object(
            mb, "prepare_documents_for_canonical_retrieval", wraps=mb.prepare_documents_for_canonical_retrieval
        ) as prep, mock.patch.object(
            mb, "retrieve_canonical_records", wraps=mb.retrieve_canonical_records
        ) as retr, mock.patch.object(
            de, "build_evidence_packet", wraps=de.build_evidence_packet
        ) as bep, mock.patch.object(
            de, "build_user_prompt", wraps=de.build_user_prompt
        ) as bup, mock.patch.object(
            de, "answer_attorney_record_question", wraps=de.answer_attorney_record_question
        ) as ans, mock.patch.object(
            mb, "compute_file_sha256", wraps=mb.compute_file_sha256
        ) as hasher:

            def model(system_prompt, user_prompt):
                return _complete_payload_from_prompt(user_prompt)

            result = self._run(model_call=model, top_k=5)

        self.assertTrue(result["ok"])
        self.assertTrue(prep.called)
        self.assertTrue(retr.called)
        self.assertTrue(bep.called)
        self.assertTrue(bup.called)
        self.assertTrue(ans.called)
        self.assertTrue(hasher.called)

    def test_no_generation_on_preflight_failure(self):
        answer = mock.Mock()
        with mock.patch.object(CLI, "assert_commits_match", side_effect=CLI.GenerationError("commit mismatch")):
            with mock.patch.object(de, "answer_attorney_record_question", answer):
                with self.assertRaises(CLI.GenerationError):
                    CLI.run_generation(
                        case_root=self.case_root,
                        question_id="Q1",
                        required_commit=self.required_commit,
                        candidate_output_root=self.out_root,
                        authorization_acknowledgement=CLI.AUTHORIZATION_ACK,
                        generation_only=True,
                        inventory_path=self.inventory,
                        skip_commit_check=False,
                        model_call=lambda s, u: {},
                    )
        answer.assert_not_called()
        self.assertEqual(list(self.out_root.iterdir()), [])

    def test_exactly_one_initial_call_and_at_most_one_repair(self):
        calls = []

        def model(system_prompt, user_prompt):
            calls.append(user_prompt)
            if len(calls) == 1:
                return _incomplete_payload_from_prompt(user_prompt)
            return _complete_payload_from_prompt(user_prompt)

        result = self._run(model_call=model, top_k=5)
        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(result["repair_invoked"])
        self.assertLessEqual(result["provider_calls"], 2)

    def test_failed_repair_not_finalized(self):
        def model(system_prompt, user_prompt):
            return _incomplete_payload_from_prompt(user_prompt)

        with self.assertRaises(CLI.GenerationError) as ctx:
            self._run(model_call=model, top_k=5)
        self.assertIn("not finalized", ctx.exception.blocker.lower())
        self.assertFalse(ctx.exception.details.get("finalized", True))
        self.assertEqual(list(self.out_root.iterdir()), [])

    def test_success_writes_four_artifacts(self):
        def model(system_prompt, user_prompt):
            return _complete_payload_from_prompt(user_prompt)

        result = self._run(model_call=model, top_k=5)
        self.assertTrue(result["ok"])
        files = result["files"]
        self.assertEqual(len(files), 4)
        self.assertIn("Q1_candidate_answer.json", files)
        self.assertIn("Q1_candidate_answer.md", files)
        self.assertIn("generation_manifest.json", files)
        self.assertIn("model_input_audit.json", files)
        for path in files.values():
            self.assertTrue(Path(path).is_file())
            self.assertTrue(Path(path).is_absolute())

    def test_no_live_model_in_tests(self):
        """Guards: tests inject model_call and never resolve a live provider."""

        def guarded_resolve(model_call=None):
            if callable(model_call):
                return model_call
            # Refuse live providers inside this test module.
            return None

        def model(system_prompt, user_prompt):
            return _complete_payload_from_prompt(user_prompt)

        with mock.patch.object(
            de, "resolve_model_provider", side_effect=guarded_resolve
        ), mock.patch.object(
            de, "_openai_responses_model_call"
        ) as openai_call, mock.patch.object(
            de, "_http_model_call"
        ) as http_call:
            result = self._run(model_call=model, top_k=5)
        self.assertTrue(result["ok"])
        openai_call.assert_not_called()
        http_call.assert_not_called()
        self.assertIs(de.resolve_model_provider(model), model)


if __name__ == "__main__":
    unittest.main()
