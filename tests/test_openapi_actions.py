"""Regression tests for Custom GPT Actions OpenAPI compatibility."""

from __future__ import annotations

import unittest
from typing import Any

from fastapi.testclient import TestClient

from app import api as api_module
from app.api import app
from mission_control.openapi_actions import (
    ACTIONS_OPENAPI_VERSION,
    HEALTH_RESPONSE_SCHEMA_NAME,
    MAX_OPERATION_DESCRIPTION_LENGTH,
    build_actions_openapi,
)

REQUIRED_OPERATION_IDS = {
    "submit_run",
    "get_run",
    "wait_for_run",
    "submit_and_wait",
}

PRODUCTION_HTTPS = "https://mission-control-production-76ff.up.railway.app"


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _operation_ids(schema: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for path_item in schema.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for method_obj in path_item.values():
            if isinstance(method_obj, dict) and method_obj.get("operationId"):
                found.add(method_obj["operationId"])
    return found


class TestOpenApiActionsCompatibility(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = app.openapi()
        self.actions = build_actions_openapi(self.raw)

    def test_actions_schema_uses_openapi_31_version_string(self) -> None:
        # Regression: ChatGPT Actions rejects openapi 3.0.x with
        # ('openapi',): Input should be '3.1.1' or '3.1.0'
        # even when operations otherwise import successfully.
        self.assertEqual(self.actions["openapi"], ACTIONS_OPENAPI_VERSION)
        self.assertIn(self.actions["openapi"], {"3.1.0", "3.1.1"})
        self.assertNotIn(
            self.actions["openapi"],
            {"3.0.0", "3.0.1", "3.0.2", "3.0.3"},
        )
        # Only the document root may declare openapi; nested copies confuse
        # the importer into re-validating a child as an OpenAPI document.
        openapi_nodes = [
            node for node in _walk(self.actions) if "openapi" in node
        ]
        self.assertEqual(len(openapi_nodes), 1)
        self.assertIs(openapi_nodes[0], self.actions)

    def test_actions_schema_has_exactly_one_https_server(self) -> None:
        servers = self.actions.get("servers")
        self.assertEqual(servers, [{"url": PRODUCTION_HTTPS}])
        self.assertEqual(servers, [{"url": api_module.PRODUCTION_SERVER_URL}])

    def test_actions_schema_preserves_required_operation_ids(self) -> None:
        ids = _operation_ids(self.actions)
        self.assertTrue(REQUIRED_OPERATION_IDS.issubset(ids))

    def test_actions_schema_preserves_bearer_auth(self) -> None:
        schemes = self.actions["components"]["securitySchemes"]
        self.assertIn("HTTPBearer", schemes)
        bearer = schemes["HTTPBearer"]
        self.assertEqual(bearer.get("type"), "http")
        self.assertEqual(bearer.get("scheme"), "bearer")

        # Protected run operations must still declare bearer security.
        paths = self.actions["paths"]
        self.assertEqual(
            paths["/runs"]["post"]["security"],
            [{"HTTPBearer": []}],
        )
        self.assertEqual(
            paths["/runs/{run_id}"]["get"]["security"],
            [{"HTTPBearer": []}],
        )
        self.assertEqual(
            paths["/runs/{run_id}/wait"]["post"]["security"],
            [{"HTTPBearer": []}],
        )
        self.assertEqual(
            paths["/runs/submit-and-wait"]["post"]["security"],
            [{"HTTPBearer": []}],
        )

    def test_actions_schema_avoids_nullable_any_of_null(self) -> None:
        for node in _walk(self.actions):
            any_of = node.get("anyOf")
            if not isinstance(any_of, list):
                continue
            types = [
                option.get("type")
                for option in any_of
                if isinstance(option, dict)
            ]
            self.assertNotIn(
                "null",
                types,
                msg=f"nullable anyOf with null remains: {node}",
            )

    def test_actions_schema_avoids_ref_composition_siblings(self) -> None:
        for node in _walk(self.actions):
            if "$ref" not in node:
                continue
            self.assertNotIn("oneOf", node, msg=node)
            self.assertNotIn("anyOf", node, msg=node)
            self.assertNotIn("allOf", node, msg=node)

    def test_actions_schema_avoids_one_of(self) -> None:
        for node in _walk(self.actions):
            self.assertNotIn("oneOf", node, msg=node)

    def test_actions_schema_avoids_empty_item_schemas(self) -> None:
        for node in _walk(self.actions):
            if "items" in node:
                self.assertNotEqual(node["items"], {}, msg=node)

    def test_actions_schema_avoids_title_only_unconstrained_schemas(self) -> None:
        # ValidationError.input is title-only in the FastAPI 3.1 schema.
        raw_input = (
            self.raw["components"]["schemas"]["ValidationError"]["properties"][
                "input"
            ]
        )
        self.assertEqual(set(raw_input), {"title"})

        actions_input = self.actions["components"]["schemas"]["ValidationError"][
            "properties"
        ]["input"]
        self.assertIn("type", actions_input)

    def test_submit_and_wait_response_schema_has_no_ref_one_of_siblings(
        self,
    ) -> None:
        raw_schema = self.raw["paths"]["/runs/submit-and-wait"]["post"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]
        self.assertIn("$ref", raw_schema)
        self.assertIn("oneOf", raw_schema)

        actions_schema = self.actions["paths"]["/runs/submit-and-wait"]["post"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]
        self.assertNotIn("oneOf", actions_schema)
        self.assertTrue(
            "$ref" in actions_schema or actions_schema.get("type") == "object"
        )

    def test_nullable_fields_use_actions_nullable_form(self) -> None:
        error = self.actions["components"]["schemas"]["RunResponse"][
            "properties"
        ]["error"]
        self.assertEqual(error.get("type"), "string")
        self.assertTrue(error.get("nullable"))
        self.assertNotIn("anyOf", error)

    def test_structured_deliverables_items_are_typed(self) -> None:
        deliverables = self.actions["components"]["schemas"][
            "StructuredRunRequest"
        ]["properties"]["deliverables"]
        self.assertEqual(deliverables["items"], {"type": "object"})

    def test_structured_request_exposes_nested_approval(self) -> None:
        """OpenAPI documents flat and nested platform_push_approved inputs."""
        for schema in (self.raw, self.actions):
            with self.subTest(openapi=schema.get("openapi")):
                request_schema = schema["components"]["schemas"][
                    "StructuredRunRequest"
                ]
                props = request_schema["properties"]
                self.assertIn("platform_push_approved", props)
                self.assertIn("approval", props)
                approval = props["approval"]
                # $ref or inline object with platform_push_approved
                if "$ref" in approval:
                    ref_name = approval["$ref"].rsplit("/", 1)[-1]
                    approval_schema = schema["components"]["schemas"][
                        ref_name
                    ]
                elif "anyOf" in approval:
                    # OpenAPI 3.1 nullable union: object | null
                    object_branch = next(
                        branch
                        for branch in approval["anyOf"]
                        if branch.get("type") == "object"
                        or "$ref" in branch
                    )
                    if "$ref" in object_branch:
                        ref_name = object_branch["$ref"].rsplit("/", 1)[-1]
                        approval_schema = schema["components"]["schemas"][
                            ref_name
                        ]
                    else:
                        approval_schema = object_branch
                else:
                    approval_schema = approval
                self.assertIn(
                    "platform_push_approved",
                    approval_schema.get("properties", {}),
                )

    def test_openapi_json_unchanged_for_normal_clients(self) -> None:
        # /openapi.json remains the FastAPI OpenAPI 3.1 document.
        self.assertEqual(self.raw.get("openapi"), "3.1.0")
        client = TestClient(app)
        response = client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        schema = response.json()
        self.assertEqual(schema.get("openapi"), "3.1.0")
        self.assertEqual(
            schema.get("servers"),
            [{"url": PRODUCTION_HTTPS}],
        )

    def test_openapi_actions_json_endpoint(self) -> None:
        client = TestClient(app)
        response = client.get("/openapi-actions.json")
        self.assertEqual(response.status_code, 200)
        schema = response.json()
        self.assertEqual(schema.get("openapi"), ACTIONS_OPENAPI_VERSION)
        self.assertEqual(schema.get("servers"), [{"url": PRODUCTION_HTTPS}])
        self.assertTrue(REQUIRED_OPERATION_IDS.issubset(_operation_ids(schema)))
        for node in _walk(schema):
            self.assertNotIn("oneOf", node)
            if "$ref" in node:
                self.assertNotIn("anyOf", node)
            if "items" in node:
                self.assertNotEqual(node["items"], {})

    def test_operation_descriptions_under_actions_limit(self) -> None:
        # Raw FastAPI schema has several descriptions at or above the limit.
        raw_over_limit = []
        for path_item in self.raw.get("paths", {}).values():
            if not isinstance(path_item, dict):
                continue
            for method_obj in path_item.values():
                if not isinstance(method_obj, dict):
                    continue
                description = method_obj.get("description")
                if (
                    isinstance(description, str)
                    and len(description) >= MAX_OPERATION_DESCRIPTION_LENGTH
                ):
                    raw_over_limit.append(method_obj.get("operationId"))
        self.assertIn("get_run", raw_over_limit)
        self.assertIn("submit_and_wait", raw_over_limit)

        for path_item in self.actions.get("paths", {}).values():
            if not isinstance(path_item, dict):
                continue
            for method_obj in path_item.values():
                if not isinstance(method_obj, dict):
                    continue
                description = method_obj.get("description")
                if not isinstance(description, str):
                    continue
                self.assertLess(
                    len(description),
                    MAX_OPERATION_DESCRIPTION_LENGTH,
                    msg=(
                        f"{method_obj.get('operationId')} description length "
                        f"{len(description)} >= {MAX_OPERATION_DESCRIPTION_LENGTH}"
                    ),
                )
                # Shortened text must still carry operational meaning.
                self.assertGreater(len(description.strip()), 0)

    def test_health_response_uses_named_component_schema(self) -> None:
        raw_schema = self.raw["paths"]["/health"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]
        self.assertNotIn("$ref", raw_schema)
        self.assertEqual(raw_schema.get("type"), "object")
        self.assertIn("additionalProperties", raw_schema)

        actions_schema = self.actions["paths"]["/health"]["get"]["responses"][
            "200"
        ]["content"]["application/json"]["schema"]
        self.assertEqual(
            actions_schema,
            {"$ref": f"#/components/schemas/{HEALTH_RESPONSE_SCHEMA_NAME}"},
        )

        health_component = self.actions["components"]["schemas"][
            HEALTH_RESPONSE_SCHEMA_NAME
        ]
        self.assertEqual(health_component.get("type"), "object")
        self.assertIn("status", health_component.get("properties", {}))
        self.assertEqual(
            health_component["properties"]["status"].get("type"), "string"
        )
        self.assertIn("status", health_component.get("required", []))
        # Named explicit properties — not the importer-rejected inline map.
        self.assertNotEqual(
            health_component.get("additionalProperties"),
            {"type": "string"},
        )

        # Runtime liveness payload shape is unchanged.
        client = TestClient(app)
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
