"""Focused tests for mission_control.mission_builder."""

from __future__ import annotations

import unittest

from mission_control.mission_builder import (
    build_mission_spec,
    render_mission_yaml,
    resolve_structured_persistence_mode,
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
        # Create/modify structured missions default to push when mode omitted.
        self.assertEqual(mission["persistence"]["mode"], "push")
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
        # Push without approval is structurally valid YAML but not executable
        # until platform-push approval is supplied.
        execute = validate_mission_for_execute(mission)
        self.assertFalse(execute.ok)

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

    def test_create_defaults_to_push(self) -> None:
        self.assertEqual(
            resolve_structured_persistence_mode(
                create_files=True,
                modify_files=False,
            ),
            "push",
        )
        spec = build_mission_spec(
            mission_id="create-default",
            title="Create",
            instructions="Create a file.",
            deliverables=["summary"],
            create_files=True,
            modify_files=False,
        )
        self.assertEqual(spec["persistence"]["mode"], "push")

    def test_modify_defaults_to_push(self) -> None:
        self.assertEqual(
            resolve_structured_persistence_mode(
                create_files=False,
                modify_files=True,
            ),
            "push",
        )
        spec = build_mission_spec(
            mission_id="modify-default",
            title="Modify",
            instructions="Modify a file.",
            deliverables=["summary"],
            create_files=False,
            modify_files=True,
        )
        self.assertEqual(spec["persistence"]["mode"], "push")

    def test_delete_only_defaults_to_push(self) -> None:
        # Structured v1 hardcodes delete_files=false, but the resolver treats
        # delete-only as a repository mutation when supported.
        self.assertEqual(
            resolve_structured_persistence_mode(
                create_files=False,
                modify_files=False,
                delete_files=True,
            ),
            "push",
        )

    def test_read_only_defaults_to_none(self) -> None:
        self.assertEqual(
            resolve_structured_persistence_mode(
                create_files=False,
                modify_files=False,
            ),
            "none",
        )
        spec = build_mission_spec(
            mission_id="read-only-default",
            title="Inspect",
            instructions="Inspect only.",
            deliverables=["summary"],
            create_files=False,
            modify_files=False,
        )
        self.assertEqual(spec["persistence"]["mode"], "none")

    def test_explicit_none_remains_none(self) -> None:
        self.assertEqual(
            resolve_structured_persistence_mode(
                create_files=True,
                modify_files=False,
                persistence_mode="none",
            ),
            "none",
        )
        spec = build_mission_spec(
            mission_id="explicit-none",
            title="Workspace only",
            instructions="Create a file but do not persist.",
            deliverables=["summary"],
            create_files=True,
            modify_files=False,
            persistence_mode="none",
        )
        self.assertEqual(spec["persistence"]["mode"], "none")

    def test_explicit_commit_remains_commit(self) -> None:
        self.assertEqual(
            resolve_structured_persistence_mode(
                create_files=True,
                modify_files=False,
                persistence_mode="commit",
            ),
            "commit",
        )
        spec = build_mission_spec(
            mission_id="explicit-commit",
            title="Commit only",
            instructions="Create and commit locally.",
            deliverables=["summary"],
            create_files=True,
            modify_files=False,
            persistence_mode="commit",
        )
        self.assertEqual(spec["persistence"]["mode"], "commit")

    def test_explicit_push_remains_push(self) -> None:
        self.assertEqual(
            resolve_structured_persistence_mode(
                create_files=False,
                modify_files=False,
                persistence_mode="push",
            ),
            "push",
        )
        spec = build_mission_spec(
            mission_id="explicit-push",
            title="Push only",
            instructions="Push-only mission.",
            deliverables=["summary"],
            create_files=False,
            modify_files=False,
            persistence_mode="push",
        )
        self.assertEqual(spec["persistence"]["mode"], "push")


if __name__ == "__main__":
    unittest.main()
