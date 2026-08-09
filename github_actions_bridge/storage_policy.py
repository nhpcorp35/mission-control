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
