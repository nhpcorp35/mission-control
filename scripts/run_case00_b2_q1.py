#!/usr/bin/env python3
"""Rebuild Case-00 derived artifacts from B2, generate one candidate, upload to B2.

This wrapper runs rebuild + generation in the same checkout, renders the
attorney review packet, then uploads the five finalized candidate artifacts to
Backblaze B2. Local --candidate-output-root
paths (including /tmp) are ephemeral only; durable handoff is verified B2 object
keys under the canonical candidate prefix.

Question staging downloads the allowlisted canonical attorney-review markdown
packet from B2, verifies size and SHA-256, and extracts ``## QN.`` headings into
runner-local questions.json without logging packet or question body text.

Production Case-00 generation requires an acceptance-contract object key,
expected content SHA-256, and benchmark identity. Prefer the question-aware
canonical resolver (benchmark_id + question_id → list allowlisted versions,
select the highest compatible semantic version deterministically, then verify
size / identity / schema / embedded content hash / object integrity). CLI
flags or environment / secrets remain supported for compatibility. The
generator fails closed before model generation when the contract is absent,
invalid, identity-mismatched, or hash-mismatched. Never log or return contract
body bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import acceptance_contract as ac  # noqa: E402
import rebuild_case00_derived as rebuild_cli  # noqa: E402
from case00_attorney_eval.review_packet import (  # noqa: E402
    PACKET_FILENAME,
    write_attorney_review_packet,
)

AUTHORIZATION_ACKNOWLEDGEMENT = (
    "I_AUTHORIZE_PRIVATE_EVIDENCE_TRANSMISSION_TO_MODEL_PROVIDER"
)

# Canonical durable prefix for Case-00 attorney-feedback candidate answers.
DEFAULT_CANDIDATE_B2_PREFIX = (
    "Benchmarks/Case-00-Triborough/derived/attorney-feedback-eval/candidate-answers/"
)

# Canonical private attorney-review markdown packet (B2 only; never commit body).
CANONICAL_ATTORNEY_REVIEW_PACKET_OBJECT_KEY = (
    "Benchmarks/Case-00-Triborough/derived/attorney-feedback-eval/"
    "attorney-reviews/review-20260802-2122f82dafe3/"
    "attorney_review_packet_02-original.md"
)
CANONICAL_ATTORNEY_REVIEW_PACKET_SIZE = 57278
CANONICAL_ATTORNEY_REVIEW_PACKET_SHA256 = (
    "ce7e3a25b22ec23822aec4dcd317b1df38ce6c85b59f684f45f3bdb811316d86"
)

# Production acceptance-contract pins (prefer secrets / env; never commit values).
ACCEPTANCE_CONTRACT_OBJECT_KEY_ENV = "ACCEPTANCE_CONTRACT_OBJECT_KEY"
ACCEPTANCE_CONTRACT_CONTENT_SHA256_ENV = "ACCEPTANCE_CONTRACT_CONTENT_SHA256"
ACCEPTANCE_CONTRACT_BENCHMARK_ID_ENV = "ACCEPTANCE_CONTRACT_BENCHMARK_ID"

REQUIRED_CASE00_BENCHMARK_ID = "Case-00-Triborough"
_QUESTION_ID_RE = re.compile(r"^Q[1-9][0-9]*$")
_ACCEPTANCE_CONTRACT_SEMVER_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_ACCEPTANCE_CONTRACT_OBJECT_NAME = "acceptance_contract.json"

# (benchmark_id, question_id) pairs eligible for canonical private resolution.
CANONICAL_ACCEPTANCE_CONTRACT_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        (REQUIRED_CASE00_BENCHMARK_ID, "Q1"),
        (REQUIRED_CASE00_BENCHMARK_ID, "Q2"),
        (REQUIRED_CASE00_BENCHMARK_ID, "Q3"),
    }
)

# Backward-compatible alias: historical callers treated this as a size pin map.
# Version selection is now listing-driven; sizes come from B2 object metadata.
CANONICAL_ACCEPTANCE_CONTRACT_EXPECTED_SIZES: dict[tuple[str, str], int] = {}

_QUESTION_HEADING_RE = re.compile(
    r"^## (Q[1-9][0-9]*)\.\s+(.+?)\s*$",
    re.MULTILINE,
)


class DurableUploadError(Exception):
    """Fail-closed durable upload / verification error (never embeds secrets)."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class AcceptanceContractConfigError(Exception):
    """Fail-closed missing/invalid production acceptance-contract configuration."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class PacketStagingError(Exception):
    """Fail-closed canonical packet download / verify / extract error.

    Never embeds packet body or question text in ``message`` / ``details``.
    """

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


def verify_canonical_packet_bytes(
    payload: bytes,
    *,
    expected_size: int = CANONICAL_ATTORNEY_REVIEW_PACKET_SIZE,
    expected_sha256: str = CANONICAL_ATTORNEY_REVIEW_PACKET_SHA256,
) -> str:
    """Verify packet size and SHA-256; fail closed on any mismatch.

    Returns the computed hex digest on success. Does not log or return payload.
    """
    if not isinstance(payload, (bytes, bytearray)):
        raise PacketStagingError(
            "canonical packet payload must be bytes",
            payload_type=type(payload).__name__,
        )
    actual_size = len(payload)
    if actual_size != int(expected_size):
        raise PacketStagingError(
            "canonical packet size mismatch",
            expected_size=int(expected_size),
            actual_size=actual_size,
        )
    digest = hashlib.sha256(bytes(payload)).hexdigest()
    expected = str(expected_sha256 or "").strip().lower()
    if digest != expected:
        raise PacketStagingError(
            "canonical packet sha256 mismatch",
            expected_sha256=expected,
            actual_sha256=digest,
        )
    return digest


def extract_question_heading_from_markdown(markdown: str, question_id: str) -> str:
    """Extract the ``## QN. <text>`` heading for ``question_id``; fail closed."""
    qid = str(question_id or "").strip()
    if not qid:
        raise PacketStagingError("question_id must be non-empty for packet staging")
    if not isinstance(markdown, str):
        raise PacketStagingError(
            "packet markdown must be a string",
            markdown_type=type(markdown).__name__,
        )
    matched: dict[str, str] = {}
    for found_id, heading in _QUESTION_HEADING_RE.findall(markdown):
        text = heading.strip()
        if not text:
            continue
        # First heading wins for a given id (deterministic, fail-closed duplicates).
        if found_id not in matched:
            matched[found_id] = text
    if qid not in matched:
        raise PacketStagingError(
            "requested question heading missing from canonical packet",
            question_id=qid,
        )
    return matched[qid]


def write_staged_questions_json(
    case_root: Path,
    question_id: str,
    question_text: str,
) -> Path:
    """Write runner-local ``derived/question-text/questions.json`` for one question."""
    qid = str(question_id or "").strip()
    text = str(question_text or "").strip()
    if not qid:
        raise PacketStagingError("question_id must be non-empty for questions.json")
    if not text:
        raise PacketStagingError(
            "question text must be non-empty for questions.json",
            question_id=qid,
        )
    destination = (
        Path(case_root) / "derived" / "question-text" / "questions.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps({qid: text}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination.resolve()


def download_allowlisted_packet_bytes(
    *,
    client: Optional[Any] = None,
    config: Optional[rebuild_cli.B2Config] = None,
    environ: Optional[Mapping[str, str]] = None,
    object_key: str = CANONICAL_ATTORNEY_REVIEW_PACKET_OBJECT_KEY,
) -> bytes:
    """Download only the fixed allowlisted canonical packet object from B2."""
    key = str(object_key or "").strip()
    if key != CANONICAL_ATTORNEY_REVIEW_PACKET_OBJECT_KEY:
        raise PacketStagingError(
            "refusing non-allowlisted attorney-review packet object key",
            object_key=key,
        )
    cfg = config if config is not None else rebuild_cli.B2Config.from_env(environ)
    s3 = client if client is not None else rebuild_cli.create_b2_client(cfg)
    try:
        response = s3.get_object(Bucket=cfg.bucket, Key=key)
        body = response["Body"]
        payload = body.read() if hasattr(body, "read") else body
    except PacketStagingError:
        raise
    except Exception as exc:  # noqa: BLE001 — fail closed, no secret echo
        raise PacketStagingError(
            "B2 download failed for canonical attorney-review packet",
            object_key=key,
            error_type=type(exc).__name__,
        ) from exc
    if not isinstance(payload, (bytes, bytearray)):
        raise PacketStagingError(
            "canonical packet B2 body must be bytes",
            payload_type=type(payload).__name__,
        )
    return bytes(payload)


def stage_question_from_canonical_b2_packet(
    *,
    case_root: Path,
    question_id: str,
    client: Optional[Any] = None,
    config: Optional[rebuild_cli.B2Config] = None,
    environ: Optional[Mapping[str, str]] = None,
    expected_size: int = CANONICAL_ATTORNEY_REVIEW_PACKET_SIZE,
    expected_sha256: str = CANONICAL_ATTORNEY_REVIEW_PACKET_SHA256,
) -> dict[str, Any]:
    """Download, verify, extract, and stage one question into questions.json.

    Never prints or returns packet body or question text.
    """
    qid = str(question_id or "").strip()
    if not qid:
        raise PacketStagingError("question_id must be non-empty for packet staging")
    payload = download_allowlisted_packet_bytes(
        client=client,
        config=config,
        environ=environ,
    )
    digest = verify_canonical_packet_bytes(
        payload,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    )
    try:
        markdown = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PacketStagingError(
            "canonical packet is not valid utf-8",
            error_type=type(exc).__name__,
        ) from exc
    heading = extract_question_heading_from_markdown(markdown, qid)
    destination = write_staged_questions_json(case_root, qid, heading)
    return {
        "ok": True,
        "question_id": qid,
        "object_key": CANONICAL_ATTORNEY_REVIEW_PACKET_OBJECT_KEY,
        "size": int(expected_size),
        "sha256": digest,
        "questions_json": str(destination),
    }


def candidate_artifact_names(question_id: str) -> tuple[str, ...]:
    """Return the five durable candidate basenames for a question id.

    Q1 keeps the historical filenames; Q2+ use ``{question_id}_candidate_answer.*``.
    """
    qid = str(question_id or "").strip()
    if not qid:
        raise DurableUploadError("question_id must be non-empty for candidate artifacts")
    return (
        f"{qid}_candidate_answer.json",
        f"{qid}_candidate_answer.md",
        "generation_manifest.json",
        "model_input_audit.json",
        PACKET_FILENAME,
    )


# Historical Q1 tuple retained for callers/tests that pin the classic names.
CANDIDATE_ARTIFACT_NAMES = candidate_artifact_names("Q1")


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _emit(payload: dict) -> None:
    print(json.dumps(payload, sort_keys=True))


def _env_strip(environ: Mapping[str, str], name: str) -> str:
    return str(environ.get(name, "") or "").strip()


def canonical_acceptance_contract_id(benchmark_id: str, question_id: str) -> str:
    """Deterministic contract-id slug for the Case-00 allowlisted path."""
    bench = str(benchmark_id or "").strip()
    qid = str(question_id or "").strip()
    if bench != REQUIRED_CASE00_BENCHMARK_ID:
        raise AcceptanceContractConfigError(
            "acceptance-contract id requires Case-00-Triborough benchmark_id",
            benchmark_id=bench,
            question_id=qid,
        )
    if not qid or _QUESTION_ID_RE.fullmatch(qid) is None:
        raise AcceptanceContractConfigError(
            "acceptance-contract id requires question_id matching Q[1-9][0-9]*",
            benchmark_id=bench,
            question_id=qid,
        )
    return f"case00-triborough-{qid.lower()}"


def build_canonical_acceptance_contract_prefix(
    benchmark_id: str,
    question_id: str,
) -> str:
    """Build the version-parent prefix for an allowlisted acceptance contract.

    Safe path construction from explicit identities only — never from corpus
    contents. Restricted to Case-00-Triborough + ``Q[1-9][0-9]*``.
    """
    bench = str(benchmark_id or "").strip()
    qid = str(question_id or "").strip()
    if bench != REQUIRED_CASE00_BENCHMARK_ID:
        raise AcceptanceContractConfigError(
            "acceptance-contract object key requires Case-00-Triborough benchmark_id",
            benchmark_id=bench,
            question_id=qid,
        )
    if not qid or _QUESTION_ID_RE.fullmatch(qid) is None:
        raise AcceptanceContractConfigError(
            "acceptance-contract object key requires question_id matching Q[1-9][0-9]*",
            benchmark_id=bench,
            question_id=qid,
        )
    contract_id = canonical_acceptance_contract_id(bench, qid)
    return (
        "Benchmarks/acceptance-contracts/case-00-triborough/"
        f"{qid}/{contract_id}/"
    )


def parse_acceptance_contract_semver(version_token: str) -> tuple[int, int, int]:
    """Parse ``vMAJOR.MINOR.PATCH``; fail closed on malformed tokens."""
    token = str(version_token or "").strip()
    matched = _ACCEPTANCE_CONTRACT_SEMVER_RE.fullmatch(token)
    if matched is None:
        raise AcceptanceContractConfigError(
            "malformed acceptance-contract semantic version",
            version=token,
        )
    return int(matched.group(1)), int(matched.group(2)), int(matched.group(3))


def build_canonical_acceptance_contract_object_key(
    benchmark_id: str,
    question_id: str,
    *,
    version: str,
) -> str:
    """Build the canonical private acceptance-contract object key for a version."""
    prefix = build_canonical_acceptance_contract_prefix(benchmark_id, question_id)
    ver = str(version or "").strip()
    parse_acceptance_contract_semver(ver)
    return f"{prefix}{ver}/{_ACCEPTANCE_CONTRACT_OBJECT_NAME}"


def list_acceptance_contract_version_candidates(
    objects: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    *,
    prefix: str,
) -> list[dict[str, Any]]:
    """Parse version candidates under ``prefix``; ignore listing order.

    Fail closed on malformed version directories or duplicate version keys.
    """
    normalized_prefix = str(prefix or "")
    if not normalized_prefix.endswith("/"):
        normalized_prefix = normalized_prefix + "/"
    candidates: list[dict[str, Any]] = []
    seen_versions: dict[str, str] = {}
    for item in objects:
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("key") or item.get("Key") or "").strip()
        if not key or not key.startswith(normalized_prefix):
            continue
        rest = key[len(normalized_prefix) :]
        parts = rest.split("/")
        if len(parts) != 2 or parts[1] != _ACCEPTANCE_CONTRACT_OBJECT_NAME:
            # Version-like junk under the contract prefix is fail-closed noise.
            head = parts[0] if parts else ""
            if head.startswith("v") and any(ch.isdigit() for ch in head):
                raise AcceptanceContractConfigError(
                    "malformed acceptance-contract version object key",
                    object_key=key,
                    prefix=normalized_prefix,
                )
            continue
        version = parts[0]
        semver = parse_acceptance_contract_semver(version)
        prior = seen_versions.get(version)
        if prior is not None and prior != key:
            raise AcceptanceContractConfigError(
                "ambiguous acceptance-contract version candidates",
                version=version,
                object_keys=sorted({prior, key}),
            )
        seen_versions[version] = key
        size_raw = item.get("size", item.get("Size"))
        try:
            size = int(size_raw) if size_raw is not None else None
        except (TypeError, ValueError) as exc:
            raise AcceptanceContractConfigError(
                "acceptance-contract listing size is invalid",
                object_key=key,
                size=size_raw,
            ) from exc
        candidates.append(
            {
                "version": version,
                "semver": semver,
                "object_key": key,
                "size": size,
            }
        )
    return candidates


def select_highest_acceptance_contract_candidate(
    candidates: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    """Choose the highest semantic version deterministically (not listing order)."""
    if not candidates:
        raise AcceptanceContractConfigError(
            "no acceptance-contract version candidates under prefix",
        )
    ordered = sorted(
        (dict(c) for c in candidates),
        key=lambda c: (tuple(c["semver"]), str(c["object_key"])),
        reverse=True,
    )
    top = ordered[0]
    ties = [
        c
        for c in ordered
        if tuple(c["semver"]) == tuple(top["semver"])
        and str(c["object_key"]) != str(top["object_key"])
    ]
    if ties:
        raise AcceptanceContractConfigError(
            "ambiguous acceptance-contract highest version",
            version=top.get("version"),
            object_keys=sorted(
                {str(top["object_key"]), *(str(t["object_key"]) for t in ties)}
            ),
        )
    return top


def list_canonical_acceptance_contract_objects(
    *,
    prefix: str,
    client: Optional[Any] = None,
    config: Optional[rebuild_cli.B2Config] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> list[dict[str, Any]]:
    """List acceptance-contract objects under ``prefix`` (key + size only)."""
    normalized_prefix = str(prefix or "")
    if not normalized_prefix.startswith("Benchmarks/acceptance-contracts/"):
        raise AcceptanceContractConfigError(
            "refusing non-allowlisted acceptance-contract listing prefix",
            prefix=normalized_prefix,
        )
    if ".." in normalized_prefix.split("/") or not normalized_prefix.endswith("/"):
        raise AcceptanceContractConfigError(
            "acceptance-contract listing prefix is unsafe",
            prefix=normalized_prefix,
        )
    cfg = config if config is not None else rebuild_cli.B2Config.from_env(environ)
    s3 = client if client is not None else rebuild_cli.create_b2_client(cfg)
    objects: list[dict[str, Any]] = []
    continuation_token: Optional[str] = None
    try:
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": cfg.bucket,
                "Prefix": normalized_prefix,
            }
            if continuation_token is not None:
                kwargs["ContinuationToken"] = continuation_token
            response = s3.list_objects_v2(**kwargs)
            for item in response.get("Contents") or ():
                key = item.get("Key")
                if key is None:
                    continue
                key_str = str(key)
                if key_str.endswith("/"):
                    continue
                objects.append({"key": key_str, "size": item.get("Size")})
            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")
            if not continuation_token:
                break
    except AcceptanceContractConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 — fail closed, no secret echo
        raise AcceptanceContractConfigError(
            "B2 listing failed for acceptance-contract prefix",
            prefix=normalized_prefix,
            error_type=type(exc).__name__,
        ) from exc
    # Deterministic order for diagnostics; selection sorts by semver separately.
    objects.sort(key=lambda row: str(row["key"]))
    return objects


def resolve_canonical_acceptance_contract_spec(
    *,
    benchmark_id: str,
    question_id: str,
    object_keys: Optional[list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...]] = None,
    client: Optional[Any] = None,
    config: Optional[rebuild_cli.B2Config] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Resolve highest compatible version for an allowlisted question.

    Selection is deterministic by semantic version (not B2 listing order).
    Does not read or return contract body bytes.
    """
    bench = str(benchmark_id or "").strip()
    qid = str(question_id or "").strip()
    missing: list[str] = []
    if not bench:
        missing.append("benchmark_id")
    if not qid:
        missing.append("question_id")
    if missing:
        raise AcceptanceContractConfigError(
            "canonical acceptance-contract resolution requires benchmark_id "
            "and question_id",
            missing=missing,
        )
    if (bench, qid) not in CANONICAL_ACCEPTANCE_CONTRACT_ALLOWLIST:
        raise AcceptanceContractConfigError(
            "no allowlisted acceptance-contract for benchmark_id/question_id",
            benchmark_id=bench,
            question_id=qid,
        )
    prefix = build_canonical_acceptance_contract_prefix(bench, qid)
    contract_id = canonical_acceptance_contract_id(bench, qid)
    listed = (
        list(object_keys)
        if object_keys is not None
        else list_canonical_acceptance_contract_objects(
            prefix=prefix,
            client=client,
            config=config,
            environ=environ,
        )
    )
    # Accept bare key strings from older synthetic fixtures.
    normalized_objects: list[dict[str, Any]] = []
    for item in listed:
        if isinstance(item, str):
            normalized_objects.append({"key": item, "size": None})
        elif isinstance(item, Mapping):
            normalized_objects.append(dict(item))
    candidates = list_acceptance_contract_version_candidates(
        normalized_objects,
        prefix=prefix,
    )
    if not candidates:
        raise AcceptanceContractConfigError(
            "no acceptance-contract version candidates under prefix",
            benchmark_id=bench,
            question_id=qid,
            contract_id=contract_id,
            prefix=prefix,
        )
    selected = select_highest_acceptance_contract_candidate(candidates)
    expected_size = selected.get("size")
    if expected_size is None:
        # Optional legacy pin map (empty by default) — never invent sizes.
        expected_size = CANONICAL_ACCEPTANCE_CONTRACT_EXPECTED_SIZES.get((bench, qid))
    if expected_size is None:
        raise AcceptanceContractConfigError(
            "acceptance-contract candidate missing object size",
            benchmark_id=bench,
            question_id=qid,
            object_key=selected["object_key"],
            version=selected["version"],
        )
    return {
        "object_key": str(selected["object_key"]),
        "expected_size": int(expected_size),
        "benchmark_id": bench,
        "question_id": qid,
        "contract_id": contract_id,
        "version": str(selected["version"]),
        "prefix": prefix,
    }


def verify_acceptance_contract_object_bytes(
    payload: bytes,
    *,
    object_key: str,
    expected_size: int,
    expected_benchmark_id: str,
    expected_question_id: str,
    expected_contract_id: Optional[str] = None,
    expected_version: Optional[str] = None,
) -> dict[str, Any]:
    """Verify size, identity, schema, embedded hash, and object integrity.

    Returns safe generator pins only. Never returns or logs contract body.
    """
    if not isinstance(payload, (bytes, bytearray)):
        raise AcceptanceContractConfigError(
            "acceptance-contract payload must be bytes",
            payload_type=type(payload).__name__,
            object_key=object_key,
        )
    actual_size = len(payload)
    if actual_size != int(expected_size):
        raise AcceptanceContractConfigError(
            "acceptance-contract object size mismatch",
            object_key=object_key,
            expected_size=int(expected_size),
            actual_size=actual_size,
        )
    object_sha256 = hashlib.sha256(bytes(payload)).hexdigest()
    identity = ac.ContractIdentity(
        benchmark_id=str(expected_benchmark_id or "").strip(),
        question_id=str(expected_question_id or "").strip(),
    )
    result = ac.load_acceptance_contract_from_bytes(
        bytes(payload),
        object_key=object_key,
        expected_identity=identity,
    )
    if not result.ok or not result.computed_content_sha256:
        raise AcceptanceContractConfigError(
            "acceptance-contract authentication failed",
            object_key=object_key,
            error_code=result.error_code,
            diagnostics=list(result.diagnostics),
        )
    meta = result.metadata
    if meta is None:
        raise AcceptanceContractConfigError(
            "acceptance-contract authentication failed",
            object_key=object_key,
            error_code=result.error_code or ac.ERROR_SCHEMA_INVALID,
        )
    # Benchmark: normalized equivalence only. Question ID remains strict.
    # Preserve original supplied/stored IDs in returned pins and metadata.
    if (
        ac.normalize_benchmark_id(meta.benchmark_id)
        != ac.normalize_benchmark_id(identity.benchmark_id)
        or meta.question_id != identity.question_id
    ):
        raise AcceptanceContractConfigError(
            "acceptance-contract identity mismatch",
            object_key=object_key,
            error_code=ac.ERROR_IDENTITY_MISMATCH,
            expected_benchmark_id=identity.benchmark_id,
            expected_question_id=identity.question_id,
            actual_benchmark_id=meta.benchmark_id,
            actual_question_id=meta.question_id,
        )
    expected_cid = str(expected_contract_id or "").strip()
    # Path slug is authoritative for selection; embedded contract_id must be
    # present and, when it uses the same slug form, must match. Synthetic
    # fixtures may use distinct contract_id labels and are checked via
    # object_key + benchmark/question identity instead.
    if expected_cid:
        actual_cid = str(meta.contract_id or "").strip()
        if not actual_cid:
            raise AcceptanceContractConfigError(
                "acceptance-contract contract_id missing",
                object_key=object_key,
                error_code=ac.ERROR_IDENTITY_MISMATCH,
                expected_contract_id=expected_cid,
            )
        if actual_cid == expected_cid or actual_cid.startswith("case00-"):
            if actual_cid.startswith("case00-") and actual_cid != expected_cid:
                raise AcceptanceContractConfigError(
                    "acceptance-contract contract_id mismatch",
                    object_key=object_key,
                    error_code=ac.ERROR_IDENTITY_MISMATCH,
                    expected_contract_id=expected_cid,
                    actual_contract_id=actual_cid,
                )
    if expected_version is not None:
        # Embedded document version is MAJOR.MINOR.PATCH; object path uses v-prefix.
        path_version = str(expected_version or "").strip()
        parse_acceptance_contract_semver(path_version)
        embedded = str(meta.version or "").strip()
        embedded_token = embedded if embedded.startswith("v") else f"v{embedded}"
        if embedded_token != path_version:
            raise AcceptanceContractConfigError(
                "acceptance-contract version mismatch",
                object_key=object_key,
                error_code=ac.ERROR_IDENTITY_MISMATCH,
                expected_version=path_version,
                actual_version=meta.version,
            )
    return {
        "object_key": object_key,
        "content_sha256": result.computed_content_sha256,
        "object_sha256": object_sha256,
        "benchmark_id": identity.benchmark_id,
        "question_id": identity.question_id,
        "contract_id": meta.contract_id,
        "version": meta.version,
        "size": int(expected_size),
    }


def download_canonical_acceptance_contract_bytes(
    *,
    object_key: str,
    client: Optional[Any] = None,
    config: Optional[rebuild_cli.B2Config] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> bytes:
    """Download one allowlisted canonical acceptance-contract object from B2."""
    key = str(object_key or "").strip()
    if not key.startswith("Benchmarks/acceptance-contracts/"):
        raise AcceptanceContractConfigError(
            "refusing non-allowlisted acceptance-contract object key prefix",
            object_key=key,
        )
    if ".." in key.split("/") or key.endswith("/"):
        raise AcceptanceContractConfigError(
            "acceptance-contract object key is unsafe",
            object_key=key,
        )
    cfg = config if config is not None else rebuild_cli.B2Config.from_env(environ)
    s3 = client if client is not None else rebuild_cli.create_b2_client(cfg)
    try:
        response = s3.get_object(Bucket=cfg.bucket, Key=key)
        body = response["Body"]
        payload = body.read() if hasattr(body, "read") else body
    except AcceptanceContractConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 — fail closed, no secret/body echo
        raise AcceptanceContractConfigError(
            "B2 download failed for acceptance-contract object",
            object_key=key,
            error_type=type(exc).__name__,
        ) from exc
    if not isinstance(payload, (bytes, bytearray)):
        raise AcceptanceContractConfigError(
            "acceptance-contract B2 body must be bytes",
            payload_type=type(payload).__name__,
            object_key=key,
        )
    return bytes(payload)


def resolve_and_verify_canonical_acceptance_contract(
    *,
    benchmark_id: str,
    question_id: str,
    client: Optional[Any] = None,
    config: Optional[rebuild_cli.B2Config] = None,
    environ: Optional[Mapping[str, str]] = None,
    object_keys: Optional[list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...]] = None,
) -> dict[str, Any]:
    """Select highest compatible version, download, verify; return pins.

    Fail closed when the newest candidate fails verification — never silently
    falls back to an older version. Safe metadata only — never prints or
    returns contract body.
    """
    spec = resolve_canonical_acceptance_contract_spec(
        benchmark_id=benchmark_id,
        question_id=question_id,
        object_keys=object_keys,
        client=client,
        config=config,
        environ=environ,
    )
    payload = download_canonical_acceptance_contract_bytes(
        object_key=spec["object_key"],
        client=client,
        config=config,
        environ=environ,
    )
    verified = verify_acceptance_contract_object_bytes(
        payload,
        object_key=spec["object_key"],
        expected_size=int(spec["expected_size"]),
        expected_benchmark_id=spec["benchmark_id"],
        expected_question_id=spec["question_id"],
        expected_contract_id=spec["contract_id"],
        expected_version=spec["version"],
    )
    return {
        "ok": True,
        "object_key": verified["object_key"],
        "content_sha256": verified["content_sha256"],
        "object_sha256": verified["object_sha256"],
        "benchmark_id": verified["benchmark_id"],
        "question_id": verified["question_id"],
        "contract_id": verified["contract_id"],
        "version": verified.get("version") or spec["version"],
        "size": verified["size"],
    }


def resolve_production_acceptance_contract(
    *,
    question_id: str,
    object_key: Optional[str] = None,
    content_sha256: Optional[str] = None,
    benchmark_id: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Require acceptance-contract object key, SHA-256, and identities.

    Values may come from CLI flags or environment/secrets (including pins
    produced by ``resolve_and_verify_canonical_acceptance_contract``). Benchmark
    and question identities are passed explicitly — never inferred from private
    corpus contents. Does not read or return contract body bytes.
    """
    env = os.environ if environ is None else environ
    qid = str(question_id or "").strip()
    key = (object_key or _env_strip(env, ACCEPTANCE_CONTRACT_OBJECT_KEY_ENV)).strip()
    sha = (
        content_sha256 or _env_strip(env, ACCEPTANCE_CONTRACT_CONTENT_SHA256_ENV)
    ).strip()
    bench = (
        benchmark_id or _env_strip(env, ACCEPTANCE_CONTRACT_BENCHMARK_ID_ENV)
    ).strip()

    missing: list[str] = []
    if not key:
        missing.append("object_key")
    if not sha:
        missing.append("content_sha256")
    if not bench:
        missing.append("benchmark_id")
    if not qid:
        missing.append("question_id")
    if missing:
        raise AcceptanceContractConfigError(
            "production Case-00 generation requires acceptance-contract "
            "object key, content SHA-256, benchmark id, and question id",
            missing=missing,
        )
    return {
        "object_key": key,
        "content_sha256": sha,
        "benchmark_id": bench,
        "question_id": qid,
    }


def normalize_candidate_b2_prefix(prefix: str) -> str:
    """Normalize a candidate object prefix; reject empty / traversal / absolute."""
    raw = (prefix or "").strip().replace("\\", "/")
    if not raw:
        raise DurableUploadError("candidate B2 prefix must not be empty")
    if raw.startswith("/") or raw.startswith("~"):
        raise DurableUploadError(
            "candidate B2 prefix must be a relative object key prefix",
            prefix=raw,
        )
    # Reject Windows-style drive prefixes and URI schemes.
    if "://" in raw or (len(raw) >= 2 and raw[1] == ":"):
        raise DurableUploadError(
            "candidate B2 prefix must not include a URI or drive prefix",
            prefix=raw,
        )
    parts = [part for part in raw.split("/") if part != ""]
    if not parts:
        raise DurableUploadError("candidate B2 prefix must not be empty")
    if any(part in (".", "..") for part in parts):
        raise DurableUploadError(
            "candidate B2 prefix must not contain path traversal segments",
            prefix=raw,
        )
    # Never treat a local filesystem path as a durable object prefix.
    if parts[0] in ("tmp", "var", "private") or raw.lower().startswith("tmp/"):
        raise DurableUploadError(
            "candidate B2 prefix must not look like a local filesystem path",
            prefix=raw,
        )
    return "/".join(parts) + "/"


def assert_key_under_prefix(object_key: str, prefix: str) -> None:
    normalized_prefix = normalize_candidate_b2_prefix(prefix)
    key = (object_key or "").replace("\\", "/")
    if not key or key.endswith("/"):
        raise DurableUploadError(
            "object key must be a non-empty file key",
            key=key,
            prefix=normalized_prefix,
        )
    if any(part in (".", "..") for part in key.split("/")):
        raise DurableUploadError(
            "object key must not contain path traversal segments",
            key=key,
        )
    if not key.startswith(normalized_prefix):
        raise DurableUploadError(
            "object key escapes the selected candidate B2 prefix",
            key=key,
            prefix=normalized_prefix,
        )
    remainder = key[len(normalized_prefix) :]
    if not remainder or remainder.startswith("/") or "/../" in f"/{remainder}/":
        raise DurableUploadError(
            "object key escapes the selected candidate B2 prefix",
            key=key,
            prefix=normalized_prefix,
        )


def build_candidate_object_key(
    prefix: str,
    candidate_basename: str,
    filename: str,
    *,
    question_id: str = "Q1",
) -> str:
    normalized_prefix = normalize_candidate_b2_prefix(prefix)
    base = (candidate_basename or "").strip().replace("\\", "/")
    name = (filename or "").strip().replace("\\", "/")
    if not base or "/" in base or base in (".", ".."):
        raise DurableUploadError(
            "candidate directory basename is unsafe or empty",
            candidate_basename=candidate_basename,
        )
    allowed = candidate_artifact_names(question_id)
    if name not in allowed:
        raise DurableUploadError(
            "refusing to upload unexpected candidate artifact name",
            filename=filename,
            allowed=list(allowed),
            question_id=str(question_id or "").strip(),
        )
    key = f"{normalized_prefix}{base}/{name}"
    assert_key_under_prefix(key, normalized_prefix)
    return key


def _parse_generation_payload(stdout: str) -> dict[str, Any]:
    text = (stdout or "").strip()
    if not text:
        raise DurableUploadError("generation produced empty stdout; cannot locate artifacts")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DurableUploadError(
            "generation stdout is not valid JSON",
            error=str(exc),
        ) from exc
    if not isinstance(payload, dict):
        raise DurableUploadError("generation stdout JSON must be an object")
    if not payload.get("ok") or not payload.get("finalized"):
        raise DurableUploadError(
            "generation did not report finalized success",
            generation_ok=payload.get("ok"),
            finalized=payload.get("finalized"),
        )
    return payload


def _candidate_dir_from_payload(payload: dict[str, Any]) -> Path:
    raw = payload.get("candidate_directory")
    if not raw or not isinstance(raw, str):
        raise DurableUploadError("generation payload missing candidate_directory")
    path = Path(raw)
    if not path.is_dir():
        raise DurableUploadError(
            "candidate_directory does not exist on disk",
            candidate_directory=str(path),
        )
    return path.resolve()


def render_candidate_review_packet(
    case_root: Path,
    candidate_dir: Path,
    generation: Mapping[str, Any],
    *,
    question_id: str,
) -> Path:
    """Render a packet from finalized generation data without unstaged gold files."""
    qid = str(question_id or "").strip()
    candidate_path = Path(candidate_dir) / f"{qid}_candidate_answer.json"
    if not candidate_path.is_file():
        raise DurableUploadError(
            "candidate JSON missing before attorney review packet rendering",
            question_id=qid,
            path=str(candidate_path),
        )
    completeness = generation.get("completeness_validation")
    if not isinstance(completeness, Mapping):
        completeness = {}
    acceptance = generation.get("acceptance_contract")
    if not isinstance(acceptance, Mapping):
        acceptance = {}
    evaluation = {
        "corpus_id": "case-00-triborough",
        "benchmark_id": acceptance.get("benchmark_id") or "Case-00-Triborough",
        "packet_id": "attorney-review-packet-02-live",
        "questions": [
            {
                "question_id": qid,
                "flags": {"requires_attorney_review": True},
                "reference_answer_status": {
                    "status": "not_loaded_in_generation_workflow"
                },
                "candidate_vs_reference_diagnostics": {
                    "comparison_performed": False,
                    "method": "generation_acceptance_validation_only",
                    "note": (
                        "The production generation workflow does not stage private "
                        "gold labels; no candidate-vs-reference comparison was "
                        "performed. Review the evidence matrix, cited record, "
                        "limitations, and unresolved questions directly."
                    ),
                    "citation_evidence_coverage": {
                        "generation_finalized": bool(generation.get("finalized")),
                        "completeness_validation_present": bool(completeness),
                        "acceptance_contract_present": bool(acceptance),
                    },
                },
            }
        ],
    }
    try:
        return write_attorney_review_packet(
            candidate_path,
            evaluation,
            output_path=Path(candidate_dir) / PACKET_FILENAME,
            generation=generation,
        )
    except Exception as exc:  # noqa: BLE001 — emit type only; never private content
        raise DurableUploadError(
            "attorney review packet rendering failed",
            error_type=type(exc).__name__,
            question_id=qid,
        ) from exc


def upload_candidate_artifacts_to_b2(
    candidate_dir: Path,
    *,
    prefix: str = DEFAULT_CANDIDATE_B2_PREFIX,
    client: Optional[Any] = None,
    config: Optional[rebuild_cli.B2Config] = None,
    environ: Optional[Mapping[str, str]] = None,
    question_id: str = "Q1",
) -> dict[str, Any]:
    """Upload the five finalized artifacts and verify each with head_object.

    Local ``candidate_dir`` is treated as ephemeral. Success requires remote
    verification of every object; missing or size-mismatched objects fail closed.
    Artifact basenames follow ``candidate_artifact_names(question_id)`` so Q1
    keeps historical names while Q2+ use question-aware filenames.
    """
    cfg = config if config is not None else rebuild_cli.B2Config.from_env(environ)
    s3 = client if client is not None else rebuild_cli.create_b2_client(cfg)
    normalized_prefix = normalize_candidate_b2_prefix(prefix)
    candidate_path = Path(candidate_dir).resolve()
    basename = candidate_path.name
    artifact_names = candidate_artifact_names(question_id)

    objects: list[dict[str, Any]] = []
    for filename in artifact_names:
        local_path = candidate_path / filename
        if not local_path.is_file():
            raise DurableUploadError(
                f"required candidate artifact missing before upload: {filename}",
                path=str(local_path),
            )
        expected_size = local_path.stat().st_size
        object_key = build_candidate_object_key(
            normalized_prefix,
            basename,
            filename,
            question_id=question_id,
        )
        try:
            s3.upload_file(str(local_path), cfg.bucket, object_key)
        except Exception as exc:  # noqa: BLE001 — fail closed, no secret echo
            raise DurableUploadError(
                f"B2 upload failed for {filename}",
                object_key=object_key,
                error_type=type(exc).__name__,
            ) from exc
        try:
            head = s3.head_object(Bucket=cfg.bucket, Key=object_key)
        except Exception as exc:  # noqa: BLE001
            raise DurableUploadError(
                f"B2 head_object verification failed for {filename}",
                object_key=object_key,
                error_type=type(exc).__name__,
            ) from exc
        remote_size = head.get("ContentLength")
        if remote_size != expected_size:
            raise DurableUploadError(
                f"B2 object size mismatch for {filename}",
                object_key=object_key,
                expected_size=expected_size,
                remote_size=remote_size,
            )
        entry: dict[str, Any] = {
            "filename": filename,
            "object_key": object_key,
            "size": expected_size,
        }
        etag = head.get("ETag")
        if isinstance(etag, str) and etag.strip():
            entry["etag"] = etag.strip().strip('"')
        objects.append(entry)

    if len(objects) != len(artifact_names):
        raise DurableUploadError(
            "durable upload incomplete; refusing success",
            uploaded=len(objects),
            required=len(artifact_names),
        )

    return {
        "bucket": cfg.bucket,
        "prefix": normalized_prefix,
        "candidate_basename": basename,
        "question_id": str(question_id or "").strip(),
        "object_keys": [item["object_key"] for item in objects],
        "objects": objects,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild Case-00 from Backblaze B2, generate one attorney-feedback "
            "candidate, and upload verified candidate artifacts to B2. "
            "Local --candidate-output-root is ephemeral only."
        )
    )
    parser.add_argument("--case-root", required=True)
    parser.add_argument("--question-id", required=True)
    parser.add_argument("--required-commit", required=True)
    parser.add_argument(
        "--candidate-output-root",
        required=True,
        help=(
            "Ephemeral local directory root for generation outputs "
            "(for example a temp path). Not a durable destination."
        ),
    )
    parser.add_argument(
        "--candidate-b2-prefix",
        default=DEFAULT_CANDIDATE_B2_PREFIX,
        help=(
            "Explicit B2 object prefix for durable candidate artifacts "
            f"(default: {DEFAULT_CANDIDATE_B2_PREFIX}). "
            "Never falls back to a local /tmp path as durable storage."
        ),
    )
    parser.add_argument(
        "--authorization-confirmed",
        action="store_true",
        required=True,
        help="Confirms the caller already obtained authorization to transmit private evidence.",
    )
    parser.add_argument(
        "--generation-only",
        action="store_true",
        required=True,
        help="Required safety gate; evaluation is not run by this wrapper.",
    )
    parser.add_argument(
        "--reuse-derived",
        action="store_true",
        help=(
            "Validate and reuse pre-staged derived artifacts instead of "
            "rebuilding the full source docket."
        ),
    )
    parser.add_argument(
        "--acceptance-contract-object-key",
        default=None,
        help=(
            "Required private acceptance-contract B2 object key "
            f"(or set {ACCEPTANCE_CONTRACT_OBJECT_KEY_ENV})."
        ),
    )
    parser.add_argument(
        "--acceptance-contract-content-sha256",
        default=None,
        help=(
            "Required expected acceptance-contract content SHA-256 "
            f"(or set {ACCEPTANCE_CONTRACT_CONTENT_SHA256_ENV})."
        ),
    )
    parser.add_argument(
        "--acceptance-contract-benchmark-id",
        default=None,
        help=(
            "Required explicit benchmark identity for the acceptance contract "
            f"(or set {ACCEPTANCE_CONTRACT_BENCHMARK_ID_ENV})."
        ),
    )
    parser.add_argument(
        "--validated-claims-path",
        default=None,
        help=(
            "Optional privacy-safe validated structured-claims JSON emitted by "
            "Q2 production-boundary preflight in the same job."
        ),
    )
    parser.add_argument(
        "--validated-claims-sha256",
        default=None,
        help=(
            "Expected SHA-256 of the canonical validated claims JSON. Required "
            "when --validated-claims-path is set."
        ),
    )
    args = parser.parse_args(argv)

    try:
        acceptance = resolve_production_acceptance_contract(
            question_id=args.question_id,
            object_key=args.acceptance_contract_object_key,
            content_sha256=args.acceptance_contract_content_sha256,
            benchmark_id=args.acceptance_contract_benchmark_id,
        )
    except AcceptanceContractConfigError as exc:
        _emit(
            {
                "ok": False,
                "phase": "acceptance_contract",
                "blocker": exc.message,
                **exc.details,
            }
        )
        return 1

    try:
        candidate_prefix = normalize_candidate_b2_prefix(args.candidate_b2_prefix)
    except DurableUploadError as exc:
        _emit(
            {
                "ok": False,
                "phase": "durable_upload",
                "blocker": exc.message,
                **exc.details,
            }
        )
        return 1

    repo_root = Path(__file__).resolve().parents[1]
    rebuild_script = repo_root / "scripts" / "rebuild_case00_derived.py"
    generator_script = repo_root / "scripts" / "generate_attorney_feedback_candidate.py"

    rebuild_argv = [
        sys.executable,
        str(rebuild_script),
        "--case-root",
        args.case_root,
    ]
    if args.reuse_derived:
        rebuild_argv.append("--validate-only")
    else:
        rebuild_argv.append("--b2-prefix")

    rebuild = _run(rebuild_argv, repo_root)
    if rebuild.returncode != 0:
        _emit(
            {
                "ok": False,
                "phase": "rebuild",
                "return_code": rebuild.returncode,
                "stdout": rebuild.stdout,
                "stderr": rebuild.stderr,
            }
        )
        return rebuild.returncode or 1

    generation_argv = [
        sys.executable,
        str(generator_script),
        "--case-root",
        args.case_root,
        "--question-id",
        acceptance["question_id"],
        "--required-commit",
        args.required_commit,
        "--candidate-output-root",
        args.candidate_output_root,
        "--authorize-private-evidence-transmission",
        AUTHORIZATION_ACKNOWLEDGEMENT,
        "--generation-only",
        "--repo-root",
        str(repo_root),
        "--acceptance-contract-object-key",
        acceptance["object_key"],
        "--acceptance-contract-content-sha256",
        acceptance["content_sha256"],
        "--acceptance-contract-benchmark-id",
        acceptance["benchmark_id"],
    ]
    claims_path = (args.validated_claims_path or "").strip()
    claims_sha = (args.validated_claims_sha256 or "").strip()
    if claims_path or claims_sha:
        if not claims_path or not claims_sha:
            _emit(
                {
                    "ok": False,
                    "phase": "validated_claims",
                    "blocker": "validated_claims_handoff_incomplete",
                    "reason_code": "validated_claims_handoff_incomplete",
                    "has_path": bool(claims_path),
                    "has_sha256": bool(claims_sha),
                }
            )
            return 1
        generation_argv.extend(
            [
                "--validated-claims-path",
                claims_path,
                "--validated-claims-sha256",
                claims_sha,
            ]
        )

    generation = _run(
        generation_argv,
        repo_root,
    )
    if generation.returncode != 0:
        _emit(
            {
                "ok": False,
                "phase": "generation",
                "return_code": generation.returncode,
                "stdout": generation.stdout,
                "stderr": generation.stderr,
            }
        )
        return generation.returncode or 1

    # Local generation success is not durable success — upload + verify required.
    try:
        generation_payload = _parse_generation_payload(generation.stdout)
        candidate_dir = _candidate_dir_from_payload(generation_payload)
        render_candidate_review_packet(
            Path(args.case_root),
            candidate_dir,
            generation_payload,
            question_id=acceptance["question_id"],
        )
        durable = upload_candidate_artifacts_to_b2(
            candidate_dir,
            prefix=candidate_prefix,
            question_id=acceptance["question_id"],
        )
    except rebuild_cli.RebuildError as exc:
        _emit(
            {
                "ok": False,
                "phase": "durable_upload",
                "blocker": exc.message,
                **exc.details,
                "ephemeral_local_directory": None,
            }
        )
        return 1
    except DurableUploadError as exc:
        _emit(
            {
                "ok": False,
                "phase": "durable_upload",
                "blocker": exc.message,
                **exc.details,
                "ephemeral_local_directory": str(candidate_dir)
                if "candidate_dir" in locals()
                else None,
            }
        )
        return 1

    _emit(
        {
            "ok": True,
            "phase": "complete",
            "rebuild_stdout": rebuild.stdout,
            "generation_stdout": generation.stdout,
            "ephemeral_local_directory": str(candidate_dir),
            "durable_artifacts": {
                "bucket": durable["bucket"],
                "prefix": durable["prefix"],
                "object_keys": durable["object_keys"],
                "objects": durable["objects"],
            },
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
