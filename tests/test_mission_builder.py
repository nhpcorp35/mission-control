"""Focused tests for mission_control.mission_builder."""

from __future__ import annotations

import unittest

from mission_control.mission_builder import (
    build_mission_spec,
    render_mission_yaml,
)
from mission_control.validator import (
    load_mission_yaml,
    validate_mission_for_execute,
)


class TestMissionBuilder(unittest.TestCase):
    def test_builder_defaults_produce_yaml_accepted_by_load_mission_yaml(
        self,
    ) -> None:
        yaml_text = render_mission_yaml(
            mission_id="2026-07-24-builder",
            title="Builder Defaults",
            instructions="Create a smoke file.",
            deliverables=["summary"],
            create_files=True,
            modify_files=False,
        )
        result, mission = load_mission_yaml(yaml_text)
        self.assertTrue(result.ok, result.error)
        assert mission is not None
        self.assertEqual(mission["version"], "1.0")
        self.assertEqual(mission["execution"]["agent"], "cursor")
        self.assertEqual(mission["execution"]["mode"], "execute")
        self.assertTrue(mission["execution"]["sandbox"])
        self.assertFalse(mission["execution"]["worktree"])
        self.assertEqual(mission["repository"]["name"], "Mission-Control")
        self.assertEqual(mission["repository"]["path"], ".")
        self.assertEqual(mission["repository"]["base_branch"], "main")
        self.assertEqual(mission["persistence"]["mode"], "none")
        self.assertTrue(mission["permissions"]["read"])
        self.assertTrue(mission["permissions"]["create_files"])
        self.assertFalse(mission["permissions"]["modify_files"])
        self.assertFalse(mission["permissions"]["delete_files"])
        self.assertTrue(mission["permissions"]["run_commands"])
        self.assertFalse(mission["permissions"]["stage_changes"])
        self.assertFalse(mission["permissions"]["commit"])
        self.assertFalse(mission["permissions"]["push"])
        self.assertTrue(mission["approval"]["execute_without_approval"])
        self.assertTrue(mission["approval"]["commit_requires_approval"])
        self.assertTrue(mission["approval"]["push_requires_approval"])
        self.assertFalse(mission["approval"]["platform_push_approved"])
        self.assertFalse(
            mission["approval"]["allow_automatic_platform_push"]
        )
        execute = validate_mission_for_execute(mission)
        self.assertTrue(execute.ok, execute.error)

    def test_build_mission_spec_dict_matches_render_round_trip(self) -> None:
        spec = build_mission_spec(
            mission_id="id-1",
            title="Title",
            instructions="Do the thing.",
            deliverables=["a", "b"],
            create_files=False,
            modify_files=True,
            persistence_mode="commit",
            repository_name="Other",
            repository_path="/tmp/repo",
            base_branch="develop",
            run_commands=False,
            platform_push_approved=True,
            allow_automatic_platform_push=True,
        )
        yaml_text = render_mission_yaml(
            mission_id="id-1",
            title="Title",
            instructions="Do the thing.",
            deliverables=["a", "b"],
            create_files=False,
            modify_files=True,
            persistence_mode="commit",
            repository_name="Other",
            repository_path="/tmp/repo",
            base_branch="develop",
            run_commands=False,
            platform_push_approved=True,
            allow_automatic_platform_push=True,
        )
        result, loaded = load_mission_yaml(yaml_text)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(loaded, spec)


if __name__ == "__main__":
    unittest.main()
