from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any


CASE00_ROOT = "Benchmarks/Case-00-Triborough"
CASE00_PREFIXES = {
    "all": f"{CASE00_ROOT}/",
    "source": f"{CASE00_ROOT}/original/",
    "questions": f"{CASE00_ROOT}/derived/question-text/",
    "candidate_answers": (
        f"{CASE00_ROOT}/derived/attorney-feedback-eval/candidate-answers/"
    ),
    "attorney_reviews": (
        f"{CASE00_ROOT}/derived/attorney-feedback-eval/attorney-reviews/"
    ),
    "attorney_review_packets": (
        f"{CASE00_ROOT}/derived/attorney-feedback-eval/attorney-review-packets/"
    ),
}

ATTORNEY_REVIEW_FILENAMES = {
    "original_packet": "attorney_review_packet_02-original.md",
    "feedback_email": "John-Cuomo-Case00-Attorney-Feedback-Email-2026-08-02.md",
    "structured_evaluation": (
        "John-Cuomo-Case00-Structured-Evaluation-2026-08-02.json"
    ),
    "manifest": "John-Cuomo-Case00-Feedback-Preservation-Manifest.json",
}

REVIEW_PACKET_MANIFEST_FILENAME = "review-packet-preservation-manifest.json"
DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

MAX_ARCHIVE_ITEM_BYTES = 2 * 1024 * 1024
MAX_REVIEW_PACKET_BYTES = MAX_ARCHIVE_ITEM_BYTES
MAX_REVIEW_PACKET_BASE64_CHARS = ((MAX_REVIEW_PACKET_BYTES + 2) // 3) * 4
MAX_RECIPIENT_CHARS = 128
MAX_SENT_AT_CHARS = 40
MAX_ORIGINAL_FILENAME_CHARS = 128
ALLOWED_QUESTION_IDS = frozenset({"Q1", "Q2", "Q3", "Q4", "Q5"})
_ARCHIVE_PUT_PRECONDITION_CODES = frozenset(
    {"PreconditionFailed", "412", "ConditionNotMet"}
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SENT_AT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_ORIGINAL_FILENAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.docx$"
)
# Bounded syntactic email check for review-packet recipients. Authorization is
# enforced by the authenticated MCP user boundary; this only validates shape and
# length, then lowercases for deterministic archive IDs and manifests.
_RECIPIENT_EMAIL_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9._+-]{0,62}[a-z0-9])?"
    r"@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


def inventory_prefix(category: str) -> str:
    try:
        return CASE00_PREFIXES[category]
    except KeyError as exc:
        raise ValueError(f"unsupported Case-00 storage category: {category}") from exc


def _encoded(value: str, label: str) -> bytes:
    payload = value.encode("utf-8")
    if not payload:
        raise ValueError(f"{label} must not be empty")
    if len(payload) > MAX_ARCHIVE_ITEM_BYTES:
        raise ValueError(f"{label} exceeds the 2 MiB archive limit")
    return payload


def build_attorney_review_archive(
    *,
    evaluation_date: str,
    original_packet_md: str,
    feedback_email_md: str,
    structured_evaluation_json: str,
    archived_by: str,
) -> tuple[str, list[dict[str, Any]]]:
    if not _DATE_RE.fullmatch(evaluation_date):
        raise ValueError("evaluation_date must use YYYY-MM-DD")
    try:
        datetime.strptime(evaluation_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("evaluation_date is not a valid calendar date") from exc

    packet = _encoded(original_packet_md, "original_packet_md")
    email = _encoded(feedback_email_md, "feedback_email_md")
    evaluation = _encoded(
        structured_evaluation_json, "structured_evaluation_json"
    )
    try:
        parsed_evaluation = json.loads(evaluation)
    except json.JSONDecodeError as exc:
        raise ValueError("structured_evaluation_json must be valid JSON") from exc
    if not isinstance(parsed_evaluation, dict):
        raise ValueError("structured_evaluation_json must contain a JSON object")

    digest = hashlib.sha256(packet + b"\0" + email + b"\0" + evaluation).hexdigest()
    archive_id = f"review-{evaluation_date.replace('-', '')}-{digest[:12]}"
    prefix = f"{CASE00_PREFIXES['attorney_reviews']}{archive_id}/"

    source_items = [
        ("original_packet", packet, "text/markdown; charset=utf-8"),
        ("feedback_email", email, "text/markdown; charset=utf-8"),
        ("structured_evaluation", evaluation, "application/json"),
    ]
    manifest = {
        "schema_version": "1.0",
        "archive_id": archive_id,
        "case_id": "Case-00-Triborough",
        "evaluation_date": evaluation_date,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "archived_by": archived_by,
        "canonical_storage": "Backblaze B2",
        "files": [
            {
                "filename": ATTORNEY_REVIEW_FILENAMES[label],
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
            for label, payload, _content_type in source_items
        ],
    }
    manifest_payload = json.dumps(
        manifest, indent=2, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")

    items = [
        {
            "filename": ATTORNEY_REVIEW_FILENAMES[label],
            "object_key": f"{prefix}{ATTORNEY_REVIEW_FILENAMES[label]}",
            "payload": payload,
            "content_type": content_type,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for label, payload, content_type in source_items
    ]
    items.append(
        {
            "filename": ATTORNEY_REVIEW_FILENAMES["manifest"],
            "object_key": f"{prefix}{ATTORNEY_REVIEW_FILENAMES['manifest']}",
            "payload": manifest_payload,
            "content_type": "application/json",
            "sha256": hashlib.sha256(manifest_payload).hexdigest(),
        }
    )
    return archive_id, items


def _parse_sent_at(sent_at: str) -> datetime:
    if len(sent_at) > MAX_SENT_AT_CHARS:
        raise ValueError(f"sent_at exceeds {MAX_SENT_AT_CHARS} characters")
    if not _SENT_AT_RE.fullmatch(sent_at):
        raise ValueError("sent_at must be an ISO-8601 timestamp with timezone")
    normalized = sent_at.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("sent_at is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("sent_at must include a timezone")
    return parsed


def normalize_review_packet_recipient(recipient: str) -> str:
    """Return a lowercased recipient when it is a bounded, syntactically valid email."""
    if not isinstance(recipient, str):
        raise ValueError("recipient must be a valid email address")
    if len(recipient) > MAX_RECIPIENT_CHARS:
        raise ValueError(f"recipient exceeds {MAX_RECIPIENT_CHARS} characters")
    normalized = recipient.lower()
    if not _RECIPIENT_EMAIL_RE.fullmatch(normalized):
        raise ValueError("recipient is not a valid email address")
    return normalized


def _validate_review_packet_metadata(
    *,
    recipient: str,
    question_id: str,
    sent_at: str,
    original_filename: str,
) -> tuple[str, datetime]:
    normalized_recipient = normalize_review_packet_recipient(recipient)
    if question_id not in ALLOWED_QUESTION_IDS:
        raise ValueError("question_id is not in the Case-00 allowlist")
    sent_at_dt = _parse_sent_at(sent_at)
    if not isinstance(original_filename, str):
        raise ValueError("original_filename must be a DOCX filename")
    if len(original_filename) > MAX_ORIGINAL_FILENAME_CHARS:
        raise ValueError(
            f"original_filename exceeds {MAX_ORIGINAL_FILENAME_CHARS} characters"
        )
    if "/" in original_filename or "\\" in original_filename:
        raise ValueError("original_filename must not contain path separators")
    if not _ORIGINAL_FILENAME_RE.fullmatch(original_filename):
        raise ValueError(
            "original_filename must be a basename ending in .docx "
            "with allowlisted characters"
        )
    return normalized_recipient, sent_at_dt


def decode_review_packet_docx_base64(docx_base64: str) -> bytes:
    if not isinstance(docx_base64, str) or not docx_base64:
        raise ValueError("docx_base64 must be a non-empty base64 string")
    if len(docx_base64) > MAX_REVIEW_PACKET_BASE64_CHARS:
        raise ValueError("docx_base64 exceeds the bounded payload size")
    if any(ch.isspace() for ch in docx_base64):
        raise ValueError("docx_base64 must not contain whitespace")
    try:
        payload = base64.b64decode(docx_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("docx_base64 must be strict standard base64") from exc
    if not payload:
        raise ValueError("decoded DOCX payload must not be empty")
    if len(payload) > MAX_REVIEW_PACKET_BYTES:
        raise ValueError("decoded DOCX exceeds the 2 MiB archive limit")
    return payload


def validate_docx_bytes(payload: bytes) -> None:
    if not payload.startswith(b"PK"):
        raise ValueError("DOCX payload must be a ZIP/OOXML archive")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
            names = bundle.namelist()
            if not names:
                raise ValueError("DOCX archive contains no entries")
            if "[Content_Types].xml" not in names:
                raise ValueError("DOCX missing required [Content_Types].xml")
            if "word/document.xml" not in names:
                raise ValueError("DOCX missing required word/document.xml")
            corrupt = bundle.testzip()
            if corrupt is not None:
                raise ValueError(f"DOCX archive is corrupt at {corrupt}")
            bundle.read("[Content_Types].xml")
            bundle.read("word/document.xml")
    except zipfile.BadZipFile as exc:
        raise ValueError("DOCX payload is not a valid ZIP/OOXML archive") from exc


def archive_create_only_put_params() -> dict[str, str]:
    """Extra PutObject parameters for archive writes on B2's S3-compatible API.

    Backblaze B2 rejects conditional headers such as IfNoneMatch on PutObject.
    Collision protection is the explicit HEAD preflight
    (assert_archive_objects_absent), not a conditional put.
    """
    return {}


def map_archive_put_precondition_failure(
    *,
    object_key: str,
    error_code: str,
    http_status_code: int | None = None,
) -> ValueError | None:
    """Map unexpected PutObject precondition failures to archive collision errors."""
    code = str(error_code)
    if code in _ARCHIVE_PUT_PRECONDITION_CODES or http_status_code == 412:
        return ValueError(f"archive object already exists: {object_key}")
    return None


def assert_archive_objects_absent(
    items: list[dict[str, Any]],
    *,
    object_exists: Callable[[str], bool],
) -> None:
    for item in items:
        key = item["object_key"]
        if object_exists(key):
            raise ValueError(f"archive object already exists: {key}")


def build_review_packet_archive(
    *,
    docx_base64: str,
    recipient: str,
    question_id: str,
    sent_at: str,
    original_filename: str,
    archived_by: str,
) -> tuple[str, list[dict[str, Any]]]:
    normalized_recipient, sent_at_dt = _validate_review_packet_metadata(
        recipient=recipient,
        question_id=question_id,
        sent_at=sent_at,
        original_filename=original_filename,
    )
    docx_bytes = decode_review_packet_docx_base64(docx_base64)
    validate_docx_bytes(docx_bytes)

    material = (
        f"{normalized_recipient}\0{question_id}\0{sent_at}\0{original_filename}\0".encode(
            "utf-8"
        )
        + docx_bytes
    )
    digest = hashlib.sha256(material).hexdigest()
    archive_id = (
        f"packet-{question_id.lower()}-{sent_at_dt.strftime('%Y%m%d')}-{digest[:12]}"
    )
    prefix = f"{CASE00_PREFIXES['attorney_review_packets']}{archive_id}/"

    docx_sha256 = hashlib.sha256(docx_bytes).hexdigest()
    manifest = {
        "schema_version": "1.0",
        "archive_id": archive_id,
        "case_id": "Case-00-Triborough",
        "recipient": normalized_recipient,
        "question_id": question_id,
        "sent_at": sent_at,
        "original_filename": original_filename,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "archived_by": archived_by,
        "canonical_storage": "Backblaze B2",
        "files": [
            {
                "filename": original_filename,
                "sha256": docx_sha256,
                "size": len(docx_bytes),
            }
        ],
    }
    manifest_payload = json.dumps(
        manifest, indent=2, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")

    items = [
        {
            "filename": original_filename,
            "object_key": f"{prefix}{original_filename}",
            "payload": docx_bytes,
            "content_type": DOCX_CONTENT_TYPE,
            "sha256": docx_sha256,
        },
        {
            "filename": REVIEW_PACKET_MANIFEST_FILENAME,
            "object_key": f"{prefix}{REVIEW_PACKET_MANIFEST_FILENAME}",
            "payload": manifest_payload,
            "content_type": "application/json",
            "sha256": hashlib.sha256(manifest_payload).hexdigest(),
        },
    ]
    for item in items:
        if not item["object_key"].startswith(
            CASE00_PREFIXES["attorney_review_packets"]
        ):
            raise ValueError("review packet object key escaped the canonical prefix")
        if ".." in item["object_key"]:
            raise ValueError("review packet object key must not contain '..'")
    return archive_id, items


# ---------------------------------------------------------------------------
# Generic acceptance_contract.v1 archival (no case-/person-specific allowlists)
# ---------------------------------------------------------------------------

ACCEPTANCE_CONTRACT_SCHEMA = "acceptance_contract.v1"
ACCEPTANCE_CONTRACT_PREFIX = "Benchmarks/acceptance-contracts/"
CANONICAL_LEGALAI_BUCKET = "legalai-corpus"
MAX_ACCEPTANCE_CONTRACT_BYTES = MAX_ARCHIVE_ITEM_BYTES
MAX_ACCEPTANCE_CONTRACT_BASE64_CHARS = ((MAX_ACCEPTANCE_CONTRACT_BYTES + 2) // 3) * 4
MAX_ACCEPTANCE_CONTRACT_KEY_CHARS = 512
MAX_ACCEPTANCE_IDENTITY_CHARS = 128
_ACCEPTANCE_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def acceptance_contract_prefix() -> str:
    """Canonical B2 prefix for private acceptance-contract objects."""
    return ACCEPTANCE_CONTRACT_PREFIX


def _validate_acceptance_identity(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    if len(value) > MAX_ACCEPTANCE_IDENTITY_CHARS:
        raise ValueError(f"{label} exceeds {MAX_ACCEPTANCE_IDENTITY_CHARS} characters")
    if not _ACCEPTANCE_IDENTITY_RE.fullmatch(value):
        raise ValueError(f"{label} has an invalid identity shape")
    return value


def validate_sha256_hex(value: object, *, label: str = "expected_sha256") -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty hex string")
    normalized = value.lower()
    if not _SHA256_HEX_RE.fullmatch(normalized):
        raise ValueError(f"{label} must be a 64-character lowercase hex SHA-256")
    return normalized


def decode_acceptance_contract_json_base64(contract_json_base64: str) -> bytes:
    if not isinstance(contract_json_base64, str) or not contract_json_base64:
        raise ValueError("contract_json_base64 must be a non-empty base64 string")
    if len(contract_json_base64) > MAX_ACCEPTANCE_CONTRACT_BASE64_CHARS:
        raise ValueError("contract_json_base64 exceeds the bounded payload size")
    if any(ch.isspace() for ch in contract_json_base64):
        raise ValueError("contract_json_base64 must not contain whitespace")
    try:
        payload = base64.b64decode(contract_json_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(
            "contract_json_base64 must be strict standard base64"
        ) from exc
    if not payload:
        raise ValueError("decoded acceptance contract payload must not be empty")
    if len(payload) > MAX_ACCEPTANCE_CONTRACT_BYTES:
        raise ValueError("decoded acceptance contract exceeds the 2 MiB archive limit")
    return payload


def validate_acceptance_contract_object_key(object_key: object) -> str:
    if not isinstance(object_key, str) or not object_key:
        raise ValueError("object_key must be a non-empty string")
    if len(object_key) > MAX_ACCEPTANCE_CONTRACT_KEY_CHARS:
        raise ValueError(
            f"object_key exceeds {MAX_ACCEPTANCE_CONTRACT_KEY_CHARS} characters"
        )
    if object_key.startswith("/") or "\\" in object_key:
        raise ValueError("object_key must not be absolute or use backslashes")
    if ".." in object_key.split("/"):
        raise ValueError("object_key must not contain path traversal segments")
    if "//" in object_key:
        raise ValueError("object_key must not contain empty path segments")
    if not object_key.startswith(ACCEPTANCE_CONTRACT_PREFIX):
        raise ValueError(
            "object_key must stay under the canonical acceptance-contracts prefix"
        )
    remainder = object_key[len(ACCEPTANCE_CONTRACT_PREFIX) :]
    if not remainder or remainder.endswith("/"):
        raise ValueError("object_key must name a concrete object under the prefix")
    return object_key


def assert_canonical_legalai_bucket(bucket: object) -> str:
    if not isinstance(bucket, str) or not bucket:
        raise ValueError("bucket must be configured")
    if bucket != CANONICAL_LEGALAI_BUCKET:
        raise ValueError(
            f"refusing non-canonical bucket; expected {CANONICAL_LEGALAI_BUCKET}"
        )
    return bucket


def parse_acceptance_contract_v1(payload: bytes) -> dict[str, Any]:
    """Parse and strictly validate the generic acceptance_contract.v1 object."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("acceptance contract must be UTF-8 JSON") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("acceptance contract must be valid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("acceptance contract root must be a JSON object")

    schema = document.get("schema")
    if schema != ACCEPTANCE_CONTRACT_SCHEMA:
        raise ValueError(
            f"acceptance contract schema must be {ACCEPTANCE_CONTRACT_SCHEMA}"
        )

    contract_id = _validate_acceptance_identity(
        document.get("contract_id"), label="contract_id"
    )
    version = _validate_acceptance_identity(document.get("version"), label="version")
    benchmark_id = _validate_acceptance_identity(
        document.get("benchmark_id"), label="benchmark_id"
    )
    question_id = _validate_acceptance_identity(
        document.get("question_id"), label="question_id"
    )
    # Return only safe identity fields — never criterion prose or private content.
    return {
        "schema": ACCEPTANCE_CONTRACT_SCHEMA,
        "contract_id": contract_id,
        "version": version,
        "benchmark_id": benchmark_id,
        "question_id": question_id,
    }


def canonical_acceptance_contract_sha256(payload: bytes) -> str:
    """SHA-256 of the exact archived UTF-8 JSON bytes (content-addressed)."""
    return hashlib.sha256(payload).hexdigest()


def build_acceptance_contract_archive(
    *,
    contract_json_base64: str,
    expected_object_key: str,
    expected_benchmark_id: str,
    expected_question_id: str,
    expected_contract_id: str,
    expected_version: str,
    expected_sha256: str,
) -> dict[str, Any]:
    """Validate inputs and return one create-only archive item (safe metadata)."""
    object_key = validate_acceptance_contract_object_key(expected_object_key)
    expected_benchmark = _validate_acceptance_identity(
        expected_benchmark_id, label="expected_benchmark_id"
    )
    expected_question = _validate_acceptance_identity(
        expected_question_id, label="expected_question_id"
    )
    expected_contract = _validate_acceptance_identity(
        expected_contract_id, label="expected_contract_id"
    )
    expected_ver = _validate_acceptance_identity(
        expected_version, label="expected_version"
    )
    expected_digest = validate_sha256_hex(expected_sha256)

    payload = decode_acceptance_contract_json_base64(contract_json_base64)
    identity = parse_acceptance_contract_v1(payload)
    digest = canonical_acceptance_contract_sha256(payload)
    if digest != expected_digest:
        raise ValueError("acceptance contract SHA-256 does not match expected_sha256")

    if identity["benchmark_id"] != expected_benchmark:
        raise ValueError("benchmark_id does not match expected_benchmark_id")
    if identity["question_id"] != expected_question:
        raise ValueError("question_id does not match expected_question_id")
    if identity["contract_id"] != expected_contract:
        raise ValueError("contract_id does not match expected_contract_id")
    if identity["version"] != expected_ver:
        raise ValueError("version does not match expected_version")

    filename = object_key.rsplit("/", 1)[-1]
    return {
        "filename": filename,
        "object_key": object_key,
        "payload": payload,
        "content_type": "application/json",
        "sha256": digest,
        "size": len(payload),
        "contract_id": identity["contract_id"],
        "version": identity["version"],
        "benchmark_id": identity["benchmark_id"],
        "question_id": identity["question_id"],
        "schema": identity["schema"],
    }
