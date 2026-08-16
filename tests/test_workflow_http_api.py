"""Focused tests for feature-gated workflow HTTP submit/status (Slice A)."""

from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

import app.api as api_module
from app.api import app
from mission_control.openapi_actions import (
    MAX_OPERATION_DESCRIPTION_LENGTH,
    build_actions_openapi,
)
from mission_control.run_registry import RunRegistry
from mission_control.workflow_registry import (
    StepType,
    WorkflowRegistry,
    WorkflowState,
)
from mission_control.workflow_submit import (
    MAX_DEPENDENCIES_PER_STEP,
    MAX_STEPS,
    MAX_WORKFLOW_YAML_BYTES,
    WorkflowConflictError,
    WorkflowSubmitError,
    cancel_workflow,
    parse_idempotency_key,
    parse_workflow_yaml,
    submit_workflow,
    workflow_id_for_idempotency_key,
)

TEST_API_KEY = "mc_test_authentication_key"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_API_KEY}"}
TEST_SECRET_VALUE = "TEST_SECRET_VALUE"
_FEATURE_ON = {"MISSION_CONTROL_WORKFLOW_ORCHESTRATION": "true"}
_FEATURE_OFF = {"MISSION_CONTROL_WORKFLOW_ORCHESTRATION": "false"}
os.environ["MISSION_CONTROL_API_KEY"] = TEST_API_KEY


def _mission(*, kind: str = "implement", extra: str = "") -> str:
    if kind in {"review", "re_review"}:
        return (
            f"mission: {kind}\n"
            "permissions:\n"
            "  create_files: false\n"
            "  modify_files: false\n"
            "persistence:\n"
            "  mode: none\n"
            f"instructions: emit review envelope {extra}\n"
        )
    return (
        f"mission: {kind}\n"
        f"instructions: do the {kind} work {extra}\n"
    )


def _workflow_yaml(
    *,
    extra_top: str = "",
    extra_policy: str = "",
    include_fix: bool = False,
    impl_extra: str = "",
    review_extra: str = "",
    impl_depends: str | None = None,
    review_depends: str | None = None,
    extra_steps: str = "",
    repository_name: str = "Mission-Control",
) -> str:
    impl_dep = (
        ""
        if impl_depends is None
        else f"    depends_on: {impl_depends}\n"
    )
    rev_dep = (
        "    depends_on: [implementation]\n"
        if review_depends is None
        else f"    depends_on: {review_depends}\n"
    )
    fix_block = ""
    if include_fix:
        fix_block = (
            "  - id: fix\n"
            "    type: fix\n"
            "    depends_on: [review]\n"
            "    mission_yaml: |\n"
            f"{textwrap.indent(_mission(kind='fix'), '      ')}\n"
            "  - id: re_review\n"
            "    type: re_review\n"
            "    depends_on: [fix]\n"
            "    mission_yaml: |\n"
            f"{textwrap.indent(_mission(kind='re_review'), '      ')}\n"
        )
    return (
        "version: '1.0'\n"
        f"{extra_top}"
        "policy:\n"
        f"  repository_name: {repository_name}\n"
        "  base_branch: main\n"
        "  target_branch: wf/http-slice-a\n"
        "  implementation_scope:\n"
        "    - mission_control/\n"
        "    - tests/\n"
        f"{extra_policy}"
        "steps:\n"
        "  - id: implementation\n"
        "    type: implementation\n"
        f"{impl_dep}"
        "    mission_yaml: |\n"
        f"{textwrap.indent(_mission(extra=impl_extra), '      ')}\n"
        "  - id: review\n"
        "    type: review\n"
        f"{rev_dep}"
        "    mission_yaml: |\n"
        f"{textwrap.indent(_mission(kind='review', extra=review_extra), '      ')}\n"
        f"{fix_block}"
        f"{extra_steps}"
    )


class ParseWorkflowYamlTests(unittest.TestCase):
    def test_parses_minimal_implementation_and_review(self) -> None:
        parsed = parse_workflow_yaml(_workflow_yaml())
        self.assertEqual(parsed.policy.repository_name, "Mission-Control")
        self.assertEqual(parsed.implementation.step_type.value, "implementation")
        self.assertEqual(parsed.review.step_type.value, "review")
        self.assertIsNone(parsed.fix)
        self.assertTrue(parsed.fingerprint)

    def test_parses_optional_fix_and_re_review(self) -> None:
        parsed = parse_workflow_yaml(_workflow_yaml(include_fix=True))
        self.assertIsNotNone(parsed.fix)
        self.assertIsNotNone(parsed.re_review)

    def test_accepts_numeric_version_one(self) -> None:
        yaml_text = _workflow_yaml().replace("version: '1.0'", "version: 1.0")
        parsed = parse_workflow_yaml(yaml_text)
        self.assertEqual(parsed.policy.target_branch, "wf/http-slice-a")

    def test_rejects_unknown_top_level_field(self) -> None:
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(_workflow_yaml(extra_top="kind: other\n"))
        self.assertEqual(ctx.exception.code, "unknown_field")

    def test_rejects_unknown_policy_field(self) -> None:
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(
                _workflow_yaml(extra_policy="  allow_shell: true\n")
            )
        self.assertEqual(ctx.exception.code, "unknown_field")
        self.assertNotIn("true", ctx.exception.message)

    def test_rejects_unknown_step_field(self) -> None:
        extra = (
            "  - id: implementation\n"
            "    type: implementation\n"
            "    env: {}\n"
            "    mission_yaml: |\n"
            "      mission: x\n"
        )
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(_workflow_yaml() + extra)
        self.assertEqual(ctx.exception.code, "unknown_field")

    def test_rejects_unknown_step_key_on_single_step_doc(self) -> None:
        yaml_text = textwrap.dedent(
            """
            version: '1.0'
            policy:
              repository_name: Mission-Control
              base_branch: main
              target_branch: wf/http-slice-a
              implementation_scope: [mission_control/]
            steps:
              - type: implementation
                extra_field: nope
                mission_yaml: |
                  mission: implement
                  instructions: work
              - type: review
                mission_yaml: |
                  mission: review
                  permissions:
                    create_files: false
                    modify_files: false
                  persistence:
                    mode: none
                  instructions: review
            """
        )
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(yaml_text)
        self.assertEqual(ctx.exception.code, "unknown_field")

    def test_rejects_missing_policy(self) -> None:
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml("version: '1.0'\nsteps: []\n")
        self.assertEqual(ctx.exception.code, "missing_field")

    def test_rejects_non_mapping_yaml(self) -> None:
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml("- just\n- a\n- list\n")
        self.assertEqual(ctx.exception.code, "invalid_yaml")

    def test_rejects_invalid_yaml_syntax(self) -> None:
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml("version: [\n")
        self.assertEqual(ctx.exception.code, "invalid_yaml")

    def test_rejects_empty_yaml(self) -> None:
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml("   \n")
        self.assertEqual(ctx.exception.code, "invalid_yaml")

    def test_rejects_oversized_yaml(self) -> None:
        huge = "x" * (MAX_WORKFLOW_YAML_BYTES + 1)
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(huge)
        self.assertEqual(ctx.exception.code, "yaml_too_large")
        self.assertNotIn(huge[:32], ctx.exception.message)

    def test_rejects_too_many_steps(self) -> None:
        extra = "".join(
            (
                f"  - id: extra{i}\n"
                f"    type: implementation\n"
                "    mission_yaml: |\n"
                "      mission: extra\n"
            )
            for i in range(MAX_STEPS)
        )
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(_workflow_yaml(extra_steps=extra))
        self.assertIn(ctx.exception.code, {"limit_exceeded", "duplicate_step"})

    def test_rejects_missing_review_step(self) -> None:
        yaml_text = textwrap.dedent(
            """
            version: '1.0'
            policy:
              repository_name: Mission-Control
              base_branch: main
              target_branch: wf/http-slice-a
              implementation_scope: [mission_control/]
            steps:
              - type: implementation
                mission_yaml: |
                  mission: implement
                  instructions: work
            """
        )
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(yaml_text)
        self.assertEqual(ctx.exception.code, "missing_required_step")

    def test_rejects_re_review_without_fix(self) -> None:
        extra = (
            "  - id: re_review\n"
            "    type: re_review\n"
            "    depends_on: [review]\n"
            "    mission_yaml: |\n"
            f"{textwrap.indent(_mission(kind='re_review'), '      ')}\n"
        )
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(_workflow_yaml(extra_steps=extra))
        self.assertIn(
            ctx.exception.code,
            {"missing_required_step", "invalid_dependency"},
        )

    def test_rejects_unknown_step_type(self) -> None:
        extra = (
            "  - id: deploy\n"
            "    type: deploy\n"
            "    mission_yaml: |\n"
            "      mission: deploy\n"
        )
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(_workflow_yaml(extra_steps=extra))
        self.assertEqual(ctx.exception.code, "unknown_step_type")

    def test_rejects_unknown_dependency(self) -> None:
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(
                _workflow_yaml(review_depends="[does-not-exist]")
            )
        self.assertEqual(ctx.exception.code, "unknown_dependency")

    def test_rejects_non_canonical_dependency(self) -> None:
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(_workflow_yaml(impl_depends="[review]"))
        self.assertEqual(ctx.exception.code, "invalid_dependency")

    def test_rejects_too_many_dependencies(self) -> None:
        deps = "[" + ", ".join(f"s{i}" for i in range(MAX_DEPENDENCIES_PER_STEP + 1)) + "]"
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(_workflow_yaml(review_depends=deps))
        self.assertEqual(ctx.exception.code, "limit_exceeded")

    def test_rejects_empty_mission_yaml(self) -> None:
        yaml_text = textwrap.dedent(
            """
            version: '1.0'
            policy:
              repository_name: Mission-Control
              base_branch: main
              target_branch: wf/http-slice-a
              implementation_scope: [mission_control/]
            steps:
              - type: implementation
                mission_yaml: '   '
              - type: review
                mission_yaml: |
                  mission: review
                  permissions:
                    create_files: false
                    modify_files: false
                  persistence:
                    mode: none
                  instructions: review
            """
        )
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(yaml_text)
        self.assertEqual(ctx.exception.code, "invalid_field")

    def test_rejects_non_mapping_mission_yaml(self) -> None:
        yaml_text = textwrap.dedent(
            """
            version: '1.0'
            policy:
              repository_name: Mission-Control
              base_branch: main
              target_branch: wf/http-slice-a
              implementation_scope: [mission_control/]
            steps:
              - type: implementation
                mission_yaml: 'just a string'
              - type: review
                mission_yaml: |
                  mission: review
                  permissions:
                    create_files: false
                    modify_files: false
                  persistence:
                    mode: none
                  instructions: review
            """
        )
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(yaml_text)
        self.assertEqual(ctx.exception.code, "invalid_mission_yaml")

    def test_rejects_review_write_permissions(self) -> None:
        yaml_text = _workflow_yaml().replace(
            "create_files: false",
            "create_files: true",
            1,
        )
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(yaml_text)
        self.assertEqual(ctx.exception.code, "policy_denied")

    def test_rejects_repository_mismatch_in_mission(self) -> None:
        yaml_text = _workflow_yaml(impl_extra="\nrepository_name: other-repo")
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(yaml_text)
        self.assertEqual(ctx.exception.code, "policy_denied")

    def test_rejects_unsupported_version(self) -> None:
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(
                _workflow_yaml().replace("version: '1.0'", "version: '2.0'")
            )
        self.assertEqual(ctx.exception.code, "unsupported_version")

    def test_rejects_empty_scope(self) -> None:
        yaml_text = textwrap.dedent(
            """
            version: '1.0'
            policy:
              repository_name: Mission-Control
              base_branch: main
              target_branch: wf/http-slice-a
              implementation_scope: []
            steps:
              - type: implementation
                mission_yaml: |
                  mission: implement
                  instructions: work
              - type: review
                mission_yaml: |
                  mission: review
                  permissions:
                    create_files: false
                    modify_files: false
                  persistence:
                    mode: none
                  instructions: review
            """
        )
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(yaml_text)
        self.assertEqual(ctx.exception.code, "invalid_field")

    def test_rejects_bool_for_integer_policy(self) -> None:
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(
                _workflow_yaml(extra_policy="  max_fix_cycles: true\n")
            )
        self.assertEqual(ctx.exception.code, "invalid_field")

    def test_rejects_out_of_bounds_ceiling(self) -> None:
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(
                _workflow_yaml(extra_policy="  max_child_runs: 99\n")
            )
        self.assertEqual(ctx.exception.code, "limit_exceeded")

    def test_parse_idempotency_key_none_and_valid(self) -> None:
        self.assertIsNone(parse_idempotency_key(None))
        self.assertIsNone(parse_idempotency_key("  "))
        self.assertEqual(parse_idempotency_key("wf-case-01"), "wf-case-01")

    def test_parse_idempotency_key_rejects_spaces(self) -> None:
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_idempotency_key("not a valid key")
        self.assertEqual(ctx.exception.code, "invalid_idempotency_key")

    def test_errors_do_not_echo_secret_placeholder(self) -> None:
        blob = "version: [" + TEST_SECRET_VALUE + "\n"
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(blob)
        self.assertEqual(ctx.exception.code, "invalid_yaml")
        self.assertNotIn(TEST_SECRET_VALUE, ctx.exception.message)

    def test_rejects_recursive_depth_flow_sequence(self) -> None:
        nested = "[" * 1500 + TEST_SECRET_VALUE + "]" * 1500
        self.assertLess(len(nested.encode("utf-8")), MAX_WORKFLOW_YAML_BYTES)
        with self.assertRaises(RecursionError):
            yaml.safe_load(nested)
        with self.assertRaises(WorkflowSubmitError) as ctx:
            parse_workflow_yaml(nested)
        self.assertEqual(ctx.exception.code, "invalid_yaml")
        self.assertEqual(ctx.exception.message, "Workflow YAML could not be parsed")
        self.assertNotIn(TEST_SECRET_VALUE, ctx.exception.message)
        self.assertNotIn(nested[:32], ctx.exception.message)
        self.assertNotIn("[" * 16, ctx.exception.message)


class WorkflowHttpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._wf_fd, self._wf_path = tempfile.mkstemp(suffix="-wf.db")
        self._run_fd, self._run_path = tempfile.mkstemp(suffix="-run.db")
        os.close(self._wf_fd)
        os.close(self._run_fd)
        self._prev_wf = api_module.workflow_registry
        self._prev_run = api_module.run_registry
        api_module.workflow_registry = WorkflowRegistry(self._wf_path)
        api_module.run_registry = RunRegistry(self._run_path)
        self.client = TestClient(app, headers=AUTH_HEADERS)
        self._flag = patch.dict(os.environ, _FEATURE_ON, clear=False)
        self._flag.start()

    def tearDown(self) -> None:
        self._flag.stop()
        api_module.workflow_registry.close()
        api_module.run_registry.close()
        api_module.workflow_registry = self._prev_wf
        api_module.run_registry = self._prev_run
        os.unlink(self._wf_path)
        os.unlink(self._run_path)

    def _post(self, yaml_text: str | None = None, **kwargs):
        body = {"workflow_yaml": yaml_text if yaml_text is not None else _workflow_yaml()}
        return self.client.post("/workflows", json=body, **kwargs)


class FeatureGateHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, headers=AUTH_HEADERS)
        self.unauth = TestClient(app)

    def test_post_feature_off_returns_403(self) -> None:
        with patch.dict(os.environ, _FEATURE_OFF, clear=False):
            response = self.client.post(
                "/workflows",
                json={"workflow_yaml": _workflow_yaml()},
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["detail"],
            "Workflow orchestration is disabled",
        )

    def test_get_feature_off_returns_403(self) -> None:
        with patch.dict(os.environ, _FEATURE_OFF, clear=False):
            response = self.client.get(
                "/workflows/00000000-0000-4000-8000-000000000001"
            )
        self.assertEqual(response.status_code, 403)

    def test_cancel_feature_off_returns_403(self) -> None:
        with patch.dict(os.environ, _FEATURE_OFF, clear=False):
            response = self.client.post(
                "/workflows/00000000-0000-4000-8000-000000000001/cancel"
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["detail"],
            "Workflow orchestration is disabled",
        )

    def test_unauthenticated_post_returns_401_even_when_flag_off(self) -> None:
        with patch.dict(os.environ, _FEATURE_OFF, clear=False):
            response = self.unauth.post(
                "/workflows",
                json={"workflow_yaml": _workflow_yaml()},
            )
        self.assertEqual(response.status_code, 401)

    def test_unauthenticated_get_returns_401(self) -> None:
        with patch.dict(os.environ, _FEATURE_ON, clear=False):
            response = self.unauth.get(
                "/workflows/00000000-0000-4000-8000-000000000001"
            )
        self.assertEqual(response.status_code, 401)

    def test_unauthenticated_cancel_returns_401(self) -> None:
        with patch.dict(os.environ, _FEATURE_ON, clear=False):
            response = self.unauth.post(
                "/workflows/00000000-0000-4000-8000-000000000001/cancel"
            )
        self.assertEqual(response.status_code, 401)

    def test_invalid_bearer_returns_401(self) -> None:
        client = TestClient(
            app, headers={"Authorization": "Bearer not-the-test-key"}
        )
        with patch.dict(os.environ, _FEATURE_ON, clear=False):
            response = client.post(
                "/workflows",
                json={"workflow_yaml": _workflow_yaml()},
            )
        self.assertEqual(response.status_code, 401)


class SubmitHttpTests(WorkflowHttpTestCase):
    def test_post_accepts_strict_yaml(self) -> None:
        response = self._post()
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertIn("workflow_id", body)
        self.assertEqual(body["state"], "pending")
        self.assertFalse(body["idempotent_replay"])

    def test_post_persists_in_registry(self) -> None:
        body = self._post().json()
        record = api_module.workflow_registry.get_workflow(body["workflow_id"])
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.state.value, "pending")
        self.assertIn("implementation", record.step_specs)

    def test_post_with_fix_steps(self) -> None:
        response = self._post(_workflow_yaml(include_fix=True))
        self.assertEqual(response.status_code, 202)
        status = self.client.get(f"/workflows/{response.json()['workflow_id']}")
        types = [row["step_type"] for row in status.json()["step_templates"]]
        self.assertEqual(
            types, ["implementation", "review", "fix", "re_review"]
        )

    def test_post_rejects_unknown_json_field(self) -> None:
        response = self.client.post(
            "/workflows",
            json={"workflow_yaml": _workflow_yaml(), "extra": 1},
        )
        self.assertEqual(response.status_code, 422)

    def test_post_rejects_unknown_yaml_field_with_sanitized_code(self) -> None:
        response = self._post(_workflow_yaml(extra_top="owner: root\n"))
        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "unknown_field")
        self.assertNotIn("root", str(detail).lower())

    def test_post_sanitized_error_does_not_include_mission_yaml(self) -> None:
        yaml_text = _workflow_yaml(impl_extra="do not leak " + TEST_SECRET_VALUE)
        yaml_text = yaml_text.replace("create_files: false", "create_files: true", 1)
        response = self._post(yaml_text)
        self.assertEqual(response.status_code, 400)
        raw = response.text
        self.assertNotIn(TEST_SECRET_VALUE, raw)
        self.assertNotIn("mission_yaml", raw)

    def test_post_rejects_recursive_depth_yaml_with_sanitized_400(self) -> None:
        nested = "[" * 1500 + TEST_SECRET_VALUE + "]" * 1500
        self.assertLess(len(nested.encode("utf-8")), MAX_WORKFLOW_YAML_BYTES)
        response = self._post(nested)
        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "invalid_yaml")
        self.assertEqual(detail["message"], "Workflow YAML could not be parsed")
        raw = response.text
        self.assertNotIn(TEST_SECRET_VALUE, raw)
        self.assertNotIn(nested[:32], raw)
        self.assertNotIn("[" * 16, raw)

    def test_idempotent_replay_returns_same_id(self) -> None:
        headers = {"Idempotency-Key": "wf-replay-case-01"}
        first = self._post(headers=headers)
        second = self._post(headers=headers)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(
            first.json()["workflow_id"], second.json()["workflow_id"]
        )
        self.assertTrue(second.json()["idempotent_replay"])
        self.assertFalse(first.json()["idempotent_replay"])

    def test_idempotent_conflict_on_payload_mismatch(self) -> None:
        headers = {"Idempotency-Key": "wf-conflict-case-01"}
        first = self._post(_workflow_yaml(), headers=headers)
        self.assertEqual(first.status_code, 202)
        other = _workflow_yaml(extra_policy="  max_fix_cycles: 1\n")
        second = self._post(other, headers=headers)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(
            second.json()["detail"]["code"], "idempotency_payload_mismatch"
        )

    def test_invalid_idempotency_key_rejected(self) -> None:
        response = self._post(headers={"Idempotency-Key": "has spaces"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"]["code"], "invalid_idempotency_key"
        )

    def test_distinct_keys_create_distinct_workflows(self) -> None:
        a = self._post(headers={"Idempotency-Key": "wf-a"}).json()
        b = self._post(headers={"Idempotency-Key": "wf-b"}).json()
        self.assertNotEqual(a["workflow_id"], b["workflow_id"])

    def test_submit_helper_feature_disabled(self) -> None:
        with self.assertRaises(WorkflowSubmitError) as ctx:
            submit_workflow(
                _workflow_yaml(),
                workflow_registry=api_module.workflow_registry,
                environ=_FEATURE_OFF,
            )
        self.assertEqual(ctx.exception.code, "feature_disabled")

    def test_idempotency_key_maps_to_stable_uuid(self) -> None:
        first = workflow_id_for_idempotency_key("stable-key")
        second = workflow_id_for_idempotency_key("stable-key")
        self.assertEqual(first, second)
        self.assertNotEqual(first, workflow_id_for_idempotency_key("other"))


class StatusHttpTests(WorkflowHttpTestCase):
    def test_get_after_submit(self) -> None:
        created = self._post().json()
        response = self.client.get(f"/workflows/{created['workflow_id']}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["workflow_id"], created["workflow_id"])
        self.assertEqual(body["state"], "pending")
        self.assertEqual(body["child_run_count"], 0)
        self.assertEqual(body["policy"]["repository_name"], "Mission-Control")
        self.assertEqual(len(body["step_templates"]), 2)
        self.assertEqual(body["steps"], [])

    def test_get_unknown_workflow_404(self) -> None:
        response = self.client.get(
            "/workflows/00000000-0000-4000-8000-000000000099"
        )
        self.assertEqual(response.status_code, 404)

    def test_get_never_exposes_mission_yaml_or_stdout(self) -> None:
        created = self._post(
            _workflow_yaml(impl_extra="keep " + TEST_SECRET_VALUE)
        ).json()
        dumped = self.client.get(
            f"/workflows/{created['workflow_id']}"
        ).text
        self.assertNotIn("mission_yaml", dumped)
        self.assertNotIn("stdout", dumped)
        self.assertNotIn("stderr", dumped)
        self.assertNotIn(TEST_SECRET_VALUE, dumped)

    def test_get_child_run_summary_omits_raw_yaml(self) -> None:
        created = self._post().json()
        workflow_id = created["workflow_id"]
        record = api_module.workflow_registry.get_workflow(workflow_id)
        assert record is not None
        parent = api_module.run_registry.create_run()
        claim = api_module.workflow_registry.claim_child_launch(
            workflow_id=workflow_id,
            expected_version=record.version,
            step_type=StepType.IMPLEMENTATION,
            mission_yaml=record.step_specs["implementation"]["mission_yaml"],
            cycle=0,
            attempt=1,
            parent_run_id=parent.run_id,
        )
        self.assertTrue(claim.ok, claim.error)
        assert claim.step is not None
        child_id = claim.step.child_run_id
        assert child_id is not None
        reserved = api_module.run_registry.create_run(
            run_id=child_id,
            mission_yaml=claim.step.mission_yaml,
            retried_from=parent.run_id,
        )
        self.assertEqual(reserved.outcome.value, "created")
        api_module.run_registry.store_result(
            child_id,
            stdout="agent said " + TEST_SECRET_VALUE,
            stderr="trace " + TEST_SECRET_VALUE,
            error=None,
        )
        response = self.client.get(f"/workflows/{workflow_id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["steps"]), 1)
        child = body["steps"][0]["child_run"]
        self.assertIsNotNone(child)
        assert child is not None
        self.assertEqual(child["run_id"], child_id)
        self.assertEqual(child["status"], "queued")
        self.assertNotIn("mission_yaml", child)
        self.assertNotIn("stdout", child)
        dumped = response.text
        self.assertNotIn(TEST_SECRET_VALUE, dumped)
        self.assertNotIn(claim.step.mission_yaml.splitlines()[0], dumped)

    def test_get_redacts_workflow_error(self) -> None:
        created = self._post().json()
        workflow_id = created["workflow_id"]
        record = api_module.workflow_registry.get_workflow(workflow_id)
        assert record is not None
        api_module.workflow_registry.apply_cas_transition(
            workflow_id=workflow_id,
            expected_version=record.version,
            to_state=record.state,
            reason="error",
            workflow_updates={
                "error": f"token {TEST_SECRET_VALUE}_XXXXXXXX",
            },
        )
        body = self.client.get(f"/workflows/{workflow_id}").json()
        self.assertIsNotNone(body["error"])
        self.assertNotIn(TEST_SECRET_VALUE, body["error"])
        self.assertIn("[redacted]", body["error"])


class CancelHttpTests(WorkflowHttpTestCase):
    def test_cancel_pending_returns_sanitized_status(self) -> None:
        created = self._post().json()
        workflow_id = created["workflow_id"]
        response = self.client.post(f"/workflows/{workflow_id}/cancel")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["workflow_id"], workflow_id)
        self.assertEqual(body["state"], "cancelled")
        self.assertIsNotNone(body["completed_at"])
        self.assertEqual(body["last_decision_action"], "cancel")
        self.assertNotIn("mission_yaml", response.text)
        self.assertNotIn("stdout", response.text)
        self.assertNotIn(TEST_SECRET_VALUE, response.text)
        record = api_module.workflow_registry.get_workflow(workflow_id)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.state, WorkflowState.CANCELLED)

    def test_cancel_unknown_workflow_404(self) -> None:
        response = self.client.post(
            "/workflows/00000000-0000-4000-8000-000000000099/cancel"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Workflow not found")

    def test_cancel_already_cancelled_returns_409(self) -> None:
        created = self._post().json()
        workflow_id = created["workflow_id"]
        first = self.client.post(f"/workflows/{workflow_id}/cancel")
        self.assertEqual(first.status_code, 200)
        second = self.client.post(f"/workflows/{workflow_id}/cancel")
        self.assertEqual(second.status_code, 409)
        detail = second.json()["detail"]
        self.assertEqual(detail["code"], "workflow_already_cancelled")
        self.assertNotIn(TEST_SECRET_VALUE, second.text)

    def test_cancel_other_terminal_returns_409(self) -> None:
        created = self._post().json()
        workflow_id = created["workflow_id"]
        record = api_module.workflow_registry.get_workflow(workflow_id)
        assert record is not None
        failed = api_module.workflow_registry.apply_cas_transition(
            workflow_id=workflow_id,
            expected_version=record.version,
            to_state=WorkflowState.FAILED,
            reason="error",
            workflow_updates={"error": "boom"},
        )
        self.assertTrue(failed.ok)
        response = self.client.post(f"/workflows/{workflow_id}/cancel")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "workflow_terminal")
        latched = api_module.workflow_registry.get_workflow(workflow_id)
        assert latched is not None
        self.assertEqual(latched.state, WorkflowState.FAILED)

    def test_cancel_latches_cas_against_revival(self) -> None:
        created = self._post().json()
        workflow_id = created["workflow_id"]
        response = self.client.post(f"/workflows/{workflow_id}/cancel")
        self.assertEqual(response.status_code, 200)
        record = api_module.workflow_registry.get_workflow(workflow_id)
        assert record is not None
        revived = api_module.workflow_registry.apply_cas_transition(
            workflow_id=workflow_id,
            expected_version=record.version,
            to_state=WorkflowState.RUNNING,
            reason="child_status",
            detail={"child_status": "running"},
        )
        self.assertFalse(revived.ok)
        self.assertEqual(revived.error, "workflow_terminal")
        latched = api_module.workflow_registry.get_workflow(workflow_id)
        assert latched is not None
        self.assertEqual(latched.state, WorkflowState.CANCELLED)
        status = self.client.get(f"/workflows/{workflow_id}")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["state"], "cancelled")

    def test_cancel_helper_feature_disabled(self) -> None:
        with self.assertRaises(WorkflowSubmitError) as ctx:
            cancel_workflow(
                "00000000-0000-4000-8000-000000000001",
                workflow_registry=api_module.workflow_registry,
                run_registry=api_module.run_registry,
                environ=_FEATURE_OFF,
            )
        self.assertEqual(ctx.exception.code, "feature_disabled")


class OpenApiWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        app.openapi_schema = None
        self.raw = app.openapi()
        self.actions = build_actions_openapi(self.raw)

    def tearDown(self) -> None:
        app.openapi_schema = None

    def test_paths_registered(self) -> None:
        self.assertIn("/workflows", self.raw["paths"])
        self.assertIn("post", self.raw["paths"]["/workflows"])
        self.assertIn("/workflows/{workflow_id}", self.raw["paths"])
        self.assertIn("get", self.raw["paths"]["/workflows/{workflow_id}"])
        self.assertIn("/workflows/{workflow_id}/cancel", self.raw["paths"])
        self.assertIn("post", self.raw["paths"]["/workflows/{workflow_id}/cancel"])

    def test_operation_ids(self) -> None:
        self.assertEqual(
            self.raw["paths"]["/workflows"]["post"]["operationId"],
            "submit_workflow",
        )
        self.assertEqual(
            self.raw["paths"]["/workflows/{workflow_id}"]["get"]["operationId"],
            "get_workflow",
        )
        self.assertEqual(
            self.raw["paths"]["/workflows/{workflow_id}/cancel"]["post"][
                "operationId"
            ],
            "cancel_workflow",
        )

    def test_actions_schema_includes_workflow_operations(self) -> None:
        self.assertEqual(
            self.actions["paths"]["/workflows"]["post"]["operationId"],
            "submit_workflow",
        )
        self.assertEqual(
            self.actions["paths"]["/workflows/{workflow_id}"]["get"][
                "operationId"
            ],
            "get_workflow",
        )
        self.assertEqual(
            self.actions["paths"]["/workflows/{workflow_id}/cancel"]["post"][
                "operationId"
            ],
            "cancel_workflow",
        )

    def test_bearer_security_declared(self) -> None:
        self.assertEqual(
            self.raw["paths"]["/workflows"]["post"]["security"],
            [{"HTTPBearer": []}],
        )
        self.assertEqual(
            self.raw["paths"]["/workflows/{workflow_id}"]["get"]["security"],
            [{"HTTPBearer": []}],
        )
        self.assertEqual(
            self.raw["paths"]["/workflows/{workflow_id}/cancel"]["post"][
                "security"
            ],
            [{"HTTPBearer": []}],
        )
        self.assertEqual(
            self.actions["paths"]["/workflows"]["post"]["security"],
            [{"HTTPBearer": []}],
        )

    def test_actions_descriptions_under_limit(self) -> None:
        for path, method in (
            ("/workflows", "post"),
            ("/workflows/{workflow_id}", "get"),
            ("/workflows/{workflow_id}/cancel", "post"),
        ):
            description = self.actions["paths"][path][method]["description"]
            self.assertLess(len(description), MAX_OPERATION_DESCRIPTION_LENGTH)
            self.assertGreater(len(description.strip()), 0)

    def test_actions_endpoint_serves_workflow_operations(self) -> None:
        client = TestClient(app)
        response = client.get("/openapi-actions.json")
        self.assertEqual(response.status_code, 200)
        schema = response.json()
        self.assertIn("submit_workflow", {
            schema["paths"]["/workflows"]["post"]["operationId"],
            schema["paths"]["/workflows/{workflow_id}"]["get"]["operationId"],
            schema["paths"]["/workflows/{workflow_id}/cancel"]["post"][
                "operationId"
            ],
        })


class IdempotencyConflictClassTests(unittest.TestCase):
    def test_conflict_is_workflow_submit_error(self) -> None:
        exc = WorkflowConflictError("idempotency_payload_mismatch", "x")
        self.assertIsInstance(exc, WorkflowSubmitError)
        self.assertEqual(exc.code, "idempotency_payload_mismatch")


if __name__ == "__main__":
    unittest.main()
