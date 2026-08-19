"""Private B2 loader for versioned acceptance contracts.

Fail-closed for missing, malformed, unversioned, identity-mismatched, or
hash-mismatched contracts. Never logs or returns contract body contents —
only safe metadata and path/code diagnostics.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from acceptance_contract.schema import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    contract_version_of,
    schema_version_of,
    validate_acceptance_contract_schema,
)

# Error codes (stable, safe to log / return).
ERROR_MISSING_OBJECT = "missing_object"
ERROR_MALFORMED_JSON = "malformed_json"
ERROR_SCHEMA_INVALID = "schema_invalid"
ERROR_MISSING_VERSION = "missing_version"
ERROR_UNSUPPORTED_VERSION = "unsupported_version"
ERROR_IDENTITY_MISMATCH = "identity_mismatch"
ERROR_OBJECT_KEY_MISMATCH = "object_key_mismatch"
ERROR_HASH_MISMATCH = "hash_mismatch"
ERROR_B2_READ = "b2_read_error"


def _load_rebuild_cli():
    """Load scripts/rebuild_case00_derived.py for shared B2 retry helpers."""
    repo_root = Path(__file__).resolve().parents[1]
    path = repo_root / "scripts" / "rebuild_case00_derived.py"
    mod_name = "rebuild_case00_derived_acceptance_contract"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load B2 helper module")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@dataclass(frozen=True)
class ContractIdentity:
    """Benchmark / question identity expected for a private contract object."""

    benchmark_id: str
    question_id: str

    def __post_init__(self) -> None:
        if not str(self.benchmark_id).strip():
            raise ValueError("benchmark_id must be non-empty")
        if not str(self.question_id).strip():
            raise ValueError("question_id must be non-empty")


@dataclass(frozen=True)
class SafeContractMetadata:
    """Safe-to-log/return fields extracted from a validated contract."""

    contract_id: str
    version: str
    schema_version: str
    benchmark_id: str
    question_id: str
    required_criterion_ids: tuple[str, ...]
    object_key: str
    content_sha256: str
    evidence_constraints: Mapping[str, Any]
    semantic_preservation: Mapping[str, Any]
    duplication_rules: Mapping[str, Any]

    def __repr__(self) -> str:
        return (
            "SafeContractMetadata("
            f"contract_id={self.contract_id!r}, "
            f"version={self.version!r}, "
            f"schema_version={self.schema_version!r}, "
            f"benchmark_id={self.benchmark_id!r}, "
            f"question_id={self.question_id!r}, "
            f"required_criterion_ids={list(self.required_criterion_ids)!r}, "
            f"object_key={self.object_key!r}, "
            f"content_sha256={self.content_sha256!r})"
        )


@dataclass(frozen=True)
class AcceptanceContractLoadResult:
    """Loader outcome: success metadata or fail-closed diagnostics only.

    On success, ``evaluation`` holds an in-memory evaluation view for phase-2
    validation. Repr never embeds contract body or criterion prose.
    """

    ok: bool
    object_key: str
    error_code: Optional[str] = None
    diagnostics: tuple[str, ...] = ()
    metadata: Optional[SafeContractMetadata] = None
    computed_content_sha256: Optional[str] = None
    evaluation: Any = None  # Optional[ContractEvaluationView]; typed loosely to avoid cycle

    def __repr__(self) -> str:
        return (
            "AcceptanceContractLoadResult("
            f"ok={self.ok!r}, "
            f"object_key={self.object_key!r}, "
            f"error_code={self.error_code!r}, "
            f"diagnostics={list(self.diagnostics)!r}, "
            f"metadata={self.metadata!r}, "
            f"computed_content_sha256={self.computed_content_sha256!r}, "
            f"evaluation={self.evaluation!r})"
        )


class AcceptanceContractError(Exception):
    """Fail-closed loader error; message and details never embed contract body."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        object_key: str = "",
        diagnostics: Optional[list[str] | tuple[str, ...]] = None,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.object_key = object_key
        self.diagnostics = tuple(diagnostics or ())
        # Drop any accidental body-like keys callers might pass.
        safe_details = {
            key: value
            for key, value in details.items()
            if key
            not in {
                "body",
                "content",
                "contract",
                "document",
                "payload",
                "raw",
                "text",
            }
        }
        self.details = safe_details

    def __repr__(self) -> str:
        return (
            "AcceptanceContractError("
            f"message={self.message!r}, "
            f"error_code={self.error_code!r}, "
            f"object_key={self.object_key!r}, "
            f"diagnostics={list(self.diagnostics)!r})"
        )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_benchmark_id(benchmark_id: str) -> str:
    """Canonical form for benchmark-ID identity equivalence checks only.

    Trims surrounding whitespace and applies casefold. Does not strip
    punctuation or otherwise collapse distinct identifiers. Callers must
    preserve original supplied/stored IDs in metadata and outputs — this
    helper is never used to rewrite archived contract bodies.
    """
    return str(benchmark_id or "").strip().casefold()


def canonical_json_bytes(document: Mapping[str, Any]) -> bytes:
    """Deterministic UTF-8 JSON bytes (sorted keys, no content_sha256 field)."""
    without = {k: v for k, v in document.items() if k != "content_sha256"}
    return json.dumps(
        without,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_content_sha256(document: Mapping[str, Any]) -> str:
    """SHA-256 of canonical JSON excluding the ``content_sha256`` field."""
    return sha256_hex(canonical_json_bytes(document))


def _fail(
    *,
    object_key: str,
    error_code: str,
    diagnostics: list[str] | tuple[str, ...] = (),
    computed_content_sha256: Optional[str] = None,
) -> AcceptanceContractLoadResult:
    return AcceptanceContractLoadResult(
        ok=False,
        object_key=object_key,
        error_code=error_code,
        diagnostics=tuple(diagnostics),
        metadata=None,
        computed_content_sha256=computed_content_sha256,
        evaluation=None,
    )


def _extract_safe_metadata(document: Mapping[str, Any]) -> SafeContractMetadata:
    identity = document["identity"]
    return SafeContractMetadata(
        contract_id=str(document["contract_id"]),
        version=str(document["version"]),
        schema_version=str(document["schema_version"]),
        benchmark_id=str(identity["benchmark_id"]),
        question_id=str(identity["question_id"]),
        required_criterion_ids=tuple(str(x) for x in document["required_criterion_ids"]),
        object_key=str(document["object_key"]),
        content_sha256=str(document["content_sha256"]),
        evidence_constraints=dict(document["evidence_constraints"]),
        semantic_preservation=dict(document["semantic_preservation"]),
        duplication_rules=dict(document["duplication_rules"]),
    )


def validate_and_authenticate_contract(
    document: Any,
    *,
    object_key: str,
    expected_identity: ContractIdentity,
    expected_content_sha256: Optional[str] = None,
) -> AcceptanceContractLoadResult:
    """Validate a parsed contract object against schema, identity, and hash.

    Does not return or embed the raw document — only safe metadata on success.
    """
    if not isinstance(document, dict):
        return _fail(
            object_key=object_key,
            error_code=ERROR_MALFORMED_JSON,
            diagnostics=["$: expected type object"],
        )

    schema_ver = schema_version_of(document)
    doc_ver = contract_version_of(document)
    if schema_ver is None or doc_ver is None:
        return _fail(
            object_key=object_key,
            error_code=ERROR_MISSING_VERSION,
            diagnostics=["missing schema_version and/or version"],
        )
    if schema_ver not in SUPPORTED_SCHEMA_VERSIONS:
        return _fail(
            object_key=object_key,
            error_code=ERROR_UNSUPPORTED_VERSION,
            diagnostics=[f"unsupported schema_version (supported: {SCHEMA_VERSION})"],
        )

    schema_diags = validate_acceptance_contract_schema(document)
    if schema_diags:
        return _fail(
            object_key=object_key,
            error_code=ERROR_SCHEMA_INVALID,
            diagnostics=schema_diags,
        )

    identity = document.get("identity") or {}
    bench = identity.get("benchmark_id") if isinstance(identity, dict) else None
    qid = identity.get("question_id") if isinstance(identity, dict) else None
    # Benchmark IDs: trim + casefold equivalence only. Question IDs stay strict.
    bench_ok = (
        bench is not None
        and normalize_benchmark_id(str(bench))
        == normalize_benchmark_id(expected_identity.benchmark_id)
    )
    if not bench_ok or qid != expected_identity.question_id:
        return _fail(
            object_key=object_key,
            error_code=ERROR_IDENTITY_MISMATCH,
            diagnostics=["identity.benchmark_id/question_id mismatch"],
        )

    doc_key = document.get("object_key")
    if doc_key != object_key:
        return _fail(
            object_key=object_key,
            error_code=ERROR_OBJECT_KEY_MISMATCH,
            diagnostics=["object_key does not match loaded key"],
        )

    computed = compute_content_sha256(document)
    declared = document.get("content_sha256")
    if declared != computed:
        return _fail(
            object_key=object_key,
            error_code=ERROR_HASH_MISMATCH,
            diagnostics=["declared content_sha256 does not match computed digest"],
            computed_content_sha256=computed,
        )
    if expected_content_sha256 is not None and expected_content_sha256 != computed:
        return _fail(
            object_key=object_key,
            error_code=ERROR_HASH_MISMATCH,
            diagnostics=["computed content_sha256 does not match expected provenance"],
            computed_content_sha256=computed,
        )

    metadata = _extract_safe_metadata(document)
    # Deferred import keeps schema/loader free of validate-module cycles at import.
    from acceptance_contract.validate import build_evaluation_view_from_document

    evaluation = build_evaluation_view_from_document(document, metadata=metadata)
    return AcceptanceContractLoadResult(
        ok=True,
        object_key=object_key,
        error_code=None,
        diagnostics=(),
        metadata=metadata,
        computed_content_sha256=computed,
        evaluation=evaluation,
    )


def parse_contract_json_bytes(raw: bytes) -> Any:
    """Parse UTF-8 JSON bytes; raise AcceptanceContractError on malformed input."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AcceptanceContractError(
            "contract object is not valid UTF-8",
            error_code=ERROR_MALFORMED_JSON,
            diagnostics=["utf-8 decode failed"],
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AcceptanceContractError(
            "contract object is not valid JSON",
            error_code=ERROR_MALFORMED_JSON,
            diagnostics=[f"json decode failed at line {exc.lineno}"],
        ) from exc


def load_acceptance_contract_from_bytes(
    raw: bytes,
    *,
    object_key: str,
    expected_identity: ContractIdentity,
    expected_content_sha256: Optional[str] = None,
) -> AcceptanceContractLoadResult:
    """Parse and authenticate a contract from raw object bytes (no body returned)."""
    try:
        document = parse_contract_json_bytes(raw)
    except AcceptanceContractError as exc:
        return _fail(
            object_key=object_key,
            error_code=exc.error_code,
            diagnostics=list(exc.diagnostics),
        )
    return validate_and_authenticate_contract(
        document,
        object_key=object_key,
        expected_identity=expected_identity,
        expected_content_sha256=expected_content_sha256,
    )


def _is_missing_object_error(exc: BaseException) -> bool:
    try:
        from botocore.exceptions import ClientError
    except ImportError:  # pragma: no cover
        ClientError = ()  # type: ignore[assignment,misc]
    if not isinstance(exc, ClientError):
        return False
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return False
    error = response.get("Error")
    code = ""
    if isinstance(error, Mapping):
        code = str(error.get("Code") or "")
    meta = response.get("ResponseMetadata")
    status = None
    if isinstance(meta, Mapping):
        status = meta.get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404


def fetch_b2_object_bytes(
    client: Any,
    bucket: str,
    key: str,
    *,
    call_with_retry: Optional[Callable[..., Any]] = None,
) -> bytes:
    """Read a B2 object body via existing bounded-retry helper patterns."""
    rebuild = _load_rebuild_cli()
    retry = call_with_retry or rebuild.call_b2_with_read_retry

    def _once() -> bytes:
        response = client.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        try:
            return body.read()
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

    try:
        return retry(_once)
    except Exception as exc:  # noqa: BLE001 — classify missing vs other
        if _is_missing_object_error(exc):
            raise AcceptanceContractError(
                "acceptance contract object not found in B2",
                error_code=ERROR_MISSING_OBJECT,
                object_key=key,
                diagnostics=["B2 object missing"],
            ) from exc
        raise AcceptanceContractError(
            "failed to read acceptance contract object from B2",
            error_code=ERROR_B2_READ,
            object_key=key,
            diagnostics=["B2 read failed"],
        ) from exc


def load_acceptance_contract_from_b2(
    *,
    client: Any,
    bucket: str,
    object_key: str,
    expected_identity: ContractIdentity,
    expected_content_sha256: Optional[str] = None,
    call_with_retry: Optional[Callable[..., Any]] = None,
) -> AcceptanceContractLoadResult:
    """Load a private acceptance contract from canonical B2 and authenticate it.

    On failure returns ``ok=False`` with a stable error_code and safe diagnostics.
    Never returns contract body contents.
    """
    try:
        raw = fetch_b2_object_bytes(
            client,
            bucket,
            object_key,
            call_with_retry=call_with_retry,
        )
    except AcceptanceContractError as exc:
        return _fail(
            object_key=object_key,
            error_code=exc.error_code,
            diagnostics=list(exc.diagnostics),
        )
    return load_acceptance_contract_from_bytes(
        raw,
        object_key=object_key,
        expected_identity=expected_identity,
        expected_content_sha256=expected_content_sha256,
    )


def build_synthetic_contract(
    *,
    contract_id: str,
    version: str,
    benchmark_id: str,
    question_id: str,
    object_key: str,
    required_criterion_ids: list[str],
    schema_version: str = SCHEMA_VERSION,
    criteria: Optional[list[dict[str, Any]]] = None,
    structure_requirements: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build a wholly generic contract dict with a correct content_sha256.

    Intended for tests only — uses synthetic IDs, never private benchmark text.
    When ``criteria`` is omitted, builds minimal evaluation specs from each
    required id (presence phrase = id token) so phase-2 validators can run.
    """
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
    document["content_sha256"] = compute_content_sha256(document)
    return document
