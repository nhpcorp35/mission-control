"""Focused tests for scripts/rebuild_case00_derived.py."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pypdf import PdfWriter


def _load_cli():
    path = Path(__file__).resolve().parent / "scripts" / "rebuild_case00_derived.py"
    spec = importlib.util.spec_from_file_location("rebuild_case00_derived", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Ensure legal-ai (or PYTHONPATH) root is importable for matter_builder.
    repo_root = Path(__file__).resolve().parent
    if str(repo_root) not in os.sys.path:
        os.sys.path.insert(0, str(repo_root))
    # Register before exec so @dataclass can resolve the module namespace.
    os.sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


CLI = _load_cli()


def _write_blank_pdf(path: Path, page_count: int = 1) -> Path:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def _mock_reader(page_texts):
    reader = MagicMock()
    pages = []
    for text in page_texts:
        page = MagicMock()
        page.extract_text.return_value = text
        pages.append(page)
    reader.pages = pages
    return reader


ADEQUATE = "Party roster and caption text " + ("x" * 120)


def _b2_env() -> dict[str, str]:
    return {
        "B2_KEY_ID": "key-id-secret-value",
        "B2_APPLICATION_KEY": "app-key-secret-value",
        "B2_BUCKET": "legalai-corpus",
        "B2_ENDPOINT": "https://s3.us-east-005.backblazeb2.com",
        "B2_REGION": "us-east-005",
    }


def _inventory_payload(filename: str, nyscef: int, sha256: str) -> dict:
    return {
        "inventory_version": "1.0",
        "corpus_id": "case-00-fixture",
        "filings": [
            {
                "filename": filename,
                "nyscef_document_number": nyscef,
                "page_count": 1,
                "sha256": sha256,
                "ingest_canonical": True,
                "duplicate_marker": False,
            }
        ],
    }


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _seed_case_root(case_root: Path, *, with_question: bool = True) -> None:
    """Create preserved attorney/gold artifacts that rebuild must not touch."""
    gold = case_root / "derived" / "attorney-gold-benchmark-01" / "labels.json"
    gold.parent.mkdir(parents=True)
    gold.write_text(
        json.dumps({"Q1": {"gold": "do-not-overwrite"}}, indent=2) + "\n",
        encoding="utf-8",
    )
    provisional = (
        case_root / "derived" / "attorney-gold-benchmark-01" / "provisional-gold-answers"
    )
    provisional.mkdir(parents=True, exist_ok=True)
    (provisional / "answers.json").write_text(
        json.dumps({"Q1": "provisional"}, indent=2) + "\n",
        encoding="utf-8",
    )
    packet_dir = case_root / "derived" / "attorney-review-packet-02-live"
    packet_dir.mkdir(parents=True, exist_ok=True)
    (packet_dir / "attorney_review_packet_02.json").write_text(
        json.dumps(
            {
                "questions": [
                    {
                        "question_id": "Q1",
                        "text": "Who are the parties?",
                    }
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if with_question:
        qdir = case_root / "derived" / "question-text"
        qdir.mkdir(parents=True, exist_ok=True)
        (qdir / "questions.json").write_text(
            json.dumps({"Q1": "Who are the parties?"}, indent=2) + "\n",
            encoding="utf-8",
        )


class RebuildCase00LocalSourceTests(unittest.TestCase):
    def test_local_source_rebuild_writes_deterministic_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case_root = root / "case-00"
            source_dir = root / "pdfs"
            case_root.mkdir()
            _seed_case_root(case_root)

            filename = "nyscef_doc_no_3_complaint.pdf"
            pdf_path = source_dir / filename
            _write_blank_pdf(pdf_path, 2)
            digest = _sha256_file(pdf_path)
            inv_path = case_root / "nyscef_filing_inventory.json"
            inv_path.write_text(
                json.dumps(_inventory_payload(filename, 3, digest), indent=2) + "\n",
                encoding="utf-8",
            )

            page_texts = [ADEQUATE, ADEQUATE + " page two"]
            with patch.object(CLI.mb, "PdfReader", return_value=_mock_reader(page_texts)):
                with patch.object(CLI.mb, "extract_pdf_ocr_page", return_value=""):
                    result = CLI.rebuild_case00_derived(
                        case_root=case_root,
                        source_dir=source_dir,
                        inventory_path=inv_path,
                    )

            self.assertTrue(result["ok"])
            paths = CLI.resolve_derived_paths(case_root)
            self.assertEqual(
                paths["page_records"],
                case_root.resolve()
                / "derived"
                / "page-extraction"
                / "canonical_page_records.json",
            )
            self.assertEqual(
                paths["exhibit_map"],
                case_root.resolve()
                / "derived"
                / "exhibit-segmentation"
                / "filing_exhibit_map.json",
            )
            self.assertEqual(
                paths["case_map"],
                case_root.resolve() / "derived" / "case-map" / "case_map.json",
            )
            for path in paths.values():
                self.assertTrue(path.is_file(), path)

            page_wrap = json.loads(paths["page_records"].read_text(encoding="utf-8"))
            self.assertEqual(len(page_wrap["pages"]), 2)
            self.assertEqual(page_wrap["pages"][0]["nyscef_document_number"], 3)
            self.assertEqual(page_wrap["pages"][0]["page_id"], "nyscef-003-page-0001")

            exhibit_map = json.loads(paths["exhibit_map"].read_text(encoding="utf-8"))
            self.assertEqual(len(exhibit_map["filings"]), 1)
            self.assertEqual(exhibit_map["filings"][0]["nyscef_document_number"], 3)

            case_map_wrap = json.loads(paths["case_map"].read_text(encoding="utf-8"))
            self.assertIn("case_map", case_map_wrap)
            self.assertIsInstance(case_map_wrap["case_map"], dict)

            # Source PDF untouched (still the only file in source_dir).
            self.assertTrue(pdf_path.is_file())
            self.assertEqual(_sha256_file(pdf_path), digest)

    def test_preserves_attorney_and_gold_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case_root = root / "case-00"
            source_dir = root / "pdfs"
            case_root.mkdir()
            _seed_case_root(case_root)

            gold = (
                case_root
                / "derived"
                / "attorney-gold-benchmark-01"
                / "labels.json"
            )
            provisional = (
                case_root
                / "derived"
                / "attorney-gold-benchmark-01"
                / "provisional-gold-answers"
                / "answers.json"
            )
            packet = (
                case_root
                / "derived"
                / "attorney-review-packet-02-live"
                / "attorney_review_packet_02.json"
            )
            gold_before = gold.read_text(encoding="utf-8")
            provisional_before = provisional.read_text(encoding="utf-8")
            packet_before = packet.read_text(encoding="utf-8")
            gold_mtime = gold.stat().st_mtime_ns

            filename = "nyscef_doc_no_9_motion.pdf"
            pdf_path = source_dir / filename
            _write_blank_pdf(pdf_path, 1)
            inv_path = case_root / "nyscef_filing_inventory.json"
            inv_path.write_text(
                json.dumps(
                    _inventory_payload(filename, 9, _sha256_file(pdf_path)),
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(
                CLI.mb, "PdfReader", return_value=_mock_reader([ADEQUATE])
            ):
                with patch.object(CLI.mb, "extract_pdf_ocr_page", return_value=""):
                    CLI.rebuild_case00_derived(
                        case_root=case_root,
                        source_dir=source_dir,
                        inventory_path=inv_path,
                    )

            self.assertEqual(gold.read_text(encoding="utf-8"), gold_before)
            self.assertEqual(
                provisional.read_text(encoding="utf-8"), provisional_before
            )
            self.assertEqual(packet.read_text(encoding="utf-8"), packet_before)
            self.assertEqual(gold.stat().st_mtime_ns, gold_mtime)


class RebuildCase00B2Tests(unittest.TestCase):
    def test_b2_materialization_uses_mocked_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case_root = root / "case-00"
            case_root.mkdir()
            _seed_case_root(case_root)

            filename = "nyscef_doc_no_5_answer.pdf"
            pdf_bytes_path = root / "fixture.pdf"
            _write_blank_pdf(pdf_bytes_path, 1)
            pdf_bytes = pdf_bytes_path.read_bytes()
            digest = _sha256_file(pdf_bytes_path)

            inv_path = case_root / "nyscef_filing_inventory.json"
            inv_path.write_text(
                json.dumps(_inventory_payload(filename, 5, digest), indent=2) + "\n",
                encoding="utf-8",
            )

            prefix = "Benchmarks/Case-00-Triborough/original/Tribrough Full Docket/"
            key = prefix + filename
            client = MagicMock()
            client.list_objects_v2.return_value = {
                "Contents": [{"Key": key}],
                "IsTruncated": False,
            }

            def fake_download(bucket, object_key, filename_path):
                self.assertEqual(bucket, "legalai-corpus")
                self.assertEqual(object_key, key)
                Path(filename_path).parent.mkdir(parents=True, exist_ok=True)
                Path(filename_path).write_bytes(pdf_bytes)

            client.download_file.side_effect = fake_download
            config = CLI.B2Config.from_env(_b2_env())

            with patch.object(
                CLI.mb, "PdfReader", return_value=_mock_reader([ADEQUATE])
            ):
                with patch.object(CLI.mb, "extract_pdf_ocr_page", return_value=""):
                    result = CLI.rebuild_case00_derived(
                        case_root=case_root,
                        b2_prefix=prefix,
                        inventory_path=inv_path,
                        b2_client=client,
                        b2_config=config,
                    )

            self.assertTrue(result["ok"])
            client.list_objects_v2.assert_called()
            client.download_file.assert_called_once()
            paths = CLI.resolve_derived_paths(case_root)
            page_wrap = json.loads(paths["page_records"].read_text(encoding="utf-8"))
            self.assertEqual(page_wrap["pages"][0]["nyscef_document_number"], 5)

    def test_no_secret_logging_in_b2_config_or_errors(self) -> None:
        env = _b2_env()
        config = CLI.B2Config.from_env(env)
        rendered = repr(config)
        self.assertNotIn("key-id-secret-value", rendered)
        self.assertNotIn("app-key-secret-value", rendered)
        self.assertIn("***", rendered)

        incomplete = dict(env)
        del incomplete["B2_APPLICATION_KEY"]
        with self.assertRaises(CLI.RebuildError) as ctx:
            CLI.B2Config.from_env(incomplete)
        message = str(ctx.exception)
        self.assertIn("B2_APPLICATION_KEY", message)
        self.assertNotIn("app-key-secret-value", message)
        self.assertNotIn("key-id-secret-value", message)

        # CLI main stderr must not leak secrets when B2 env is incomplete.
        with tempfile.TemporaryDirectory() as tmp:
            case_root = Path(tmp) / "case"
            case_root.mkdir()
            _seed_case_root(case_root)
            (case_root / "nyscef_filing_inventory.json").write_text(
                json.dumps({"filings": []}, indent=2) + "\n",
                encoding="utf-8",
            )
            partial_env = {"B2_KEY_ID": "key-id-secret-value"}
            stderr = io.StringIO()
            with patch.dict(os.environ, partial_env, clear=True):
                with patch("sys.stderr", stderr):
                    code = CLI.main(
                        [
                            "--case-root",
                            str(case_root),
                            "--b2-prefix",
                        ]
                    )
            self.assertNotEqual(code, 0)
            err_text = stderr.getvalue()
            self.assertNotIn("key-id-secret-value", err_text)
            self.assertNotIn("app-key-secret-value", err_text)


class RebuildCase00ValidationTests(unittest.TestCase):
    def test_validate_only_success_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_root = Path(tmp) / "case-00"
            case_root.mkdir()
            _seed_case_root(case_root)
            inv_path = case_root / "nyscef_filing_inventory.json"
            inv_path.write_text(json.dumps({"filings": []}, indent=2) + "\n")

            # Failure: derived artifacts missing.
            report = CLI.validate_generator_inputs(
                case_root, inventory_path=inv_path
            )
            self.assertFalse(report["ok"])
            self.assertTrue(any("canonical_page_records" in e for e in report["errors"]))

            # Success: write minimal valid derived files.
            paths = CLI.resolve_derived_paths(case_root)
            CLI.atomic_write_json(paths["page_records"], {"pages": []})
            CLI.atomic_write_json(paths["exhibit_map"], {"filings": []})
            CLI.atomic_write_json(
                paths["case_map"], {"case_map": CLI.mb.empty_case_map()}
            )
            report_ok = CLI.validate_generator_inputs(
                case_root, inventory_path=inv_path
            )
            self.assertTrue(report_ok["ok"], report_ok["errors"])

            # validate-only CLI exit codes
            code_ok = CLI.main(
                [
                    "--case-root",
                    str(case_root),
                    "--inventory-path",
                    str(inv_path),
                    "--validate-only",
                ]
            )
            self.assertEqual(code_ok, 0)

            paths["page_records"].unlink()
            code_fail = CLI.main(
                [
                    "--case-root",
                    str(case_root),
                    "--inventory-path",
                    str(inv_path),
                    "--validate-only",
                ]
            )
            self.assertEqual(code_fail, 2)

    def test_atomic_write_leaves_no_half_json_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "derived" / "case-map" / "case_map.json"
            target.parent.mkdir(parents=True)
            target.write_text('{"case_map": {"ok": true}}\n', encoding="utf-8")
            before = target.read_text(encoding="utf-8")

            class Boom(Exception):
                pass

            real_dump = json.dump

            def failing_dump(*args, **kwargs):
                raise Boom("serialize failed")

            with patch.object(CLI.json, "dump", side_effect=failing_dump):
                with self.assertRaises(Boom):
                    CLI.atomic_write_json(target, {"case_map": {"ok": False}})

            self.assertEqual(target.read_text(encoding="utf-8"), before)
            leftovers = list(target.parent.glob(".case_map.json.*.tmp"))
            self.assertEqual(leftovers, [])
            # json.dump still available for other tests
            self.assertIs(CLI.json.dump, real_dump)


class RebuildCase00RerunSafetyTests(unittest.TestCase):
    def test_rerun_overwrites_only_derived_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case_root = root / "case-00"
            source_dir = root / "pdfs"
            case_root.mkdir()
            _seed_case_root(case_root)

            filename = "nyscef_doc_no_2_complaint.pdf"
            pdf_path = source_dir / filename
            _write_blank_pdf(pdf_path, 1)
            inv_path = case_root / "nyscef_filing_inventory.json"
            inv_path.write_text(
                json.dumps(
                    _inventory_payload(filename, 2, _sha256_file(pdf_path)),
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(
                CLI.mb, "PdfReader", return_value=_mock_reader([ADEQUATE])
            ):
                with patch.object(CLI.mb, "extract_pdf_ocr_page", return_value=""):
                    first = CLI.rebuild_case00_derived(
                        case_root=case_root,
                        source_dir=source_dir,
                        inventory_path=inv_path,
                    )
                    second = CLI.rebuild_case00_derived(
                        case_root=case_root,
                        source_dir=source_dir,
                        inventory_path=inv_path,
                    )

            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            paths = CLI.resolve_derived_paths(case_root)
            first_pages = json.loads(paths["page_records"].read_text(encoding="utf-8"))
            second_pages = json.loads(paths["page_records"].read_text(encoding="utf-8"))
            self.assertEqual(first_pages, second_pages)


if __name__ == "__main__":
    unittest.main()
