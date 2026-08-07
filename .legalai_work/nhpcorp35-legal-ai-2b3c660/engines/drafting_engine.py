# engines/drafting_engine.py
"""
Retrieval-grounded attorney Q&A reasoner.

Consumes canonical retrieval hits (and optional case-map / exhibit context)
and produces a structured, citation-bounded answer for attorney review.

Model calls go through the configured provider abstraction
(``resolve_model_provider`` / injectable ``model_call``). Generation is
opt-in; callers must request it explicitly.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


ENGINE_VERSION = "Attorney QA Reasoner v1 — Retrieval Grounded"

# ---------------------------------------------------------------------------
# Classifications & constants
# ---------------------------------------------------------------------------

PROPOSITION_CLASSIFICATIONS = (
    "verified_record_fact",
    "party_allegation",
    "legal_position",
    "inference",
    "unknown",
)

ALLEATION_SOURCE_KINDS = frozenset(
    {
        "party_allegation",
        "allegation",
    }
)

LEGAL_POSITION_SOURCE_KINDS = frozenset(
    {
        "legal_position",
    }
)

STATUS_READY = "READY"
STATUS_NOT_READY = "NOT READY"

LEGALAI_MODEL_ENDPOINT_ENV = "LEGALAI_MODEL_ENDPOINT"
LEGALAI_MODEL_API_KEY_ENV = "LEGALAI_MODEL_API_KEY"
LEGALAI_MODEL_TIMEOUT_ENV = "LEGALAI_MODEL_TIMEOUT_SECONDS"

OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
LEGALAI_OPENAI_MODEL_ENV = "LEGALAI_OPENAI_MODEL"
LEGALAI_OPENAI_ENDPOINT_ENV = "LEGALAI_OPENAI_ENDPOINT"
DEFAULT_OPENAI_MODEL = "gpt-5.6-sol"
DEFAULT_OPENAI_ENDPOINT = "https://api.openai.com/v1/responses"
ATTORNEY_QA_SCHEMA_NAME = "attorney_qa_answer"

_POLICY_RE = re.compile(r"\b(?:POL(?:ICY)?[-\s]?)?\d{3,}[A-Z0-9-]*\b", re.I)
_DATE_RE = re.compile(
    r"\b(?:\d{1,2}/\d{1,2}/\d{2,4}|"
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},?\s+\d{4})\b",
    re.I,
)
_PARTY_LIKE_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9.&'-]+(?:\s+[A-Z][A-Za-z0-9.&'-]+){0,4}"
    r"\s+(?:LLC|Inc\.?|Corp\.?|Co\.?|Company|Ltd\.?))\b"
)


RECORD_ANALYSIS_SYSTEM_PROMPT = """You are a record-analysis assistant for New York civil litigation attorney review.

You answer ONLY from the supplied retrieval evidence packet. This workflow is record-only by default.

Hard rules:
1. Use only the provided retrieval hits, page excerpts, and optional case-map/exhibit context.
2. Do not add external statutes, case law, or doctrines unless they appear in the allowed_sources field.
3. Case-map nodes and review candidates are retrieval signals only — never independent proof.
4. Every substantive proposition must include: classification, nyscef_document_number, page_id, pdf_page, source_excerpt, confidence, rationale.
5. classification must be one of: verified_record_fact, party_allegation, legal_position, inference, unknown.
6. verified_record_fact is limited to procedural/documentary facts directly established by the cited page. Never promote party assertions or legal arguments to facts.
7. Preserve competing positions separately. Do not silently reconcile conflicts.
8. Claims of absence, completeness, chronology, conflict, or strongest evidence require an explained review_scope and must stay qualified when the retrieved record cannot establish completeness.
9. Cite only page_id / NYSCEF / pdf_page values present in the evidence packet. Quote minimally and preserve exact wording in source_excerpt.
10. Express uncertainty explicitly. Label any legal conclusion as legal_position or inference for attorney review. Do not give a final coverage conclusion unless the record and question clearly justify it.
11. Answer the specific attorney question directly. Include only facts that are materially useful to that question.
12. Prefer concise practical attorney work product. Exclude irrelevant procedural narrative, motion calendars, RJI boilerplate, and chronology that does not affect the answer.
13. Preserve necessary qualifications, conflicts, amendments, and uncertainty about identity or role when the supplied record shows them.
14. Remain fully citation-grounded: every substantive claim must rest on retrieval evidence in the packet. Do not use provisional answers, gold answers, or outside knowledge.

Return a single JSON object with keys:
proposed_answer, propositions, supporting_evidence, contrary_evidence,
unresolved_questions, documents_pages_reviewed, confidence, attorney_review, review_scope.

propositions items: proposition_id, text, classification, nyscef_document_number,
page_id, pdf_page, source_excerpt, confidence, rationale, polarity
(polarity: supporting | contrary | unresolved).

attorney_review must include: requires_attorney_review (true), review_notes,
legal_conclusions_labeled, coverage_conclusion (null unless justified and qualified).
"""


# ---------------------------------------------------------------------------
# Party-and-role question intent & materiality filtering
# ---------------------------------------------------------------------------

_PARTY_ROLE_QUERY_PHRASES = (
    "party role",
    "party roles",
    "roles of the parties",
    "parties and their roles",
    "parties and roles",
    "who are the parties",
    "who is the plaintiff",
    "who is the defendant",
    "identify the parties",
    "identify parties",
    "named parties",
    "parties to the action",
    "parties to this action",
    "procedural roles",
    "each party's role",
    "plaintiff and defendant",
    "petitioner and respondent",
    "third-party plaintiff",
    "third-party defendant",
    "respondent on appeal",
)

_MOTION_PRIMARY_QUERY_PHRASES = (
    "notice of motion",
    "summary judgment",
    "motion to dismiss",
    "motion for",
    "returnable",
)

_PARTY_ROLE_BEARING_RE = re.compile(
    r"(?i)\b(?:"
    r"parties\b|"
    r"third[\s-]+party\s+(?:plaintiffs?|defendants?)|"
    r"plaintiffs?\b|"
    r"defendants?\b|"
    r"petitioners?\b|"
    r"respondents?(?:\s+on\s+(?:the\s+)?appeal)?\b|"
    r"appellants?\b|"
    r"appellees?\b|"
    r"limited\s+liability\s+(?:company|corporation)|"
    r"sued\s+herein|"
    r"joined\s+(?:herein|as\s+a\s+party)|"
    r"necessary\s+party|"
    r"real\s+party\s+in\s+interest|"
    r"notice\s+defendants?|"
    r"named\s+insured|"
    r"additional\s+insured|"
    r"principal\s+place\s+of\s+business|"
    r"place\s+of\s+business|"
    r"residen(?:t|ce|ts)\b|"
    r"resid(?:es|ed|ing)\b|"
    r"\bindividuals?\b|"
    r"(?:domestic|foreign)\s+corporation"
    r")"
)

_PARTY_IDENTITY_ESTABLISHING_RE = re.compile(
    r"(?i)\b(?:"
    r"(?:plaintiffs?|defendants?|petitioners?|respondents?|appellants?|appellees?)"
    r"\s+(?:is|are|was|were)\b|"
    r"(?:is|are|was|were)\s+(?:a\s+|the\s+)?"
    r"(?:plaintiffs?|defendants?|petitioners?|respondents?|appellants?|appellees?)\b|"
    r"third[\s-]+party\s+(?:plaintiffs?|defendants?)\b|"
    r"joined\s+(?:herein|as\s+a\s+party|as\s+(?:an?\s+)?(?:additional\s+)?party)\b|"
    r"necessary\s+party\b|"
    r"real\s+party\s+in\s+interest\b|"
    r"sued\s+(?:herein|as)\b|"
    r"(?:domestic|foreign)\s+corporation\b|"
    r"limited\s+liability\s+(?:company|corporation|partnership)\b|"
    r"(?:authorized|organized)\s+to\s+do\s+business\b|"
    r"notice\s+defendants?\b|"
    r"named\s+insured\b|"
    r"additional\s+insured\b|"
    r"principal\s+place\s+of\s+business\b|"
    r"place\s+of\s+business\b|"
    r"(?:is|are|was|were)\s+(?:an?\s+)?(?:individual|resident)s?\b|"
    r"resid(?:es|ed|ing)\s+in\b|"
    r"resident\s+of\b|"
    r"was\s+and\s+still\s+is\s+a\b|"
    r"duly\s+authorized\s+and\s+existing\b"
    r")"
)

_PARTY_ROLE_QUALIFICATION_OR_CHANGE_RE = re.compile(
    r"(?i)\b(?:"
    r"amended\s+(?:complaint|petition|answer|pleading|caption|summons)\b|"
    r"incorrectly\s+named\b|"
    r"sued\s+(?:herein\s+)?as\b|"
    r"also\s+known\s+as\b|"
    r"now\s+known\s+as\b|"
    r"formerly\s+known\s+as\b|"
    r"substituted\s+(?:as\s+)?(?:party|plaintiff|defendant)\b|"
    r"successor\s+(?:in\s+interest|party)?\b|"
    r"capacity\b|"
    r"dismissed\s+as\s+(?:a\s+)?(?:party|defendant|plaintiff)\b|"
    r"discontinued\s+as\s+to\b|"
    r"leave\s+to\s+(?:amend|add|drop|serve)\b|"
    r"joined\s+(?:herein|as)\b|"
    r"misnomer\b|"
    r"without\s+prejudice\s+to\b|"
    r"appears?\s+specially\b|"
    r"(?:role|caption|party\s+status)\s+(?:is\s+)?(?:disputed|uncertain|unclear|unresolved)\b|"
    r"(?:disputed|uncertain|unclear|unresolved)\s+(?:role|caption|party\s+status)\b|"
    r"conflict(?:s|ing)?\s+(?:as\s+to\s+)?(?:party|role|caption)\b|"
    r"nominally\b|"
    r"purportedly\b|"
    r"allegedly\s+(?:a\s+)?(?:party|plaintiff|defendant)\b"
    r")"
)

# Affirmative change / qualification / conflict language required before a
# procedural record (motion/RJI/affirmation/service/order/history) may survive
# party-role materiality. Caption labels and incidental role words are not enough.
_PARTY_ROLE_MATERIAL_CHANGE_RE = re.compile(
    r"(?i)\b(?:"
    r"amended\s+(?:complaint|petition|answer|pleading|caption|summons)\b|"
    r"incorrectly\s+named\b|"
    r"sued\s+(?:herein\s+)?as\b|"
    r"also\s+known\s+as\b|"
    r"now\s+known\s+as\b|"
    r"formerly\s+known\s+as\b|"
    r"substituted\s+(?:as\s+)?(?:party|plaintiff|defendant)\b|"
    r"successor\s+(?:in\s+interest|party)\b|"
    r"dismissed\s+as\s+(?:a\s+)?(?:party|defendant|plaintiff)\b|"
    r"discontinued\s+as\s+to\b|"
    r"leave\s+to\s+(?:amend|add|drop|serve)\b|"
    r"(?:add(?:ed|ing)?|join(?:ed|ing)?)\s+(?:as\s+)?(?:a\s+)?"
    r"(?:necessary\s+)?(?:party|defendant|plaintiff)\b|"
    r"misnomer\b|"
    r"without\s+prejudice\s+to\b|"
    r"appears?\s+specially\b|"
    r"(?:role|caption|party\s+status)\s+(?:is\s+)?(?:disputed|uncertain|unclear|unresolved)\b|"
    r"(?:disputed|uncertain|unclear|unresolved)\s+(?:role|caption|party\s+status)\b|"
    r"conflict(?:s|ing)?\s+(?:as\s+to\s+)?(?:party|role|caption)\b|"
    r"(?:appear(?:s|ing)?\s+in\s+(?:a\s+)?representative\s+capacity|"
    r"capacity\s+(?:as|of)\s+(?:a\s+)?(?:party|plaintiff|defendant|fiduciary)|"
    r"in\s+(?:his|her|its|their)\s+capacity\s+as|"
    r"if\s+capacity\s+is\s+later\s+established)\b|"
    r"nominally\b|"
    r"purportedly\b|"
    r"allegedly\s+(?:a\s+)?(?:party|plaintiff|defendant)\b"
    r")"
)

_PROCEDURAL_NOISE_RE = re.compile(
    r"(?i)\b(?:"
    r"notice\s+of\s+motion\b|"
    r"request\s+for\s+judicial\s+intervention\b|"
    r"\brji\b|"
    r"returnable\b|"
    r"procedural\s+calendar\b|"
    r"procedural\s+history\b|"
    r"conference\s+(?:date|scheduled)\b|"
    r"scheduling\s+order\b|"
    r"affirmation\s+of\s+(?:service|mailing|good\s+faith)\b|"
    r"affidavit\s+of\s+(?:service|mailing)\b|"
    r"proof\s+of\s+service\b|"
    r"admission\s+of\s+service\b|"
    r"certificate\s+of\s+service\b"
    r")"
)

_SERVICE_FILING_RE = re.compile(
    r"(?i)\b(?:"
    r"affirmation\s+of\s+(?:service|mailing)|"
    r"affidavit\s+of\s+(?:service|mailing)|"
    r"proof\s+of\s+service|"
    r"admission\s+of\s+service|"
    r"certificate\s+of\s+service|"
    r"affixing\s+to\b"
    r")\b"
)

# Hard-excluded filing kinds for party-role questions unless material-change
# language is affirmatively present.
_PROCEDURAL_HARD_EXCLUDE_KINDS = frozenset(
    {
        "motion",
        "rji",
        "affirmation",
        "service",
        "order",
        "procedural_history",
    }
)

# Total party-role evidence-packet budget (across the whole packet, not per hit).
# Selection is deterministic: protected controlling-pleading pages are kept first,
# then material change/qualification hits, then remaining material hits by score.
# Excerpts are never truncated to meet the budget.
PARTY_ROLE_PACKET_MAX_HITS = 12
PARTY_ROLE_PACKET_MAX_CHARS = 24000

# Final party-role drafting instruction. Appended after evidence serialization so
# it is not weakened by earlier concision / materiality / formatting guidance.
PARTY_ROLE_DRAFTING_COMPLETENESS_INSTRUCTION = (
    "PARTY-ROLE DRAFTING REQUIREMENT (mandatory; not optional):\n"
    "For every evidence-supported party present in the packet, the answer must "
    "report each of the following attributes when the supplied evidence supports "
    "them:\n"
    "1. identity\n"
    "2. procedural role\n"
    "3. entity type / entity form\n"
    "4. residence or principal place of business\n"
    "5. pleaded role basis, including notice-defendant basis where applicable\n"
    "Prefer concise practical attorney work product, but required party "
    "attributes are not optional and cannot be omitted for brevity, concision, "
    "or materiality. Do not invent attributes absent from the evidence."
)


ModelCall = Callable[[str, str], Any]


# ---------------------------------------------------------------------------
# Provider abstraction (generic + optional OpenAI Responses path)
# ---------------------------------------------------------------------------


class OpenAIResponsesProviderError(RuntimeError):
    """Structured rejection of OpenAI Responses API output or transport failure."""


def _env_timeout_seconds(default: float = 60.0) -> float:
    raw = os.environ.get(LEGALAI_MODEL_TIMEOUT_ENV)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _openai_model_name() -> str:
    return (os.environ.get(LEGALAI_OPENAI_MODEL_ENV) or "").strip() or DEFAULT_OPENAI_MODEL


def _openai_endpoint() -> str:
    return (
        (os.environ.get(LEGALAI_OPENAI_ENDPOINT_ENV) or "").strip()
        or DEFAULT_OPENAI_ENDPOINT
    )


def attorney_qa_response_json_schema() -> dict:
    """Strict JSON Schema matching the attorney Q&A answer contract."""
    proposition = {
        "type": "object",
        "properties": {
            "proposition_id": {"type": "string"},
            "text": {"type": "string"},
            "classification": {
                "type": "string",
                "enum": list(PROPOSITION_CLASSIFICATIONS),
            },
            "nyscef_document_number": {"type": ["integer", "null"]},
            "page_id": {"type": ["string", "null"]},
            "pdf_page": {"type": ["integer", "null"]},
            "source_excerpt": {"type": "string"},
            "confidence": {"type": "number"},
            "rationale": {"type": "string"},
            "polarity": {
                "type": "string",
                "enum": ["supporting", "contrary", "unresolved"],
            },
        },
        "required": [
            "proposition_id",
            "text",
            "classification",
            "nyscef_document_number",
            "page_id",
            "pdf_page",
            "source_excerpt",
            "confidence",
            "rationale",
            "polarity",
        ],
        "additionalProperties": False,
    }
    evidence_item = {
        "type": "object",
        "properties": {
            "page_id": {"type": ["string", "null"]},
            "nyscef_document_number": {"type": ["integer", "null"]},
            "pdf_page": {"type": ["integer", "null"]},
            "excerpt": {"type": "string"},
            "note": {"type": "string"},
        },
        "required": [
            "page_id",
            "nyscef_document_number",
            "pdf_page",
            "excerpt",
            "note",
        ],
        "additionalProperties": False,
    }
    reviewed_page = {
        "type": "object",
        "properties": {
            "nyscef_document_number": {"type": ["integer", "null"]},
            "page_id": {"type": ["string", "null"]},
            "pdf_page": {"type": ["integer", "null"]},
            "source_filename": {"type": "string"},
            "document_type": {"type": "string"},
        },
        "required": [
            "nyscef_document_number",
            "page_id",
            "pdf_page",
            "source_filename",
            "document_type",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "proposed_answer": {"type": "string"},
            "propositions": {"type": "array", "items": proposition},
            "supporting_evidence": {"type": "array", "items": evidence_item},
            "contrary_evidence": {"type": "array", "items": evidence_item},
            "unresolved_questions": {
                "type": "array",
                "items": {"type": "string"},
            },
            "documents_pages_reviewed": {
                "type": "array",
                "items": reviewed_page,
            },
            "confidence": {"type": "number"},
            "attorney_review": {
                "type": "object",
                "properties": {
                    "requires_attorney_review": {"type": "boolean"},
                    "review_notes": {"type": "string"},
                    "legal_conclusions_labeled": {"type": "boolean"},
                    "coverage_conclusion": {"type": ["string", "null"]},
                },
                "required": [
                    "requires_attorney_review",
                    "review_notes",
                    "legal_conclusions_labeled",
                    "coverage_conclusion",
                ],
                "additionalProperties": False,
            },
            "review_scope": {
                "type": "object",
                "properties": {
                    "completeness": {"type": "string"},
                    "qualification": {"type": "string"},
                    "explanation": {"type": "string"},
                },
                "required": ["completeness", "qualification", "explanation"],
                "additionalProperties": False,
            },
        },
        "required": [
            "proposed_answer",
            "propositions",
            "supporting_evidence",
            "contrary_evidence",
            "unresolved_questions",
            "documents_pages_reviewed",
            "confidence",
            "attorney_review",
            "review_scope",
        ],
        "additionalProperties": False,
    }


def _http_model_call(endpoint: str, system_prompt: str, user_prompt: str) -> Any:
    """POST JSON to a configured model endpoint using stdlib only."""
    payload = {
        "system": system_prompt,
        "user": user_prompt,
        "response_format": "json",
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    api_key = os.environ.get(LEGALAI_MODEL_API_KEY_ENV)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(
        endpoint,
        data=data,
        headers=headers,
        method="POST",
    )
    timeout = _env_timeout_seconds()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body)


def build_openai_responses_request(
    system_prompt: str,
    user_prompt: str,
    *,
    model: Optional[str] = None,
) -> dict:
    """Build a Responses API request with strict structured JSON output."""
    return {
        "model": model or _openai_model_name(),
        "instructions": system_prompt,
        "input": user_prompt,
        "text": {
            "format": {
                "type": "json_schema",
                "name": ATTORNEY_QA_SCHEMA_NAME,
                "strict": True,
                "schema": attorney_qa_response_json_schema(),
            }
        },
    }


def _schema_type_ok(value: Any, type_spec: Any) -> bool:
    if isinstance(type_spec, list):
        return any(_schema_type_ok(value, option) for option in type_spec)
    if type_spec == "object":
        return isinstance(value, dict)
    if type_spec == "array":
        return isinstance(value, list)
    if type_spec == "string":
        return isinstance(value, str)
    if type_spec == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_spec == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_spec == "boolean":
        return isinstance(value, bool)
    if type_spec == "null":
        return value is None
    return False


def _validate_against_json_schema(value: Any, schema: dict, path: str = "$") -> None:
    """Minimal strict-schema checker (no external jsonschema dependency)."""
    if "enum" in schema and value not in schema["enum"]:
        raise OpenAIResponsesProviderError(
            f"Schema-invalid response at {path}: value not in enum"
        )
    type_spec = schema.get("type")
    if type_spec is not None and not _schema_type_ok(value, type_spec):
        raise OpenAIResponsesProviderError(
            f"Schema-invalid response at {path}: expected {type_spec}"
        )
    if schema.get("type") == "object" or (
        isinstance(schema.get("type"), list) and "object" in schema["type"]
    ):
        if not isinstance(value, dict):
            return
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                raise OpenAIResponsesProviderError(
                    f"Schema-invalid response at {path}: missing '{key}'"
                )
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                raise OpenAIResponsesProviderError(
                    f"Schema-invalid response at {path}: unexpected keys {sorted(extra)}"
                )
        for key, child in value.items():
            child_schema = props.get(key)
            if child_schema is not None:
                _validate_against_json_schema(child, child_schema, f"{path}.{key}")
    if schema.get("type") == "array" or (
        isinstance(schema.get("type"), list) and "array" in schema["type"]
    ):
        if not isinstance(value, list):
            return
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_against_json_schema(item, item_schema, f"{path}[{index}]")


def _extract_openai_output_texts(response_body: dict) -> Tuple[List[str], List[str]]:
    """Walk Responses API output/content; return (texts, refusals)."""
    texts: List[str] = []
    refusals: List[str] = []
    output = response_body.get("output")
    if not isinstance(output, list):
        return texts, refusals
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "refusal" and item.get("refusal"):
            refusals.append(str(item.get("refusal")))
            continue
        # Only assistant message items carry structured answer content.
        if item_type not in (None, "message"):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type == "refusal":
                refusals.append(str(part.get("refusal") or "refused"))
            elif part_type in ("output_text", "text"):
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text)
    return texts, refusals


def parse_openai_responses_payload(response_body: Any) -> dict:
    """
    Parse structured attorney-Q&A JSON from a Responses API body.

    Rejects refusal, incomplete, malformed, missing, or schema-invalid output.
    Does not fall back to unvalidated free-form text.
    """
    if not isinstance(response_body, dict):
        raise OpenAIResponsesProviderError(
            "Malformed Responses API body: expected JSON object"
        )

    status = response_body.get("status")
    if status == "incomplete":
        details = response_body.get("incomplete_details") or {}
        reason = details.get("reason") if isinstance(details, dict) else details
        raise OpenAIResponsesProviderError(
            f"Incomplete Responses API output"
            + (f": {reason}" if reason else "")
        )
    if status not in (None, "completed"):
        raise OpenAIResponsesProviderError(
            f"Responses API status not usable: {status!r}"
        )

    error = response_body.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else error
        raise OpenAIResponsesProviderError(
            f"Responses API error: {message or 'unknown'}"
        )

    texts, refusals = _extract_openai_output_texts(response_body)
    if refusals:
        # Never treat refusal prose as the answer contract.
        raise OpenAIResponsesProviderError(
            f"Model refused to produce structured output: {refusals[0]}"
        )
    if not texts:
        raise OpenAIResponsesProviderError(
            "Missing structured output text in Responses API content"
        )

    parsed_obj: Optional[dict] = None
    last_parse_error = "no JSON object found"
    for text in texts:
        try:
            candidate = json.loads(text)
        except json.JSONDecodeError as exc:
            last_parse_error = str(exc)
            continue
        if isinstance(candidate, dict):
            parsed_obj = candidate
            break
        last_parse_error = "JSON root was not an object"

    if parsed_obj is None:
        raise OpenAIResponsesProviderError(
            f"Malformed JSON in Responses API output: {last_parse_error}"
        )

    _validate_against_json_schema(parsed_obj, attorney_qa_response_json_schema())
    return parsed_obj


def _openai_responses_model_call(system_prompt: str, user_prompt: str) -> dict:
    """Live OpenAI Responses API call (stdlib urllib; no SDK)."""
    api_key = (os.environ.get(OPENAI_API_KEY_ENV) or "").strip()
    if not api_key:
        raise OpenAIResponsesProviderError(
            f"{OPENAI_API_KEY_ENV} is required for OpenAI Responses provider"
        )

    endpoint = _openai_endpoint()
    payload = build_openai_responses_request(system_prompt, user_prompt)
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers=headers,
        method="POST",
    )
    timeout = _env_timeout_seconds()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # Do not include request headers (may contain credentials).
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:  # noqa: BLE001
            detail = ""
        raise OpenAIResponsesProviderError(
            f"OpenAI Responses HTTP {exc.code}"
            + (f": {detail}" if detail else "")
        ) from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise OpenAIResponsesProviderError(
            f"OpenAI Responses transport error: {type(reason).__name__}: {reason}"
        ) from None
    except TimeoutError as exc:
        raise OpenAIResponsesProviderError(
            f"OpenAI Responses timeout after {_env_timeout_seconds()}s"
        ) from exc

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise OpenAIResponsesProviderError(
            f"Malformed Responses API HTTP body: {exc}"
        ) from None

    return parse_openai_responses_payload(body)


def resolve_model_provider(
    model_call: Optional[ModelCall] = None,
) -> Optional[ModelCall]:
    """
    Resolve the configured model provider.

    Priority:
      1. Explicit injectable ``model_call`` (tests / host integration)
      2. ``LEGALAI_MODEL_ENDPOINT`` HTTP JSON endpoint (stdlib urllib)
      3. ``OPENAI_API_KEY`` → OpenAI Responses API (unless generic endpoint set)
      4. Unavailable (None) → caller must return structured NOT READY
    """
    if callable(model_call):
        return model_call

    endpoint = (os.environ.get(LEGALAI_MODEL_ENDPOINT_ENV) or "").strip()
    if endpoint:
        def _configured(system_prompt: str, user_prompt: str) -> Any:
            return _http_model_call(endpoint, system_prompt, user_prompt)

        return _configured

    openai_key = (os.environ.get(OPENAI_API_KEY_ENV) or "").strip()
    if openai_key:
        def _openai(system_prompt: str, user_prompt: str) -> Any:
            return _openai_responses_model_call(system_prompt, user_prompt)

        return _openai

    return None


def model_provider_available(model_call: Optional[ModelCall] = None) -> bool:
    return resolve_model_provider(model_call) is not None


# ---------------------------------------------------------------------------
# Text / evidence helpers
# ---------------------------------------------------------------------------


def normalize_whitespace(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


# Vocabulary used only to heal OCR-fractured words during citation matching.
_OCR_CITATION_JOIN_WORDS = frozenset(
    {
        "additional",
        "association",
        "authorized",
        "business",
        "companies",
        "company",
        "corporation",
        "corporations",
        "declaration",
        "defendant",
        "defendants",
        "domestic",
        "existing",
        "foreign",
        "individual",
        "individuals",
        "insured",
        "liability",
        "limited",
        "maintained",
        "named",
        "notice",
        "organized",
        "partnership",
        "partnerships",
        "plaintiff",
        "plaintiffs",
        "principal",
        "resident",
        "residents",
        "residing",
        "residence",
        "underwriters",
    }
)

_ELLIPSIS_SPLIT_RE = re.compile(r"(?:\.{3}|…|\[\s*\.\.\.\s*\])")


# Legal-entity suffixes commonly fractured by OCR inside party identities.
_OCR_PARTY_IDENTITY_JOIN_WORDS = frozenset(
    set(_OCR_CITATION_JOIN_WORDS)
    | {
        "co",
        "corp",
        "inc",
        "incorporated",
        "llc",
        "llp",
        "lp",
        "ltd",
        "pc",
        "plc",
        "pllc",
    }
)

# Short left tokens that are legitimate standalone name particles, not OCR
# prefix fragments (keeps ``of London`` / ``de Vito`` word boundaries).
_OCR_IDENTITY_PREFIX_PARTICLES = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "by",
        "da",
        "de",
        "for",
        "in",
        "of",
        "on",
        "the",
        "to",
        "van",
        "von",
    }
)

# Legal-entity suffixes that must never absorb a short OCR prefix token
# (keeps ``II LLC`` / ``II Corporation``; ordinary-word healing unchanged).
_OCR_IDENTITY_LEGAL_ENTITY_SUFFIXES = frozenset(
    {
        "co",
        "company",
        "companies",
        "corp",
        "corporation",
        "corporations",
        "inc",
        "incorporated",
        "llc",
        "llp",
        "lp",
        "ltd",
        "pc",
        "plc",
        "pllc",
    }
)


def heal_ocr_intra_word_spaces(
    text: Any,
    join_words: Optional[frozenset] = None,
) -> str:
    """Join OCR-fractured vocabulary words for matching / identity healing."""
    raw = str(text or "")
    if not raw:
        return ""
    vocab = join_words if join_words is not None else _OCR_CITATION_JOIN_WORDS

    def _pass(value: str) -> str:
        tokens = re.findall(r"\S+|\s+", value)
        if len(tokens) <= 1:
            return value
        out: List[str] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.isspace() or i + 2 >= len(tokens):
                out.append(tok)
                i += 1
                continue
            nxt = tokens[i + 2] if tokens[i + 1].isspace() else None
            if nxt is None:
                out.append(tok)
                i += 1
                continue
            left_m = re.match(r"^([^A-Za-z]*)([A-Za-z]+)([^A-Za-z]*)$", tok)
            right_m = re.match(r"^([^A-Za-z]*)([A-Za-z]+)([^A-Za-z]*)$", nxt)
            if not left_m or not right_m or right_m.group(1) or left_m.group(3):
                out.append(tok)
                i += 1
                continue
            joined_alpha = f"{left_m.group(2)}{right_m.group(2)}".lower()
            if joined_alpha in vocab:
                out.append(
                    f"{left_m.group(1)}{left_m.group(2)}"
                    f"{right_m.group(2)}{right_m.group(3)}"
                )
                i += 3
                continue
            out.append(tok)
            i += 1
        return "".join(out)

    prev = None
    current = raw
    for _ in range(6):
        if current == prev:
            break
        prev = current
        current = _pass(current)
    return current


def _heal_party_identity_prefix_fractures(text: str) -> str:
    """
    Join short alphabetic OCR prefixes onto the remainder of a fractured word.

    Recognizes splits such as ``CO LLINS`` → ``COLLINS`` without joining
    legitimate multi-word boundaries (``John Smith``, ``of London``).
    """

    def _pass(value: str) -> str:
        tokens = re.findall(r"\S+|\s+", value)
        if len(tokens) <= 1:
            return value
        out: List[str] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.isspace() or i + 2 >= len(tokens):
                out.append(tok)
                i += 1
                continue
            nxt = tokens[i + 2] if tokens[i + 1].isspace() else None
            if nxt is None:
                out.append(tok)
                i += 1
                continue
            left_m = re.match(r"^([^A-Za-z]*)([A-Za-z]+)([^A-Za-z]*)$", tok)
            right_m = re.match(r"^([^A-Za-z]*)([A-Za-z]+)([^A-Za-z]*)$", nxt)
            if not left_m or not right_m or right_m.group(1) or left_m.group(3):
                out.append(tok)
                i += 1
                continue
            left_alpha = left_m.group(2)
            right_alpha = right_m.group(2)
            left_l = left_alpha.lower()
            right_l = right_alpha.lower()
            if (
                left_l in _OCR_IDENTITY_PREFIX_PARTICLES
                or right_l in _OCR_IDENTITY_LEGAL_ENTITY_SUFFIXES
                or len(left_alpha) != 2
                or len(right_alpha) < 3
                or not left_alpha.isalpha()
                or not right_alpha.isalpha()
            ):
                out.append(tok)
                i += 1
                continue
            out.append(
                f"{left_m.group(1)}{left_alpha}{right_alpha}{right_m.group(3)}"
            )
            i += 3
        return "".join(out)

    prev = None
    current = text
    for _ in range(6):
        if current == prev:
            break
        prev = current
        current = _pass(current)
    return current


def heal_party_identity_ocr_spaces(text: Any) -> str:
    """
    Apply OCR intra-word healing to party identity text.

    Uses legal-suffix vocabulary healing plus short-prefix fracture repair.
    Clean identities and legitimate multi-word names pass through unchanged.
    """
    raw = str(text or "")
    if not raw:
        return ""
    healed = heal_ocr_intra_word_spaces(raw, join_words=_OCR_PARTY_IDENTITY_JOIN_WORDS)
    return _heal_party_identity_prefix_fractures(healed)


def normalize_citation_text(value: Any) -> str:
    """Whitespace-normalize and OCR-heal text for citation comparisons."""
    return heal_ocr_intra_word_spaces(normalize_whitespace(value)).lower()


def _substantive_citation_segment(segment: str) -> bool:
    tokens = re.findall(r"[A-Za-z0-9]+", segment or "")
    if not tokens:
        return False
    if len(tokens) >= 2:
        return True
    return len(tokens[0]) >= 4


def _citation_segment_occurs(segment: str, hay_raw: str, hay_ocr: str) -> bool:
    needle = normalize_whitespace(segment)
    if not needle:
        return False
    if needle in hay_raw:
        return True
    needle_ocr = normalize_citation_text(needle)
    return bool(needle_ocr and needle_ocr in hay_ocr)


def excerpt_occurs_on_page(excerpt: Any, page_text: Any) -> bool:
    """
    True when a cited excerpt is supported by page text.

    Accepts whitespace-normalized contiguous matches, OCR-healed word matches,
    and ellipsis-separated quotations when every substantive segment is
    independently supported. Unsupported segments fail the whole quotation.
    """
    needle = normalize_whitespace(excerpt)
    hay = normalize_whitespace(page_text)
    if not needle or not hay:
        return False
    if needle in hay:
        return True

    hay_ocr = normalize_citation_text(hay)
    needle_ocr = normalize_citation_text(needle)
    if needle_ocr and needle_ocr in hay_ocr:
        return True

    if _ELLIPSIS_SPLIT_RE.search(needle):
        segments = [
            part.strip()
            for part in _ELLIPSIS_SPLIT_RE.split(needle)
            if _substantive_citation_segment(part)
        ]
        if not segments:
            return False
        return all(_citation_segment_occurs(seg, hay, hay_ocr) for seg in segments)

    return False


def _page_lookup_from_documents(documents: Optional[Sequence[dict]]) -> Dict[str, dict]:
    lookup: Dict[str, dict] = {}
    for document in documents or []:
        if not isinstance(document, dict):
            continue
        nyscef = document.get("nyscef_document_number")
        filename = document.get("filename") or document.get("name") or ""
        doc_type = document.get("type") or document.get("document_type") or "other"
        for page in document.get("pages") or []:
            if not isinstance(page, dict):
                continue
            page_id = page.get("page_id")
            if not page_id:
                continue
            lookup[page_id] = {
                "page_id": page_id,
                "page_number": page.get("page_number"),
                "text": page.get("text") or "",
                "nyscef_document_number": nyscef,
                "filename": filename,
                "document_type": doc_type,
            }
    return lookup


def _evidence_index(retrieval_results: Sequence[dict]) -> Dict[str, dict]:
    """Index retrieval hits by page_id (first/highest-ranked wins)."""
    index: Dict[str, dict] = {}
    for hit in retrieval_results or []:
        if not isinstance(hit, dict):
            continue
        page_id = hit.get("page_id")
        if not page_id or page_id in index:
            continue
        index[page_id] = hit
    return index


def _hit_matches_citation(hit: dict, page_id, nyscef, pdf_page) -> bool:
    if not hit:
        return False
    if page_id and hit.get("page_id") != page_id:
        return False
    if nyscef is not None and hit.get("nyscef_document_number") != nyscef:
        return False
    if pdf_page is not None and hit.get("pdf_page") != pdf_page:
        return False
    return True


def _coerce_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_confidence(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number < 0:
        return 0.0
    if number > 1:
        # Allow 0-100 style scores.
        if number <= 100:
            return round(number / 100.0, 6)
        return 1.0
    return round(number, 6)


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _parse_model_payload(raw: Any) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        # Some providers wrap content.
        if "propositions" in raw or "proposed_answer" in raw:
            return raw
        for key in ("content", "answer", "result", "data", "json"):
            nested = raw.get(key)
            if isinstance(nested, dict):
                return nested
            if isinstance(nested, str):
                try:
                    parsed = json.loads(nested)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Try fenced JSON.
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                return {}
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# ---------------------------------------------------------------------------
# Party-role intent detection & question-conditioned materiality
# ---------------------------------------------------------------------------


def _question_tokens(question: str) -> set:
    return set(re.findall(r"[a-z0-9']+", normalize_whitespace(question).lower()))


def _query_seeks_motion_primary(question: str) -> bool:
    joined = normalize_whitespace(question).lower()
    tokens = _question_tokens(question)
    if any(phrase in joined for phrase in _MOTION_PRIMARY_QUERY_PHRASES):
        return True
    if "motion" in tokens:
        if any(phrase in joined for phrase in _PARTY_ROLE_QUERY_PHRASES):
            return False
        return True
    return False


def detect_party_role_question_intent(question: str) -> bool:
    """
    Detect party-and-role identity questions using general language.

    Does not fire for motion-primary queries so motion evidence stays available.
    """
    joined = normalize_whitespace(question).lower()
    tokens = _question_tokens(question)
    if not joined:
        return False
    if _query_seeks_motion_primary(question):
        return False
    if any(phrase in joined for phrase in _PARTY_ROLE_QUERY_PHRASES):
        return True
    if "parties" in tokens and any(
        cue in joined
        for cue in ("role", "roles", "who", "identify", "named", "caption")
    ):
        return True
    role_identity = tokens.intersection(
        {
            "plaintiff",
            "defendant",
            "petitioner",
            "respondent",
            "appellant",
            "appellee",
        }
    )
    if role_identity and any(
        cue in joined
        for cue in (
            "who is",
            "who are",
            "role",
            "roles",
            "named as",
            "identify",
            "caption",
            "parties",
        )
    ):
        return True
    if "plaintiff" in tokens and "defendant" in tokens:
        return True
    if "petitioner" in tokens and "respondent" in tokens:
        return True
    if "third-party" in joined or ("third" in tokens and "party" in tokens):
        if role_identity or "party" in tokens or "parties" in tokens:
            return True
    return False


def _hit_materiality_text(hit: dict) -> str:
    """
    Text used for party-role materiality decisions.

    Prefers full canonical page text when available so short query-centered
    excerpts cannot hide caption role labels or later party-role paragraphs.
    Isolated metadata / filenames remain secondary context only.
    """
    page_text = normalize_whitespace(
        hit.get("page_text") or hit.get("full_page_text") or ""
    )
    primary = page_text or normalize_whitespace(hit.get("excerpt") or "")
    parts = [
        primary,
        hit.get("source_filename"),
        hit.get("document_type"),
        " ".join(str(x) for x in (hit.get("classifications") or [])),
        hit.get("assertion_kind"),
    ]
    return normalize_whitespace(" ".join(str(p or "") for p in parts))


def _hit_page_materiality_text(hit: dict) -> str:
    """Actual page/excerpt text, excluding filenames, types, and metadata."""
    page_text = normalize_whitespace(
        hit.get("page_text") or hit.get("full_page_text") or ""
    )
    return page_text or normalize_whitespace(hit.get("excerpt") or "")


def _classify_hit_filing_kind(hit: dict) -> str:
    doc_type = normalize_whitespace(hit.get("document_type")).lower()
    filename = normalize_whitespace(hit.get("source_filename")).lower()
    # Prefer a short head of full-page text when present; fall back to excerpt.
    body = normalize_whitespace(
        hit.get("page_text") or hit.get("full_page_text") or hit.get("excerpt") or ""
    )[:240].lower()
    hay = f"{filename} {doc_type} {body}"

    if "rji" in hay or "request for judicial intervention" in hay:
        return "rji"
    if _SERVICE_FILING_RE.search(hay) or re.search(
        r"\b(?:affidavit|affirmation)\s+of\s+service\b", filename
    ):
        return "service"
    if doc_type == "motion" or "notice of motion" in hay or (
        re.search(r"\bmotion\b", hay) and "summons" not in hay
    ):
        return "motion"
    if doc_type in {"affirmation", "affidavit"} or re.search(
        r"\b(?:affirmation|affidavit)\b", hay
    ):
        return "affirmation"
    if doc_type == "order" or re.search(
        r"\b(?:decision and order|it is hereby ordered|ordered that|scheduling\s+order)\b",
        hay,
    ):
        return "order"
    if "amended" in hay:
        if any(
            token in hay
            for token in (
                "complaint",
                "petition",
                "answer",
                "summons",
                "pleading",
            )
        ):
            return "amended_pleading"
    if doc_type == "complaint" or any(
        token in hay for token in ("complaint", "summons", "petition")
    ):
        return "initiating"
    if doc_type == "answer" or re.search(r"\banswers?\b", hay):
        return "answer"
    if _PROCEDURAL_NOISE_RE.search(hay) and not any(
        token in hay for token in ("complaint", "summons", "petition", "answer")
    ):
        return "procedural_history"
    return "other"


def _hit_establishes_party_identity_or_role(text: str) -> bool:
    if not text:
        return False
    if _PARTY_IDENTITY_ESTABLISHING_RE.search(text):
        return True
    healed = heal_ocr_intra_word_spaces(text)
    if healed != text and _PARTY_IDENTITY_ESTABLISHING_RE.search(healed):
        return True
    # Require role-bearing language plus identity/relationship verbs or entity
    # status words. Bare "Inc." / "LLC" inside a caption name is not enough.
    probe = healed if healed else text
    if _PARTY_ROLE_BEARING_RE.search(probe) and re.search(
        r"(?i)\b(?:is|are|was|were|named|joined|sued|authorized|organized|"
        r"corporation|partnership|company|individual|resident|residing|"
        r"principal\s+place|place\s+of\s+business|"
        r"notice\s+defendant|named\s+insured)\b",
        probe,
    ):
        return True
    return False


def _hit_qualifies_or_changes_party_role(text: str) -> bool:
    return bool(text and _PARTY_ROLE_QUALIFICATION_OR_CHANGE_RE.search(text))


def _hit_materially_changes_party_role(text: str) -> bool:
    """
    Affirmative change / qualification / conflict evidence.

    Used as the sole survival path for hard-excluded procedural records.
    Generic caption labels, isolated names, and incidental role words do not
    satisfy this gate.
    """
    return bool(text and _PARTY_ROLE_MATERIAL_CHANGE_RE.search(text))


def _hit_is_necessary_party_role_exception(hit: dict) -> bool:
    """Require the page's own text to demonstrate a material role exception."""
    return _hit_materially_changes_party_role(_hit_page_materiality_text(hit))


def _hit_is_mere_procedural_noise(text: str, kind: str) -> bool:
    if kind in _PROCEDURAL_HARD_EXCLUDE_KINDS:
        return not _hit_materially_changes_party_role(text)
    if kind == "other":
        if _PROCEDURAL_NOISE_RE.search(text or "") and not (
            _hit_establishes_party_identity_or_role(text)
            or _hit_qualifies_or_changes_party_role(text)
        ):
            return True
    return False


def _hit_has_isolated_name_only_signal(text: str) -> bool:
    """
    True when text lacks role/identity language (a bare name is not material).
    """
    if not text:
        return True
    if _hit_establishes_party_identity_or_role(text):
        return False
    if _PARTY_ROLE_BEARING_RE.search(text):
        return False
    if _hit_qualifies_or_changes_party_role(text):
        return False
    return True


def hit_is_material_for_party_role_question(hit: dict) -> bool:
    """
    Question-conditioned materiality for party-and-role intent.

    Prefers identity/role/entity/joinder/operative-pleading evidence and later
    filings that change, qualify, or conflict with a party's role. Hard-excludes
    motions, RJI, affirmations, service papers, orders, and procedural-history
    records unless they affirmatively change/qualify/conflict party role.
    Isolated name/caption/incidental role words cannot override that exclusion.
    Uses full-page text when available.
    """
    if not isinstance(hit, dict):
        return False
    text = _hit_materiality_text(hit)
    kind = _classify_hit_filing_kind(hit)

    # Procedural noise: only affirmative material-change language survives.
    if kind in _PROCEDURAL_HARD_EXCLUDE_KINDS:
        return _hit_materially_changes_party_role(text)

    if _hit_qualifies_or_changes_party_role(text):
        return True
    if _hit_is_mere_procedural_noise(text, kind):
        return False
    if _hit_has_isolated_name_only_signal(text):
        return False
    if kind in {"initiating", "amended_pleading", "answer"}:
        return _hit_establishes_party_identity_or_role(text) or bool(
            _PARTY_ROLE_BEARING_RE.search(text)
        )
    return _hit_establishes_party_identity_or_role(text)


def _hit_serialized_char_count(hit: dict) -> int:
    """Approximate serialized size of a packet hit (excerpt-focused)."""
    parts = [
        hit.get("result_id"),
        hit.get("page_id"),
        hit.get("nyscef_document_number"),
        hit.get("pdf_page"),
        hit.get("source_filename"),
        hit.get("document_type"),
        hit.get("excerpt"),
        hit.get("assertion_kind"),
        " ".join(str(x) for x in (hit.get("classifications") or [])),
    ]
    return len(normalize_whitespace(" ".join(str(p or "") for p in parts)))


_PARTY_ROLE_PARTIES_SECTION_HEADING_RE = re.compile(
    r"(?i)(?:^|[\n\r]|(?<=\.)\s|(?<=:)\s*)"
    r"(?:(?:section|article|part)\s+[ivxlcdm\d]+"
    r"(?:\s*[.:=\-—–]\s*|\s+)|(?:[ivxlcdm]+|\d+)(?:\.\d+)*[.)]?\s+)?"
    r"(?:the\s+)?parties\b"
)

_PARTY_ROLE_INTRO_SECTION_HEADING_RE = re.compile(
    r"(?i)(?:^|[\n\r]|(?<=\.)\s|(?<=:)\s*)"
    r"(?:(?:section|article|part)\s+[ivxlcdm\d]+"
    r"(?:\s*[.:=\-—–]\s*|\s+)|(?:[ivxlcdm]+|\d+)(?:\.\d+)*[.)]?\s+)?"
    r"(?:nature\s+of\s+(?:the\s+)?action|preliminary\s+statement|introduction)"
    r"\s*:?(?=\s*(?:$|\d+\.|(?-i:[A-Z(\"'])))"
)


def _hit_is_party_role_caption_or_section_page(hit: dict) -> bool:
    """True for an operative pleading's caption, PARTIES, or intro-section pages."""
    kind = _classify_hit_filing_kind(hit)
    if kind not in {"initiating", "amended_pleading", "answer"}:
        return False
    if hit.get("party_role_section_expanded"):
        return True
    text = _hit_materiality_text(hit)
    # Contiguous PARTIES-section pages.
    if _PARTY_ROLE_PARTIES_SECTION_HEADING_RE.search(text):
        return True
    # Concise initiating opening sections (intro / nature / preliminary).
    if _PARTY_ROLE_INTRO_SECTION_HEADING_RE.search(text):
        return True
    # Caption-bearing early pleading pages.
    page_no = hit.get("pdf_page")
    try:
        early = page_no is None or int(page_no) <= 3
    except (TypeError, ValueError):
        early = True
    if early and re.search(r"(?i)\bv\.|\bagainst\b", text):
        if re.search(
            r"(?i)\b(?:plaintiffs?|defendants?|petitioners?|respondents?)\b",
            text,
        ):
            return True
    return False


def _hit_party_role_protected_section_kind(hit: dict) -> Optional[str]:
    """
    Classify a hit into a discrete protected section set.

    Returns ``\"parties\"``, ``\"intro\"``, or ``None``. Caption-only pages are
    not assigned here; they remain independently protectable.
    """
    text = _hit_materiality_text(hit)
    if _PARTY_ROLE_PARTIES_SECTION_HEADING_RE.search(text):
        return "parties"
    if _PARTY_ROLE_INTRO_SECTION_HEADING_RE.search(text):
        return "intro"
    if not hit.get("party_role_section_expanded"):
        return None
    # Expanded continuation without a repeated heading stays inside its set.
    if (
        _PARTY_ROLE_BEARING_RE.search(text)
        or _PARTY_IDENTITY_ESTABLISHING_RE.search(text)
        or re.search(
            r"(?i)\b(?:notice\s+defendant|named\s+insured|necessary\s+party|"
            r"joined(?:\s+herein|\s+as)?|sued\s+herein)\b",
            text,
        )
    ):
        return "parties"
    return "intro"


def _party_role_source_key(hit: dict) -> str:
    """Stable filing identity used to keep pleading pages grouped."""
    doc_no = hit.get("nyscef_document_number")
    if doc_no is not None:
        return f"doc:{doc_no}"
    filename = normalize_whitespace(hit.get("source_filename") or "").lower()
    if filename:
        return f"file:{filename}"
    return f"result:{hit.get('result_id') or hit.get('document_type') or 'unknown'}"


def _controlling_party_role_source(hits: Sequence[dict]) -> Optional[str]:
    """
    Select one controlling pleading from retrieval/source priority.

    A source's page count is deliberately absent from the ordering, so a later
    answer cannot displace the higher-priority complaint merely by contributing
    more hits.  First-seen order preserves the retrieval rank within a kind.
    """
    kind_priority = {"initiating": 0, "amended_pleading": 1, "answer": 2}
    candidates = []
    seen = set()
    for index, hit in enumerate(hits or []):
        kind = _classify_hit_filing_kind(hit)
        if kind not in kind_priority:
            continue
        key = _party_role_source_key(hit)
        if key in seen:
            continue
        seen.add(key)
        candidates.append((kind_priority[kind], index, key))
    return min(candidates)[2] if candidates else None


def _discrete_section_page_range(
    page_numbers: Sequence[int],
) -> Optional[Tuple[int, int]]:
    """
    Min-max page span within one recognized section set.

    Preserves unmarked cross-page continuation inside that set only. Callers
    must not merge intro and PARTIES page lists before invoking this helper.
    """
    if not page_numbers:
        return None
    ordered = [int(p) for p in page_numbers]
    return (min(ordered), max(ordered))


def _mark_controlling_party_role_group(hits: Sequence[dict]) -> List[dict]:
    """Mark controlling caption, discrete intro, and discrete PARTIES pages."""
    marked = [dict(hit) for hit in (hits or [])]
    source_key = _controlling_party_role_source(marked)
    if source_key is None:
        return marked

    source_hits = [
        hit for hit in marked if _party_role_source_key(hit) == source_key
    ]
    intro_pages: List[int] = []
    parties_pages: List[int] = []
    for hit in source_hits:
        kind = _hit_party_role_protected_section_kind(hit)
        if kind is None:
            continue
        try:
            page_no = int(hit.get("pdf_page"))
        except (TypeError, ValueError):
            continue
        if kind == "intro":
            intro_pages.append(page_no)
        elif kind == "parties":
            parties_pages.append(page_no)

    # Within each recognized section, include already-retrieved pages between
    # endpoints so unmarked intra-section continuations stay protected. Never
    # min-max fill intervening pages between intro and PARTIES sets.
    protected_ranges = [
        span
        for span in (
            _discrete_section_page_range(intro_pages),
            _discrete_section_page_range(parties_pages),
        )
        if span is not None
    ]

    for hit in source_hits:
        protected = _hit_is_party_role_caption_or_section_page(hit)
        if not protected and protected_ranges:
            try:
                page_no = int(hit.get("pdf_page"))
            except (TypeError, ValueError):
                page_no = None
            if page_no is not None:
                protected = any(
                    start <= page_no <= end for start, end in protected_ranges
                )
        if protected:
            hit["controlling_party_role_pleading"] = True
    return marked


def _party_role_hit_dedupe_key(hit: dict) -> str:
    page_id = normalize_whitespace(hit.get("page_id") or "")
    if page_id:
        return f"page:{page_id}"
    excerpt = normalize_whitespace(hit.get("excerpt") or "").lower()
    return (
        f"ex:{hit.get('nyscef_document_number')}-"
        f"{hit.get('pdf_page')}-{excerpt[:160]}"
    )


def _compress_protected_party_role_hit(hit: dict) -> dict:
    """Remove nonresponsive prose while preserving complete role-bearing lines."""
    excerpt = str(hit.get("excerpt") or "")
    lines = excerpt.splitlines()
    if len(lines) < 2:
        return hit
    responsive = []
    identity_line_re = re.compile(
        r"(?i)\b(?:"
        r"principal\s+place\s+of\s+business|place\s+of\s+business|"
        r"residen(?:t|ce|ts)|resid(?:es|ed|ing)|"
        r"individual|corporation|partnership|company|"
        r"domestic|foreign|authorized|organized|"
        r"notice\s+defendant|named\s+insured|"
        r"was\s+and\s+still\s+is|"
        r"introduction|preliminary\s+statement|"
        r"nature\s+of\s+(?:the\s+)?action|"
        r"this\s+is\s+an\s+action|brings?\s+this\s+(?:action|proceeding)"
        r")\b"
    )
    for line in lines:
        if (
            _PARTY_ROLE_BEARING_RE.search(line)
            or _PARTY_ROLE_QUALIFICATION_OR_CHANGE_RE.search(line)
            or _PARTY_IDENTITY_ESTABLISHING_RE.search(line)
            or identity_line_re.search(line)
            or re.search(r"(?i)\b(?:parties|against|index\s+no\.?|supreme\s+court)\b", line)
        ):
            responsive.append(line.rstrip())
            continue
        # OCR-tolerant retention for fractured entity / residence cues.
        healed = heal_ocr_intra_word_spaces(line)
        if healed != line and (
            _PARTY_ROLE_BEARING_RE.search(healed)
            or _PARTY_IDENTITY_ESTABLISHING_RE.search(healed)
            or identity_line_re.search(healed)
        ):
            responsive.append(line.rstrip())
    compressed = "\n".join(line for line in responsive if line.strip()).strip()
    if not compressed or len(compressed) >= len(excerpt):
        return hit
    result = dict(hit)
    result["excerpt"] = compressed
    result["party_role_excerpt_compressed"] = True
    return result


def apply_party_role_packet_budget(
    hits: Sequence[dict],
    *,
    max_hits: int = PARTY_ROLE_PACKET_MAX_HITS,
    max_chars: int = PARTY_ROLE_PACKET_MAX_CHARS,
) -> Tuple[List[dict], dict]:
    """
    Enforce a total party-role evidence budget after materiality filtering.

    Deterministic selection:
    1. Deduplicate redundant pages/propositions (stable first-seen order).
    2. Always retain controlling initiating/operative caption, intro, and PARTIES pages.
    3. Then retain only page-text-demonstrated change/qualification/conflict evidence.
    4. Never truncate party names or role paragraphs — omit whole non-protected
       hits when the budget would otherwise be exceeded.
    """
    source = [hit for hit in (hits or []) if isinstance(hit, dict)]
    deduped = []
    seen = set()
    for hit in source:
        key = _party_role_hit_dedupe_key(hit)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(hit)

    deduped = _mark_controlling_party_role_group(deduped)

    protected = []
    qualifying = []
    for hit in deduped:
        if hit.get("controlling_party_role_pleading"):
            protected.append(hit)
        elif _hit_is_necessary_party_role_exception(hit):
            qualifying.append(hit)

    # If the protected group itself creates character pressure, deterministically
    # remove only nonresponsive prose from each page.  Complete role-bearing
    # lines and every page-level citation remain intact; if those responsive
    # passages alone exceed the limit, protection still wins over silent loss.
    if max_chars is not None and sum(
        _hit_serialized_char_count(hit) for hit in protected
    ) > max_chars:
        protected = [_compress_protected_party_role_hit(hit) for hit in protected]

    def _sort_key(item: dict):
        return (
            -(float(item.get("score") or 0.0)),
            item.get("nyscef_document_number") is None,
            item.get("nyscef_document_number")
            if item.get("nyscef_document_number") is not None
            else 10**9,
            item.get("pdf_page") or 0,
            item.get("result_id") or "",
        )

    qualifying.sort(key=_sort_key)

    selected: List[dict] = []
    selected_ids = set()
    chars = 0

    def _try_add(hit: dict, *, force: bool) -> bool:
        nonlocal chars
        key = _party_role_hit_dedupe_key(hit)
        if key in selected_ids:
            return False
        size = _hit_serialized_char_count(hit)
        if not force:
            if max_hits is not None and len(selected) >= max_hits:
                return False
            if max_chars is not None and chars + size > max_chars:
                return False
        selected.append(hit)
        selected_ids.add(key)
        chars += size
        return True

    for hit in protected:
        _try_add(hit, force=True)
    for hit in qualifying:
        _try_add(hit, force=False)

    meta = {
        "max_hits": max_hits,
        "max_chars": max_chars,
        "input_hit_count": len(source),
        "deduped_hit_count": len(deduped),
        "kept_hit_count": len(selected),
        "excluded_by_budget": max(0, len(deduped) - len(selected)),
        "serialized_chars": chars,
        "protected_hit_count": sum(
            1 for hit in selected if hit.get("controlling_party_role_pleading")
        ),
    }
    return selected, meta


def filter_hits_for_party_role_materiality(
    hits: Sequence[dict],
) -> Tuple[List[dict], dict]:
    """
    Apply party-role materiality filtering while preserving hit order.

    Falls back to initiating/operative pleadings, then to the original hits,
    when filtering would otherwise empty the generation packet. After
    materiality, applies the total party-role evidence-packet budget.
    """
    source = [hit for hit in (hits or []) if isinstance(hit, dict)]
    marked_source = _mark_controlling_party_role_group(source)
    kept = [
        hit
        for hit in marked_source
        if hit.get("controlling_party_role_pleading")
        or hit_is_material_for_party_role_question(hit)
    ]
    fallback = "none"
    if not kept:
        pleading_kinds = {"initiating", "amended_pleading", "answer"}
        kept = [
            hit
            for hit in source
            if _classify_hit_filing_kind(hit) in pleading_kinds
        ]
        fallback = "operative_pleadings" if kept else "unfiltered"
        if not kept:
            kept = list(source)
    excluded_count = max(0, len(source) - len(kept))
    kept, budget_meta = apply_party_role_packet_budget(kept)
    meta = {
        "intent": "party_role",
        "input_hit_count": len(source),
        "kept_hit_count": len(kept),
        "excluded_hit_count": excluded_count + int(budget_meta.get("excluded_by_budget") or 0),
        "fallback": fallback,
        "packet_budget": budget_meta,
    }
    return kept, meta


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def build_evidence_packet(
    question: str,
    retrieval: Optional[dict],
    *,
    case_map: Optional[dict] = None,
    exhibit_context: Optional[Any] = None,
    allowed_sources: Optional[Sequence[str]] = None,
) -> dict:
    results = list((retrieval or {}).get("results") or [])
    materiality_filter = None
    party_role_intent = detect_party_role_question_intent(question)
    if party_role_intent:
        results, materiality_filter = filter_hits_for_party_role_materiality(results)

    # Lazy import avoids the matter_builder ↔ drafting_engine import cycle.
    sanitize_linkage_label = None
    if party_role_intent:
        from matter_builder import (  # noqa: WPS433
            _sanitize_party_role_case_map_linkage_label,
        )

        sanitize_linkage_label = _sanitize_party_role_case_map_linkage_label

    compact_hits = []
    for hit in results:
        if not isinstance(hit, dict):
            continue
        linkage = hit.get("case_map_linkage")
        if party_role_intent and isinstance(linkage, dict):
            linkage = dict(linkage)
            raw_label = linkage.get("label")
            if raw_label is not None and sanitize_linkage_label is not None:
                cleaned_label = sanitize_linkage_label(raw_label)
                if cleaned_label:
                    linkage["label"] = cleaned_label
                else:
                    # Omit label when only procedural boilerplate remained.
                    linkage.pop("label", None)
        compact_hits.append(
            {
                "result_id": hit.get("result_id"),
                "page_id": hit.get("page_id"),
                "nyscef_document_number": hit.get("nyscef_document_number"),
                "pdf_page": hit.get("pdf_page"),
                "source_filename": hit.get("source_filename"),
                "document_type": hit.get("document_type"),
                "excerpt": hit.get("excerpt"),
                "classifications": list(hit.get("classifications") or []),
                "assertion_kind": hit.get("assertion_kind"),
                "case_map_linkage": linkage,
                "exhibit_segment": hit.get("exhibit_segment"),
                "score": hit.get("score"),
            }
        )

    case_map_signals = None
    if isinstance(case_map, dict):
        # Provide only compact retrieval-signal summaries — not proof.
        case_map_signals = {
            "note": (
                "Case-map entries are retrieval signals only and are not "
                "independent proof."
            ),
            "validation": (case_map.get("validation") or {}),
            "counts": {
                collection: len(case_map.get(collection) or [])
                for collection in (
                    "parties",
                    "policies",
                    "claims",
                    "defenses",
                    "allegations",
                    "evidence",
                    "timeline_events",
                    "motions",
                    "court_orders",
                )
            },
        }

    packet = {
        "question": normalize_whitespace(question),
        "retrieval_query": (retrieval or {}).get("query"),
        "retrieval_hit_count": len(compact_hits),
        "retrieval_hits": compact_hits,
        "case_map_signals": case_map_signals,
        "exhibit_context": exhibit_context,
        "allowed_sources": list(allowed_sources or []),
        "record_only_default": True,
    }
    if materiality_filter is not None:
        packet["materiality_filter"] = materiality_filter
    return packet


def build_user_prompt(
    evidence_packet: dict,
    *,
    party_role_completeness: bool = False,
) -> str:
    prompt = (
        "Analyze the attorney question using only this evidence packet.\n"
        "Return the required JSON object and nothing else.\n\n"
        + _stable_json(evidence_packet)
    )
    if party_role_completeness:
        # Must follow evidence serialization so no later instruction weakens it.
        prompt = (
            prompt
            + "\n\n"
            + PARTY_ROLE_DRAFTING_COMPLETENESS_INSTRUCTION
        )
    return prompt


# ---------------------------------------------------------------------------
# Party-role drafting completeness (extract → validate → one repair)
# ---------------------------------------------------------------------------

_PARTY_ROLE_DRAFT_LABEL = (
    r"third[\s-]+party\s+plaintiffs?|"
    r"third[\s-]+party\s+defendants?|"
    r"respondents?\s+on\s+(?:the\s+)?appeal|"
    r"plaintiffs?|"
    r"defendants?|"
    r"petitioners?|"
    r"respondents?|"
    r"appellants?|"
    r"appellees?"
)

_PARTY_ROLE_DRAFT_RE = re.compile(
    r"(?:"
    r"\b(?P<role_leading>" + _PARTY_ROLE_DRAFT_LABEL + r")\s+"
    r"(?P<name_leading>(?-i:[A-Z0-9])[A-Za-z0-9&.,' -]{0,80}?)"
    r"(?=\s+(?:is|was|are|were|has|have|brings|commenced|,|\.|$|;))|"
    r"\b(?P<name>(?-i:[A-Z0-9])[A-Za-z0-9&.,' -]{0,80}?),\s*"
    r"(?P<role>" + _PARTY_ROLE_DRAFT_LABEL + r")\b"
    r")",
    re.IGNORECASE,
)

# Generic pleading-allegation identity/role discovery (primary parser path).
_PARTY_ROLE_NAME_FRAGMENT = (
    r"(?-i:[A-Z][A-Za-z0-9&'’.\-–—]*|"
    r"LLC|LLP|LP|Inc\.?|Corp\.?|Co\.?|Ltd\.?|PLLC|PC|PLC|P\.C\.)"
)
# Digit-leading org tokens (``123``, ``21st``) only when a following alphabetic
# name fragment makes an organization/party sequence plausible.
_PARTY_ROLE_DIGIT_LEADING_FRAGMENT = (
    r"(?-i:[0-9]+[A-Za-z][A-Za-z0-9&'’.\-–—]*|[0-9]+)"
)
# Join multi-token identities on spaces, commas (``Freight, Inc.``), or slashes
# (``John/Jane``, ``Smith/Jones``). Optional lowercase particles keep collective
# org names intact (``Underwriters at Lloyd's of London``) without absorbing
# role/entity clauses.
_PARTY_ROLE_NAME_PARTICLE = r"(?:of|at|the|and|for|in|by|on|to|de|da|von|van)"
_PARTY_ROLE_NAME_CONNECTOR = (
    r"(?:\s*/\s*|\s*,\s*|\s+(?:" + _PARTY_ROLE_NAME_PARTICLE + r"\s+)?)"
)
_PARTY_ROLE_NAME_RE = (
    r"(?P<name>"
    r"(?!(?:the\s+)?(?:" + _PARTY_ROLE_DRAFT_LABEL + r")\b)"
    r"(?:"
    r"(?:John|Jane)(?:\s*/\s*(?:John|Jane))?\s+Does?|"
    r"(?-i:[A-Z]{2,})(?:\s*/\s*(?-i:[A-Z]{2,}))*\s+CORPS?\.?|"
    # Digits then at least one alphabetic/org fragment (``123 Freight LLC``).
    + r"(?:"
    + _PARTY_ROLE_DIGIT_LEADING_FRAGMENT
    + _PARTY_ROLE_NAME_CONNECTOR
    + _PARTY_ROLE_NAME_FRAGMENT
    + r"(?:"
    + _PARTY_ROLE_NAME_CONNECTOR
    + _PARTY_ROLE_NAME_FRAGMENT
    + r"){0,7})|"
    + _PARTY_ROLE_NAME_FRAGMENT
    + r")"
    r"(?:" + _PARTY_ROLE_NAME_CONNECTOR + _PARTY_ROLE_NAME_FRAGMENT + r"){0,8}"
    r"(?:\s+\d+(?:\s*(?:[-–—]|through)\s*\d+)?)?"
    r")"
)
_PARTY_ROLE_NAME_FIND_RE = re.compile(_PARTY_ROLE_NAME_RE)
_PARTY_ROLE_COPULA = (
    r"(?:was\s+and\s+still\s+is|is|are|was|were|has\s+been|have\s+been)"
)
# Optional parenthetical defined-term after the full identity; never part of the
# canonical name capture (alias body is recorded separately when present).
_PARTY_ROLE_DEFINED_ALIAS_PAREN = r"(?:\s*\(\s*(?P<alias_body>[^)]{1,80})\))?"
_PARTY_ROLE_ALLEGATION_ROLE_BEFORE_RE = re.compile(
    r"(?i)(?:^\s*\d+\.\s*)?\s*"
    r"(?:the\s+)?(?P<role>" + _PARTY_ROLE_DRAFT_LABEL + r")\s+"
    + _PARTY_ROLE_NAME_RE
    + _PARTY_ROLE_DEFINED_ALIAS_PAREN
    + r"\s*(?:,|\s+" + _PARTY_ROLE_COPULA + r")",
)
_PARTY_ROLE_ALLEGATION_ROLE_AFTER_RE = re.compile(
    r"(?i)(?:^\s*\d+\.\s*)?\s*"
    r"(?:the\s+)?" + _PARTY_ROLE_NAME_RE
    + _PARTY_ROLE_DEFINED_ALIAS_PAREN
    + r"\s*"
    r"(?:"
    r",\s*(?P<role_comma>" + _PARTY_ROLE_DRAFT_LABEL + r")\b|"
    r"\s+" + _PARTY_ROLE_COPULA + r"\s+(?:(?:a|an|the)\s+)?"
    r"(?P<role_pred>" + _PARTY_ROLE_DRAFT_LABEL + r")\b"
    r")",
)
_PARTY_ROLE_ALLEGATION_ENTITY_RE = re.compile(
    r"(?i)(?:^\s*\d+\.\s*)?\s*"
    r"(?:the\s+)?" + _PARTY_ROLE_NAME_RE
    + _PARTY_ROLE_DEFINED_ALIAS_PAREN
    + r"\s+"
    + _PARTY_ROLE_COPULA + r"\s+"
    r"(?:(?:a|an|the)\s+)?"
    r"(?:"
    r"domestic|foreign|limited|individuals?|corporations?|partnerships?|"
    r"associations?|compan(?:y|ies)|llc|llp"
    r")\b",
)

# Tokens that alone cannot uniquely identify a party for shorthand consolidation.
_PARTY_ROLE_GENERIC_NAME_TOKENS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "by",
        "co",
        "company",
        "corp",
        "corporation",
        "da",
        "de",
        "for",
        "in",
        "inc",
        "limited",
        "liability",
        "llc",
        "llp",
        "lp",
        "ltd",
        "of",
        "on",
        "pc",
        "plc",
        "pllc",
        "the",
        "to",
        "van",
        "von",
    }
)
_PARTY_ROLE_GROUPED_BASIS_RE = re.compile(
    r"(?i)\b(?:the\s+)?(?:foregoing|said|these|those|above(?:-|\s+)named)\s+"
    r"defendants?\b.*\bnotice\s+defendants?\b",
)
_PARTY_ROLE_PLACEHOLDER_GROUP_RE = re.compile(
    r"(?i)(?<![/\w])(?P<name>"
    r"(?:"
    r"(?:John|Jane)(?:\s*/\s*(?:John|Jane))?\s+Does?|"
    r"(?-i:[A-Z]{2,})(?:\s*/\s*(?-i:[A-Z]{2,}))*\s+CORPS?\.?"
    r")"
    r"\s+\d+(?:\s*(?:[-–—]|through)\s*\d+)?)"
    r"(?:[^.]{0,80}?\b(?P<role>" + _PARTY_ROLE_DRAFT_LABEL + r")\b)?",
)

_PARTY_ROLE_ENTITY_TYPE_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (
        r"domestic\s+limited\s+liability\s+compan(?:y|ies)",
        "domestic limited liability company",
    ),
    (
        r"foreign\s+limited\s+liability\s+compan(?:y|ies)",
        "foreign limited liability company",
    ),
    (r"limited\s+liability\s+partnership", "limited liability partnership"),
    (r"limited\s+liability\s+corporation", "limited liability corporation"),
    (r"limited\s+liability\s+compan(?:y|ies)", "limited liability company"),
    (r"domestic\s+corporation", "domestic corporation"),
    (r"foreign\s+corporation", "foreign corporation"),
    (r"\bassociations?\b", "association"),
    (r"\bcorporation\b", "corporation"),
    (r"\bpartnership\b", "partnership"),
    (r"\bindividuals?\b", "individual"),
)

_PARTY_ROLE_RESIDENCE_PPB_RE = re.compile(
    r"(?i)("
    r"(?:principal\s+)?place\s+of\s+business\b[^.]{0,140}"
    r"|resident\s+of\b[^.]{0,100}"
    r"|residing\s+(?:in|at)\b[^.]{0,100}"
    r"|resides\s+(?:in|at)\b[^.]{0,100}"
    r"|is\s+a\s+resident\b[^.]{0,100}"
    r")"
)

_PARTY_ROLE_PLEADED_BASIS_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"notice\s+defendants?", "notice defendant"),
    (r"named\s+insured", "named insured"),
    (r"additional\s+insured", "additional insured"),
    (
        r"joined\s+herein\s+as\s+a\s+necessary\s+party",
        "joined herein as a necessary party",
    ),
    (r"necessary\s+party", "necessary party"),
    (r"real\s+party\s+in\s+interest", "real party in interest"),
    (r"sued\s+herein", "sued herein"),
)

_PARTY_ROLE_NAME_BLOCKLIST = frozenset(
    {
        "parties",
        "wherefore",
        "venue",
        "jurisdiction",
        "introduction",
        "preliminary statement",
        "nature of the action",
        "nature of action",
        "verification",
        "plaintiff",
        "plaintiffs",
        "defendant",
        "defendants",
        "petitioner",
        "petitioners",
        "respondent",
        "respondents",
    }
)


def _normalize_party_role_draft_label(value: Any) -> Optional[str]:
    role = normalize_whitespace(value).lower().replace("third party", "third-party")
    role = role.strip(" .,;:")
    if not role:
        return None
    if role.startswith("third-party plaintiff"):
        return "third-party plaintiff"
    if role.startswith("third-party defendant"):
        return "third-party defendant"
    if re.match(r"respondents?\s+on\s+(?:the\s+)?appeal$", role):
        return "respondent on appeal"
    if role.startswith("plaintiff"):
        return "plaintiff"
    if role.startswith("defendant"):
        return "defendant"
    if role.startswith("petitioner"):
        return "petitioner"
    if role.startswith("respondent"):
        return "respondent"
    if role.startswith("appellant"):
        return "appellant"
    if role.startswith("appellee"):
        return "appellee"
    return None


def _plausible_party_role_draft_name(name: Any) -> bool:
    cleaned = normalize_whitespace(name).strip(" .,;:")
    if not cleaned or len(cleaned) < 3:
        return False
    lowered = cleaned.lower()
    if lowered in _PARTY_ROLE_NAME_BLOCKLIST:
        return False
    if re.match(
        r"(?i)^(is|was|are|were|has|have|seeks?|brings?|joined|authorized)\b",
        cleaned,
    ):
        return False
    return True


def _ocr_flexible_phrase_present(phrase: str, haystack_norm: str) -> bool:
    """OCR-tolerant substring check for multi-word attribute values.

    Allows optional whitespace inside needle tokens (OCR letter splits) and
    punctuation-only / whitespace separators between tokens so full fractured
    residence/PPB strings (e.g. ``35- 06 U nion Street, Queens, New York``)
    match clean drafted text that retains commas.
    """
    words = [w for w in re.split(r"\s+", normalize_whitespace(phrase).lower()) if w]
    if not words:
        return False
    if " ".join(words) in haystack_norm:
        return True
    word_patterns = []
    for word in words:
        letters = [re.escape(ch) for ch in word if ch.isalnum() or ch in {"'", "-"}]
        if not letters:
            continue
        word_patterns.append(r"\s*".join(letters))
    if not word_patterns:
        return False
    # Inter-word: whitespace or punctuation-only separators (commas in addresses).
    pattern = re.compile(r"\W*".join(word_patterns), re.I)
    return bool(pattern.search(haystack_norm))


def _party_role_attribute_present(value: Any, draft_norm: str) -> bool:
    text = normalize_whitespace(value)
    if not text or not draft_norm:
        return False
    hay = _normalize_party_role_match_text(draft_norm)
    needle = _normalize_party_role_match_text(text)
    if needle and needle in hay:
        return True
    return _ocr_flexible_phrase_present(text, hay)


def _normalize_party_role_match_text(value: Any) -> str:
    """
    Comparison-only identity key: case-fold and unify hyphen/en-dash/em-dash.

    Applies party-identity OCR intra-word healing before keying so fractured
    and clean surface forms share one inventory bucket. Does not itself mutate
    the pleaded identity string stored on party records.
    """
    text = heal_party_identity_ocr_spaces(normalize_whitespace(value)).lower()
    if not text:
        return ""
    for dash in ("\u2013", "\u2014", "\u2212"):
        text = text.replace(dash, "-")
    return text


def _clean_party_role_alias(raw_alias: Any) -> Optional[str]:
    """Extract a defined-term alias from parenthetical body text."""
    body = normalize_whitespace(raw_alias)
    if not body:
        return None
    body = re.sub(
        r"^(?:hereinafter|a/?k/?a\.?|also\s+known\s+as)\s+",
        "",
        body,
        flags=re.I,
    ).strip()
    generic = {
        "company",
        "corporation",
        "partnership",
        "association",
        "llc",
        "llp",
        "inc",
        "corp",
        "ltd",
        "limited",
    }
    # Common form: (the "Company") → the Company
    wrapped = re.match(
        r"(?i)^(?P<article>the|a|an)\s+[\"'“”](.+?)[\"'“”]\s*$",
        body,
    )
    if wrapped:
        inner = normalize_whitespace(wrapped.group(2)).strip(" .,;:")
        if not inner:
            return None
        if inner.lower() in generic:
            return f"the {inner}"
        return inner
    alias = body.strip(" \"'“”'")
    alias = normalize_whitespace(alias).strip(" .,;:")
    if not alias or len(alias) < 2:
        return None
    # Reject role labels and empty defined terms.
    if _normalize_party_role_draft_label(alias):
        return None
    # Bare generic nouns are too ambiguous without their article.
    if alias.lower() in generic:
        return None
    return alias


def _alias_variants(alias: str) -> List[str]:
    """Shorthand forms that should resolve to the same canonical identity."""
    variants = [alias]
    stripped = re.sub(r"^(?:the|a|an)\s+", "", alias, flags=re.I).strip(" .,;:")
    if not stripped or stripped.lower() == alias.lower():
        return variants
    # Avoid bare entity nouns ("Company") matching entity-type clauses.
    if stripped.lower() in {
        "company",
        "corporation",
        "partnership",
        "association",
        "llc",
        "llp",
        "inc",
        "corp",
        "ltd",
        "limited",
    }:
        return variants
    variants.append(stripped)
    return variants


def _evidence_text_from_packet(evidence_packet: dict) -> str:
    """
    Text of the exact evidence supplied to the model.

    Uses the same packet object that ``build_user_prompt`` serializes: question
    plus retrieval-hit excerpts only (no protected references).
    """
    parts: List[str] = [str((evidence_packet or {}).get("question") or "")]
    for hit in (evidence_packet or {}).get("retrieval_hits") or []:
        if not isinstance(hit, dict):
            continue
        parts.append(str(hit.get("excerpt") or ""))
    return "\n".join(parts)


def _split_party_role_evidence_units(text: str) -> List[str]:
    """
    Split serialized pleading evidence into allegation-sized units.

    Keeps numbered paragraphs intact (including multiline continuations) and
    avoids fracturing ``1. Defendant ...`` into a bare ``1.`` token.
    """
    raw = str(text or "")
    if not raw:
        return []
    numbered = re.compile(r"^\s*\d+\.\s+\S")
    units: List[str] = []
    buf: List[str] = []

    def flush() -> None:
        if not buf:
            return
        joined = normalize_whitespace(" ".join(buf))
        if joined:
            units.append(joined)
        buf.clear()

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if numbered.match(stripped):
            flush()
            buf.append(stripped)
            continue
        if buf and not re.search(r"[.!?]\s*$", buf[-1]):
            # Unambiguous immediate continuation of an unfinished allegation.
            buf.append(stripped)
            continue
        flush()
        buf.append(stripped)
    flush()

    # Sentence-level split for dense single-line blocks, but never after a
    # leading paragraph number (``1. Name ...``).
    expanded: List[str] = []
    sentence_split = re.compile(
        r"(?<=[a-z0-9)\"'])\.\s+(?=[A-Z])|(?<=\.)\s+(?=\d+\.\s+)"
    )
    for unit in units:
        if numbered.match(unit):
            expanded.append(unit)
            continue
        parts = [
            normalize_whitespace(part)
            for part in sentence_split.split(unit)
            if normalize_whitespace(part)
        ]
        expanded.extend(parts or [unit])
    return expanded


def _extract_entity_type_from_unit(unit: str) -> Optional[str]:
    healed = heal_ocr_intra_word_spaces(unit)
    for pattern, label in _PARTY_ROLE_ENTITY_TYPE_PATTERNS:
        if re.search(pattern, healed, re.I) or re.search(pattern, unit, re.I):
            return label
    return None


def _extract_residence_or_ppb_from_unit(unit: str) -> Optional[str]:
    healed = heal_ocr_intra_word_spaces(unit)
    match = _PARTY_ROLE_RESIDENCE_PPB_RE.search(healed) or _PARTY_ROLE_RESIDENCE_PPB_RE.search(
        unit
    )
    if not match:
        return None
    value = normalize_whitespace(match.group(1))
    return value or None


def _extract_pleaded_role_basis_from_unit(unit: str) -> Optional[str]:
    healed = heal_ocr_intra_word_spaces(unit)
    for pattern, label in _PARTY_ROLE_PLEADED_BASIS_PATTERNS:
        if re.search(pattern, healed, re.I) or re.search(pattern, unit, re.I):
            return label
    return None


# Caption horizontal-rule (or spaced equivalent) immediately followed by the
# standalone boundary marker ``X``. Only this boundary syntax is stripped;
# ordinary X-leading party names are left intact.
_PARTY_ROLE_CAPTION_BOUNDARY_X_RE = re.compile(
    r"(?P<sep>(?:[-_═=─—–]\s*){3,})\s*X\b"
)


def _strip_caption_boundary_marker_x(text: str) -> str:
    """Exclude caption-boundary ``X`` that immediately follows a rule separator."""
    if not text:
        return text
    return _PARTY_ROLE_CAPTION_BOUNDARY_X_RE.sub(r"\g<sep>", text)


# Leading caption-administration headers only (court / county / venue / index /
# IAS part). Patterns are start-anchored and consumed iteratively so geographic
# words inside party names after parsing begins are never stripped.
_CAPTION_ADMIN_HEADER_STOP = (
    r"ias|index|part|venue|venued|supreme|civil|county|state|court|"
    r"plaintiffs?|defendants?|petitioners?|respondents?"
)
_LEADING_CAPTION_ADMIN_HEADER_RE = re.compile(
    r"(?is)^\s*(?:"
    r"(?:the\s+)?supreme\s+court(?:\s+of(?:\s+the)?\s+state\s+of\s+[a-z]+"
    r"(?:\s+(?!" + _CAPTION_ADMIN_HEADER_STOP + r"\b)[a-z]+)?)?"
    r"|(?:the\s+)?civil\s+court(?:\s+of(?:\s+the)?\s+city\s+of\s+[a-z]+"
    r"(?:\s+(?!" + _CAPTION_ADMIN_HEADER_STOP + r"\b)[a-z]+)?)?"
    r"|(?:the\s+)?county\s+court(?:\s+of(?:\s+the)?\s+(?:state|county)\s+of\s+[a-z]+"
    r"(?:\s+(?!" + _CAPTION_ADMIN_HEADER_STOP + r"\b)[a-z]+)?)?"
    r"|(?:the\s+)?surrogate'?s?\s+court(?:\s+of(?:\s+the)?\s+(?:state|county)\s+of\s+[a-z]+"
    r"(?:\s+(?!" + _CAPTION_ADMIN_HEADER_STOP + r"\b)[a-z]+)?)?"
    r"|appellate\s+division(?:\s+[^\n,.]{0,60})?"
    r"|united\s+states(?:\s+district)?\s+court(?:\s+for\s+the\s+[^\n,.]{0,80})?"
    r"|state\s+of\s+[a-z]+(?:\s+(?!" + _CAPTION_ADMIN_HEADER_STOP + r"\b)[a-z]+)?"
    r"|county\s+of\s+[a-z]+(?:\s+(?!" + _CAPTION_ADMIN_HEADER_STOP + r"\b)[a-z]+)?"
    r"|(?:venue|venued)\s*(?:[:.\-]|(?:\s+(?:in|at)))?\s*"
    r"(?:county\s+of\s+)?[a-z]+(?:\s+(?!" + _CAPTION_ADMIN_HEADER_STOP + r"\b)[a-z]+)?"
    r"(?:\s+county)?"
    r"|index\s+(?:no\.?|number)\s*[:#]?\s*[0-9][0-9A-Za-z/-]*"
    r"|ias\s+part\s+[a-z0-9-]+"
    r")\s*"
)

# Optional leading folio/page number immediately before a caption-admin header.
# Scoped to header context only so digit-leading party names stay intact.
_LEADING_CAPTION_FOLIO_RE = re.compile(r"(?is)^\s*\d{1,4}\b\s*")


def _caption_admin_header_line_only(stripped: str) -> bool:
    """True when ``stripped`` is entirely a caption-administration header."""
    if not stripped:
        return False
    return bool(
        _LEADING_CAPTION_ADMIN_HEADER_RE.match(stripped)
        and _LEADING_CAPTION_ADMIN_HEADER_RE.sub("", stripped, count=1).strip() == ""
    )


def _strip_leading_caption_folio_before_header(text: str) -> str:
    """
    Remove an optional leading folio/page number only when a caption-admin
    header follows. Does not strip digit-leading party identities.
    """
    if not text:
        return text
    folio_m = _LEADING_CAPTION_FOLIO_RE.match(text)
    if not folio_m:
        return text
    remainder = text[folio_m.end() :]
    if _LEADING_CAPTION_ADMIN_HEADER_RE.match(remainder):
        return remainder
    return text


def _strip_leading_caption_admin_headers(text: str) -> str:
    """
    Remove leading court/county/venue/index/caption-admin headers only.

    Stops at the first non-header token so party identities (including names
    that contain geographic words) remain intact. Preserves caption punctuation
    and ordering needed by boundary-rule and against/v. parsing. Optional
    leading folio/page numbers are stripped only immediately before a caption
    admin header.
    """
    if not text:
        return text
    # Prefer line-oriented stripping when caption newlines are still present.
    if re.search(r"\r?\n", text):
        lines = re.split(r"\r?\n", text)
        idx = 0
        while idx < len(lines):
            candidate = lines[idx]
            stripped = candidate.strip()
            if not stripped:
                idx += 1
                continue
            # Folio-only line when the next non-empty line is an admin header.
            if re.fullmatch(r"\d{1,4}", stripped):
                j = idx + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines) and _caption_admin_header_line_only(
                    lines[j].strip()
                ):
                    idx += 1
                    continue
                break
            # Same-line folio before header (``12 SUPREME COURT...``).
            folio_header = re.match(r"^(\d{1,4})\s+(\S.*)$", stripped)
            if folio_header and _caption_admin_header_line_only(
                folio_header.group(2)
            ):
                idx += 1
                continue
            if _caption_admin_header_line_only(stripped):
                idx += 1
                continue
            break
        text = "\n".join(lines[idx:])
    # Also peel leading folio + header phrases after whitespace-collapsed units.
    cleaned = text
    while True:
        with_folio = _strip_leading_caption_folio_before_header(cleaned)
        if with_folio != cleaned:
            cleaned = with_folio
            continue
        updated = _LEADING_CAPTION_ADMIN_HEADER_RE.sub("", cleaned, count=1)
        if updated == cleaned:
            break
        cleaned = updated
    return cleaned


def _strip_caption_horizontal_rules(text: str) -> str:
    """Remove leading/trailing caption rule separators after boundary-X handling."""
    if not text:
        return text
    cleaned = _strip_caption_boundary_marker_x(text)
    cleaned = re.sub(
        r"^(?:[-_═=─—–]\s*){3,}\s*",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"\s*(?:[-_═=─—–]\s*){3,}\s*$",
        "",
        cleaned,
    )
    return cleaned


def _party_role_identity_tokens(name: Any) -> List[str]:
    return re.findall(r"[a-z0-9']+", _normalize_party_role_match_text(name))


def _party_role_has_distinctive_token(tokens: Sequence[str]) -> bool:
    return any(tok not in _PARTY_ROLE_GENERIC_NAME_TOKENS for tok in tokens)


def _party_role_tokens_contiguous(
    needle: Sequence[str], haystack: Sequence[str]
) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    n = len(needle)
    needle_list = list(needle)
    for idx in range(len(haystack) - n + 1):
        if list(haystack[idx : idx + n]) == needle_list:
            return True
    return False


def _split_caption_party_name_list(block: str) -> List[str]:
    """Split a caption plaintiff/defendant block into individual party names."""
    text = normalize_whitespace(block).strip(" .,;:")
    if not text:
        return []
    # Drop a trailing role label if the block absorbed one.
    text = re.sub(
        r"(?i)(?:,\s*)?\b(?:" + _PARTY_ROLE_DRAFT_LABEL + r")\s*$",
        "",
        text,
    ).strip(" .,;:")
    # Keep ``Name, Inc.`` / ``Name, LLC`` intact while splitting list commas.
    _suffix = (
        r"LLC|LLP|LP|Inc\.?|Corp\.?|Co\.?|Ltd\.?|PLLC|PC|PLC|P\.C\.?"
    )
    protected = re.sub(
        rf",\s*(?=(?:{_suffix})\b)",
        r" «CS» ",
        text,
        flags=re.I,
    )
    parts = re.split(r"\s*,\s*|\s+and\s+", protected, flags=re.I)
    names: List[str] = []
    for part in parts:
        part = part.replace(" «CS» ", ", ").replace("«CS»", ",")
        part = re.sub(r"(?i)^(and|the|a|an)\s+", "", part).strip(" .,;:")
        cleaned = _clean_party_role_identity_name(part)
        if not cleaned:
            continue
        if _normalize_party_role_draft_label(cleaned):
            continue
        if not _plausible_party_role_draft_name(cleaned):
            continue
        # Require a plausible name-shaped token sequence.
        if not _PARTY_ROLE_NAME_FIND_RE.search(cleaned):
            continue
        # Reject bare legal-suffix leftovers from list parsing.
        if re.fullmatch(
            r"(?i)(?:LLC|LLP|LP|Inc\.?|Corp\.?|Co\.?|Ltd\.?|PLLC|PC|PLC|P\.C\.?)",
            cleaned,
        ):
            continue
        if cleaned not in names:
            names.append(cleaned)
    return names


def _unit_looks_like_party_role_caption(unit: str) -> bool:
    """True for caption-style units (not numbered allegation paragraphs)."""
    healed = heal_party_identity_ocr_spaces(unit or "")
    if not healed or re.match(r"^\s*\d+\.\s+\S", healed):
        return False
    if re.search(r"(?i)\b(?:-?\s*against\s*-?|\bv\.?)\b", healed):
        return True
    if re.search(
        r"(?i),\s*(?:plaintiffs?|defendants?|petitioners?|respondents?)\s*[,.]?\s*$",
        healed,
    ):
        return True
    return False


def _discover_caption_party_identities(unit: str) -> List[dict]:
    """
    Parse responsive caption identities/roles generically.

    Supports plaintiff names before Plaintiff, defendant names in an against
    block, multiline caption lists, and OCR-healed role labels. Leading
    court/county/venue/index caption-administration headers are stripped
    before party-block capture so they do not pollute identities.
    """
    without_headers = _strip_leading_caption_admin_headers(unit)
    healed = heal_party_identity_ocr_spaces(
        _strip_caption_horizontal_rules(without_headers)
    )
    healed = normalize_whitespace(healed)
    if not healed or not _unit_looks_like_party_role_caption(healed):
        return []

    found: List[dict] = []

    def add_names(names: Sequence[str], role: Optional[str]) -> None:
        for name in names:
            found.append(
                {
                    "identity": name,
                    "procedural_role": role,
                    "_aliases": [],
                }
            )

    against = re.search(
        r"(?is)"
        r"(?P<plaintiff_block>.+?)\s*,?\s*"
        r"\b(?P<plaintiff_role>plaintiffs?|petitioners?)\b\s*[,.]?\s*"
        r"(?:-+\s*)?(?:against|v\.?)\s*(?:-+\s*)?"
        r"(?P<defendant_block>.+?)\s*,?\s*"
        r"\b(?P<defendant_role>defendants?|respondents?)\b",
        healed,
    )
    if against:
        p_role = _normalize_party_role_draft_label(against.group("plaintiff_role"))
        d_role = _normalize_party_role_draft_label(against.group("defendant_role"))
        add_names(_split_caption_party_name_list(against.group("plaintiff_block")), p_role)
        add_names(
            _split_caption_party_name_list(against.group("defendant_block")), d_role
        )
        return found

    # Single-side caption: names listed before a terminal role label.
    single = re.search(
        r"(?is)^(?P<block>.+?)\s*,?\s*"
        r"\b(?P<role>" + _PARTY_ROLE_DRAFT_LABEL + r")\b\s*[,.]?\s*$",
        healed,
    )
    if single:
        role = _normalize_party_role_draft_label(single.group("role"))
        add_names(_split_caption_party_name_list(single.group("block")), role)
    return found


def _shorthand_resolves_to_canonical(short_name: str, long_name: str) -> bool:
    """
    True when ``short_name`` is an unambiguous shortening of ``long_name``.

    Supports collective/leading-token shorthand, omitted legal suffixes, and
    abbreviated company phrases. Rejects generic-token-only matches.
    """
    short_key = _normalize_party_role_match_text(short_name)
    long_key = _normalize_party_role_match_text(long_name)
    if not short_key or not long_key or short_key == long_key:
        return False
    if len(short_key) > len(long_key):
        return False
    short_tokens = _party_role_identity_tokens(short_name)
    long_tokens = _party_role_identity_tokens(long_name)
    if not short_tokens or len(short_tokens) >= len(long_tokens):
        return False
    if not _party_role_has_distinctive_token(short_tokens):
        return False
    # Contiguous phrase inside the longer identity (incl. omitted suffix cases).
    if _party_role_tokens_contiguous(short_tokens, long_tokens):
        return True
    # Distinctive leading token/phrase: short equals long's leading distinctive
    # span (e.g. role+shorthand collective references).
    long_distinctive = [
        tok for tok in long_tokens if tok not in _PARTY_ROLE_GENERIC_NAME_TOKENS
    ]
    short_distinctive = [
        tok for tok in short_tokens if tok not in _PARTY_ROLE_GENERIC_NAME_TOKENS
    ]
    if short_distinctive and long_distinctive[: len(short_distinctive)] == list(
        short_distinctive
    ):
        return True
    return False


def _merge_party_role_bucket_attrs(target: dict, source: dict) -> None:
    for field in (
        "procedural_role",
        "entity_type",
        "residence_or_ppb",
        "pleaded_role_basis",
    ):
        if not target.get(field) and source.get(field):
            target[field] = source[field]


def _rekey_party_alias_bucket(
    parties: Dict[str, dict],
    alias_to_canon: Dict[str, str],
    alias_key: str,
    canon_key: str,
    canon_identity: str,
) -> None:
    """
    Merge an existing alias-keyed party bucket into the canonical bucket.

    Preserves prior attributes, removes the standalone alias identity, and
    retargets alias map entries that pointed at the alias key.
    """
    if not alias_key or not canon_key or alias_key == canon_key:
        return
    alias_party = parties.pop(alias_key, None)
    existing = parties.get(canon_key)
    if existing is None:
        parties[canon_key] = {
            "identity": canon_identity,
            "procedural_role": (alias_party or {}).get("procedural_role"),
            "entity_type": (alias_party or {}).get("entity_type"),
            "residence_or_ppb": (alias_party or {}).get("residence_or_ppb"),
            "pleaded_role_basis": (alias_party or {}).get("pleaded_role_basis"),
        }
    else:
        if canon_identity:
            existing["identity"] = canon_identity
        if alias_party:
            _merge_party_role_bucket_attrs(existing, alias_party)
    for mapped_alias, mapped_canon in list(alias_to_canon.items()):
        if mapped_canon == alias_key:
            alias_to_canon[mapped_alias] = canon_key
    alias_to_canon[alias_key] = canon_key


def _consolidate_unambiguous_party_shorthands(
    parties: Dict[str, dict],
    alias_to_canon: Dict[str, str],
) -> None:
    """
    Merge shorter standalone identities into longer canonical ones when the
    shorthand correspondence is unique. Does not merge on a common word alone.
    """
    changed = True
    while changed:
        changed = False
        keys = list(parties.keys())
        for short_key in keys:
            if short_key not in parties:
                continue
            short_party = parties[short_key]
            short_name = short_party.get("identity") or ""
            matches = [
                long_key
                for long_key in keys
                if long_key in parties
                and long_key != short_key
                and _shorthand_resolves_to_canonical(
                    short_name, parties[long_key].get("identity") or ""
                )
            ]
            if len(matches) != 1:
                continue
            long_key = matches[0]
            long_identity = parties[long_key].get("identity") or short_name
            _rekey_party_alias_bucket(
                parties,
                alias_to_canon,
                short_key,
                long_key,
                long_identity,
            )
            changed = True


def _clean_party_role_identity_name(raw_name: Any) -> str:
    name = normalize_whitespace(raw_name).strip(" .,;:")
    name = re.sub(r"^(?:the|a|an)\s+", "", name, flags=re.I).strip(" .,;:")
    # Strip a trailing parenthetical defined term; never keep the alias alone.
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip(" .,;:")
    # If a role label was absorbed into the name, peel it off.
    peeled = re.match(
        r"(?i)^(" + _PARTY_ROLE_DRAFT_LABEL + r")\s+(.+)$",
        name,
    )
    if peeled:
        name = peeled.group(2).strip(" .,;:")
        name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip(" .,;:")
    # Heal OCR-fractured identity tokens; already-clean forms are unchanged.
    return heal_party_identity_ocr_spaces(name)


def _discover_party_role_identities_in_unit(unit: str) -> List[dict]:
    """
    Discover party identities from generic numbered pleading allegations.

    Supports role-before-name, role-after-name, entity/residence-only
    allegations, and placeholder identity groups. Does not invent roles.
    Parenthetical defined terms are recorded as aliases; the pre-parenthetical
    identity remains canonical. Caption blocks contribute listed identities.
    """
    healed = heal_party_identity_ocr_spaces(
        _strip_caption_boundary_marker_x(unit)
    )
    found: Dict[str, dict] = {}

    def remember(
        raw_name: Any,
        raw_role: Any = None,
        raw_alias_body: Any = None,
    ) -> None:
        name = _clean_party_role_identity_name(raw_name)
        role = _normalize_party_role_draft_label(raw_role) if raw_role else None
        # Recover role when the raw span still began with a role label.
        if not role:
            leading = re.match(
                r"(?i)^(" + _PARTY_ROLE_DRAFT_LABEL + r")\s+",
                normalize_whitespace(raw_name or ""),
            )
            if leading:
                role = _normalize_party_role_draft_label(leading.group(1))
        if not _plausible_party_role_draft_name(name):
            return
        # Reject grouped referents that are not concrete identities.
        if re.match(
            r"(?i)^(foregoing|said|these|those|above(?:-|\s+)named)\b",
            name,
        ):
            return
        # Never promote a parenthetical alias over the full pleaded identity.
        alias = _clean_party_role_alias(raw_alias_body)
        if alias and _normalize_party_role_match_text(
            alias
        ) == _normalize_party_role_match_text(name):
            alias = None
        key = _normalize_party_role_match_text(name)
        existing = found.get(key)
        if existing is None:
            found[key] = {
                "identity": name,
                "procedural_role": role,
                "_aliases": list(_alias_variants(alias)) if alias else [],
            }
            return
        if role and not existing.get("procedural_role"):
            existing["procedural_role"] = role
        if alias:
            alias_list = existing.setdefault("_aliases", [])
            for variant in _alias_variants(alias):
                if variant not in alias_list:
                    alias_list.append(variant)

    # Caption lists first so multiline against blocks keep every named party.
    caption_items = _discover_caption_party_identities(unit)
    for item in caption_items:
        remember(item.get("identity"), item.get("procedural_role"), None)
    # Caption units are list/role structured; skip allegation parsers that would
    # re-slice ``Name, Inc.`` into a bare suffix before the role label.
    if caption_items:
        return list(found.values())

    for match in _PARTY_ROLE_ALLEGATION_ROLE_BEFORE_RE.finditer(healed):
        remember(match.group("name"), match.group("role"), match.group("alias_body"))
    for match in _PARTY_ROLE_ALLEGATION_ROLE_AFTER_RE.finditer(healed):
        remember(
            match.group("name"),
            match.group("role_comma") or match.group("role_pred"),
            match.group("alias_body"),
        )
    for match in _PARTY_ROLE_ALLEGATION_ENTITY_RE.finditer(healed):
        remember(match.group("name"), None, match.group("alias_body"))
    for match in _PARTY_ROLE_PLACEHOLDER_GROUP_RE.finditer(healed):
        remember(match.group("name"), match.group("role"), None)

    # Legacy role-leading / "name, role" forms remain as a fallback.
    if not found:
        for match in _PARTY_ROLE_DRAFT_RE.finditer(healed):
            groups = match.groupdict()
            remember(
                groups.get("name_leading") or groups.get("name"),
                groups.get("role_leading") or groups.get("role"),
                None,
            )

    return list(found.values())


def _unit_names_party(identity: str, unit: str) -> bool:
    if not identity or not unit:
        return False
    return _party_role_attribute_present(identity, normalize_citation_text(unit))


def _unit_refers_via_alias(alias: str, unit: str) -> bool:
    """
    True when ``alias`` appears as a standalone party reference in ``unit``.

    Rejects matches that are merely a short token embedded inside a longer
    proper name (e.g. alias ``Acme`` inside ``North Acme Holdings LLC``).
    """
    alias_key = _normalize_party_role_match_text(alias)
    if not alias_key or not unit:
        return False
    for match in _PARTY_ROLE_NAME_FIND_RE.finditer(unit):
        raw_name = match.group("name")
        cleaned_key = _normalize_party_role_match_text(
            _clean_party_role_identity_name(raw_name)
        )
        raw_key = _normalize_party_role_match_text(raw_name)
        if cleaned_key == alias_key or raw_key == alias_key:
            return True
        # Article may sit outside the name capture: ``the Company has...``.
        before = unit[: match.start("name")]
        for article in ("the", "a", "an"):
            if alias_key == f"{article} {cleaned_key}" and re.search(
                rf"(?i)\b{article}\s*$", before
            ):
                return True
    return False


def _prefer_healed_party_identity(current: Any, candidate: Any) -> str:
    """Prefer the OCR-healed surface form when consolidating duplicate identities."""
    cur = normalize_whitespace(current)
    cand = normalize_whitespace(candidate)
    cur_healed = heal_party_identity_ocr_spaces(cur)
    cand_healed = heal_party_identity_ocr_spaces(cand)
    if cand and cand_healed == cand and cur_healed != cur:
        return cand
    if cur and cur_healed == cur:
        return cur
    return cand_healed or cur_healed or cand or cur


def _merge_party_role_expected(
    bucket: Dict[str, dict],
    party: dict,
    *,
    alias_to_canon: Optional[Dict[str, str]] = None,
) -> None:
    identity = party.get("identity")
    # Heal before keying so fractured / clean forms share one inventory bucket.
    if identity:
        identity = heal_party_identity_ocr_spaces(normalize_whitespace(identity))
    key = _normalize_party_role_match_text(identity or "")
    if not key:
        return
    # Resolve defined-term aliases to the canonical inventory key before insert.
    if alias_to_canon and key in alias_to_canon:
        key = alias_to_canon[key]
        if key in bucket:
            identity = _prefer_healed_party_identity(
                bucket[key].get("identity"), identity
            )
    existing = bucket.get(key)
    if existing is None:
        bucket[key] = {
            "identity": identity,
            "procedural_role": party.get("procedural_role"),
            "entity_type": party.get("entity_type"),
            "residence_or_ppb": party.get("residence_or_ppb"),
            "pleaded_role_basis": party.get("pleaded_role_basis"),
        }
        return
    existing["identity"] = _prefer_healed_party_identity(
        existing.get("identity"), identity
    )
    for field in (
        "procedural_role",
        "entity_type",
        "residence_or_ppb",
        "pleaded_role_basis",
    ):
        if not existing.get(field) and party.get(field):
            existing[field] = party[field]


def extract_party_role_expected_attributes(evidence_packet: dict) -> List[dict]:
    """
    Deterministically extract evidence-supported party attributes.

    Only attributes present in the exact serialized evidence packet are
    returned. Missing categories are omitted (never invented).
    """
    serialized = _evidence_text_from_packet(evidence_packet)
    parties: Dict[str, dict] = {}
    alias_to_canon: Dict[str, str] = {}
    pending_grouped_basis: Optional[str] = None

    def register_aliases(
        canon_key: str,
        aliases: Sequence[str],
        *,
        canon_identity: Optional[str] = None,
    ) -> None:
        for alias in aliases or []:
            alias_key = _normalize_party_role_match_text(alias)
            if not alias_key or alias_key == canon_key:
                continue
            # Alias-first order: an earlier standalone alias bucket is re-keyed
            # into the canonical identity when the defined-term mapping appears.
            if alias_key in parties and alias_key != canon_key:
                _rekey_party_alias_bucket(
                    parties,
                    alias_to_canon,
                    alias_key,
                    canon_key,
                    canon_identity
                    or (parties.get(canon_key) or {}).get("identity")
                    or alias,
                )
            else:
                alias_to_canon[alias_key] = canon_key

    for unit in _split_party_role_evidence_units(serialized):
        healed = heal_party_identity_ocr_spaces(unit)
        entity_type = _extract_entity_type_from_unit(unit)
        residence = _extract_residence_or_ppb_from_unit(unit)
        pleaded_basis = _extract_pleaded_role_basis_from_unit(unit)

        # Grouped pleaded-role bases apply to known defendant identities without
        # collapsing them into a single synthetic party.
        if _PARTY_ROLE_GROUPED_BASIS_RE.search(healed):
            basis = pleaded_basis or "notice defendant"
            applied = False
            for existing in parties.values():
                role = (existing.get("procedural_role") or "").lower()
                if role == "defendant" or role.endswith("defendant"):
                    _merge_party_role_expected(
                        parties,
                        {
                            "identity": existing.get("identity"),
                            "procedural_role": existing.get("procedural_role"),
                            "pleaded_role_basis": basis,
                        },
                        alias_to_canon=alias_to_canon,
                    )
                    applied = True
            if not applied:
                pending_grouped_basis = basis
            continue

        discovered = _discover_party_role_identities_in_unit(unit)
        if discovered:
            # Attributes attach only to parties named in this allegation.
            named = [
                item
                for item in discovered
                if _unit_names_party(item.get("identity") or "", healed)
            ]
            targets = named or discovered
            share_attrs = len(targets) == 1
            for item in targets:
                aliases = list(item.get("_aliases") or [])
                raw_key = _normalize_party_role_match_text(item.get("identity") or "")
                # Record parenthetical alias -> canonical mapping before inventory
                # insert so alias-only identities resolve on this same pass.
                if aliases and raw_key:
                    register_aliases(
                        raw_key,
                        aliases,
                        canon_identity=item.get("identity"),
                    )
                _merge_party_role_expected(
                    parties,
                    {
                        "identity": item.get("identity"),
                        "procedural_role": item.get("procedural_role"),
                        "entity_type": entity_type if share_attrs else None,
                        "residence_or_ppb": residence if share_attrs else None,
                        "pleaded_role_basis": (
                            pleaded_basis if share_attrs else None
                        ),
                    },
                    alias_to_canon=alias_to_canon,
                )
                canon_key = alias_to_canon.get(raw_key, raw_key)
                if aliases:
                    register_aliases(
                        canon_key,
                        aliases,
                        canon_identity=item.get("identity"),
                    )
                if (
                    pending_grouped_basis
                    and (item.get("procedural_role") or "").lower().endswith(
                        "defendant"
                    )
                ):
                    _merge_party_role_expected(
                        parties,
                        {
                            "identity": item.get("identity"),
                            "procedural_role": item.get("procedural_role"),
                            "pleaded_role_basis": pending_grouped_basis,
                        },
                        alias_to_canon=alias_to_canon,
                    )
            if pending_grouped_basis:
                # Clear once at least one defendant identity exists to receive it.
                if any(
                    (p.get("procedural_role") or "").lower().endswith("defendant")
                    for p in parties.values()
                ):
                    pending_grouped_basis = None
            continue

        # Attribute-bearing follow-on lines that name a known party without a
        # fresh role tag (e.g. residence lines) attach to that party only.
        if not (entity_type or residence or pleaded_basis) or not parties:
            continue
        unit_norm = normalize_citation_text(healed)

        def _unit_refers_to_party(existing: dict) -> bool:
            if _party_role_attribute_present(
                existing.get("identity") or "", unit_norm
            ):
                return True
            existing_key = _normalize_party_role_match_text(
                existing.get("identity") or ""
            )
            for alias_key, canon_key in alias_to_canon.items():
                if canon_key != existing_key:
                    continue
                # Standalone alias reference only; ignore short tokens embedded
                # inside an unrelated longer identity.
                if _unit_refers_via_alias(alias_key, healed):
                    return True
            # Unambiguous caption/canonical shorthand reference in this unit.
            for match in _PARTY_ROLE_NAME_FIND_RE.finditer(healed):
                ref_name = _clean_party_role_identity_name(match.group("name"))
                if not ref_name:
                    continue
                if _shorthand_resolves_to_canonical(
                    ref_name, existing.get("identity") or ""
                ):
                    # Require uniqueness across the inventory.
                    others = [
                        party
                        for party in parties.values()
                        if party is not existing
                        and _shorthand_resolves_to_canonical(
                            ref_name, party.get("identity") or ""
                        )
                    ]
                    if not others:
                        return True
            return False

        matches = [
            existing for existing in parties.values() if _unit_refers_to_party(existing)
        ]
        if len(matches) != 1:
            continue
        existing = matches[0]
        _merge_party_role_expected(
            parties,
            {
                "identity": existing.get("identity"),
                "procedural_role": existing.get("procedural_role"),
                "entity_type": entity_type,
                "residence_or_ppb": residence,
                "pleaded_role_basis": pleaded_basis,
            },
            alias_to_canon=alias_to_canon,
        )

    # Merge caption/shorthand identities into unambiguous longer canonical forms.
    _consolidate_unambiguous_party_shorthands(parties, alias_to_canon)

    # Stable order by identity for deterministic missing-attribute lists.
    ordered = sorted(
        parties.values(),
        key=lambda item: _normalize_party_role_match_text(item.get("identity") or ""),
    )
    return ordered


def _draft_text_for_party_role_completeness(raw_response: Any) -> str:
    payload = _parse_model_payload(raw_response)
    parts: List[str] = [str(payload.get("proposed_answer") or "")]
    props = payload.get("propositions")
    if isinstance(props, list):
        for prop in props:
            if not isinstance(prop, dict):
                continue
            parts.append(str(prop.get("text") or ""))
            parts.append(str(prop.get("source_excerpt") or prop.get("excerpt") or ""))
    return "\n".join(parts)


def find_missing_party_role_attributes(
    raw_response: Any,
    expected_parties: Sequence[dict],
) -> List[dict]:
    """
    Return evidence-supported attributes absent from the draft.

    Deterministic: identical draft + expected inputs yield identical missing lists.
    """
    draft_norm = normalize_citation_text(
        _draft_text_for_party_role_completeness(raw_response)
    )
    missing: List[dict] = []
    for party in expected_parties or []:
        identity = normalize_whitespace(party.get("identity"))
        if not identity:
            continue
        if not _party_role_attribute_present(identity, draft_norm):
            missing.append(
                {
                    "party": identity,
                    "category": "identity",
                    "value": identity,
                }
            )
            # Without identity, still report other supported categories so the
            # repair prompt lists every omission for that party.
        category_fields = (
            ("procedural_role", party.get("procedural_role")),
            ("entity_type", party.get("entity_type")),
            ("residence_or_ppb", party.get("residence_or_ppb")),
            ("pleaded_role_basis", party.get("pleaded_role_basis")),
        )
        for category, value in category_fields:
            text = normalize_whitespace(value)
            if not text:
                continue
            if not _party_role_attribute_present(text, draft_norm):
                missing.append(
                    {
                        "party": identity,
                        "category": category,
                        "value": text,
                    }
                )
    return missing


def build_party_role_repair_prompt(
    *,
    question: str,
    evidence_packet: dict,
    current_draft: Any,
    missing_attributes: Sequence[dict],
) -> str:
    """
    Bounded repair prompt: original question, original evidence, current draft,
    and the deterministic missing-attribute list only.
    """
    draft_payload = _parse_model_payload(current_draft)
    return (
        "Repair the party-role draft for completeness.\n"
        "Preserve all correct content. Add only the missing required attributes "
        "listed below that are supported by the evidence. Do not omit required "
        "party attributes for brevity. Do not invent attributes absent from the "
        "evidence.\n"
        "Return the required JSON object and nothing else.\n\n"
        f"Original question:\n{normalize_whitespace(question)}\n\n"
        f"Evidence packet:\n{_stable_json(evidence_packet)}\n\n"
        f"Current draft:\n{_stable_json(draft_payload)}\n\n"
        f"Missing required attributes:\n{_stable_json(list(missing_attributes))}\n"
    )


def _party_role_completeness_failure(
    *,
    question: str,
    retrieval: Optional[dict],
    missing_attributes: Sequence[dict],
    provider_error: Optional[str] = None,
) -> dict:
    reason = (
        "Party-role drafting completeness failed after one repair retry; "
        "required evidence-supported party attributes remain missing."
    )
    result = _empty_answer_shell(
        status=STATUS_NOT_READY,
        question=question,
        retrieval=retrieval,
        reason=reason,
    )
    result["audit"]["provider_available"] = True
    result["audit"]["party_role_completeness_failed"] = True
    result["audit"]["party_role_repair_attempted"] = True
    result["audit"]["party_role_provider_calls"] = 2
    result["audit"]["missing_party_role_attributes"] = list(missing_attributes)
    result["audit"]["notes"].append(reason)
    if provider_error:
        result["audit"]["provider_error"] = provider_error
    return result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _source_flags_for_hit(hit: Optional[dict]) -> List[str]:
    if not hit:
        return []
    flags = [str(x) for x in (hit.get("classifications") or [])]
    kind = hit.get("assertion_kind")
    if kind:
        flags.append(str(kind))
    linkage = hit.get("case_map_linkage") or {}
    if linkage.get("assertion_kind"):
        flags.append(str(linkage["assertion_kind"]))
    return flags


def _looks_like_case_map_only_claim(prop: dict, hit: Optional[dict]) -> bool:
    rationale = normalize_whitespace(prop.get("rationale")).lower()
    text = normalize_whitespace(prop.get("text")).lower()
    joined = f"{rationale} {text}"
    if "case-map" in joined or "case map" in joined:
        if not hit or not hit.get("page_id"):
            return True
        # Explicit claim that case-map alone proves the point.
        if "case map alone" in joined or "case-map alone" in joined:
            return True
        if "independent proof" in joined and "case" in joined:
            return True
    # No retrieval hit but proposition claims a citation → invalid / map-only.
    if not hit:
        return True
    return False


def _detect_invented_tokens(prop_text: str, evidence_corpus: str) -> List[str]:
    invented: List[str] = []
    corpus_norm = normalize_whitespace(evidence_corpus).lower()
    if not corpus_norm:
        return invented

    for match in _DATE_RE.finditer(prop_text or ""):
        token = normalize_whitespace(match.group(0))
        if token.lower() not in corpus_norm:
            invented.append(f"date:{token}")

    for match in _POLICY_RE.finditer(prop_text or ""):
        token = normalize_whitespace(match.group(0))
        # Skip bare short numbers that are likely NYSCEF refs.
        if token.isdigit() and len(token) <= 4:
            continue
        if token.lower() not in corpus_norm:
            invented.append(f"policy_or_id:{token}")

    for match in _PARTY_LIKE_RE.finditer(prop_text or ""):
        token = normalize_whitespace(match.group(0))
        if token.lower() not in corpus_norm:
            invented.append(f"party:{token}")

    return invented


def _normalize_classification(value: Any) -> str:
    text = normalize_whitespace(value).lower().replace(" ", "_")
    aliases = {
        "verified_fact": "verified_record_fact",
        "fact": "verified_record_fact",
        "allegation": "party_allegation",
        "party_assertion": "party_allegation",
        "legal_argument": "legal_position",
        "argument": "legal_position",
        "position": "legal_position",
    }
    text = aliases.get(text, text)
    if text not in PROPOSITION_CLASSIFICATIONS:
        return "unknown"
    return text


def _documents_pages_reviewed_from_hits(hits: Sequence[dict]) -> List[dict]:
    reviewed = []
    seen = set()
    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        key = (
            hit.get("nyscef_document_number"),
            hit.get("page_id"),
            hit.get("pdf_page"),
        )
        if key in seen:
            continue
        seen.add(key)
        reviewed.append(
            {
                "nyscef_document_number": hit.get("nyscef_document_number"),
                "page_id": hit.get("page_id"),
                "pdf_page": hit.get("pdf_page"),
                "source_filename": hit.get("source_filename"),
                "document_type": hit.get("document_type"),
            }
        )
    reviewed.sort(
        key=lambda item: (
            item.get("nyscef_document_number") is None,
            item.get("nyscef_document_number")
            if item.get("nyscef_document_number") is not None
            else 10**9,
            item.get("pdf_page") or 0,
            item.get("page_id") or "",
        )
    )
    return reviewed


def _default_attorney_review(notes: str = "") -> dict:
    return {
        "requires_attorney_review": True,
        "review_notes": notes
        or (
            "Structured retrieval-grounded draft only. "
            "All legal conclusions are positions/inferences for attorney review."
        ),
        "legal_conclusions_labeled": True,
        "coverage_conclusion": None,
    }


def _empty_answer_shell(
    *,
    status: str,
    question: str,
    retrieval: Optional[dict],
    reason: str,
) -> dict:
    hits = list((retrieval or {}).get("results") or [])
    return {
        "status": status,
        "engine_version": ENGINE_VERSION,
        "question": normalize_whitespace(question),
        "proposed_answer": "",
        "propositions": [],
        "supporting_evidence": [],
        "contrary_evidence": [],
        "unresolved_questions": [],
        "documents_pages_reviewed": _documents_pages_reviewed_from_hits(hits),
        "confidence": 0.0,
        "attorney_review": _default_attorney_review(reason),
        "review_scope": {
            "retrieved_hit_count": len(hits),
            "completeness": "not_established",
            "qualification": (
                "Answer generation did not complete; retrieved evidence is "
                "returned for attorney review without a fabricated answer."
            ),
            "reason": reason,
        },
        "audit": {
            "removed_propositions": [],
            "rejection_reasons": [],
            "duplicate_proposition_ids": [],
            "provider_available": False,
            "notes": [reason],
        },
        "retrieved_evidence": hits,
    }


def validate_attorney_qa_response(
    raw_response: Any,
    *,
    question: str,
    retrieval: Optional[dict],
    documents: Optional[Sequence[dict]] = None,
    case_map: Optional[dict] = None,
) -> dict:
    """
    Deterministic post-generation validation.

    Identical structured model responses yield identical validated output.
    Unsupported propositions are removed from the attorney-facing answer and
    preserved in ``audit.removed_propositions``.
    """
    del case_map  # Case-map is never independent proof; page evidence required.
    payload = _parse_model_payload(raw_response)
    hits = list((retrieval or {}).get("results") or [])
    hit_index = _evidence_index(hits)
    page_lookup = _page_lookup_from_documents(documents)

    evidence_corpus_parts = []
    for hit in hits:
        evidence_corpus_parts.append(str(hit.get("excerpt") or ""))
        page = page_lookup.get(hit.get("page_id") or "")
        if page:
            evidence_corpus_parts.append(str(page.get("text") or ""))
    evidence_corpus = "\n".join(evidence_corpus_parts)

    raw_props = payload.get("propositions")
    if not isinstance(raw_props, list):
        raw_props = []

    kept: List[dict] = []
    removed: List[dict] = []
    rejection_reasons: List[dict] = []
    seen_ids: Dict[str, int] = {}
    duplicate_ids: List[str] = []

    # Stable iteration order: original order, then proposition_id.
    indexed_props: List[Tuple[int, dict]] = []
    for index, prop in enumerate(raw_props):
        if isinstance(prop, dict):
            indexed_props.append((index, prop))
    indexed_props.sort(
        key=lambda item: (
            item[0],
            str(item[1].get("proposition_id") or ""),
        )
    )

    for index, prop in indexed_props:
        prop_id = normalize_whitespace(prop.get("proposition_id")) or f"P{index+1:03d}"
        if prop_id in seen_ids:
            duplicate_ids.append(prop_id)
            rejection_reasons.append(
                {
                    "proposition_id": prop_id,
                    "reason": "duplicate_proposition_id",
                    "detail": f"Duplicate of earlier proposition at index {seen_ids[prop_id]}",
                }
            )
            removed.append(
                {
                    **deepcopy(prop),
                    "proposition_id": prop_id,
                    "removal_reason": "duplicate_proposition_id",
                }
            )
            continue
        seen_ids[prop_id] = index

        text = normalize_whitespace(prop.get("text"))
        classification = _normalize_classification(prop.get("classification"))
        page_id = normalize_whitespace(prop.get("page_id")) or None
        nyscef = _coerce_int(prop.get("nyscef_document_number"))
        pdf_page = _coerce_int(prop.get("pdf_page"))
        excerpt = prop.get("source_excerpt")
        if excerpt is None:
            excerpt = prop.get("excerpt")
        excerpt_text = normalize_whitespace(excerpt)
        confidence = _coerce_confidence(prop.get("confidence"), 0.0)
        rationale = normalize_whitespace(prop.get("rationale"))
        polarity = normalize_whitespace(prop.get("polarity")).lower() or "supporting"
        if polarity not in {"supporting", "contrary", "unresolved"}:
            polarity = "supporting"

        # Unknown with no citation is allowed as unresolved.
        if classification == "unknown" and not page_id and not excerpt_text:
            kept.append(
                {
                    "proposition_id": prop_id,
                    "text": text or "Unresolved on the supplied record.",
                    "classification": "unknown",
                    "nyscef_document_number": None,
                    "page_id": None,
                    "pdf_page": None,
                    "source_excerpt": "",
                    "confidence": confidence,
                    "rationale": rationale or "Not established by retrieved evidence.",
                    "polarity": "unresolved",
                }
            )
            continue

        hit = hit_index.get(page_id) if page_id else None
        if hit and not _hit_matches_citation(hit, page_id, nyscef, pdf_page):
            # Try to locate a hit that matches all three provenance fields.
            hit = None
            for candidate in hits:
                if _hit_matches_citation(candidate, page_id, nyscef, pdf_page):
                    hit = candidate
                    break

        removal_reason = None
        detail = ""

        if not page_id or nyscef is None or pdf_page is None:
            removal_reason = "missing_provenance"
            detail = "Substantive proposition requires NYSCEF, page_id, and pdf_page."
        elif page_id not in hit_index and page_id not in page_lookup:
            removal_reason = "citation_not_in_retrieval_context"
            detail = f"page_id {page_id} is not present in retrieval context."
        elif hit is None and page_id not in page_lookup:
            removal_reason = "hallucinated_citation"
            detail = "Cited page/NYSCEF/PDF page combination not in retrieval context."
        elif hit is not None and not _hit_matches_citation(hit, page_id, nyscef, pdf_page):
            removal_reason = "provenance_mismatch"
            detail = "NYSCEF / page_id / pdf_page do not match a retrieval hit."
        elif _looks_like_case_map_only_claim(prop, hit):
            removal_reason = "case_map_only_not_proof"
            detail = (
                "Case-map inferences/review candidates are retrieval signals "
                "only and cannot independently prove a proposition."
            )

        page_text = ""
        if page_id and page_id in page_lookup:
            page_text = page_lookup[page_id].get("text") or ""
        elif hit is not None:
            page_text = hit.get("excerpt") or ""

        if removal_reason is None:
            if not excerpt_text:
                removal_reason = "missing_excerpt"
                detail = "Substantive proposition requires a short source excerpt."
            elif page_text and not excerpt_occurs_on_page(excerpt_text, page_text):
                # Also accept exact match against the retrieval excerpt window.
                hit_excerpt = (hit or {}).get("excerpt") or ""
                if not excerpt_occurs_on_page(excerpt_text, hit_excerpt):
                    removal_reason = "excerpt_mismatch"
                    detail = (
                        "source_excerpt does not occur on the cited page "
                        "(whitespace/OCR-normalized; ellipsis segments checked "
                        "independently)."
                    )

        if removal_reason is None and classification == "verified_record_fact":
            flags = {f.lower() for f in _source_flags_for_hit(hit)}
            if flags & ALLEATION_SOURCE_KINDS or "allegation" in flags:
                removal_reason = "allegation_to_fact_promotion"
                detail = (
                    "Party allegation cannot be promoted to verified_record_fact."
                )
            elif flags & LEGAL_POSITION_SOURCE_KINDS or "legal_position" in flags:
                removal_reason = "legal_position_to_fact_promotion"
                detail = (
                    "Legal position cannot be promoted to verified_record_fact."
                )
            elif "inference" in flags and "verified_fact" not in flags:
                # Soft: inference-tagged sources cannot become verified facts.
                removal_reason = "inference_to_fact_promotion"
                detail = "Inference-tagged source cannot become verified_record_fact."

        if removal_reason is None and text:
            invented = _detect_invented_tokens(text, evidence_corpus)
            if invented:
                removal_reason = "invented_content"
                detail = "Invented tokens not present in retrieval evidence: " + ", ".join(
                    invented[:8]
                )

        if removal_reason is None and not text:
            removal_reason = "empty_proposition"
            detail = "Proposition text is empty."

        # If citation is in page_lookup but not retrieval hits, still reject —
        # model must be constrained to supplied retrieval evidence.
        if removal_reason is None and page_id and page_id not in hit_index:
            removal_reason = "citation_not_in_retrieval_context"
            detail = f"page_id {page_id} was not returned by retrieval."

        if removal_reason is not None:
            rejection_reasons.append(
                {
                    "proposition_id": prop_id,
                    "reason": removal_reason,
                    "detail": detail,
                }
            )
            removed.append(
                {
                    **deepcopy(prop),
                    "proposition_id": prop_id,
                    "classification": classification,
                    "removal_reason": removal_reason,
                    "removal_detail": detail,
                }
            )
            continue

        # Align provenance to the authoritative hit when present.
        final_nyscef = nyscef
        final_pdf = pdf_page
        final_page_id = page_id
        if hit is not None:
            final_nyscef = hit.get("nyscef_document_number")
            final_pdf = hit.get("pdf_page")
            final_page_id = hit.get("page_id")

        kept.append(
            {
                "proposition_id": prop_id,
                "text": text,
                "classification": classification,
                "nyscef_document_number": final_nyscef,
                "page_id": final_page_id,
                "pdf_page": final_pdf,
                "source_excerpt": excerpt_text,
                "confidence": confidence,
                "rationale": rationale,
                "polarity": polarity,
            }
        )

    supporting = [p for p in kept if p.get("polarity") == "supporting"]
    contrary = [p for p in kept if p.get("polarity") == "contrary"]
    unresolved_props = [p for p in kept if p.get("polarity") == "unresolved"]

    # Merge model-provided unresolved questions with unknown props.
    unresolved_questions = []
    raw_unresolved = payload.get("unresolved_questions")
    if isinstance(raw_unresolved, list):
        for item in raw_unresolved:
            if isinstance(item, str) and normalize_whitespace(item):
                unresolved_questions.append(normalize_whitespace(item))
            elif isinstance(item, dict):
                q = normalize_whitespace(item.get("question") or item.get("text"))
                if q:
                    unresolved_questions.append(q)
    for prop in unresolved_props:
        if prop.get("text") and prop["text"] not in unresolved_questions:
            unresolved_questions.append(prop["text"])

    # Supporting / contrary evidence lists: prefer validated props, else model lists filtered.
    def _filter_evidence_list(raw_list, polarity_label):
        out = []
        if not isinstance(raw_list, list):
            return out
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            page_id = normalize_whitespace(item.get("page_id")) or None
            if page_id and page_id not in hit_index:
                continue
            out.append(
                {
                    "page_id": page_id,
                    "nyscef_document_number": _coerce_int(
                        item.get("nyscef_document_number")
                    ),
                    "pdf_page": _coerce_int(item.get("pdf_page")),
                    "excerpt": normalize_whitespace(
                        item.get("excerpt") or item.get("source_excerpt")
                    ),
                    "note": normalize_whitespace(item.get("note") or item.get("text")),
                    "polarity": polarity_label,
                }
            )
        return out

    supporting_evidence = [
        {
            "proposition_id": p["proposition_id"],
            "page_id": p["page_id"],
            "nyscef_document_number": p["nyscef_document_number"],
            "pdf_page": p["pdf_page"],
            "excerpt": p["source_excerpt"],
            "classification": p["classification"],
        }
        for p in supporting
    ] or _filter_evidence_list(payload.get("supporting_evidence"), "supporting")

    contrary_evidence = [
        {
            "proposition_id": p["proposition_id"],
            "page_id": p["page_id"],
            "nyscef_document_number": p["nyscef_document_number"],
            "pdf_page": p["pdf_page"],
            "excerpt": p["source_excerpt"],
            "classification": p["classification"],
        }
        for p in contrary
    ] or _filter_evidence_list(payload.get("contrary_evidence"), "contrary")

    reviewed = payload.get("documents_pages_reviewed")
    if not isinstance(reviewed, list) or not reviewed:
        reviewed = _documents_pages_reviewed_from_hits(hits)
    else:
        cleaned_reviewed = []
        for item in reviewed:
            if not isinstance(item, dict):
                continue
            page_id = normalize_whitespace(item.get("page_id")) or None
            if page_id and page_id not in hit_index and page_id not in page_lookup:
                continue
            cleaned_reviewed.append(
                {
                    "nyscef_document_number": _coerce_int(
                        item.get("nyscef_document_number")
                    ),
                    "page_id": page_id,
                    "pdf_page": _coerce_int(item.get("pdf_page")),
                    "source_filename": item.get("source_filename"),
                    "document_type": item.get("document_type"),
                }
            )
        reviewed = cleaned_reviewed or _documents_pages_reviewed_from_hits(hits)

    attorney_review = payload.get("attorney_review")
    if not isinstance(attorney_review, dict):
        attorney_review = _default_attorney_review()
    else:
        attorney_review = {
            "requires_attorney_review": True,
            "review_notes": normalize_whitespace(
                attorney_review.get("review_notes")
            )
            or _default_attorney_review()["review_notes"],
            "legal_conclusions_labeled": bool(
                attorney_review.get("legal_conclusions_labeled", True)
            ),
            "coverage_conclusion": attorney_review.get("coverage_conclusion"),
        }

    review_scope = payload.get("review_scope")
    if not isinstance(review_scope, dict):
        review_scope = {}
    review_scope = {
        "retrieved_hit_count": len(hits),
        "documents_pages_reviewed_count": len(reviewed),
        "completeness": review_scope.get("completeness") or "not_established",
        "qualification": normalize_whitespace(review_scope.get("qualification"))
        or (
            "Findings are limited to the supplied retrieval hits; absence or "
            "completeness is not established beyond that scope."
        ),
        "explanation": normalize_whitespace(review_scope.get("explanation")),
    }

    proposed = normalize_whitespace(payload.get("proposed_answer"))
    if not proposed and kept:
        proposed = kept[0]["text"]
    if removed and not proposed:
        proposed = (
            "No validated propositions remained after citation review; "
            "see unresolved questions and audit."
        )

    overall_confidence = _coerce_confidence(payload.get("confidence"), 0.0)
    if kept:
        overall_confidence = min(
            overall_confidence or 0.0,
            round(
                sum(p["confidence"] for p in kept) / max(len(kept), 1),
                6,
            )
            if overall_confidence == 0
            else overall_confidence,
        )

    status = STATUS_READY if kept or proposed else STATUS_NOT_READY
    if not hits and not kept:
        status = STATUS_NOT_READY

    return {
        "status": status,
        "engine_version": ENGINE_VERSION,
        "question": normalize_whitespace(question),
        "proposed_answer": proposed,
        "propositions": kept,
        "supporting_evidence": supporting_evidence,
        "contrary_evidence": contrary_evidence,
        "unresolved_questions": unresolved_questions,
        "documents_pages_reviewed": reviewed,
        "confidence": overall_confidence,
        "attorney_review": attorney_review,
        "review_scope": review_scope,
        "audit": {
            "removed_propositions": removed,
            "rejection_reasons": rejection_reasons,
            "duplicate_proposition_ids": sorted(set(duplicate_ids)),
            "provider_available": True,
            "notes": [],
            "validation_deterministic": True,
        },
        "retrieved_evidence": hits,
    }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def answer_attorney_record_question(
    question: str,
    retrieval: Optional[dict],
    *,
    documents: Optional[Sequence[dict]] = None,
    case_map: Optional[dict] = None,
    exhibit_context: Optional[Any] = None,
    allowed_sources: Optional[Sequence[str]] = None,
    model_call: Optional[ModelCall] = None,
    system_prompt: Optional[str] = None,
) -> dict:
    """
    Produce a structured, citation-bounded attorney-review answer.

    If no model provider is available, returns structured NOT READY with the
    retrieved evidence packet (does not fabricate an answer).

    Party-and-role questions receive a final completeness instruction and, when
    evidence-supported attributes are omitted, exactly one bounded repair retry.
    """
    question_text = normalize_whitespace(question)
    retrieval = retrieval or {"query": question_text, "results": []}

    provider = resolve_model_provider(model_call)
    if provider is None:
        result = _empty_answer_shell(
            status=STATUS_NOT_READY,
            question=question_text,
            retrieval=retrieval,
            reason=(
                "Model/provider capability unavailable. "
                "Configure OPENAI_API_KEY, LEGALAI_MODEL_ENDPOINT, or pass "
                "model_call; retrieved evidence is attached for attorney review."
            ),
        )
        result["audit"]["provider_available"] = False
        return result

    evidence_packet = build_evidence_packet(
        question_text,
        retrieval,
        case_map=case_map,
        exhibit_context=exhibit_context,
        allowed_sources=allowed_sources,
    )
    party_role_intent = detect_party_role_question_intent(question_text)
    user_prompt = build_user_prompt(
        evidence_packet,
        party_role_completeness=party_role_intent,
    )
    active_system = system_prompt or RECORD_ANALYSIS_SYSTEM_PROMPT

    provider_calls = 0
    try:
        raw = provider(active_system, user_prompt)
        provider_calls = 1
    except Exception as exc:  # noqa: BLE001 — surface as NOT READY, never fabricate
        result = _empty_answer_shell(
            status=STATUS_NOT_READY,
            question=question_text,
            retrieval=retrieval,
            reason=f"Model provider call failed: {type(exc).__name__}: {exc}",
        )
        result["audit"]["provider_available"] = True
        result["audit"]["provider_error"] = str(exc)
        result["audit"]["party_role_provider_calls"] = 0
        return result

    repair_attempted = False
    if party_role_intent:
        expected = extract_party_role_expected_attributes(evidence_packet)
        missing = find_missing_party_role_attributes(raw, expected)
        if missing:
            repair_attempted = True
            repair_prompt = build_party_role_repair_prompt(
                question=question_text,
                evidence_packet=evidence_packet,
                current_draft=raw,
                missing_attributes=missing,
            )
            try:
                raw = provider(active_system, repair_prompt)
                provider_calls = 2
            except Exception as exc:  # noqa: BLE001
                return _party_role_completeness_failure(
                    question=question_text,
                    retrieval=retrieval,
                    missing_attributes=missing,
                    provider_error=f"{type(exc).__name__}: {exc}",
                )
            missing_after = find_missing_party_role_attributes(raw, expected)
            if missing_after:
                return _party_role_completeness_failure(
                    question=question_text,
                    retrieval=retrieval,
                    missing_attributes=missing_after,
                )

    validated = validate_attorney_qa_response(
        raw,
        question=question_text,
        retrieval=retrieval,
        documents=documents,
        case_map=case_map,
    )
    validated["audit"]["provider_available"] = True
    validated["evidence_packet_hit_count"] = evidence_packet["retrieval_hit_count"]
    if party_role_intent:
        validated["audit"]["party_role_provider_calls"] = provider_calls
        validated["audit"]["party_role_repair_attempted"] = repair_attempted
    return validated


def build_retrieval_grounded_qa(
    summary: Optional[dict],
    documents: Optional[Sequence[dict]],
    *,
    question: str,
    retrieval: Optional[dict] = None,
    case_map: Optional[dict] = None,
    exhibit_context: Optional[Any] = None,
    allowed_sources: Optional[Sequence[str]] = None,
    model_call: Optional[ModelCall] = None,
) -> dict:
    """Bridge used by matter_builder attorney-work-product orchestration."""
    del summary  # Reserved for future posture-aware prompting.
    return answer_attorney_record_question(
        question,
        retrieval,
        documents=documents,
        case_map=case_map,
        exhibit_context=exhibit_context,
        allowed_sources=allowed_sources,
        model_call=model_call,
    )
