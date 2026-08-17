from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable, Mapping
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
# Generic acceptance_contract.v1 archival (LegalAI-compatible schema/hashing)
# ---------------------------------------------------------------------------

ACCEPTANCE_CONTRACT_SCHEMA = "acceptance_contract.v1"
ACCEPTANCE_CONTRACT_SCHEMA_VERSION = ACCEPTANCE_CONTRACT_SCHEMA
ACCEPTANCE_CONTRACT_PREFIX = "Benchmarks/acceptance-contracts/"
CANONICAL_LEGALAI_BUCKET = "legalai-corpus"
MAX_ACCEPTANCE_CONTRACT_BYTES = MAX_ARCHIVE_ITEM_BYTES
MAX_ACCEPTANCE_CONTRACT_BASE64_CHARS = ((MAX_ACCEPTANCE_CONTRACT_BYTES + 2) // 3) * 4
MAX_ACCEPTANCE_CONTRACT_KEY_CHARS = 512
MAX_ACCEPTANCE_IDENTITY_CHARS = 128
_ACCEPTANCE_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_ACCEPTANCE_TOP_LEVEL = (
    "schema_version",
    "contract_id",
    "version",
    "identity",
    "required_criterion_ids",
    "evidence_constraints",
    "semantic_preservation",
    "duplication_rules",
    "object_key",
    "content_sha256",
)
_SAFE_SCALAR_ECHO_PATH_PREFIXES = (
    "$.schema_version",
    "$.contract_id",
    "$.version",
    "$.identity.",
    "$.object_key",
    "$.content_sha256",
    "expected_",
)


class AcceptanceContractValidationError(ValueError):
    """Fail-closed validation error with safe, actionable diagnostics."""

    def __init__(
        self,
        *,
        path: str,
        constraint: str,
        received: object,
    ) -> None:
        self.path = path
        self.constraint = constraint
        self.received_type = _received_type_name(received)
        self.received_value = _safe_received_value(path, received)
        super().__init__(
            f"{path}: expected {constraint}; "
            f"received_type={self.received_type} "
            f"received_value={self.received_value}"
        )


def _received_type_name(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _safe_received_value(path: str, value: object) -> str:
    """Return a safe diagnostic value that never echoes contract prose/content."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, list):
        return f"array(len={len(value)})"
    if isinstance(value, dict):
        return f"object(keys={len(value)})"
    if isinstance(value, str):
        allow_echo = any(path.startswith(prefix) for prefix in _SAFE_SCALAR_ECHO_PATH_PREFIXES)
        if allow_echo and len(value) <= MAX_ACCEPTANCE_IDENTITY_CHARS:
            return repr(value)
        return f"string(len={len(value)})"
    return f"unsupported({type(value).__name__})"


def _reject(path: str, constraint: str, received: object) -> None:
    raise AcceptanceContractValidationError(
        path=path, constraint=constraint, received=received
    )


def acceptance_contract_prefix() -> str:
    """Canonical B2 prefix for private acceptance-contract objects."""
    return ACCEPTANCE_CONTRACT_PREFIX


def acceptance_contract_json_schema() -> dict[str, Any]:
    """Exact acceptance_contract.v1 nested-identity JSON Schema (generic)."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(_REQUIRED_ACCEPTANCE_TOP_LEVEL),
        "properties": {
            "schema_version": {
                "type": "string",
                "enum": [ACCEPTANCE_CONTRACT_SCHEMA_VERSION],
            },
            "contract_id": {"type": "string", "minLength": 1},
            "version": {"type": "string", "minLength": 1},
            "identity": {
                "type": "object",
                "additionalProperties": False,
                "required": ["benchmark_id", "question_id"],
                "properties": {
                    "benchmark_id": {"type": "string", "minLength": 1},
                    "question_id": {"type": "string", "minLength": 1},
                },
            },
            "required_criterion_ids": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
            "evidence_constraints": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "allowed_source_types",
                    "require_page_citations",
                    "max_excerpts_per_criterion",
                ],
                "properties": {
                    "allowed_source_types": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "require_page_citations": {"type": "boolean"},
                    "max_excerpts_per_criterion": {
                        "type": "integer",
                        "minimum": 1,
                    },
                },
            },
            "semantic_preservation": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "require_same_party_roles",
                    "forbid_material_omissions",
                    "require_preserve_negation",
                ],
                "properties": {
                    "require_same_party_roles": {"type": "boolean"},
                    "forbid_material_omissions": {"type": "boolean"},
                    "require_preserve_negation": {"type": "boolean"},
                },
            },
            "duplication_rules": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "forbid_duplicate_criterion_ids",
                    "forbid_overlapping_evidence_spans",
                    "max_duplicate_phrase_ratio",
                ],
                "properties": {
                    "forbid_duplicate_criterion_ids": {"type": "boolean"},
                    "forbid_overlapping_evidence_spans": {"type": "boolean"},
                    "max_duplicate_phrase_ratio": {"type": "number", "minimum": 0},
                },
            },
            "criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id"],
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "presence_phrases": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "evidence_phrases": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "semantic_required_phrases": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "semantic_forbidden_phrases": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "fallback_text": {"type": "string"},
                        "category": {"type": "string"},
                    },
                },
            },
            "structure_requirements": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "required_kinds",
                    "required_ranges",
                    "required_categories",
                ],
                "properties": {
                    "required_kinds": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                    "required_ranges": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["kind", "start", "end"],
                            "properties": {
                                "kind": {"type": "string", "minLength": 1},
                                "start": {"type": "integer"},
                                "end": {"type": "integer"},
                                "category": {"type": "string"},
                            },
                        },
                    },
                    "required_categories": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
            "object_key": {"type": "string", "minLength": 1},
            "content_sha256": {
                "type": "string",
                "minLength": 64,
                "maxLength": 64,
                "pattern": "^[0-9a-f]{64}$",
            },
        },
    }


def canonical_acceptance_hashing_rules() -> dict[str, Any]:
    """Document LegalAI canonical hashing rules (no private material)."""
    return {
        "contract_sha256": {
            "description": (
                "SHA-256 of canonical UTF-8 JSON excluding the content_sha256 field"
            ),
            "json_dumps": {
                "sort_keys": True,
                "separators": [",", ":"],
                "ensure_ascii": False,
            },
            "excludes_field": "content_sha256",
        },
        "content_sha256": {
            "description": (
                "Embedded field that must equal contract_sha256 for the same document"
            ),
        },
        "object_sha256": {
            "description": (
                "SHA-256 of the exact serialized object bytes stored in B2 "
                "(independent of canonical JSON reformatting)"
            ),
        },
        "canonical_object_key": {
            "template": (
                f"{ACCEPTANCE_CONTRACT_PREFIX}"
                "{benchmark_id}/{question_id}/{contract_id}/v{version}/"
                "acceptance_contract.json"
            ),
            "prefix": ACCEPTANCE_CONTRACT_PREFIX,
            "version_path_prefix": "v",
        },
    }


def _validate_acceptance_identity(value: object, *, path: str) -> str:
    if not isinstance(value, str) or not value:
        _reject(
            path,
            f"non-empty string (max {MAX_ACCEPTANCE_IDENTITY_CHARS} chars, "
            r"shape ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$)",
            value,
        )
    if len(value) > MAX_ACCEPTANCE_IDENTITY_CHARS:
        _reject(
            path,
            f"identity string length <= {MAX_ACCEPTANCE_IDENTITY_CHARS}",
            value,
        )
    if not _ACCEPTANCE_IDENTITY_RE.fullmatch(value):
        _reject(
            path,
            r"identity shape ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
            value,
        )
    return value


def canonical_acceptance_contract_object_key(
    *,
    benchmark_id: str,
    question_id: str,
    contract_id: str,
    version: str,
) -> str:
    """Build the deterministic B2 object key from validated identity fields."""
    key = (
        f"{ACCEPTANCE_CONTRACT_PREFIX}"
        f"{benchmark_id}/{question_id}/{contract_id}/v{version}/"
        "acceptance_contract.json"
    )
    return validate_acceptance_contract_object_key(key)


def validate_sha256_hex(value: object, *, label: str = "expected_sha256") -> str:
    path = label if label.startswith(("$", "expected_")) else label
    if not isinstance(value, str) or not value:
        _reject(path, "non-empty 64-character lowercase hex SHA-256", value)
    normalized = value.lower()
    if not _SHA256_HEX_RE.fullmatch(normalized):
        _reject(path, "64-character lowercase hex SHA-256", value)
    return normalized


def decode_acceptance_contract_json_base64(contract_json_base64: str) -> bytes:
    if not isinstance(contract_json_base64, str) or not contract_json_base64:
        _reject(
            "contract_json_base64",
            "non-empty strict standard base64 string",
            contract_json_base64,
        )
    if len(contract_json_base64) > MAX_ACCEPTANCE_CONTRACT_BASE64_CHARS:
        _reject(
            "contract_json_base64",
            f"base64 length <= {MAX_ACCEPTANCE_CONTRACT_BASE64_CHARS}",
            contract_json_base64,
        )
    if any(ch.isspace() for ch in contract_json_base64):
        _reject(
            "contract_json_base64",
            "base64 without whitespace",
            contract_json_base64,
        )
    try:
        payload = base64.b64decode(contract_json_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AcceptanceContractValidationError(
            path="contract_json_base64",
            constraint="strict standard base64",
            received=contract_json_base64,
        ) from exc
    if not payload:
        _reject("contract_json_base64", "non-empty decoded payload", payload)
    if len(payload) > MAX_ACCEPTANCE_CONTRACT_BYTES:
        _reject(
            "contract_json_base64",
            f"decoded payload <= {MAX_ACCEPTANCE_CONTRACT_BYTES} bytes",
            payload,
        )
    return payload


def validate_acceptance_contract_object_key(object_key: object) -> str:
    path = "$.object_key"
    if not isinstance(object_key, str) or not object_key:
        _reject(path, "non-empty string under canonical prefix", object_key)
    if len(object_key) > MAX_ACCEPTANCE_CONTRACT_KEY_CHARS:
        _reject(
            path,
            f"length <= {MAX_ACCEPTANCE_CONTRACT_KEY_CHARS}",
            object_key,
        )
    if object_key.startswith("/") or "\\" in object_key:
        _reject(path, "relative key without backslashes", object_key)
    if ".." in object_key.split("/"):
        _reject(path, "no path traversal segments", object_key)
    if "//" in object_key:
        _reject(path, "no empty path segments", object_key)
    if not object_key.startswith(ACCEPTANCE_CONTRACT_PREFIX):
        _reject(
            path,
            f"prefix {ACCEPTANCE_CONTRACT_PREFIX!r}",
            object_key,
        )
    remainder = object_key[len(ACCEPTANCE_CONTRACT_PREFIX) :]
    if not remainder or remainder.endswith("/"):
        _reject(path, "concrete object name under prefix", object_key)
    return object_key


def assert_canonical_legalai_bucket(bucket: object) -> str:
    if not isinstance(bucket, str) or not bucket:
        raise ValueError("bucket must be configured")
    if bucket != CANONICAL_LEGALAI_BUCKET:
        raise ValueError(
            f"refusing non-canonical bucket; expected {CANONICAL_LEGALAI_BUCKET}"
        )
    return bucket


def _require_non_empty_string_list(value: object, *, path: str) -> list[str]:
    if not isinstance(value, list) or not value:
        _reject(path, "non-empty array of strings", value)
    out: list[str] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, str) or not item:
            _reject(item_path, "non-empty string", item)
        out.append(item)
    return out


def _validate_evidence_constraints(value: object) -> None:
    path = "$.evidence_constraints"
    if not isinstance(value, dict):
        _reject(path, "object", value)
    required = (
        "allowed_source_types",
        "require_page_citations",
        "max_excerpts_per_criterion",
    )
    missing = [key for key in required if key not in value]
    if missing:
        _reject(path, f"required properties {list(required)}", value)
    extra = sorted(set(value) - set(required))
    if extra:
        _reject(path, f"only properties {list(required)}", value)
    _require_non_empty_string_list(
        value.get("allowed_source_types"),
        path=f"{path}.allowed_source_types",
    )
    if not isinstance(value.get("require_page_citations"), bool):
        _reject(
            f"{path}.require_page_citations",
            "boolean",
            value.get("require_page_citations"),
        )
    max_excerpts = value.get("max_excerpts_per_criterion")
    if (
        not isinstance(max_excerpts, int)
        or isinstance(max_excerpts, bool)
        or max_excerpts < 1
    ):
        _reject(
            f"{path}.max_excerpts_per_criterion",
            "integer >= 1",
            max_excerpts,
        )


def _validate_semantic_preservation(value: object) -> None:
    path = "$.semantic_preservation"
    if not isinstance(value, dict):
        _reject(path, "object", value)
    required = (
        "require_same_party_roles",
        "forbid_material_omissions",
        "require_preserve_negation",
    )
    missing = [key for key in required if key not in value]
    if missing:
        _reject(path, f"required properties {list(required)}", value)
    extra = sorted(set(value) - set(required))
    if extra:
        _reject(path, f"only properties {list(required)}", value)
    for key in required:
        if not isinstance(value.get(key), bool):
            _reject(f"{path}.{key}", "boolean", value.get(key))


def _validate_duplication_rules(value: object) -> None:
    path = "$.duplication_rules"
    if not isinstance(value, dict):
        _reject(path, "object", value)
    required = (
        "forbid_duplicate_criterion_ids",
        "forbid_overlapping_evidence_spans",
        "max_duplicate_phrase_ratio",
    )
    missing = [key for key in required if key not in value]
    if missing:
        _reject(path, f"required properties {list(required)}", value)
    extra = sorted(set(value) - set(required))
    if extra:
        _reject(path, f"only properties {list(required)}", value)
    for key in (
        "forbid_duplicate_criterion_ids",
        "forbid_overlapping_evidence_spans",
    ):
        if not isinstance(value.get(key), bool):
            _reject(f"{path}.{key}", "boolean", value.get(key))
    ratio = value.get("max_duplicate_phrase_ratio")
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or float(ratio) < 0:
        _reject(f"{path}.max_duplicate_phrase_ratio", "number >= 0", ratio)


def _validate_optional_string_list(value: object, *, path: str) -> None:
    if not isinstance(value, list):
        _reject(path, "array of strings", value)
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            _reject(f"{path}[{index}]", "non-empty string", item)


def _validate_optional_criteria(value: object) -> None:
    path = "$.criteria"
    if not isinstance(value, list):
        _reject(path, "array", value)
    allowed = {
        "id",
        "presence_phrases",
        "evidence_phrases",
        "semantic_required_phrases",
        "semantic_forbidden_phrases",
        "fallback_text",
        "category",
    }
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            _reject(item_path, "object", item)
        if "id" not in item:
            _reject(item_path, "required property 'id'", item)
        if not isinstance(item.get("id"), str) or not item["id"]:
            _reject(f"{item_path}.id", "non-empty string", item.get("id"))
        extra = sorted(set(item) - allowed)
        if extra:
            _reject(item_path, f"only properties {sorted(allowed)}", item)
        for key in (
            "presence_phrases",
            "evidence_phrases",
            "semantic_required_phrases",
            "semantic_forbidden_phrases",
        ):
            if key in item:
                _validate_optional_string_list(
                    item[key], path=f"{item_path}.{key}"
                )
        for key in ("fallback_text", "category"):
            if key in item and not isinstance(item[key], str):
                _reject(f"{item_path}.{key}", "string", item[key])


def _validate_optional_structure_requirements(value: object) -> None:
    path = "$.structure_requirements"
    if not isinstance(value, dict):
        _reject(path, "object", value)
    required = ("required_kinds", "required_ranges", "required_categories")
    missing = [key for key in required if key not in value]
    if missing:
        _reject(path, f"required properties {list(required)}", value)
    extra = sorted(set(value) - set(required))
    if extra:
        _reject(path, f"only properties {list(required)}", value)
    _validate_optional_string_list(
        value.get("required_kinds"), path=f"{path}.required_kinds"
    )
    _validate_optional_string_list(
        value.get("required_categories"),
        path=f"{path}.required_categories",
    )
    ranges = value.get("required_ranges")
    if not isinstance(ranges, list):
        _reject(f"{path}.required_ranges", "array", ranges)
    for index, item in enumerate(ranges):
        item_path = f"{path}.required_ranges[{index}]"
        if not isinstance(item, dict):
            _reject(item_path, "object", item)
        for key in ("kind", "start", "end"):
            if key not in item:
                _reject(item_path, f"required property '{key}'", item)
        if not isinstance(item.get("kind"), str) or not item["kind"]:
            _reject(f"{item_path}.kind", "non-empty string", item.get("kind"))
        for key in ("start", "end"):
            if not isinstance(item.get(key), int) or isinstance(item.get(key), bool):
                _reject(f"{item_path}.{key}", "integer", item.get(key))
        allowed = {"kind", "start", "end", "category"}
        extra_item = sorted(set(item) - allowed)
        if extra_item:
            _reject(item_path, f"only properties {sorted(allowed)}", item)
        if "category" in item and not isinstance(item["category"], str):
            _reject(f"{item_path}.category", "string", item["category"])


def canonical_acceptance_contract_json_bytes(document: Mapping[str, Any]) -> bytes:
    """LegalAI canonical UTF-8 JSON: sorted keys, compact, excluding content_sha256."""
    without = {k: v for k, v in document.items() if k != "content_sha256"}
    return json.dumps(
        without,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def serialize_acceptance_contract_stored_bytes(document: Mapping[str, Any]) -> bytes:
    """Deterministic UTF-8 JSON bytes for the exact object stored in B2."""
    return json.dumps(
        dict(document),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_acceptance_contract_sha256(document: Mapping[str, Any]) -> str:
    """SHA-256 of LegalAI canonical JSON excluding content_sha256 (contract_sha256)."""
    return hashlib.sha256(canonical_acceptance_contract_json_bytes(document)).hexdigest()


def compute_acceptance_object_sha256(payload: bytes) -> str:
    """SHA-256 of the exact serialized object bytes stored in B2."""
    return hashlib.sha256(payload).hexdigest()


def canonical_acceptance_contract_sha256(payload: bytes) -> str:
    """Backward-compatible alias: contract_sha256 from parsed object bytes."""
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceContractValidationError(
            path="$",
            constraint="UTF-8 JSON object",
            received=None,
        ) from exc
    if not isinstance(document, dict):
        _reject("$", "JSON object", document)
    return compute_acceptance_contract_sha256(document)


def build_synthetic_acceptance_contract(
    *,
    contract_id: str,
    version: str,
    benchmark_id: str,
    question_id: str,
    object_key: str,
    required_criterion_ids: list[str],
    schema_version: str = ACCEPTANCE_CONTRACT_SCHEMA_VERSION,
    criteria: list[dict[str, Any]] | None = None,
    structure_requirements: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Match LegalAI ``build_synthetic_contract`` (generic IDs only; tests/fixtures)."""
    if criteria is None:
        built_criteria: list[dict[str, Any]] = []
        for cid in required_criterion_ids:
            token = cid.replace("-", " ")
            built_criteria.append(
                {
                    "id": cid,
                    "presence_phrases": [token],
                    "evidence_phrases": [f"evidence:{cid}"],
                    "semantic_required_phrases": [f"preserve:{cid}"],
                    "semantic_forbidden_phrases": [f"negate:{cid}"],
                    "fallback_text": (
                        f"Synthetic fallback for {cid} covering {token} "
                        f"with evidence:{cid} and preserve:{cid}."
                    ),
                    "category": "",
                }
            )
        criteria = built_criteria
    if structure_requirements is None:
        structure_requirements = {
            "required_kinds": [],
            "required_ranges": [],
            "required_categories": [],
        }
    document: dict[str, Any] = {
        "schema_version": schema_version,
        "contract_id": contract_id,
        "version": version,
        "identity": {
            "benchmark_id": benchmark_id,
            "question_id": question_id,
        },
        "required_criterion_ids": list(required_criterion_ids),
        "evidence_constraints": {
            "allowed_source_types": ["complaint", "answer"],
            "require_page_citations": True,
            "max_excerpts_per_criterion": 3,
        },
        "semantic_preservation": {
            "require_same_party_roles": True,
            "forbid_material_omissions": True,
            "require_preserve_negation": True,
        },
        "duplication_rules": {
            "forbid_duplicate_criterion_ids": True,
            "forbid_overlapping_evidence_spans": False,
            "max_duplicate_phrase_ratio": 0.25,
        },
        "criteria": list(criteria),
        "structure_requirements": dict(structure_requirements),
        "object_key": object_key,
    }
    document["content_sha256"] = compute_acceptance_contract_sha256(document)
    return document


def build_acceptance_contract_template() -> dict[str, Any]:
    """Read-only template: schema, hashing rules, and one synthetic example."""
    benchmark_id = "synth-benchmark-alpha"
    question_id = "Q-SYNTH-01"
    contract_id = "contract-synth-alpha-q01"
    version = "1.0.0"
    object_key = canonical_acceptance_contract_object_key(
        benchmark_id=benchmark_id,
        question_id=question_id,
        contract_id=contract_id,
        version=version,
    )
    example = build_synthetic_acceptance_contract(
        contract_id=contract_id,
        version=version,
        benchmark_id=benchmark_id,
        question_id=question_id,
        object_key=object_key,
        required_criterion_ids=["crit-presence", "crit-negation", "crit-roles"],
    )
    return {
        "ok": True,
        "schema_version": ACCEPTANCE_CONTRACT_SCHEMA_VERSION,
        "prefix": ACCEPTANCE_CONTRACT_PREFIX,
        "bucket": CANONICAL_LEGALAI_BUCKET,
        "json_schema": acceptance_contract_json_schema(),
        "canonical_hashing": canonical_acceptance_hashing_rules(),
        "example": example,
        "archive_preparation": {
            "tool": "archive_acceptance_contract",
            "preferred_field": "contract",
            "pass_example_directly": True,
            "notes": (
                "Pass template['example'] as archive_acceptance_contract(contract=...) "
                "with no Base64, Web Crypto, or client-side hash/key computation."
            ),
        },
    }


def parse_acceptance_contract_v1(payload: bytes) -> dict[str, Any]:
    """Parse and strictly validate LegalAI acceptance_contract.v1 (safe metadata only)."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AcceptanceContractValidationError(
            path="$",
            constraint="UTF-8 JSON object",
            received=None,
        ) from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AcceptanceContractValidationError(
            path="$",
            constraint="valid JSON object",
            received=None,
        ) from exc
    if not isinstance(document, dict):
        _reject("$", "JSON object", document)

    # Reject the pre-LegalAI flat shape (schema / top-level benchmark_id / question_id).
    if "schema" in document or (
        ("benchmark_id" in document or "question_id" in document)
        and "identity" not in document
    ):
        _reject(
            "$",
            "nested-identity acceptance_contract.v1 (obsolete flat schema rejected)",
            document,
        )

    schema_version = document.get("schema_version")
    if schema_version != ACCEPTANCE_CONTRACT_SCHEMA_VERSION:
        _reject(
            "$.schema_version",
            f"enum [{ACCEPTANCE_CONTRACT_SCHEMA_VERSION}]",
            schema_version,
        )

    missing = [key for key in _REQUIRED_ACCEPTANCE_TOP_LEVEL if key not in document]
    if missing:
        _reject(
            "$",
            f"required properties {list(_REQUIRED_ACCEPTANCE_TOP_LEVEL)}",
            document,
        )

    allowed_top = set(_REQUIRED_ACCEPTANCE_TOP_LEVEL) | {
        "criteria",
        "structure_requirements",
    }
    unexpected = sorted(set(document) - allowed_top)
    if unexpected:
        _reject("$", f"only properties {sorted(allowed_top)}", document)

    contract_id = _validate_acceptance_identity(
        document.get("contract_id"), path="$.contract_id"
    )
    version = _validate_acceptance_identity(document.get("version"), path="$.version")

    identity = document.get("identity")
    if not isinstance(identity, dict):
        _reject("$.identity", "object", identity)
    if sorted(identity.keys()) != ["benchmark_id", "question_id"]:
        _reject(
            "$.identity",
            "only properties ['benchmark_id', 'question_id']",
            identity,
        )
    benchmark_id = _validate_acceptance_identity(
        identity.get("benchmark_id"), path="$.identity.benchmark_id"
    )
    question_id = _validate_acceptance_identity(
        identity.get("question_id"), path="$.identity.question_id"
    )

    required_criterion_ids = _require_non_empty_string_list(
        document.get("required_criterion_ids"), path="$.required_criterion_ids"
    )
    _validate_evidence_constraints(document.get("evidence_constraints"))
    _validate_semantic_preservation(document.get("semantic_preservation"))
    _validate_duplication_rules(document.get("duplication_rules"))
    if "criteria" in document:
        _validate_optional_criteria(document.get("criteria"))
    if "structure_requirements" in document:
        _validate_optional_structure_requirements(
            document.get("structure_requirements")
        )

    embedded_object_key = validate_acceptance_contract_object_key(
        document.get("object_key")
    )
    embedded_content_sha256 = validate_sha256_hex(
        document.get("content_sha256"), label="$.content_sha256"
    )
    recomputed = compute_acceptance_contract_sha256(document)
    if embedded_content_sha256 != recomputed:
        _reject(
            "$.content_sha256",
            "digest equal to recomputed contract_sha256",
            embedded_content_sha256,
        )

    # Safe metadata only — never return criteria / rule prose.
    return {
        "schema_version": ACCEPTANCE_CONTRACT_SCHEMA_VERSION,
        "schema": ACCEPTANCE_CONTRACT_SCHEMA_VERSION,
        "contract_id": contract_id,
        "version": version,
        "benchmark_id": benchmark_id,
        "question_id": question_id,
        "required_criterion_ids": required_criterion_ids,
        "object_key": embedded_object_key,
        "content_sha256": embedded_content_sha256,
        "contract_sha256": recomputed,
    }


def build_acceptance_contract_archive(
    *,
    contract: Mapping[str, Any] | None = None,
    contract_json_base64: str | None = None,
    expected_benchmark_id: str | None = None,
    expected_question_id: str | None = None,
    expected_contract_id: str | None = None,
    expected_version: str | None = None,
    expected_contract_sha256: str | None = None,
    expected_sha256: str | None = None,
    expected_object_key: str | None = None,
) -> dict[str, Any]:
    """Validate inputs and return one create-only archive item (safe metadata).

    Preferred path: pass ``contract`` as a structured acceptance_contract.v1 object.
    The server performs canonical UTF-8 serialization, validates the schema,
    computes/verifies ``contract_sha256`` (excluding ``content_sha256``), generates
    the canonical object key, and computes ``object_sha256`` of the stored bytes.
    Identity and digest ``expected_*`` fields are not required on this path.

    Legacy path: ``contract_json_base64`` plus required nested identity expectations
    remains supported for backward compatibility.
    """
    has_contract = contract is not None
    has_base64 = bool(contract_json_base64)
    if has_contract and has_base64:
        _reject(
            "contract",
            "provide either structured contract or contract_json_base64, not both",
            "both",
        )
    if not has_contract and not has_base64:
        _reject(
            "contract",
            "structured JSON object (preferred) or legacy contract_json_base64",
            None,
        )

    def _optional_str(value: str | None) -> str | None:
        if value is None or value == "":
            return None
        return value

    expected_object_key = _optional_str(expected_object_key)
    expected_contract_sha256 = _optional_str(expected_contract_sha256)
    expected_sha256 = _optional_str(expected_sha256)
    expected_benchmark_id = _optional_str(expected_benchmark_id)
    expected_question_id = _optional_str(expected_question_id)
    expected_contract_id = _optional_str(expected_contract_id)
    expected_version = _optional_str(expected_version)

    if has_contract:
        if not isinstance(contract, Mapping) or isinstance(contract, (str, bytes)):
            _reject("contract", "JSON object", contract)
        try:
            # Normalize to a plain JSON-compatible dict (rejects non-JSON values).
            document = json.loads(
                json.dumps(dict(contract), ensure_ascii=False, allow_nan=False)
            )
        except (TypeError, ValueError) as exc:
            raise AcceptanceContractValidationError(
                path="contract",
                constraint="JSON-serializable object",
                received=None,
            ) from exc
        if not isinstance(document, dict):
            _reject("contract", "JSON object", document)
        payload = serialize_acceptance_contract_stored_bytes(document)
        identity = parse_acceptance_contract_v1(payload)
        object_key = canonical_acceptance_contract_object_key(
            benchmark_id=identity["benchmark_id"],
            question_id=identity["question_id"],
            contract_id=identity["contract_id"],
            version=identity["version"],
        )
        if identity["object_key"] != object_key:
            _reject(
                "$.object_key",
                f"equal to generated key {object_key!r}",
                identity["object_key"],
            )
        # Optional caller checks (never required for structured path).
        if expected_benchmark_id is not None:
            expected_benchmark = _validate_acceptance_identity(
                expected_benchmark_id, path="expected_benchmark_id"
            )
            if identity["benchmark_id"] != expected_benchmark:
                _reject(
                    "$.identity.benchmark_id",
                    f"equal to expected_benchmark_id {expected_benchmark!r}",
                    identity["benchmark_id"],
                )
        if expected_question_id is not None:
            expected_question = _validate_acceptance_identity(
                expected_question_id, path="expected_question_id"
            )
            if identity["question_id"] != expected_question:
                _reject(
                    "$.identity.question_id",
                    f"equal to expected_question_id {expected_question!r}",
                    identity["question_id"],
                )
        if expected_contract_id is not None:
            expected_contract = _validate_acceptance_identity(
                expected_contract_id, path="expected_contract_id"
            )
            if identity["contract_id"] != expected_contract:
                _reject(
                    "$.contract_id",
                    f"equal to expected_contract_id {expected_contract!r}",
                    identity["contract_id"],
                )
        if expected_version is not None:
            expected_ver = _validate_acceptance_identity(
                expected_version, path="expected_version"
            )
            if identity["version"] != expected_ver:
                _reject(
                    "$.version",
                    f"equal to expected_version {expected_ver!r}",
                    identity["version"],
                )
    else:
        if expected_benchmark_id is None:
            _reject(
                "expected_benchmark_id",
                "non-empty identity string (required with contract_json_base64)",
                expected_benchmark_id,
            )
        if expected_question_id is None:
            _reject(
                "expected_question_id",
                "non-empty identity string (required with contract_json_base64)",
                expected_question_id,
            )
        if expected_contract_id is None:
            _reject(
                "expected_contract_id",
                "non-empty identity string (required with contract_json_base64)",
                expected_contract_id,
            )
        if expected_version is None:
            _reject(
                "expected_version",
                "non-empty identity string (required with contract_json_base64)",
                expected_version,
            )
        expected_benchmark = _validate_acceptance_identity(
            expected_benchmark_id, path="expected_benchmark_id"
        )
        expected_question = _validate_acceptance_identity(
            expected_question_id, path="expected_question_id"
        )
        expected_contract = _validate_acceptance_identity(
            expected_contract_id, path="expected_contract_id"
        )
        expected_ver = _validate_acceptance_identity(
            expected_version, path="expected_version"
        )
        object_key = canonical_acceptance_contract_object_key(
            benchmark_id=expected_benchmark,
            question_id=expected_question,
            contract_id=expected_contract,
            version=expected_ver,
        )
        assert contract_json_base64 is not None
        payload = decode_acceptance_contract_json_base64(contract_json_base64)
        identity = parse_acceptance_contract_v1(payload)
        if identity["object_key"] != object_key:
            _reject(
                "$.object_key",
                f"equal to generated key {object_key!r}",
                identity["object_key"],
            )
        if identity["benchmark_id"] != expected_benchmark:
            _reject(
                "$.identity.benchmark_id",
                f"equal to expected_benchmark_id {expected_benchmark!r}",
                identity["benchmark_id"],
            )
        if identity["question_id"] != expected_question:
            _reject(
                "$.identity.question_id",
                f"equal to expected_question_id {expected_question!r}",
                identity["question_id"],
            )
        if identity["contract_id"] != expected_contract:
            _reject(
                "$.contract_id",
                f"equal to expected_contract_id {expected_contract!r}",
                identity["contract_id"],
            )
        if identity["version"] != expected_ver:
            _reject(
                "$.version",
                f"equal to expected_version {expected_ver!r}",
                identity["version"],
            )

    if expected_object_key is not None:
        provided_key = expected_object_key
        if not isinstance(provided_key, str):
            _reject(
                "expected_object_key",
                "optional string equal to server-generated canonical key",
                provided_key,
            )
        try:
            validate_acceptance_contract_object_key(provided_key)
        except AcceptanceContractValidationError as exc:
            raise AcceptanceContractValidationError(
                path="expected_object_key",
                constraint=exc.constraint,
                received=provided_key,
            ) from exc
        if provided_key != object_key:
            _reject(
                "expected_object_key",
                f"equal to generated key {object_key!r}",
                provided_key,
            )

    contract_sha256 = identity["contract_sha256"]
    object_sha256 = compute_acceptance_object_sha256(payload)

    expected_digest_raw = (
        expected_contract_sha256
        if expected_contract_sha256 is not None
        else expected_sha256
    )
    if has_base64 and expected_digest_raw is None:
        _reject(
            "expected_contract_sha256",
            "non-empty 64-character lowercase hex SHA-256",
            expected_digest_raw,
        )
    if expected_digest_raw is not None:
        expected_digest = validate_sha256_hex(
            expected_digest_raw, label="expected_contract_sha256"
        )
        if contract_sha256 != expected_digest:
            _reject(
                "expected_contract_sha256",
                "equal to recomputed contract_sha256",
                expected_digest,
            )
        if identity["content_sha256"] != expected_digest:
            _reject(
                "$.content_sha256",
                "equal to expected_contract_sha256",
                identity["content_sha256"],
            )

    filename = object_key.rsplit("/", 1)[-1]
    return {
        "filename": filename,
        "object_key": object_key,
        "payload": payload,
        "content_type": "application/json",
        "sha256": object_sha256,
        "contract_sha256": contract_sha256,
        "object_sha256": object_sha256,
        "content_sha256": identity["content_sha256"],
        "size": len(payload),
        "contract_id": identity["contract_id"],
        "version": identity["version"],
        "benchmark_id": identity["benchmark_id"],
        "question_id": identity["question_id"],
        "schema": identity["schema_version"],
        "schema_version": identity["schema_version"],
        "required_criterion_ids": identity["required_criterion_ids"],
        "b2_metadata": {
            "contract_sha256": contract_sha256,
            "object_sha256": object_sha256,
            "sha256": object_sha256,
        },
    }


def resolve_acceptance_contract_retrieval_key(
    *,
    benchmark_id: object,
    question_id: object,
    contract_id: object,
    version: object,
) -> dict[str, str]:
    """Validate bounded identity inputs and return the server-generated B2 key.

    Callers must never accept arbitrary object keys, buckets, prefixes, URLs, or
    filesystem paths — only these four identity fields.
    """
    bid = _validate_acceptance_identity(benchmark_id, path="benchmark_id")
    qid = _validate_acceptance_identity(question_id, path="question_id")
    cid = _validate_acceptance_identity(contract_id, path="contract_id")
    ver = _validate_acceptance_identity(version, path="version")
    object_key = canonical_acceptance_contract_object_key(
        benchmark_id=bid,
        question_id=qid,
        contract_id=cid,
        version=ver,
    )
    return {
        "benchmark_id": bid,
        "question_id": qid,
        "contract_id": cid,
        "version": ver,
        "object_key": object_key,
        "prefix": ACCEPTANCE_CONTRACT_PREFIX,
        "schema_version": ACCEPTANCE_CONTRACT_SCHEMA_VERSION,
    }


def verify_retrieved_acceptance_contract(
    *,
    payload: object,
    benchmark_id: object,
    question_id: object,
    contract_id: object,
    version: object,
    expected_size: object = None,
    stored_contract_sha256: object = None,
    stored_object_sha256: object = None,
    resolved_object_key: object = None,
) -> dict[str, Any]:
    """Fail-closed verified read of one acceptance_contract.v1 object.

    Checks canonical key/identity, byte size, embedded content_sha256 /
    contract_sha256, and independently computes object_sha256. Legacy objects
    may omit digest metadata; any digest metadata that is present must match the
    independently recomputed value. Returns safe metadata plus the structured
    contract only after every check passes. Never returns unrelated objects.
    """
    requested = resolve_acceptance_contract_retrieval_key(
        benchmark_id=benchmark_id,
        question_id=question_id,
        contract_id=contract_id,
        version=version,
    )
    object_key = requested["object_key"]
    if resolved_object_key is not None:
        object_key = validate_acceptance_contract_object_key(resolved_object_key)

    if not isinstance(payload, (bytes, bytearray)):
        _reject("payload", "bytes", payload)
    body = bytes(payload)
    size = len(body)
    if size < 1 or size > MAX_ACCEPTANCE_CONTRACT_BYTES:
        _reject(
            "payload",
            f"byte length between 1 and {MAX_ACCEPTANCE_CONTRACT_BYTES}",
            size,
        )

    if expected_size is not None:
        if not isinstance(expected_size, int) or isinstance(expected_size, bool):
            _reject("size", "positive integer ContentLength", expected_size)
        if expected_size != size:
            _reject(
                "size",
                f"equal to downloaded payload length {size}",
                expected_size,
            )

    object_sha256 = compute_acceptance_object_sha256(body)
    if stored_object_sha256 not in (None, ""):
        stored_object = validate_sha256_hex(
            stored_object_sha256, label="stored_object_sha256"
        )
        if stored_object != object_sha256:
            _reject(
                "object_sha256",
                "equal to independently computed object digest",
                stored_object,
            )

    identity = parse_acceptance_contract_v1(body)

    if identity["object_key"] != object_key:
        _reject(
            "$.object_key",
            f"equal to canonical key {object_key!r}",
            identity["object_key"],
        )
    benchmark_matches = identity["benchmark_id"] == requested["benchmark_id"]
    if resolved_object_key is not None:
        benchmark_matches = (
            identity["benchmark_id"].casefold()
            == requested["benchmark_id"].casefold()
        )
    if not benchmark_matches:
        _reject(
            "$.identity.benchmark_id",
            f"equal to requested benchmark_id {requested['benchmark_id']!r}",
            identity["benchmark_id"],
        )
    if identity["question_id"] != requested["question_id"]:
        _reject(
            "$.identity.question_id",
            f"equal to requested question_id {requested['question_id']!r}",
            identity["question_id"],
        )
    if identity["contract_id"] != requested["contract_id"]:
        _reject(
            "$.contract_id",
            f"equal to requested contract_id {requested['contract_id']!r}",
            identity["contract_id"],
        )
    if identity["version"] != requested["version"]:
        _reject(
            "$.version",
            f"equal to requested version {requested['version']!r}",
            identity["version"],
        )

    if stored_contract_sha256 not in (None, ""):
        stored_contract = validate_sha256_hex(
            stored_contract_sha256, label="stored_contract_sha256"
        )
        if stored_contract != identity["contract_sha256"]:
            _reject(
                "contract_sha256",
                "equal to recomputed contract_sha256 / $.content_sha256",
                stored_contract,
            )

    # Structured contract only after fail-closed verification (do not log body).
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceContractValidationError(
            path="$",
            constraint="UTF-8 JSON object",
            received=None,
        ) from exc
    if not isinstance(document, dict):
        _reject("$", "JSON object", document)

    return {
        "ok": True,
        "verified": True,
        "prefix": ACCEPTANCE_CONTRACT_PREFIX,
        "schema": ACCEPTANCE_CONTRACT_SCHEMA_VERSION,
        "schema_version": ACCEPTANCE_CONTRACT_SCHEMA_VERSION,
        "benchmark_id": identity["benchmark_id"],
        "question_id": identity["question_id"],
        "contract_id": identity["contract_id"],
        "version": identity["version"],
        "object_key": object_key,
        "size": size,
        "content_sha256": identity["content_sha256"],
        "contract_sha256": identity["contract_sha256"],
        "object_sha256": object_sha256,
        "required_criterion_ids": identity["required_criterion_ids"],
        "contract": document,
    }
