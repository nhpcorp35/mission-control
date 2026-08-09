from __future__ import annotations

import hashlib
import json
import re
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
}

ATTORNEY_REVIEW_FILENAMES = {
    "original_packet": "attorney_review_packet_02-original.md",
    "feedback_email": "John-Cuomo-Case00-Attorney-Feedback-Email-2026-08-02.md",
    "structured_evaluation": (
        "John-Cuomo-Case00-Structured-Evaluation-2026-08-02.json"
    ),
    "manifest": "John-Cuomo-Case00-Feedback-Preservation-Manifest.json",
}

MAX_ARCHIVE_ITEM_BYTES = 2 * 1024 * 1024
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
