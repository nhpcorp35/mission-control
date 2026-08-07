"""Tests for one-command Case-00 generate-and-evaluate workflow."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import matter_builder as mb
from engines import drafting_engine as de
from test_case00_attorney_eval import SummaryCountTests
from test_generate_attorney_feedback_candidate import (
    CLI,
    _complete_payload_from_prompt,
    _inventory,
    _synthetic_case,
)


def _load_workflow():
    path = (
        Path(__file__).resolve().parent
        / "scripts"
        / "run_case00_generate_and_evaluate.py"
    )
    spec = importlib.util.spec_from_file_location(
        "run_case00_generate_and_evaluate", path
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


WF = _load_workflow()


class GenerateAndEvaluateWorkflowTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.case_root = self.root / "case"
        self.out_root = self.root / "runs"
        self.case_root.mkdir()
        self.out_root.mkdir()
        _synthetic_case(self.case_root)
        # Attach mini eval corpus artifacts alongside generation inputs.
        SummaryCountTests()._write_mini_corpus(self.case_root)
        self.inventory = _inventory(self.root / "inventory.json")
        self.required_commit = "95407c73201ca375b7f824d8cbcbe06ed598405c"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_help(self):
        with self.assertRaises(SystemExit) as ctx:
            WF.build_parser().parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_workflow_writes_candidate_and_eval_artifacts(self):
        def model(system_prompt, user_prompt):
            return _complete_payload_from_prompt(user_prompt)

        result = WF.run_workflow(
            case_root=self.case_root,
            question_id="Q1",
            required_commit=self.required_commit,
            output_dir=self.out_root,
            authorization_acknowledgement=CLI.AUTHORIZATION_ACK,
            inventory_path=self.inventory,
            skip_commit_check=True,
            model_call=model,
        )
        self.assertTrue(result["ok"])
        run_dir = Path(result["run_dir"])
        self.assertTrue(run_dir.is_dir())
        self.assertTrue((run_dir / "Q1_candidate_answer.json").is_file())
        self.assertTrue((run_dir / "case00_attorney_feedback_eval.json").is_file())
        self.assertTrue(
            (run_dir / "case00_attorney_feedback_eval_summary.txt").is_file()
        )
        eval_payload = json.loads(
            (run_dir / "case00_attorney_feedback_eval.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(eval_payload["questions"]), 1)
        self.assertEqual(eval_payload["questions"][0]["question_id"], "Q1")
        self.assertIn(
            "candidate_vs_reference_diagnostics",
            eval_payload["questions"][0],
        )

    def test_machine_readable_error_on_auth_failure(self):
        code = WF.main(
            [
                "--case-root",
                str(self.case_root),
                "--question-id",
                "Q1",
                "--required-commit",
                self.required_commit,
                "--output-dir",
                str(self.out_root),
                "--authorize-private-evidence-transmission",
                "nope",
                "--skip-commit-check",
            ]
        )
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
