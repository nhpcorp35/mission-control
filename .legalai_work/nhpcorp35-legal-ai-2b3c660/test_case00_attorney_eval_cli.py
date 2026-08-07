"""CLI regression tests for Case-00 attorney evaluator."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from case00_attorney_eval import cli
from test_case00_attorney_eval import SummaryCountTests


class EvaluatorCLITests(unittest.TestCase):
    def test_help_exits_zero(self):
        parser = cli.build_parser()
        with self.assertRaises(SystemExit) as ctx:
            parser.parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_module_help_mentions_key_flags(self):
        help_text = cli.build_parser().format_help()
        for token in (
            "--case-root",
            "--candidate-answers",
            "--candidate-dir",
            "--question-id",
            "--json-out",
            "--summary-out",
        ):
            self.assertIn(token, help_text)

    def test_cli_writes_explicit_outputs_and_filters_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "case"
            SummaryCountTests()._write_mini_corpus(root)
            cand = Path(tmp) / "candidates.json"
            cand.write_text(json.dumps({"Q1": "candidate one"}), encoding="utf-8")
            json_out = Path(tmp) / "eval.json"
            summary_out = Path(tmp) / "eval.txt"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = cli.main(
                    [
                        "--case-root",
                        str(root),
                        "--candidate-answers",
                        str(cand),
                        "--question-id",
                        "Q1",
                        "--json-out",
                        str(json_out),
                        "--summary-out",
                        str(summary_out),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue(json_out.is_file())
            self.assertTrue(summary_out.is_file())
            payload = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["questions"]), 1)
            self.assertEqual(payload["questions"][0]["question_id"], "Q1")
            self.assertEqual(
                payload["questions"][0]["evaluated_answer"], "candidate one"
            )
            self.assertEqual(
                payload["questions"][0]["preserved_original_legalai_answer"],
                "original one",
            )
            self.assertIn("candidate_vs_reference_diagnostics", payload["questions"][0])

    def test_candidate_dir_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "case"
            SummaryCountTests()._write_mini_corpus(root)
            cand_dir = Path(tmp) / "cand"
            cand_dir.mkdir()
            (cand_dir / "Q2_candidate_answer.json").write_text(
                json.dumps(
                    {
                        "question_id": "Q2",
                        "proposed_answer": "from directory",
                    }
                ),
                encoding="utf-8",
            )
            out = Path(tmp) / "out"
            code = cli.main(
                [
                    "--case-root",
                    str(root),
                    "--candidate-dir",
                    str(cand_dir),
                    "--question-id",
                    "Q2",
                    "--out",
                    str(out),
                ]
            )
            self.assertEqual(code, 0)
            payload = json.loads(
                (out / "case00_attorney_feedback_eval.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["questions"][0]["evaluated_answer"], "from directory")

    def test_missing_case_root_is_machine_readable(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = cli.main(
                [
                    "--case-root",
                    "/tmp/definitely-missing-case00-root-xyz",
                ]
            )
        self.assertEqual(code, 2)
        payload = json.loads(stderr.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn(payload["code"], {"CASE00_ARTIFACTS_MISSING", "EVALUATOR_UNEXPECTED"})


if __name__ == "__main__":
    unittest.main()
