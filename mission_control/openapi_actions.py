"""Transform FastAPI OpenAPI 3.1 schemas for ChatGPT Custom GPT Actions.

The Custom GPT Actions importer requires top-level ``openapi`` to be
``3.1.0`` or ``3.1.1``, and still rejects several FastAPI / Pydantic
constructs (long descriptions, inline ``additionalProperties`` maps,
``$ref``+composition siblings, empty ``items``, unconstrained title-only
schemas). When parsing fails, the editor often reports a misleading
``Could not find a valid URL in `servers``` error even when ``servers``
is present and valid.

This module produces a documentation-only Actions-compatible view. It does not
change runtime request or response handling.
"""

from __future__ import annotations

import copy
from typing import Any

# ChatGPT Actions pydantic validation: Input should be '3.1.1' or '3.1.0'.
# Declaring 3.0.x (even with otherwise-valid docs) fails import after ops load.
ACTIONS_OPENAPI_VERSION = "3.1.0"

# Custom GPT Actions rejects operation descriptions at or above this length.
MAX_OPERATION_DESCRIPTION_LENGTH = 300

HEALTH_RESPONSE_SCHEMA_NAME = "HealthResponse"

# Curated Actions descriptions keep meaning under the importer limit.
# Keys are operationId values from the generated FastAPI schema.
_ACTIONS_OPERATION_DESCRIPTIONS: dict[str, str] = {
    "submit_run": (
        "Validate an execute-mode mission and queue it for asynchronous "
        "execution in an isolated workspace. Only one Cursor execution is "
        "active at a time; additional runs wait in FIFO order. Poll "
        "GET /runs/{run_id} for status. Run records persist in SQLite across "
        "restarts."
    ),
    "submit_and_wait": (
        "Queue YAML then wait until terminal (completed/failed/timed_out/"
        "cancelled) or timeout. Returns monitoring fields and resumable "
        "cursor. Wait expiry does not mutate the run. Resume with "
        "wait_for_run + cursor."
    ),
    "get_run": (
        "Return status, output, error, commit SHA, summary, and structured "
        "result for a run from POST /runs. Prefer summary, result.persistence, "
        "and commit_sha over agent stdout/stderr for persistence claims. "
        "Failed/completed runs stay in the SQLite registry; retried_from links "
        "retries."
    ),
    "retry_run": (
        "Create a new async run from the stored mission YAML of a failed run. "
        "Source run is unchanged. New run gets a fresh run_id and "
        "retried_from. Only failed status may be retried; other statuses "
        "return 409."
    ),
    "wait_for_run": (
        "Wait until completed/failed/timed_out/cancelled or timeout_seconds. "
        "Returns heartbeat_health, monitoring_history, and resumable cursor. "
        "Optional cursor resumes history. Does not mutate run state."
    ),
    "run_repository_command": (
        "Run an allowlisted repository command in an ephemeral checkout. "
        "Typed fields: repository, ref, argv, working_directory, timeout, "
        "allowed_env_names. Persistence is always none. Sensitive argv "
        "values are redacted."
    ),
}


def build_actions_openapi(schema: dict[str, Any]) -> dict[str, Any]:
    """Return an OpenAPI document shaped for Custom GPT Actions import."""
    actions = copy.deepcopy(schema)
    actions["openapi"] = ACTIONS_OPENAPI_VERSION
    actions["servers"] = _normalized_servers(actions.get("servers"))
    _sanitize_node(actions)
    _shorten_operation_descriptions(actions)
    _normalize_health_response_schema(actions)
    return actions


def _normalized_servers(servers: Any) -> list[dict[str, str]]:
    """Keep exactly one absolute HTTPS server URL when possible."""
    if not isinstance(servers, list):
        return []
    https_urls: list[dict[str, str]] = []
    for entry in servers:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if isinstance(url, str) and url.startswith("https://"):
            https_urls.append({"url": url.rstrip("/")})
    if not https_urls:
        return []
    # Prefer the first absolute HTTPS URL; Actions wants a single clear target.
    return [https_urls[0]]


def _shorten_operation_descriptions(actions: dict[str, Any]) -> None:
    """Ensure every operation description is under the Actions length limit."""
    paths = actions.get("paths")
    if not isinstance(paths, dict):
        return
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for method_obj in path_item.values():
            if not isinstance(method_obj, dict):
                continue
            if "operationId" not in method_obj and "responses" not in method_obj:
                continue
            operation_id = method_obj.get("operationId")
            if (
                isinstance(operation_id, str)
                and operation_id in _ACTIONS_OPERATION_DESCRIPTIONS
            ):
                method_obj["description"] = _ACTIONS_OPERATION_DESCRIPTIONS[
                    operation_id
                ]
            description = method_obj.get("description")
            if isinstance(description, str):
                method_obj["description"] = _clamp_description(description)


def _clamp_description(description: str) -> str:
    """Truncate a description to fewer than MAX_OPERATION_DESCRIPTION_LENGTH."""
    limit = MAX_OPERATION_DESCRIPTION_LENGTH
    if len(description) < limit:
        return description
    # Prefer the last complete sentence that fits.
    truncated = description[: limit - 1]
    sentence_end = max(
        truncated.rfind(". "),
        truncated.rfind("! "),
        truncated.rfind("? "),
    )
    if sentence_end >= limit // 3:
        return description[: sentence_end + 1].rstrip()
    # Otherwise break on a word boundary and mark truncation.
    word_end = truncated.rfind(" ")
    if word_end >= limit // 3:
        return truncated[:word_end].rstrip() + "…"
    return truncated.rstrip() + "…"


def _normalize_health_response_schema(actions: dict[str, Any]) -> None:
    """Replace the inline /health response schema with a named component.

    ChatGPT Actions rejects the FastAPI-generated inline
    ``additionalProperties: {type: string}`` object for ``GET /health``.
    A small named object schema with an explicit ``status`` property imports
    cleanly and matches the runtime ``{\"status\": \"ok\"}`` payload.
    """
    paths = actions.get("paths")
    if not isinstance(paths, dict):
        return
    health = paths.get("/health")
    if not isinstance(health, dict):
        return
    get_op = health.get("get")
    if not isinstance(get_op, dict):
        return
    responses = get_op.get("responses")
    if not isinstance(responses, dict):
        return
    ok_response = responses.get("200")
    if not isinstance(ok_response, dict):
        return
    content = ok_response.setdefault("content", {})
    if not isinstance(content, dict):
        return
    json_media = content.setdefault("application/json", {})
    if not isinstance(json_media, dict):
        return

    components = actions.setdefault("components", {})
    if not isinstance(components, dict):
        return
    schemas = components.setdefault("schemas", {})
    if not isinstance(schemas, dict):
        return
    schemas[HEALTH_RESPONSE_SCHEMA_NAME] = {
        "type": "object",
        "title": HEALTH_RESPONSE_SCHEMA_NAME,
        "properties": {
            "status": {
                "type": "string",
                "title": "Status",
                "description": "Liveness indicator; typically \"ok\".",
            }
        },
        "required": ["status"],
    }
    json_media["schema"] = {
        "$ref": f"#/components/schemas/{HEALTH_RESPONSE_SCHEMA_NAME}"
    }


def _sanitize_node(node: Any) -> None:
    """Recursively rewrite importer-sensitive schema constructs in place."""
    if isinstance(node, list):
        for item in node:
            _sanitize_node(item)
        return
    if not isinstance(node, dict):
        return

    # Operation / media schemas that combine $ref with oneOf/anyOf siblings
    # (allowed as keywords beside $ref in OAS 3.1; rejected by Actions).
    if "$ref" in node and ("oneOf" in node or "anyOf" in node):
        sibling_ref = node["$ref"]
        composition = node.get("oneOf") or node.get("anyOf")
        if isinstance(composition, list) and composition:
            # Prefer an explicit composition member; fall back to the sibling ref.
            replacement = copy.deepcopy(composition[0])
            preserved = {
                key: value
                for key, value in node.items()
                if key
                not in {
                    "$ref",
                    "oneOf",
                    "anyOf",
                    "allOf",
                    "title",
                    "description",
                    "examples",
                    "example",
                }
            }
            node.clear()
            if isinstance(replacement, dict):
                node.update(replacement)
            else:
                node["$ref"] = sibling_ref
            for key, value in preserved.items():
                if key not in node:
                    node[key] = value
        else:
            node.pop("oneOf", None)
            node.pop("anyOf", None)

    if "anyOf" in node and isinstance(node["anyOf"], list):
        rewritten = _rewrite_nullable_any_of(node)
        if rewritten is not None:
            node.clear()
            node.update(rewritten)
        else:
            # Non-null unions are also fragile in Actions; keep the first branch.
            branches = node["anyOf"]
            if branches:
                replacement = copy.deepcopy(branches[0])
                extras = {
                    key: value
                    for key, value in node.items()
                    if key not in {"anyOf", "oneOf", "allOf"}
                }
                node.clear()
                if isinstance(replacement, dict):
                    node.update(replacement)
                    for key, value in extras.items():
                        if key not in node:
                            node[key] = value
                else:
                    node.update(extras)

    if "oneOf" in node and isinstance(node["oneOf"], list):
        # Actions support for oneOf is unreliable; collapse to the first branch.
        branches = node["oneOf"]
        if branches:
            replacement = copy.deepcopy(branches[0])
            extras = {
                key: value
                for key, value in node.items()
                if key not in {"oneOf", "anyOf", "allOf"}
            }
            node.clear()
            if isinstance(replacement, dict):
                node.update(replacement)
                for key, value in extras.items():
                    if key not in node:
                        node[key] = value
            else:
                node.update(extras)

    if "items" in node and node["items"] == {}:
        # Empty item schemas are rejected; structured deliverables are objects
        # (string entries are also accepted at runtime as free-form objects).
        node["items"] = {"type": "object"}

    # Unconstrained property schemas (title-only) confuse the importer.
    if _is_unconstrained_schema(node):
        node.setdefault("type", "object")

    for key, value in list(node.items()):
        if key in {"example", "examples"}:
            continue
        _sanitize_node(value)


def _rewrite_nullable_any_of(node: dict[str, Any]) -> dict[str, Any] | None:
    """Convert ``anyOf[T, null]`` into Actions-friendly ``nullable`` forms.

    FastAPI emits JSON Schema null unions. The Actions importer is more
    reliable with ``nullable: true`` on the non-null branch even when the
    document version is OpenAPI 3.1.0.
    """
    options = node.get("anyOf")
    if not isinstance(options, list) or len(options) != 2:
        return None

    null_index = None
    for index, option in enumerate(options):
        if isinstance(option, dict) and option.get("type") == "null":
            null_index = index
            break
    if null_index is None:
        return None

    other = options[1 - null_index]
    if not isinstance(other, dict):
        return None

    extras = {
        key: value
        for key, value in node.items()
        if key not in {"anyOf", "oneOf", "allOf"}
    }
    rewritten = copy.deepcopy(other)
    rewritten.update(extras)

    if "$ref" in rewritten and len(rewritten) == 1:
        # Cannot attach nullable beside a bare $ref without allOf; keep the
        # ref only (field remains optional in the parent object).
        return rewritten

    if "$ref" in rewritten:
        # Drop sibling keywords that conflict with $ref in the importer.
        return {"$ref": rewritten["$ref"]}

    rewritten["nullable"] = True
    return rewritten


def _is_unconstrained_schema(node: dict[str, Any]) -> bool:
    """True when a Schema Object has no type/composition/$ref constraint."""
    if not node:
        return False
    schema_keys = {
        "type",
        "$ref",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "properties",
        "items",
        "additionalProperties",
        "enum",
        "const",
        "format",
        "nullable",
    }
    if any(key in node for key in schema_keys):
        return False
    # Title/description-only dicts used as schemas (e.g. ValidationError.input).
    metadata_only = set(node) <= {
        "title",
        "description",
        "default",
        "example",
        "examples",
        "readOnly",
        "writeOnly",
        "deprecated",
    }
    return metadata_only and bool(node)
