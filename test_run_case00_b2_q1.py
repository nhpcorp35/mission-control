"""Focused tests for scripts/run_case00_b2_q1.py."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def _load_runner():
    path = Path(__file__).resolve().parent / "scripts" / "run_case00_b2_q1.py"
    spec = importlib.util.spec_from_file_location("run_case00_b2_q1", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


RUNNER = _load_runner()


def _mock_rebuild_mod(*, ok: bool = True, raise_error: bool = False):
    class RebuildError(Exception):
        def __init__(self, message: str, **details):
            super().__init__(message)
            self.message = message
            self.details = details

    def rebuild_case00_derived(**kwargs):
        if raise_error:
            raise RebuildError(
                "simulated rebuild failure",
                key_id="key-id-secret-value",
                application_key="app-key-secret-value",
            )
        if not ok:
            return {
                "ok": False,
                "validation": {"ok": False, "errors": ["missing pages"]},
            }
        return {
            "ok": True,
            "document_count": 1,
            "page_count": 2,
            "filing_count": 1,
            "written": {
                "page_records": (
                    "/tmp/derived/page-extraction/canonical_page_records.json"
                ),
                "exhibit_map": (
                    "/tmp/derived/exhibit-segmentation/filing_exhibit_map.json"
                ),
                "case_map": "/tmp/derived/case-map/case_map.json",
            },
            "validation": {"ok": True, "errors": []},
            "b2_config_leak": {
                "key_id": "key-id-secret-value",
                "application_key": "app-key-secret-value",
            },
        }

    return SimpleNamespace(
        DEFAULT_CASE00_B2_PREFIX=(
            "Benchmarks/Case-00-Triborough/original/Tribrough Full Docket/"
        ),
        RebuildError=RebuildError,
        rebuild_case00_derived=mock.Mock(side_effect=rebuild_case00_derived),
    )


def _mock_gen_mod(*, ok: bool = True, raise_error: bool = False):
    class GenerationError(Exception):
        def __init__(self, blocker: str, **details):
            super().__init__(blocker)
            self.blocker = blocker
            self.details = details

    def run_generation(**kwargs):
        if raise_error:
            raise GenerationError(
                "simulated generation failure",
                authorization_acknowledgement=kwargs.get(
                    "authorization_acknowledgement"
                ),
            )
        if not ok:
            return {"ok": False, "finalized": False}
        return {
            "ok": True,
            "finalized": True,
            "candidate_directory": "/tmp/candidates/q1-candidate-test",
            "files": {
                "Q1_candidate_answer.json": (
                    "/tmp/candidates/q1-candidate-test/Q1_candidate_answer.json"
                ),
            },
            "reasoner_status": "READY",
            "provider_calls": 1,
        }

    return SimpleNamespace(
        AUTHORIZATION_ACK=RUNNER.AUTHORIZATION_ACK,
        GenerationError=GenerationError,
        run_generation=mock.Mock(side_effect=run_generation),
    )


class RunCase00B2Q1Tests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.case_root = self.root / "case"
        self.out_root = self.root / "candidates"
        self.case_root.mkdir()
        self.out_root.mkdir()
        self.required_commit = "95407c73201ca375b7f824d8cbcbe06ed598405c"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _run(self, *, rebuild_mod=None, gen_mod=None, **kwargs):
        params = dict(
            case_root=self.case_root,
            question_id="Q1",
            required_commit=self.required_commit,
            candidate_output_root=self.out_root,
            authorization_acknowledgement=RUNNER.AUTHORIZATION_ACK,
            generation_only=True,
            rebuild_mod=rebuild_mod if rebuild_mod is not None else _mock_rebuild_mod(),
            gen_mod=gen_mod if gen_mod is not None else _mock_gen_mod(),
            b2_prefix=(
                "Benchmarks/Case-00-Triborough/original/Tribrough Full Docket/"
            ),
        )
        params.update(kwargs)
        return RUNNER.run_case00_b2_q1(**params)

    def _cli_argv(self, *, authorize: str | None = None) -> list[str]:
        return [
            "--case-root",
            str(self.case_root),
            "--question-id",
            "Q1",
            "--required-commit",
            self.required_commit,
            "--candidate-output-root",
            str(self.out_root),
            "--authorize-private-evidence-transmission",
            authorize if authorize is not None else RUNNER.AUTHORIZATION_ACK,
            "--generation-only",
            "--b2-prefix",
            "Benchmarks/Case-00-Triborough/original/Tribrough Full Docket/",
        ]

    def test_parser_requires_core_flags(self):
        parser = RUNNER.build_parser()
        actions = {a.dest: a for a in parser._actions}
        for dest in (
            "case_root",
            "question_id",
            "required_commit",
            "candidate_output_root",
            "authorization_acknowledgement",
            "generation_only",
        ):
            self.assertIn(dest, actions)
            self.assertTrue(actions[dest].required)

    def test_authorization_required(self):
        rebuild = _mock_rebuild_mod()
        gen = _mock_gen_mod()
        with self.assertRaises(RUNNER.RunnerError) as ctx:
            self._run(
                rebuild_mod=rebuild,
                gen_mod=gen,
                authorization_acknowledgement="not-authorized",
            )
        self.assertEqual(ctx.exception.phase, "preflight")
        self.assertEqual(ctx.exception.code, "AUTHORIZATION_REQUIRED")
        rebuild.rebuild_case00_derived.assert_not_called()
        gen.run_generation.assert_not_called()

    def test_generation_only_required(self):
        rebuild = _mock_rebuild_mod()
        gen = _mock_gen_mod()
        with self.assertRaises(RUNNER.RunnerError) as ctx:
            self._run(
                rebuild_mod=rebuild,
                gen_mod=gen,
                generation_only=False,
            )
        self.assertEqual(ctx.exception.phase, "preflight")
        self.assertEqual(ctx.exception.code, "GENERATION_ONLY_REQUIRED")
        rebuild.rebuild_case00_derived.assert_not_called()
        gen.run_generation.assert_not_called()

    def test_rebuild_precedes_generation(self):
        order: list[str] = []
        rebuild = _mock_rebuild_mod()
        gen = _mock_gen_mod()
        real_rebuild = rebuild.rebuild_case00_derived.side_effect
        real_gen = gen.run_generation.side_effect

        def tracking_rebuild(**kwargs):
            order.append("rebuild")
            return real_rebuild(**kwargs)

        def tracking_gen(**kwargs):
            order.append("generation")
            return real_gen(**kwargs)

        rebuild.rebuild_case00_derived.side_effect = tracking_rebuild
        gen.run_generation.side_effect = tracking_gen

        result = self._run(rebuild_mod=rebuild, gen_mod=gen)
        self.assertTrue(result["ok"])
        self.assertEqual(order, ["rebuild", "generation"])
        rebuild.rebuild_case00_derived.assert_called_once()
        gen.run_generation.assert_called_once()

        rebuild_kwargs = rebuild.rebuild_case00_derived.call_args.kwargs
        self.assertIsNone(rebuild_kwargs.get("source_dir"))
        self.assertEqual(
            rebuild_kwargs.get("b2_prefix"),
            "Benchmarks/Case-00-Triborough/original/Tribrough Full Docket/",
        )
        self.assertTrue(rebuild_kwargs.get("validate"))

        gen_kwargs = gen.run_generation.call_args.kwargs
        self.assertEqual(gen_kwargs["case_root"], self.case_root)
        self.assertEqual(gen_kwargs["question_id"], "Q1")
        self.assertTrue(gen_kwargs["generation_only"])
        self.assertEqual(
            gen_kwargs["authorization_acknowledgement"],
            RUNNER.AUTHORIZATION_ACK,
        )

        phases = [p["phase"] for p in result["phases"]]
        self.assertEqual(phases, ["rebuild", "generation"])
        self.assertEqual(result["source_mode"], "b2")

    def test_rebuild_failure_prevents_generation(self):
        rebuild = _mock_rebuild_mod(raise_error=True)
        gen = _mock_gen_mod()
        with self.assertRaises(RUNNER.RunnerError) as ctx:
            self._run(rebuild_mod=rebuild, gen_mod=gen)
        self.assertEqual(ctx.exception.phase, "rebuild")
        self.assertEqual(ctx.exception.code, "REBUILD_FAILED")
        rebuild.rebuild_case00_derived.assert_called_once()
        gen.run_generation.assert_not_called()

    def test_rebuild_non_ok_prevents_generation(self):
        rebuild = _mock_rebuild_mod(ok=False)
        gen = _mock_gen_mod()
        with self.assertRaises(RUNNER.RunnerError) as ctx:
            self._run(rebuild_mod=rebuild, gen_mod=gen)
        self.assertEqual(ctx.exception.phase, "rebuild")
        gen.run_generation.assert_not_called()

    def test_secret_value_never_logged(self):
        secret_env = {
            "B2_KEY_ID": "key-id-secret-value",
            "B2_APPLICATION_KEY": "app-key-secret-value",
            "B2_BUCKET": "legalai-corpus",
            "B2_ENDPOINT": "https://s3.us-east-005.backblazeb2.com",
            "B2_REGION": "us-east-005",
        }
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, secret_env, clear=False):
            with mock.patch.object(
                RUNNER,
                "run_case00_b2_q1",
                side_effect=RUNNER.RunnerError(
                    "simulated rebuild failure",
                    phase="rebuild",
                    code="REBUILD_FAILED",
                    key_id="key-id-secret-value",
                    application_key="app-key-secret-value",
                ),
            ):
                with mock.patch("sys.stdout", stdout):
                    code = RUNNER.main(self._cli_argv())

        self.assertNotEqual(code, 0)
        text = stdout.getvalue()
        self.assertNotIn("key-id-secret-value", text)
        self.assertNotIn("app-key-secret-value", text)
        payload = json.loads(text)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failed_phase"], "rebuild")
        self.assertNotIn("key_id", payload)
        self.assertNotIn("application_key", payload)

        # Success summary also strips accidental secret-bearing nested keys.
        result = self._run()
        dumped = json.dumps(result)
        self.assertNotIn("key-id-secret-value", dumped)
        self.assertNotIn("app-key-secret-value", dumped)

        sanitized = RUNNER.sanitize_for_log(
            {
                "error": "x",
                "key_id": "key-id-secret-value",
                "application_key": "app-key-secret-value",
                "bucket": "legalai-corpus",
            }
        )
        self.assertNotIn("key_id", sanitized)
        self.assertNotIn("application_key", sanitized)
        self.assertEqual(sanitized.get("bucket"), "legalai-corpus")

        # RebuildError details from the rebuild phase helper are scrubbed.
        rebuild = _mock_rebuild_mod(raise_error=True)
        gen = _mock_gen_mod()
        with self.assertRaises(RUNNER.RunnerError) as ctx:
            self._run(rebuild_mod=rebuild, gen_mod=gen)
        details = RUNNER.sanitize_for_log(ctx.exception.details)
        detail_blob = json.dumps(details)
        self.assertNotIn("key-id-secret-value", detail_blob)
        self.assertNotIn("app-key-secret-value", detail_blob)

    def test_main_authorization_missing_exits_nonzero(self):
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            code = RUNNER.main(self._cli_argv(authorize="nope"))
        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failed_phase"], "preflight")
        self.assertEqual(payload["code"], "AUTHORIZATION_REQUIRED")
        self.assertNotIn("key-id-secret-value", stdout.getvalue())

    def test_cli_requires_generation_only_flag(self):
        argv = [
            "--case-root",
            str(self.case_root),
            "--question-id",
            "Q1",
            "--required-commit",
            self.required_commit,
            "--candidate-output-root",
            str(self.out_root),
            "--authorize-private-evidence-transmission",
            RUNNER.AUTHORIZATION_ACK,
        ]
        with self.assertRaises(SystemExit) as ctx:
            RUNNER.build_parser().parse_args(argv)
        self.assertNotEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
