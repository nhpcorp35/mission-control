"""Transform FastAPI OpenAPI 3.1 schemas for ChatGPT Custom GPT Actions.

The Custom GPT Actions importer is OpenAPI 3.0-oriented and rejects several
constructs that FastAPI / Pydantic emit under OpenAPI 3.1. When parsing fails,
the editor often reports a misleading ``Could not find a valid URL in
`servers``` error even when ``servers`` is present and valid.

This module produces a documentation-only Actions-compatible view. It does not
change runtime request or response handling.
"""

from __future__ import annotations

import copy
from typing import Any

ACTIONS_OPENAPI_VERSION = "3.0.3"


def build_actions_openapi(schema: dict[str, Any]) -> dict[str, Any]:
    """Return an OpenAPI document shaped for Custom GPT Actions import."""
    actions = copy.deepcopy(schema)
    actions["openapi"] = ACTIONS_OPENAPI_VERSION
    actions["servers"] = _normalized_servers(actions.get("servers"))
    _sanitize_node(actions)
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
    """Convert OAS 3.1 ``anyOf[T, null]`` into OAS 3.0 ``nullable`` forms."""
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
        # OpenAPI 3.0 cannot attach nullable beside a bare $ref without allOf;
        # keep the ref only (field remains optional in the parent object).
        return rewritten

    if "$ref" in rewritten:
        # Drop sibling keywords that conflict with $ref under OAS 3.0.
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
