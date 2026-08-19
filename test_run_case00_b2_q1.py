"""Focused durable B2 upload regression tests for scripts/run_case00_b2_q1.py."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def _load_cli():
    path = Path(__file__).resolve().parent / "scripts" / "run_case00_b2_q1.py"
    spec = importlib.util.spec_from_file_location("run_case00_b2_q1", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    repo_root = Path(__file__).resolve().parent
    if str(repo_root) not in os.sys.path:
        os.sys.path.insert(0, str(repo_root))
    os.sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


CLI = _load_cli()

CANONICAL_PREFIX = (
    "Benchmarks/Case-00-Triborough/derived/attorney-feedback-eval/candidate-answers/"
)

ARTIFACTS = (
    "Q1_candidate_answer.json",
    "Q1_candidate_answer.md",
    "generation_manifest.json",
    "model_input_audit.json",
    "case00_attorney_review_packet.md",
)


def _b2_env() -> dict[str, str]:
    return {
        "B2_KEY_ID": "key-id-secret-value",
        "B2_APPLICATION_KEY": "app-key-secret-value",
        "B2_BUCKET": "legalai-corpus",
        "B2_ENDPOINT": "https://s3.us-east-005.backblazeb2.com",
        "B2_REGION": "us-east-005",
    }


def _acceptance_env() -> dict[str, str]:
    """Synthetic production pins — not real Case-00 contract keys/hashes."""
    return {
        CLI.ACCEPTANCE_CONTRACT_OBJECT_KEY_ENV: (
            "Contracts/synthetic/alpha/Q-SYNTH-01.acceptance_contract.json"
        ),
        CLI.ACCEPTANCE_CONTRACT_CONTENT_SHA256_ENV: "a" * 64,
        CLI.ACCEPTANCE_CONTRACT_BENCHMARK_ID_ENV: "synth-benchmark-alpha",
    }


def _wrapper_env() -> dict[str, str]:
    return {**_b2_env(), **_acceptance_env()}


def _seed_candidate_dir(path: Path) -> dict[str, int]:
    path.mkdir(parents=True, exist_ok=True)
    sizes: dict[str, int] = {}
    for index, name in enumerate(ARTIFACTS):
        body = f"artifact-{name}-{index}\n".encode("utf-8")
        (path / name).write_bytes(body)
        sizes[name] = len(body)
    return sizes


class NormalizePrefixTests(unittest.TestCase):
    def test_canonical_prefix_normalizes_with_trailing_slash(self) -> None:
        self.assertEqual(
            CLI.normalize_candidate_b2_prefix(CANONICAL_PREFIX.rstrip("/")),
            CANONICAL_PREFIX,
        )

    def test_unsafe_prefixes_rejected(self) -> None:
        for bad in (
            "",
            "   ",
            "../escape/",
            "Benchmarks/../other/",
            "/tmp/case00-runs",
            "tmp/case00-runs",
            "~/Benchmarks/",
            "s3://bucket/prefix/",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(CLI.DurableUploadError):
                    CLI.normalize_candidate_b2_prefix(bad)


class DurableUploadUnitTests(unittest.TestCase):
    def test_renders_review_packet_from_finalized_candidate_without_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_root = Path(tmp) / "case"
            candidate = Path(tmp) / "q1-candidate-render"
            candidate.mkdir(parents=True)
            candidate_json = candidate / "Q1_candidate_answer.json"
            candidate_json.write_text("{}\n", encoding="utf-8")
            expected = candidate / "case00_attorney_review_packet.md"
            generation = {"ok": True, "finalized": True}
            with patch.object(
                CLI,
                "write_attorney_review_packet",
                return_value=expected,
            ) as write_packet:
                actual = CLI.render_candidate_review_packet(
                    case_root,
                    candidate,
                    generation,
                    question_id="Q1",
                )

            self.assertEqual(actual, expected)
            args, kwargs = write_packet.call_args
            self.assertEqual(args[0], candidate_json)
            evaluation = args[1]
            diagnostic = evaluation["questions"][0][
                "candidate_vs_reference_diagnostics"
            ]
            self.assertFalse(diagnostic["comparison_performed"])
            self.assertEqual(
                diagnostic["method"], "generation_acceptance_validation_only"
            )
            self.assertEqual(kwargs["output_path"], expected)
            self.assertEqual(kwargs["generation"], generation)

    def test_review_packet_render_failure_is_privacy_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "q1-candidate-render-failure"
            candidate.mkdir(parents=True)
            (candidate / "Q1_candidate_answer.json").write_text(
                "{}\n", encoding="utf-8"
            )
            with patch.object(
                CLI,
                "write_attorney_review_packet",
                side_effect=RuntimeError("PRIVATE CASE BODY"),
            ):
                with self.assertRaises(CLI.DurableUploadError) as ctx:
                    CLI.render_candidate_review_packet(
                        Path(tmp) / "case",
                        candidate,
                        {"ok": True, "finalized": True},
                        question_id="Q1",
                    )
            self.assertEqual(ctx.exception.details["error_type"], "RuntimeError")
            self.assertNotIn("PRIVATE CASE BODY", json.dumps(ctx.exception.details))

    def test_uploads_all_five_with_exact_canonical_keys_and_head_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "q1-candidate-20260808T154500Z"
            sizes = _seed_candidate_dir(candidate)
            client = MagicMock()
            uploaded: list[tuple[str, str, str]] = []

            def fake_upload(filename, bucket, key):
                uploaded.append((filename, bucket, key))

            def fake_head(*, Bucket, Key):
                name = Key.rsplit("/", 1)[-1]
                return {"ContentLength": sizes[name], "ETag": f'"{name}-etag"'}

            client.upload_file.side_effect = fake_upload
            client.head_object.side_effect = fake_head
            config = CLI.rebuild_cli.B2Config.from_env(_b2_env())

            durable = CLI.upload_candidate_artifacts_to_b2(
                candidate,
                prefix=CANONICAL_PREFIX,
                client=client,
                config=config,
            )

            expected_keys = [
                f"{CANONICAL_PREFIX}{candidate.name}/{name}" for name in ARTIFACTS
            ]
            self.assertEqual(durable["bucket"], "legalai-corpus")
            self.assertEqual(durable["prefix"], CANONICAL_PREFIX)
            self.assertEqual(durable["object_keys"], expected_keys)
            self.assertEqual(len(uploaded), 5)
            self.assertEqual([item[2] for item in uploaded], expected_keys)
            self.assertEqual(client.head_object.call_count, 5)
            for obj, name in zip(durable["objects"], ARTIFACTS):
                self.assertEqual(obj["filename"], name)
                self.assertEqual(obj["size"], sizes[name])
                self.assertEqual(obj["etag"], f"{name}-etag")

    def test_size_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "q1-candidate-size-mismatch"
            sizes = _seed_candidate_dir(candidate)
            client = MagicMock()
            client.upload_file.return_value = None

            def fake_head(*, Bucket, Key):
                name = Key.rsplit("/", 1)[-1]
                # First three match; last mismatches.
                if name == ARTIFACTS[-1]:
                    return {"ContentLength": sizes[name] + 99, "ETag": '"bad"'}
                return {"ContentLength": sizes[name], "ETag": '"ok"'}

            client.head_object.side_effect = fake_head
            config = CLI.rebuild_cli.B2Config.from_env(_b2_env())
            with self.assertRaises(CLI.DurableUploadError) as ctx:
                CLI.upload_candidate_artifacts_to_b2(
                    candidate,
                    prefix=CANONICAL_PREFIX,
                    client=client,
                    config=config,
                )
            self.assertIn("size mismatch", ctx.exception.message)

    def test_partial_upload_failure_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "q1-candidate-partial"
            sizes = _seed_candidate_dir(candidate)
            client = MagicMock()
            calls = {"n": 0}

            def fake_upload(filename, bucket, key):
                calls["n"] += 1
                if calls["n"] == 3:
                    raise RuntimeError("simulated upload failure")

            def fake_head(*, Bucket, Key):
                name = Key.rsplit("/", 1)[-1]
                return {"ContentLength": sizes[name], "ETag": '"ok"'}

            client.upload_file.side_effect = fake_upload
            client.head_object.side_effect = fake_head
            config = CLI.rebuild_cli.B2Config.from_env(_b2_env())
            with self.assertRaises(CLI.DurableUploadError) as ctx:
                CLI.upload_candidate_artifacts_to_b2(
                    candidate,
                    prefix=CANONICAL_PREFIX,
                    client=client,
                    config=config,
                )
            self.assertIn("upload failed", ctx.exception.message)
            self.assertEqual(client.head_object.call_count, 2)
            self.assertEqual(calls["n"], 3)

    def test_missing_local_artifact_fails_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "q1-candidate-missing"
            sizes = _seed_candidate_dir(candidate)
            (candidate / ARTIFACTS[0]).unlink()
            client = MagicMock()
            config = CLI.rebuild_cli.B2Config.from_env(_b2_env())
            with self.assertRaises(CLI.DurableUploadError) as ctx:
                CLI.upload_candidate_artifacts_to_b2(
                    candidate,
                    prefix=CANONICAL_PREFIX,
                    client=client,
                    config=config,
                )
            self.assertIn("missing before upload", ctx.exception.message)
            client.upload_file.assert_not_called()
            self.assertEqual(sizes[ARTIFACTS[1]], (candidate / ARTIFACTS[1]).stat().st_size)

    def test_no_secret_leakage_in_errors_or_durable_payload(self) -> None:
        env = _b2_env()
        config = CLI.rebuild_cli.B2Config.from_env(env)
        rendered = repr(config)
        self.assertNotIn("key-id-secret-value", rendered)
        self.assertNotIn("app-key-secret-value", rendered)

        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "q1-candidate-secrets"
            _seed_candidate_dir(candidate)
            client = MagicMock()
            client.upload_file.side_effect = RuntimeError(
                "boom key-id-secret-value app-key-secret-value"
            )
            with self.assertRaises(CLI.DurableUploadError) as ctx:
                CLI.upload_candidate_artifacts_to_b2(
                    candidate,
                    prefix=CANONICAL_PREFIX,
                    client=client,
                    config=config,
                )
            message = ctx.exception.message
            details = json.dumps(ctx.exception.details, sort_keys=True)
            self.assertNotIn("key-id-secret-value", message)
            self.assertNotIn("app-key-secret-value", message)
            self.assertNotIn("key-id-secret-value", details)
            self.assertNotIn("app-key-secret-value", details)

    def test_tmp_local_output_alone_cannot_produce_durable_success(self) -> None:
        """Local /tmp (or any ephemeral root) without verified B2 is not success."""
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            candidate = Path(tmp) / "q1-candidate-ephemeral-only"
            _seed_candidate_dir(candidate)
            self.assertTrue(str(candidate).startswith("/tmp"))
            # Generation-shaped local success payload — not durable by itself.
            generation_payload = {
                "ok": True,
                "finalized": True,
                "candidate_directory": str(candidate),
                "files": {name: str(candidate / name) for name in ARTIFACTS},
            }
            self.assertTrue(generation_payload["ok"])
            client = MagicMock()
            client.upload_file.side_effect = RuntimeError("B2 unavailable")
            config = CLI.rebuild_cli.B2Config.from_env(_b2_env())
            with self.assertRaises(CLI.DurableUploadError):
                CLI.upload_candidate_artifacts_to_b2(
                    candidate,
                    prefix=CANONICAL_PREFIX,
                    client=client,
                    config=config,
                )

            # Wrapper main must also fail closed when upload cannot verify.
            rebuild_ok = MagicMock(
                returncode=0, stdout='{"ok": true}\n', stderr=""
            )
            generation_ok = MagicMock(
                returncode=0,
                stdout=json.dumps(generation_payload, indent=2) + "\n",
                stderr="",
            )
            with patch.dict(os.environ, _wrapper_env(), clear=False):
                with patch.object(CLI, "_run", side_effect=[rebuild_ok, generation_ok]):
                    with patch.object(
                        CLI.rebuild_cli,
                        "create_b2_client",
                        return_value=client,
                    ):
                        with patch.object(
                            CLI,
                            "render_candidate_review_packet",
                            return_value=candidate / "case00_attorney_review_packet.md",
                        ):
                            captured = io.StringIO()
                            with patch("sys.stdout", captured):
                                code = CLI.main(
                                [
                                    "--case-root",
                                    str(candidate.parent),
                                    "--question-id",
                                    "Q1",
                                    "--required-commit",
                                    "a" * 40,
                                    "--candidate-output-root",
                                    str(candidate.parent),
                                    "--authorization-confirmed",
                                    "--generation-only",
                                ]
                            )
            self.assertNotEqual(code, 0)
            payload = json.loads(captured.getvalue())
            self.assertFalse(payload.get("ok"))
            self.assertEqual(payload.get("phase"), "durable_upload")
            self.assertNotIn("durable_artifacts", payload)
            blob = json.dumps(payload)
            self.assertNotIn("key-id-secret-value", blob)
            self.assertNotIn("app-key-secret-value", blob)

    def test_wrapper_success_returns_durable_artifacts_and_ephemeral_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "q1-candidate-20260808T160000Z"
            sizes = _seed_candidate_dir(candidate)
            generation_payload = {
                "ok": True,
                "finalized": True,
                "candidate_directory": str(candidate),
            }
            rebuild_ok = MagicMock(returncode=0, stdout="{}\n", stderr="")
            generation_ok = MagicMock(
                returncode=0,
                stdout=json.dumps(generation_payload) + "\n",
                stderr="",
            )
            client = MagicMock()

            def fake_head(*, Bucket, Key):
                name = Key.rsplit("/", 1)[-1]
                return {"ContentLength": sizes[name], "ETag": f'"{name}"'}

            client.upload_file.return_value = None
            client.head_object.side_effect = fake_head
            run_calls: list[list[str]] = []

            def capture_run(argv, cwd):
                run_calls.append(list(argv))
                if len(run_calls) == 1:
                    return rebuild_ok
                return generation_ok

            with patch.dict(os.environ, _wrapper_env(), clear=False):
                with patch.object(CLI, "_run", side_effect=capture_run):
                    with patch.object(
                        CLI.rebuild_cli,
                        "create_b2_client",
                        return_value=client,
                    ):
                        with patch.object(
                            CLI,
                            "render_candidate_review_packet",
                            return_value=candidate / "case00_attorney_review_packet.md",
                        ):
                            captured = io.StringIO()
                            with patch("sys.stdout", captured):
                                code = CLI.main(
                                    [
                                        "--case-root",
                                        str(candidate.parent),
                                        "--question-id",
                                        "Q1",
                                        "--required-commit",
                                        "b" * 40,
                                        "--candidate-output-root",
                                        str(candidate.parent),
                                        "--authorization-confirmed",
                                        "--generation-only",
                                    ]
                                )
            self.assertEqual(code, 0)
            self.assertEqual(len(run_calls), 2)
            gen_argv = run_calls[1]
            self.assertIn("--acceptance-contract-object-key", gen_argv)
            self.assertIn(
                "Contracts/synthetic/alpha/Q-SYNTH-01.acceptance_contract.json",
                gen_argv,
            )
            self.assertIn("--acceptance-contract-content-sha256", gen_argv)
            self.assertIn("a" * 64, gen_argv)
            self.assertIn("--acceptance-contract-benchmark-id", gen_argv)
            self.assertIn("synth-benchmark-alpha", gen_argv)
            self.assertIn("--question-id", gen_argv)
            self.assertIn("Q1", gen_argv)
            payload = json.loads(captured.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(
                payload["ephemeral_local_directory"], str(candidate.resolve())
            )
            durable = payload["durable_artifacts"]
            self.assertEqual(durable["bucket"], "legalai-corpus")
            self.assertEqual(durable["prefix"], CANONICAL_PREFIX)
            self.assertEqual(
                durable["object_keys"],
                [f"{CANONICAL_PREFIX}{candidate.name}/{name}" for name in ARTIFACTS],
            )
            blob = json.dumps(payload)
            self.assertNotIn("key-id-secret-value", blob)
            self.assertNotIn("app-key-secret-value", blob)

    def test_cli_override_prefix_used_and_keys_stay_under_it(self) -> None:
        override = "Benchmarks/Case-00-Triborough/derived/custom-candidates/"
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "q1-candidate-override"
            sizes = _seed_candidate_dir(candidate)
            client = MagicMock()

            def fake_head(*, Bucket, Key):
                self.assertTrue(Key.startswith(override))
                name = Key.rsplit("/", 1)[-1]
                return {"ContentLength": sizes[name], "ETag": '"x"'}

            client.upload_file.return_value = None
            client.head_object.side_effect = fake_head
            config = CLI.rebuild_cli.B2Config.from_env(_b2_env())
            durable = CLI.upload_candidate_artifacts_to_b2(
                candidate,
                prefix=override,
                client=client,
                config=config,
            )
            self.assertEqual(durable["prefix"], override)
            self.assertTrue(
                all(key.startswith(override) for key in durable["object_keys"])
            )

    def test_key_outside_prefix_rejected(self) -> None:
        with self.assertRaises(CLI.DurableUploadError):
            CLI.assert_key_under_prefix(
                "Benchmarks/other/q1-candidate-x/Q1_candidate_answer.json",
                CANONICAL_PREFIX,
            )


if __name__ == "__main__":
    unittest.main()
