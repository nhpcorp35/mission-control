"""Immutable, case-scoped reviewed-authority records in canonical B2."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

CASE_RE = re.compile(r"(?:Case-00-Triborough|NY-[A-Za-z]+-[0-9]{6}-[0-9]{4}-[A-Za-z0-9-]{2,80})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def authority_key(case_id: str, source_sha256: str) -> str:
    if not CASE_RE.fullmatch(case_id) or not SHA256_RE.fullmatch(source_sha256):
        raise ValueError("invalid reviewed-authority scope")
    return f"cases/{case_id}/derived/reviewed-authorities/{source_sha256}.json"


def _record_hash(record: dict[str, Any]) -> str:
    body = {key: value for key, value in record.items() if key != "sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_reviewed_authority_record(value: dict[str, Any], *, case_id: str, source_sha256: str) -> dict[str, Any]:
    """Validate the complete review contract; reject any mutation or omission."""
    required = {"schema_version", "case_id", "source_sha256", "records", "sha256"}
    if not isinstance(value, dict) or set(value) != required or value.get("schema_version") != "legalai-reviewed-authorities.v1":
        raise ValueError("invalid reviewed-authority record")
    if value.get("case_id") != case_id or value.get("source_sha256") != source_sha256:
        raise ValueError("reviewed-authority scope mismatch")
    records = value.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("reviewed-authority record is incomplete")
    seen = set()
    for item in records:
        fields = {"authority_id", "citation", "official_primary_source", "exact_holding", "filing_proposition", "filing_record_citation"}
        if not isinstance(item, dict) or set(item) != fields or not all(isinstance(item.get(k), str) and item[k].strip() for k in fields):
            raise ValueError("reviewed-authority entry is incomplete")
        if item["authority_id"] in seen:
            raise ValueError("duplicate reviewed authority")
        seen.add(item["authority_id"])
        if not item["official_primary_source"].startswith(("https://www.nycourts.gov/", "https://www.nysenate.gov/")):
            raise ValueError("reviewed authority requires official primary source")
        if not re.search(r"\bp\.\s*[1-9][0-9]*\b", item["filing_record_citation"], re.I):
            raise ValueError("reviewed authority requires filing page citation")
    if not isinstance(value.get("sha256"), str) or not SHA256_RE.fullmatch(value["sha256"]) or value["sha256"] != _record_hash(value):
        raise ValueError("reviewed-authority hash mismatch")
    return value


def create_reviewed_authority_record(*, case_id: str, source_sha256: str, records: list[dict[str, str]]) -> dict[str, Any]:
    value = {"schema_version": "legalai-reviewed-authorities.v1", "case_id": case_id, "source_sha256": source_sha256, "records": records}
    value["sha256"] = _record_hash(value)
    return validate_reviewed_authority_record(value, case_id=case_id, source_sha256=source_sha256)


def put_reviewed_authority_record(client: Any, bucket: str, value: dict[str, Any]) -> dict[str, Any]:
    value = validate_reviewed_authority_record(value, case_id=value.get("case_id", ""), source_sha256=value.get("source_sha256", ""))
    key = authority_key(value["case_id"], value["source_sha256"])
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    try:
        existing = json.loads(client.get_object(Bucket=bucket, Key=key)["Body"].read().decode())
    except Exception:
        existing = None
    if existing is not None:
        validate_reviewed_authority_record(existing, case_id=value["case_id"], source_sha256=value["source_sha256"])
        if existing == value:
            return existing
        old = {item["authority_id"]: item for item in existing["records"]}
        new = {item["authority_id"]: item for item in value["records"]}
        if (not set(old).issubset(new)
                or any(new[key] != old[key] for key in old)):
            raise ValueError("reviewed-authority record may only add new entries")
    client.put_object(Bucket=bucket, Key=key, Body=raw, ContentType="application/json", Metadata={"sha256": value["sha256"]})
    return value


def get_reviewed_authority_record(client: Any, bucket: str, *, case_id: str, source_sha256: str) -> dict[str, Any]:
    key = authority_key(case_id, source_sha256)
    raw = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    try:
        value = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid reviewed-authority record") from exc
    return validate_reviewed_authority_record(value, case_id=case_id, source_sha256=source_sha256)
