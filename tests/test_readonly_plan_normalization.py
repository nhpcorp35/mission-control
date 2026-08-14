"""Regression tests for Unified read-only plan→execute normalization."""

from __future__ import annotations

import asyncio
import logging
import unittest
from typing import Any
from unittest import mock

import yaml

from hal_legalai_gateway.readonly_plan_normalization import (
    normalize_readonly_plan_mission_yaml,
)
from mission_control.validator import (
    load_mission_yaml,
    validate_mission_for_execute,
)


def _safe_readonly_plan_yaml(**overrides: Any) -> str:
    """Build a demonstrably non-mutating plan mission (YAML text)."""
    mission: dict[str, Any] = {
        "version": "1.0",
        "mission_id": "readonly-plan-norm-001",
        "title": "Safe read-only plan review",
        "repository": {
            "name": "Mission-Control",
            "path": ".",
            "base_branch": "main",
        },
        "execution": {
            "agent": "cursor",
            "mode": "plan",
            "sandbox": True,
            "worktree": False,
        },
        "permissions": {
            "read": True,
            "create_files": False,
            "modify_files": False,
            "delete_files": False,
            "run_commands": True,
            "stage_changes": False,
            "commit": False,
            "push": False,
        },
        "persistence": {"mode": "none"},
        "instructions": (
            "Inspect the repository. Do not write files.\n"
            "Note: do not rewrite prose that mentions mode: plan.\n"
        ),
        "deliverables": ["inspection notes"],
        "approval": {
            "execute_without_approval": True,
            "commit_requires_approval": True,
            "push_requires_approval": True,
        },
    }
    for key, value in overrides.items():
        if key in {"execution", "permissions", "persistence"} and isinstance(
            value, dict
        ):
            base = dict(mission.get(key) or {})
            base.update(value)
            mission[key] = base
        else:
            mission[key] = value
    return yaml.safe_dump(
        mission,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


def _mode_from_yaml(text: str) -> Any:
    data = yaml.safe_load(text)
    assert isinstance(data, dict)
    execution = data.get("execution")
    assert isinstance(execution, dict)
    return execution.get("mode")


class SafeReadonlyPlanNormalizationTests(unittest.TestCase):
    def test_safe_readonly_plan_normalizes_to_execute(self) -> None:
        raw = _safe_readonly_plan_yaml()
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertTrue(result.normalized)
        self.assertEqual(result.reason, "safe_readonly_plan_to_execute")
        self.assertEqual(_mode_from_yaml(result.mission_yaml), "execute")
        self.assertIn("mode: plan", result.mission_yaml.split("instructions:")[1])

    def test_observability_log_is_secret_free(self) -> None:
        raw = _safe_readonly_plan_yaml()
        with self.assertLogs(
            "hal_legalai_gateway.readonly_plan_normalization",
            level=logging.INFO,
        ) as captured:
            normalize_readonly_plan_mission_yaml(
                raw, gateway_tool="mission.submit"
            )
        joined = "\n".join(captured.output)
        self.assertIn("readonly_plan_normalized", joined)
        self.assertIn("gateway_tool=mission.submit", joined)
        self.assertIn("from_mode=plan", joined)
        self.assertIn("to_mode=execute", joined)
        self.assertNotIn("Inspect the repository", joined)
        self.assertNotIn("api_key", joined.lower())


class PermissionGateTests(unittest.TestCase):
    """Every boolean mutation gate must be present and exactly false."""

    GATES = (
        "create_files",
        "modify_files",
        "delete_files",
        "stage_changes",
        "commit",
        "push",
    )

    def test_each_true_gate_blocks_normalization(self) -> None:
        for gate in self.GATES:
            with self.subTest(gate=gate):
                raw = _safe_readonly_plan_yaml(permissions={gate: True})
                result = normalize_readonly_plan_mission_yaml(raw)
                self.assertFalse(result.normalized)
                self.assertEqual(result.mission_yaml, raw)
                self.assertEqual(_mode_from_yaml(result.mission_yaml), "plan")
                self.assertIn(gate, result.reason)

    def test_each_missing_gate_blocks_normalization(self) -> None:
        for gate in self.GATES:
            with self.subTest(gate=gate):
                mission = yaml.safe_load(_safe_readonly_plan_yaml())
                del mission["permissions"][gate]
                raw = yaml.safe_dump(mission, sort_keys=False)
                result = normalize_readonly_plan_mission_yaml(raw)
                self.assertFalse(result.normalized)
                self.assertEqual(result.mission_yaml, raw)
                self.assertEqual(result.reason, f"permission_missing_{gate}")

    def test_string_false_is_ambiguous_and_blocks(self) -> None:
        for gate in self.GATES:
            with self.subTest(gate=gate):
                raw = _safe_readonly_plan_yaml(permissions={gate: "false"})
                result = normalize_readonly_plan_mission_yaml(raw)
                self.assertFalse(result.normalized)
                self.assertEqual(result.mission_yaml, raw)

    def test_integer_zero_is_ambiguous_and_blocks(self) -> None:
        raw = _safe_readonly_plan_yaml(permissions={"create_files": 0})
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertFalse(result.normalized)
        self.assertEqual(result.mission_yaml, raw)

    def test_null_permission_blocks(self) -> None:
        raw = _safe_readonly_plan_yaml(permissions={"modify_files": None})
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertFalse(result.normalized)
        self.assertEqual(result.mission_yaml, raw)


class PermissionsMappingTests(unittest.TestCase):
    def test_missing_permissions_blocks(self) -> None:
        mission = yaml.safe_load(_safe_readonly_plan_yaml())
        del mission["permissions"]
        raw = yaml.safe_dump(mission, sort_keys=False)
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertFalse(result.normalized)
        self.assertEqual(result.mission_yaml, raw)
        self.assertEqual(result.reason, "permissions_not_mapping")

    def test_malformed_permissions_list_blocks(self) -> None:
        mission = yaml.safe_load(_safe_readonly_plan_yaml())
        mission["permissions"] = ["create_files", False]
        raw = yaml.safe_dump(mission, sort_keys=False)
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertFalse(result.normalized)
        self.assertEqual(result.mission_yaml, raw)
        self.assertEqual(result.reason, "permissions_not_mapping")


class PersistenceGateTests(unittest.TestCase):
    def test_missing_persistence_blocks(self) -> None:
        mission = yaml.safe_load(_safe_readonly_plan_yaml())
        del mission["persistence"]
        raw = yaml.safe_dump(mission, sort_keys=False)
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertFalse(result.normalized)
        self.assertEqual(result.mission_yaml, raw)
        self.assertEqual(result.reason, "persistence_missing_or_not_mapping")

    def test_malformed_persistence_blocks(self) -> None:
        mission = yaml.safe_load(_safe_readonly_plan_yaml())
        mission["persistence"] = "none"
        raw = yaml.safe_dump(mission, sort_keys=False)
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertFalse(result.normalized)
        self.assertEqual(result.reason, "persistence_missing_or_not_mapping")

    def test_missing_persistence_mode_blocks(self) -> None:
        mission = yaml.safe_load(_safe_readonly_plan_yaml())
        mission["persistence"] = {}
        raw = yaml.safe_dump(mission, sort_keys=False)
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertFalse(result.normalized)
        self.assertEqual(result.reason, "persistence_mode_missing")

    def test_persistence_commit_blocks(self) -> None:
        raw = _safe_readonly_plan_yaml(persistence={"mode": "commit"})
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertFalse(result.normalized)
        self.assertEqual(result.mission_yaml, raw)
        self.assertEqual(result.reason, "persistence_mode_not_none")

    def test_persistence_push_blocks(self) -> None:
        raw = _safe_readonly_plan_yaml(persistence={"mode": "push"})
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertFalse(result.normalized)
        self.assertEqual(result.reason, "persistence_mode_not_none")

    def test_persistence_mode_null_blocks(self) -> None:
        raw = _safe_readonly_plan_yaml(persistence={"mode": None})
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertFalse(result.normalized)

    def test_persistence_mode_string_none_quoted_normalizes(self) -> None:
        # Quoted "none" is still the string none after safe_load.
        raw = """
version: "1.0"
mission_id: quoted-none
title: t
repository:
  name: Mission-Control
  path: .
  base_branch: main
execution:
  agent: cursor
  mode: plan
permissions:
  read: true
  create_files: false
  modify_files: false
  delete_files: false
  run_commands: true
  stage_changes: false
  commit: false
  push: false
persistence:
  mode: "none"
instructions: inspect
deliverables:
  - notes
approval:
  execute_without_approval: true
  commit_requires_approval: true
  push_requires_approval: true
"""
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertTrue(result.normalized)
        self.assertEqual(_mode_from_yaml(result.mission_yaml), "execute")


class ModePreservationTests(unittest.TestCase):
    def test_execute_unchanged(self) -> None:
        raw = _safe_readonly_plan_yaml(execution={"mode": "execute"})
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertFalse(result.normalized)
        self.assertEqual(result.mission_yaml, raw)
        self.assertEqual(result.reason, "already_execute")
        self.assertEqual(_mode_from_yaml(result.mission_yaml), "execute")

    def test_ask_unchanged(self) -> None:
        raw = _safe_readonly_plan_yaml(execution={"mode": "ask"})
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertFalse(result.normalized)
        self.assertEqual(result.mission_yaml, raw)
        self.assertEqual(result.reason, "ask_unchanged")
        self.assertEqual(_mode_from_yaml(result.mission_yaml), "ask")

    def test_unknown_mode_unchanged(self) -> None:
        raw = _safe_readonly_plan_yaml(execution={"mode": "draft"})
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertFalse(result.normalized)
        self.assertEqual(result.mission_yaml, raw)
        self.assertEqual(result.reason, "mode_not_plan")
        self.assertEqual(_mode_from_yaml(result.mission_yaml), "draft")


class YamlShapeTests(unittest.TestCase):
    def test_comments_preserved_when_not_normalized(self) -> None:
        raw = (
            "# keep this comment\n"
            + _safe_readonly_plan_yaml(permissions={"create_files": True})
        )
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertFalse(result.normalized)
        self.assertEqual(result.mission_yaml, raw)
        self.assertIn("# keep this comment", result.mission_yaml)

    def test_quoted_plan_mode_normalizes(self) -> None:
        raw = """
version: "1.0"
mission_id: quoted-plan
title: t
repository:
  name: Mission-Control
  path: .
  base_branch: main
execution:
  agent: cursor
  mode: "plan"
permissions:
  read: true
  create_files: false
  modify_files: false
  delete_files: false
  run_commands: true
  stage_changes: false
  commit: false
  push: false
persistence:
  mode: none
instructions: |
  Review only. Literal text mode: plan must remain.
deliverables:
  - notes
approval:
  execute_without_approval: true
  commit_requires_approval: true
  push_requires_approval: true
"""
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertTrue(result.normalized)
        self.assertEqual(_mode_from_yaml(result.mission_yaml), "execute")
        loaded = yaml.safe_load(result.mission_yaml)
        self.assertIn("mode: plan", loaded["instructions"])

    def test_instruction_text_mode_plan_not_rewritten(self) -> None:
        raw = _safe_readonly_plan_yaml()
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertTrue(result.normalized)
        loaded = yaml.safe_load(result.mission_yaml)
        self.assertIn("mode: plan", loaded["instructions"])
        # Only execution.mode flipped; instruction prose untouched.
        self.assertEqual(loaded["execution"]["mode"], "execute")

    def test_yaml_anchors_supported_when_safe_load_resolves(self) -> None:
        raw = """
version: "1.0"
mission_id: anchor-plan
title: t
repository:
  name: Mission-Control
  path: .
  base_branch: main
execution:
  agent: cursor
  mode: plan
permissions: &perms
  read: true
  create_files: false
  modify_files: false
  delete_files: false
  run_commands: true
  stage_changes: false
  commit: false
  push: false
persistence:
  mode: none
instructions: inspect with anchors
deliverables:
  - notes
approval:
  execute_without_approval: true
  commit_requires_approval: true
  push_requires_approval: true
extra_permissions_ref: *perms
"""
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertTrue(result.normalized)
        self.assertEqual(_mode_from_yaml(result.mission_yaml), "execute")

    def test_invalid_yaml_forwarded_unchanged(self) -> None:
        raw = "execution: [\n  mode: plan\n"
        result = normalize_readonly_plan_mission_yaml(raw)
        self.assertFalse(result.normalized)
        self.assertEqual(result.mission_yaml, raw)
        self.assertEqual(result.reason, "yaml_parse_error")


class IntegrationNormalizationAndEligibilityTests(unittest.TestCase):
    def test_safe_readonly_plan_reaches_downstream_as_execute(self) -> None:
        raw = _safe_readonly_plan_yaml()
        normalized = normalize_readonly_plan_mission_yaml(
            raw, gateway_tool="mission.submit"
        )
        self.assertTrue(normalized.normalized)
        structural, mission = load_mission_yaml(normalized.mission_yaml)
        self.assertTrue(structural.ok, structural.error)
        assert mission is not None
        self.assertEqual(mission["execution"]["mode"], "execute")
        eligibility = validate_mission_for_execute(mission)
        self.assertTrue(eligibility.ok, eligibility.error)

    def test_write_capable_plan_remains_plan_and_is_rejected(self) -> None:
        raw = _safe_readonly_plan_yaml(permissions={"create_files": True})
        normalized = normalize_readonly_plan_mission_yaml(raw)
        self.assertFalse(normalized.normalized)
        self.assertEqual(normalized.mission_yaml, raw)
        structural, mission = load_mission_yaml(normalized.mission_yaml)
        self.assertTrue(structural.ok, structural.error)
        assert mission is not None
        self.assertEqual(mission["execution"]["mode"], "plan")
        eligibility = validate_mission_for_execute(mission)
        self.assertFalse(eligibility.ok)
        self.assertIn("expected execute", eligibility.error or "")

    def test_mission_submit_forwards_normalized_yaml(self) -> None:
        """Gateway mission.submit applies adapter before async forward."""
        from hal_legalai_gateway import mcp_server as gw_mcp

        raw = _safe_readonly_plan_yaml()

        binding = next(
            b
            for b in gw_mcp.DEFAULT_TOOL_BINDINGS
            if b.gateway_tool == "mission.submit"
        )
        self.assertIn("read-only", binding.description.lower())
        self.assertIn("normalize", binding.description.lower())

        class _Collector:
            submit = None

        collector = _Collector()

        class _Mcp:
            def tool(self, *args: Any, **kwargs: Any):
                def decorator(fn: Any) -> Any:
                    name = kwargs.get("name") or (args[0] if args else None)
                    if name == "mission.submit":
                        collector.submit = fn
                    return fn

                return decorator

        settings = mock.Mock()
        settings.downstream_by_key.return_value = mock.Mock(
            base_url="http://mc.test"
        )
        settings.connect_timeout_seconds = 1.0
        settings.read_timeout_seconds = 1.0
        settings.mcp_path_for_service.return_value = "/mcp"
        settings.bridge_authorization = None
        settings.secret_values_for_redaction.return_value = ()

        with mock.patch(
            "hal_legalai_gateway.mcp_server._require_gateway_principal",
            return_value="tester",
        ), mock.patch(
            "hal_legalai_gateway.mcp_server.forward_mcp_tool",
            new_callable=mock.AsyncMock,
            return_value={"ok": True},
        ) as forward_mock:
            gw_mcp.register_forwarding_tools(
                _Mcp(),  # type: ignore[arg-type]
                settings,
                gw_mcp.DEFAULT_TOOL_BINDINGS,
            )
            self.assertIsNotNone(collector.submit)
            asyncio.run(collector.submit(raw))
            forward_mock.assert_awaited()
            kwargs = forward_mock.await_args.kwargs
            forwarded_yaml = kwargs["arguments"]["mission_yaml"]
            self.assertEqual(_mode_from_yaml(forwarded_yaml), "execute")
            self.assertNotEqual(forwarded_yaml, raw)


if __name__ == "__main__":
    unittest.main()
