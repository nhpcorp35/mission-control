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


def _require_non_empty_string_list(value: object, *, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty array")
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise ValueError(f"{label}[{index}] must be a non-empty string")
        out.append(item)
    return out


def _validate_evidence_constraints(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("evidence_constraints must be an object")
    required = (
        "allowed_source_types",
        "require_page_citations",
        "max_excerpts_per_criterion",
    )
    missing = [key for key in required if key not in value]
    if missing:
        raise ValueError("evidence_constraints missing required properties")
    extra = sorted(set(value) - set(required))
    if extra:
        raise ValueError("evidence_constraints has unexpected properties")
    _require_non_empty_string_list(
        value.get("allowed_source_types"), label="evidence_constraints.allowed_source_types"
    )
    if not isinstance(value.get("require_page_citations"), bool):
        raise ValueError("evidence_constraints.require_page_citations must be boolean")
    max_excerpts = value.get("max_excerpts_per_criterion")
    if not isinstance(max_excerpts, int) or isinstance(max_excerpts, bool) or max_excerpts < 1:
        raise ValueError("evidence_constraints.max_excerpts_per_criterion invalid")


def _validate_semantic_preservation(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("semantic_preservation must be an object")
    required = (
        "require_same_party_roles",
        "forbid_material_omissions",
        "require_preserve_negation",
    )
    missing = [key for key in required if key not in value]
    if missing:
        raise ValueError("semantic_preservation missing required properties")
    extra = sorted(set(value) - set(required))
    if extra:
        raise ValueError("semantic_preservation has unexpected properties")
    for key in required:
        if not isinstance(value.get(key), bool):
            raise ValueError(f"semantic_preservation.{key} must be boolean")


def _validate_duplication_rules(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("duplication_rules must be an object")
    required = (
        "forbid_duplicate_criterion_ids",
        "forbid_overlapping_evidence_spans",
        "max_duplicate_phrase_ratio",
    )
    missing = [key for key in required if key not in value]
    if missing:
        raise ValueError("duplication_rules missing required properties")
    extra = sorted(set(value) - set(required))
    if extra:
        raise ValueError("duplication_rules has unexpected properties")
    for key in (
        "forbid_duplicate_criterion_ids",
        "forbid_overlapping_evidence_spans",
    ):
        if not isinstance(value.get(key), bool):
            raise ValueError(f"duplication_rules.{key} must be boolean")
    ratio = value.get("max_duplicate_phrase_ratio")
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or float(ratio) < 0:
        raise ValueError("duplication_rules.max_duplicate_phrase_ratio invalid")


def _validate_optional_string_list(value: object, *, label: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise ValueError(f"{label}[{index}] must be a non-empty string")


def _validate_optional_criteria(value: object) -> None:
    if not isinstance(value, list):
        raise ValueError("criteria must be an array")
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
        if not isinstance(item, dict):
            raise ValueError(f"criteria[{index}] must be an object")
        if "id" not in item:
            raise ValueError(f"criteria[{index}] missing required property 'id'")
        if not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError(f"criteria[{index}].id must be a non-empty string")
        extra = sorted(set(item) - allowed)
        if extra:
            raise ValueError(f"criteria[{index}] has unexpected properties")
        for key in (
            "presence_phrases",
            "evidence_phrases",
            "semantic_required_phrases",
            "semantic_forbidden_phrases",
        ):
            if key in item:
                _validate_optional_string_list(item[key], label=f"criteria[{index}].{key}")
        for key in ("fallback_text", "category"):
            if key in item and not isinstance(item[key], str):
                raise ValueError(f"criteria[{index}].{key} must be a string")


def _validate_optional_structure_requirements(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("structure_requirements must be an object")
    required = ("required_kinds", "required_ranges", "required_categories")
    missing = [key for key in required if key not in value]
    if missing:
        raise ValueError("structure_requirements missing required properties")
    extra = sorted(set(value) - set(required))
    if extra:
        raise ValueError("structure_requirements has unexpected properties")
    _validate_optional_string_list(
        value.get("required_kinds"), label="structure_requirements.required_kinds"
    )
    _validate_optional_string_list(
        value.get("required_categories"),
        label="structure_requirements.required_categories",
    )
    ranges = value.get("required_ranges")
    if not isinstance(ranges, list):
        raise ValueError("structure_requirements.required_ranges must be an array")
    for index, item in enumerate(ranges):
        if not isinstance(item, dict):
            raise ValueError(
                f"structure_requirements.required_ranges[{index}] must be an object"
            )
        for key in ("kind", "start", "end"):
            if key not in item:
                raise ValueError(
                    f"structure_requirements.required_ranges[{index}] missing '{key}'"
                )
        if not isinstance(item.get("kind"), str) or not item["kind"]:
            raise ValueError(
                f"structure_requirements.required_ranges[{index}].kind invalid"
            )
        for key in ("start", "end"):
            if not isinstance(item.get(key), int) or isinstance(item.get(key), bool):
                raise ValueError(
                    f"structure_requirements.required_ranges[{index}].{key} invalid"
                )
        allowed = {"kind", "start", "end", "category"}
        extra_item = sorted(set(item) - allowed)
        if extra_item:
            raise ValueError(
                f"structure_requirements.required_ranges[{index}] unexpected properties"
            )
        if "category" in item and not isinstance(item["category"], str):
            raise ValueError(
                f"structure_requirements.required_ranges[{index}].category invalid"
            )


def canonical_acceptance_contract_json_bytes(document: Mapping[str, Any]) -> bytes:
    """LegalAI canonical UTF-8 JSON: sorted keys, compact, excluding content_sha256."""
    without = {k: v for k, v in document.items() if k != "content_sha256"}
    return json.dumps(
        without,
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
        raise ValueError("acceptance contract must be UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("acceptance contract root must be a JSON object")
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


def parse_acceptance_contract_v1(payload: bytes) -> dict[str, Any]:
    """Parse and strictly validate LegalAI acceptance_contract.v1 (safe metadata only)."""
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

    # Reject the pre-LegalAI flat shape (schema / top-level benchmark_id / question_id).
    if "schema" in document or (
        ("benchmark_id" in document or "question_id" in document)
        and "identity" not in document
    ):
        raise ValueError("acceptance contract rejects obsolete flat schema shape")

    schema_version = document.get("schema_version")
    if schema_version != ACCEPTANCE_CONTRACT_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version must be {ACCEPTANCE_CONTRACT_SCHEMA_VERSION}"
        )

    missing = [key for key in _REQUIRED_ACCEPTANCE_TOP_LEVEL if key not in document]
    if missing:
        raise ValueError("acceptance contract missing required top-level properties")

    allowed_top = set(_REQUIRED_ACCEPTANCE_TOP_LEVEL) | {
        "criteria",
        "structure_requirements",
    }
    unexpected = sorted(set(document) - allowed_top)
    if unexpected:
        raise ValueError("acceptance contract has unexpected top-level properties")

    contract_id = _validate_acceptance_identity(
        document.get("contract_id"), label="contract_id"
    )
    version = _validate_acceptance_identity(document.get("version"), label="version")

    identity = document.get("identity")
    if not isinstance(identity, dict):
        raise ValueError("identity must be an object")
    if sorted(identity.keys()) != ["benchmark_id", "question_id"]:
        raise ValueError("identity must contain only benchmark_id and question_id")
    benchmark_id = _validate_acceptance_identity(
        identity.get("benchmark_id"), label="identity.benchmark_id"
    )
    question_id = _validate_acceptance_identity(
        identity.get("question_id"), label="identity.question_id"
    )

    required_criterion_ids = _require_non_empty_string_list(
        document.get("required_criterion_ids"), label="required_criterion_ids"
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
        document.get("content_sha256"), label="content_sha256"
    )
    recomputed = compute_acceptance_contract_sha256(document)
    if embedded_content_sha256 != recomputed:
        raise ValueError("content_sha256 does not match recomputed contract digest")

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
    contract_json_base64: str,
    expected_object_key: str,
    expected_benchmark_id: str,
    expected_question_id: str,
    expected_contract_id: str,
    expected_version: str,
    expected_contract_sha256: str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate inputs and return one create-only archive item (safe metadata).

    ``expected_contract_sha256`` is the LegalAI canonical content digest.
    ``expected_sha256`` remains accepted as an alias for the same digest.
    """
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
    expected_digest_raw = (
        expected_contract_sha256
        if expected_contract_sha256 is not None
        else expected_sha256
    )
    if expected_digest_raw is None:
        raise ValueError("expected_contract_sha256 is required")
    expected_digest = validate_sha256_hex(
        expected_digest_raw, label="expected_contract_sha256"
    )

    payload = decode_acceptance_contract_json_base64(contract_json_base64)
    identity = parse_acceptance_contract_v1(payload)
    contract_sha256 = identity["contract_sha256"]
    object_sha256 = compute_acceptance_object_sha256(payload)

    if contract_sha256 != expected_digest:
        raise ValueError(
            "acceptance contract contract_sha256 does not match expected_contract_sha256"
        )
    if identity["content_sha256"] != expected_digest:
        raise ValueError("embedded content_sha256 does not match expected_contract_sha256")
    if identity["object_key"] != object_key:
        raise ValueError("embedded object_key does not match expected_object_key")
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
