#!/usr/bin/env python3
"""Deterministic Case-00 derived-dataset rebuild CLI.

Rebuilds the generator-required derived JSON artifacts from source PDFs by
calling existing ``matter_builder`` canonical ingestion helpers (no duplicated
extraction logic, no model-provider calls).

Source modes:
  * ``--source-dir`` — local PDF directory
  * ``--b2-prefix`` — materialize a Backblaze B2 prefix into a temp directory,
    then use the same local ingest path

Writes only:
  derived/page-extraction/canonical_page_records.json
  derived/exhibit-segmentation/filing_exhibit_map.json
  derived/case-map/case_map.json

Never overwrites source PDFs or attorney/gold/benchmark artifacts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import matter_builder as mb  # noqa: E402

# Default B2 object prefix for Case-00 source PDFs (path only — not case facts).
DEFAULT_CASE00_B2_PREFIX = (
    "Benchmarks/Case-00-Triborough/original/Tribrough Full Docket/"
)

B2_ENV_NAMES = (
    "B2_KEY_ID",
    "B2_APPLICATION_KEY",
    "B2_BUCKET",
    "B2_ENDPOINT",
    "B2_REGION",
)

DERIVED_RELATIVE_PATHS = {
    "page_records": Path("derived/page-extraction/canonical_page_records.json"),
    "exhibit_map": Path("derived/exhibit-segmentation/filing_exhibit_map.json"),
    "case_map": Path("derived/case-map/case_map.json"),
}

# Artifacts the rebuild must never modify (gold / attorney / question packets).
_PRESERVED_PATH_MARKERS = (
    "attorney-gold-benchmark",
    "provisional-gold-answers",
    "attorney-approved-gold-answers",
    "attorney_gold_labels",
    "attorney-feedback-eval",
    "attorney-review-packet",
    "question-text",
    "candidate-answers",
    "source-pdfs",
)


class RebuildError(Exception):
    """Fail-closed rebuild / validation error."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


@dataclass(frozen=True)
class B2Config:
    """B2 connection settings; secrets never appear in ``repr``."""

    key_id: str
    application_key: str
    bucket: str
    endpoint: str
    region: str

    def __repr__(self) -> str:
        return (
            "B2Config("
            "key_id='***', "
            "application_key='***', "
            f"bucket={self.bucket!r}, "
            f"endpoint={self.endpoint!r}, "
            f"region={self.region!r})"
        )

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "B2Config":
        env = os.environ if environ is None else environ
        missing = [
            name for name in B2_ENV_NAMES if not str(env.get(name, "")).strip()
        ]
        if missing:
            raise RebuildError(
                "Missing required B2 environment variables: " + ", ".join(missing),
                missing=missing,
            )
        return cls(
            key_id=str(env["B2_KEY_ID"]).strip(),
            application_key=str(env["B2_APPLICATION_KEY"]).strip(),
            bucket=str(env["B2_BUCKET"]).strip(),
            endpoint=str(env["B2_ENDPOINT"]).strip(),
            region=str(env["B2_REGION"]).strip(),
        )


def create_b2_client(config: B2Config):
    """Build a boto3 S3 client for the B2 S3-compatible endpoint."""
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=config.endpoint,
        aws_access_key_id=config.key_id,
        aws_secret_access_key=config.application_key,
        region_name=config.region,
    )


def resolve_derived_paths(case_root: Path) -> dict[str, Path]:
    root = case_root.resolve()
    return {name: root / rel for name, rel in DERIVED_RELATIVE_PATHS.items()}


def resolve_inventory_path(
    case_root: Path, inventory_path: Optional[Path] = None
) -> Path:
    if inventory_path is not None:
        return Path(inventory_path).resolve()
    local = case_root.resolve() / "nyscef_filing_inventory.json"
    if local.is_file():
        return local
    fallback = REPO_ROOT / "data" / "case-00-triborough" / "nyscef_filing_inventory.json"
    return fallback.resolve()


def _assert_not_preserved_target(path: Path) -> None:
    text = str(path).replace("\\", "/").lower()
    for marker in _PRESERVED_PATH_MARKERS:
        if marker.lower() in text:
            raise RebuildError(
                f"Refusing to write preserved artifact path: {path}",
                path=str(path),
                marker=marker,
            )


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON via a same-directory temp file + ``os.replace`` (atomic)."""
    _assert_not_preserved_target(path)
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def list_b2_keys(client: Any, bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    continuation_token: Optional[str] = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if continuation_token is not None:
            kwargs["ContinuationToken"] = continuation_token
        response = client.list_objects_v2(**kwargs)
        for item in response.get("Contents") or ():
            key = item.get("Key")
            if key is None:
                continue
            key_str = str(key)
            if key_str.endswith("/"):
                continue
            keys.append(key_str)
        if not response.get("IsTruncated"):
            break
        continuation_token = response.get("NextContinuationToken")
        if not continuation_token:
            break
    return sorted(keys)


def materialize_b2_prefix(
    prefix: str,
    dest_dir: Path,
    *,
    client: Optional[Any] = None,
    config: Optional[B2Config] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    """Download objects under ``prefix`` into ``dest_dir`` (local PDF tree)."""
    cfg = config if config is not None else B2Config.from_env(environ)
    s3 = client if client is not None else create_b2_client(cfg)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    normalized_prefix = prefix if prefix.endswith("/") else prefix + "/"
    keys = list_b2_keys(s3, cfg.bucket, normalized_prefix)
    if not keys:
        # Also try the raw prefix in case the caller already included a filename.
        keys = list_b2_keys(s3, cfg.bucket, prefix)
    if not keys:
        raise RebuildError(
            f"No B2 objects found under prefix {prefix!r}",
            prefix=prefix,
            bucket=cfg.bucket,
        )

    for key in keys:
        if key.startswith(normalized_prefix):
            relative = key[len(normalized_prefix) :]
        elif key.startswith(prefix):
            relative = key[len(prefix) :].lstrip("/")
        else:
            relative = Path(key).name
        if not relative or relative.endswith("/"):
            continue
        local_path = dest / relative
        local_path.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(cfg.bucket, key, str(local_path))

    return dest


def _page_sort_key(page: dict) -> tuple[int, int]:
    return (
        int(page.get("nyscef_document_number") or 0),
        int(page.get("page_number") or 0),
    )


def build_canonical_page_records(documents: list[dict]) -> dict[str, Any]:
    pages: list[dict] = []
    for document in documents:
        nyscef = document.get("nyscef_document_number")
        if nyscef is None:
            continue
        nyscef_int = int(nyscef)
        filename = document.get("filename") or document.get("title") or ""
        source_path = document.get("path") or ""
        for page in document.get("pages") or []:
            if not isinstance(page, dict):
                continue
            record = {
                "page_number": int(page["page_number"]),
                "page_id": page.get("page_id")
                or mb.make_page_id(nyscef_int, page["page_number"]),
                "text": page.get("text") if isinstance(page.get("text"), str) else "",
                "extraction_method": page.get("extraction_method") or "empty",
                "nyscef_document_number": nyscef_int,
                "pdf_page_number": int(page["page_number"]),
                "source_filename": filename,
                "source_path": source_path,
            }
            pages.append(record)
    pages.sort(key=_page_sort_key)
    return {"pages": pages}


def build_filing_exhibit_map(documents: list[dict]) -> dict[str, Any]:
    filings: list[dict] = []
    for document in documents:
        nyscef = document.get("nyscef_document_number")
        if nyscef is None:
            continue
        filings.append(
            {
                "nyscef_document_number": int(nyscef),
                "segments": list(document.get("exhibit_segments") or []),
                "uncertain_boundaries": list(
                    document.get("uncertain_exhibit_boundaries")
                    or document.get("uncertain_boundaries")
                    or []
                ),
            }
        )
    filings.sort(key=lambda f: int(f["nyscef_document_number"]))
    return {"filings": filings}


def ingest_source_directory(
    source_dir: Path,
    inventory_path: Path,
) -> list[dict]:
    """Run canonical matter_builder ingestion over a local PDF directory."""
    source = Path(source_dir).resolve()
    if not source.is_dir():
        raise RebuildError(f"Source directory does not exist: {source}")

    raw_documents = mb.read_matter_folder(
        folder_path=source,
        inventory_path=inventory_path,
    )
    normalized: list[dict] = []
    for document in raw_documents:
        normalized.append(
            mb.normalize_document(document, include_exhibit_segments=True)
        )
    normalized.sort(
        key=lambda d: (
            int(d["nyscef_document_number"])
            if d.get("nyscef_document_number") is not None
            else 10**9,
            str(d.get("filename") or ""),
        )
    )
    return normalized


def build_derived_payloads(documents: list[dict]) -> dict[str, Any]:
    page_records = build_canonical_page_records(documents)
    exhibit_map = build_filing_exhibit_map(documents)
    case_map = mb.build_case_map_from_documents(documents)
    return {
        "page_records": page_records,
        "exhibit_map": exhibit_map,
        "case_map": {"case_map": case_map},
    }


def write_derived_artifacts(case_root: Path, payloads: dict[str, Any]) -> dict[str, Path]:
    paths = resolve_derived_paths(case_root)
    atomic_write_json(paths["page_records"], payloads["page_records"])
    atomic_write_json(paths["exhibit_map"], payloads["exhibit_map"])
    atomic_write_json(paths["case_map"], payloads["case_map"])
    return paths


def _load_json_file(path: Path) -> Any:
    if not path.is_file():
        raise RebuildError(f"Required input missing: {path}", path=str(path))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RebuildError(
            f"Invalid JSON: {path}",
            path=str(path),
            error=str(exc),
        ) from exc


def validate_generator_inputs(
    case_root: Path,
    *,
    inventory_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Check generator-required local inputs without invoking a model provider."""
    root = case_root.resolve()
    paths = resolve_derived_paths(root)
    inv_path = resolve_inventory_path(root, inventory_path)
    errors: list[str] = []

    page_wrap = None
    exhibit_map = None
    case_map_wrap = None

    try:
        page_wrap = _load_json_file(paths["page_records"])
        if not isinstance(page_wrap, dict) or not isinstance(page_wrap.get("pages"), list):
            errors.append(f"{paths['page_records']} missing pages list")
    except RebuildError as exc:
        errors.append(exc.message)

    try:
        exhibit_map = _load_json_file(paths["exhibit_map"])
        if not isinstance(exhibit_map, dict) or not isinstance(
            exhibit_map.get("filings"), list
        ):
            errors.append(f"{paths['exhibit_map']} missing filings list")
    except RebuildError as exc:
        errors.append(exc.message)

    try:
        case_map_wrap = _load_json_file(paths["case_map"])
        if not isinstance(case_map_wrap, dict):
            errors.append(f"{paths['case_map']} is not a JSON object")
        else:
            case_map = case_map_wrap.get("case_map")
            usable = isinstance(case_map, dict) or (
                "parties" in case_map_wrap
                or "nodes" in case_map_wrap
                or "filings" in case_map_wrap
            )
            if not usable:
                errors.append(f"{paths['case_map']} missing usable case_map object")
    except RebuildError as exc:
        errors.append(exc.message)

    if not inv_path.is_file():
        errors.append(f"NYSCEF inventory missing: {inv_path}")
    else:
        inventory = mb.load_nyscef_filing_inventory(inv_path)
        if not inventory:
            errors.append(f"NYSCEF inventory unavailable: {inv_path}")

    # Question text inputs are preserved (not rebuilt); generator still needs one.
    question_text = root / "derived" / "question-text" / "questions.json"
    question_packet = (
        root
        / "derived"
        / "attorney-review-packet-02-live"
        / "attorney_review_packet_02.json"
    )
    if not question_text.is_file() and not question_packet.is_file():
        errors.append(
            "Missing question text input "
            "(derived/question-text/questions.json or "
            "derived/attorney-review-packet-02-live/attorney_review_packet_02.json)"
        )

    ok = not errors
    return {
        "ok": ok,
        "errors": errors,
        "paths": {k: str(v) for k, v in paths.items()},
        "inventory_path": str(inv_path),
        "page_count": len((page_wrap or {}).get("pages") or []) if page_wrap else 0,
        "filing_count": len((exhibit_map or {}).get("filings") or [])
        if exhibit_map
        else 0,
    }


def rebuild_case00_derived(
    *,
    case_root: Path,
    source_dir: Optional[Path] = None,
    b2_prefix: Optional[str] = None,
    inventory_path: Optional[Path] = None,
    b2_client: Optional[Any] = None,
    b2_config: Optional[B2Config] = None,
    environ: Optional[Mapping[str, str]] = None,
    validate: bool = True,
) -> dict[str, Any]:
    """Rebuild derived artifacts from ``source_dir`` or a materialized B2 prefix."""
    root = Path(case_root).resolve()
    if not root.is_dir():
        raise RebuildError(f"Case root does not exist: {root}")

    inv_path = resolve_inventory_path(root, inventory_path)
    if not inv_path.is_file():
        raise RebuildError(f"NYSCEF inventory missing: {inv_path}")

    if source_dir is not None and b2_prefix is not None:
        raise RebuildError("Pass only one of --source-dir or --b2-prefix")
    if source_dir is None and b2_prefix is None:
        raise RebuildError("One of --source-dir or --b2-prefix is required")

    temp_ctx = None
    try:
        if b2_prefix is not None:
            temp_ctx = tempfile.TemporaryDirectory(prefix="case00-b2-")
            local_source = materialize_b2_prefix(
                b2_prefix,
                Path(temp_ctx.name),
                client=b2_client,
                config=b2_config,
                environ=environ,
            )
        else:
            assert source_dir is not None
            local_source = Path(source_dir)

        documents = ingest_source_directory(local_source, inv_path)
        if not documents:
            raise RebuildError(
                f"No documents ingested from {local_source}",
                source=str(local_source),
            )

        payloads = build_derived_payloads(documents)
        written = write_derived_artifacts(root, payloads)

        result: dict[str, Any] = {
            "ok": True,
            "case_root": str(root),
            "source_dir": str(Path(local_source).resolve()),
            "inventory_path": str(inv_path),
            "written": {k: str(v) for k, v in written.items()},
            "document_count": len(documents),
            "page_count": len(payloads["page_records"]["pages"]),
            "filing_count": len(payloads["exhibit_map"]["filings"]),
        }
        if validate:
            validation = validate_generator_inputs(root, inventory_path=inv_path)
            result["validation"] = validation
            if not validation["ok"]:
                result["ok"] = False
                raise RebuildError(
                    "Rebuild wrote artifacts but validation failed: "
                    + "; ".join(validation["errors"]),
                    validation=validation,
                )
        return result
    finally:
        if temp_ctx is not None:
            temp_ctx.cleanup()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild Case-00 derived page/exhibit/case-map JSON from source PDFs "
            "via matter_builder canonical ingestion (no model calls)."
        )
    )
    parser.add_argument(
        "--case-root",
        type=Path,
        required=True,
        help="Case-00 corpus root (contains derived/ and inventory).",
    )
    parser.add_argument(
        "--inventory-path",
        type=Path,
        default=None,
        help="Optional explicit NYSCEF inventory JSON path.",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="Local directory of source PDFs (never modified).",
    )
    parser.add_argument(
        "--b2-prefix",
        nargs="?",
        const=DEFAULT_CASE00_B2_PREFIX,
        default=None,
        help=(
            "B2 object prefix to materialize into a temp directory before ingest. "
            f"If flag is present without a value, defaults to {DEFAULT_CASE00_B2_PREFIX!r}."
        ),
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help=(
            "Only check that generator-required local inputs exist "
            "(no rebuild, no model provider)."
        ),
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.validate_only:
            report = validate_generator_inputs(
                args.case_root,
                inventory_path=args.inventory_path,
            )
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0 if report["ok"] else 2

        result = rebuild_case00_derived(
            case_root=args.case_root,
            source_dir=args.source_dir,
            b2_prefix=args.b2_prefix,
            inventory_path=args.inventory_path,
        )
        # Never include env / credential material in stdout.
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 1
    except RebuildError as exc:
        payload = {"ok": False, "error": exc.message}
        if exc.details:
            # Drop any accidental secret-bearing detail keys.
            safe = {
                k: v
                for k, v in exc.details.items()
                if k.lower() not in {"key_id", "application_key", "secret"}
            }
            payload["details"] = safe
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
