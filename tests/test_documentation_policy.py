"""Regression tests for Mission Spec documentation policy support."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from mission_control.executor import (
    DOCUMENTATION_REQUIRED_INSTRUCTIONS,
    CURSOR_AGENT,
    ExecutionResult,
    build_cursor_instruction,
    execute_cursor_agent,
)
from mission_control.run_registry import RunStatus
from mission_control.run_result import (
    DOCUMENTATION_STATUS_FAILED,
    DOCUMENTATION_STATUS_NOT_REQUESTED,
    DOCUMENTATION_STATUS_NOT_REQUIRED,
    DOCUMENTATION_STATUS_UPDATED,
    DocumentationEvidence,
    PersistenceEvidence,
    StructuredRunResult,
    WARNING_DOCUMENTATION_PATH_HEURISTIC,
    build_documentation_evidence,
    deserialize_structured_result,
    finalize_structured_summary,
    looks_like_documentation_path,
    serialize_structured_result,
)
from mission_control.validator import (
    resolve_documentation_mode,
    validate_mission,
    validate_mission_for_execute,
)
from mission_control.workspace import (
    PersistenceResult,
    WorkspacePrepResult,
    execute_registered_run,
)
from tests.registry_test_utils import SqliteRegistryTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent


def _base_mission(
    *,
    documentation: dict | None = ...,  # type: ignore[assignment]
    create_files: bool = True,
    modify_files: bool = False,
    persistence_mode: str = "none",
) -> dict:
    mission: dict = {
        "version": "1.0",
        "mission_id": "2026-07-28-documentation-policy",
        "title": "Documentation Policy Test",
        "repository": {
            "name": "Mission-Control",
            "path": str(REPO_ROOT),
            "base_branch": "main",
        },
        "execution": {
            "agent": "cursor",
            "mode": "execute",
            "sandbox": True,
            "worktree": False,
        },
        "permissions": {
            "read": True,
            "create_files": create_files,
            "modify_files": modify_files,
            "delete_files": False,
            "run_commands": True,
            "stage_changes": False,
            "commit": False,
            "push": False,
        },
        "persistence": {"mode": persistence_mode},
        "instructions": "Implement the change.",
        "deliverables": ["summary"],
        "approval": {
            "execute_without_approval": True,
            "commit_requires_approval": True,
            "push_requires_approval": True,
        },
    }
    if documentation is not ...:
        if documentation is not None:
            mission["documentation"] = documentation
    return mission


def _read_only_mission(*, documentation: dict | None = ...) -> dict:
    return _base_mission(
        documentation=documentation,
        create_files=False,
        modify_files=False,
        persistence_mode="none",
    )


def _mock_completed_process(
    *,
    returncode: int = 0,
    stdout: str = "ok\n",
    stderr: str = "",
) -> MagicMock:
    proc = MagicMock()
    proc.pid = 4242
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


class TestDocumentationModeValidation(unittest.TestCase):
    def test_omitted_documentation_resolves_to_none(self) -> None:
        mission = _base_mission()
        self.assertNotIn("documentation", mission)
        result = validate_mission(mission)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(resolve_documentation_mode(mission), "none")

    def test_documentation_mode_none_accepted_and_serialized(self) -> None:
        mission = _base_mission(documentation={"mode": "none"})
        result = validate_mission(mission)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(resolve_documentation_mode(mission), "none")

        evidence = build_documentation_evidence(
            mission,
            files_changed=["docs/HAL_OPERATOR_LOG.md"],
            handling_completed=True,
        )
        structured = StructuredRunResult(
            documentation=evidence,
            persistence=PersistenceEvidence(
                mode="none",
                attempted=True,
                ok=True,
                commit_sha=None,
                pushed=False,
            ),
        )
        restored = deserialize_structured_result(
            serialize_structured_result(structured)
        )
        assert restored is not None
        assert restored.documentation is not None
        self.assertEqual(restored.documentation.mode, "none")
        self.assertEqual(
            restored.documentation.status,
            DOCUMENTATION_STATUS_NOT_REQUESTED,
        )
        assert restored.persistence is not None
        self.assertEqual(restored.persistence.mode, "none")

    def test_documentation_mode_required_accepted(self) -> None:
        mission = _base_mission(documentation={"mode": "required"})
        result = validate_mission(mission)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(resolve_documentation_mode(mission), "required")

    def test_unsupported_documentation_mode_rejected(self) -> None:
        mission = _base_mission(documentation={"mode": "optional"})
        result = validate_mission(mission)
        self.assertFalse(result.ok)
        self.assertIn("Unsupported documentation.mode", result.error or "")
        self.assertIn("optional", result.error or "")

    def test_rejects_non_mapping_documentation(self) -> None:
        mission = _base_mission(documentation="required")  # type: ignore[arg-type]
        result = validate_mission(mission)
        self.assertFalse(result.ok)
        self.assertIn("documentation must be a mapping", result.error or "")

    def test_null_mode_resolves_to_none(self) -> None:
        mission = _base_mission(documentation={"mode": None})
        result = validate_mission(mission)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(resolve_documentation_mode(mission), "none")


class TestDocumentationInstructions(unittest.TestCase):
    def test_required_mode_propagates_into_agent_instructions(self) -> None:
        mission = _base_mission(documentation={"mode": "required"})
        instruction = build_cursor_instruction(mission)
        self.assertIn("Documentation:", instruction)
        for line in DOCUMENTATION_REQUIRED_INSTRUCTIONS:
            self.assertIn(line, instruction)

        with patch(
            "mission_control.executor.find_cursor_agent_binary",
            return_value=CURSOR_AGENT,
        ), patch(
            "mission_control.executor.subprocess.Popen",
        ) as mock_popen:
            mock_popen.return_value = _mock_completed_process()
            execute_cursor_agent(mission)
            launched = mock_popen.call_args.args[0][-1]
            self.assertIn("Documentation:", launched)
            self.assertIn(
                "Treat documentation review as part of completion.",
                launched,
            )

    def test_none_mode_omits_documentation_instructions(self) -> None:
        mission = _base_mission(documentation={"mode": "none"})
        instruction = build_cursor_instruction(mission)
        self.assertNotIn("Documentation:", instruction)
        self.assertNotIn(
            "Treat documentation review as part of completion.",
            instruction,
        )

    def test_omitted_documentation_omits_documentation_instructions(
        self,
    ) -> None:
        mission = _base_mission()
        instruction = build_cursor_instruction(mission)
        self.assertNotIn("Documentation:", instruction)


class TestReadOnlyCompatibility(unittest.TestCase):
    def test_read_only_execute_valid_when_documentation_omitted(self) -> None:
        mission = _read_only_mission()
        self.assertNotIn("documentation", mission)
        structural = validate_mission(mission)
        self.assertTrue(structural.ok, structural.error)
        execute = validate_mission_for_execute(mission)
        self.assertTrue(execute.ok, execute.error)
        self.assertEqual(resolve_documentation_mode(mission), "none")

    def test_read_only_execute_valid_when_documentation_none(self) -> None:
        mission = _read_only_mission(documentation={"mode": "none"})
        structural = validate_mission(mission)
        self.assertTrue(structural.ok, structural.error)
        execute = validate_mission_for_execute(mission)
        self.assertTrue(execute.ok, execute.error)


class TestDocumentationStructuredResult(unittest.TestCase):
    def test_looks_like_documentation_path_heuristic(self) -> None:
        self.assertTrue(looks_like_documentation_path("docs/HAL_OPERATOR_LOG.md"))
        self.assertTrue(looks_like_documentation_path("README.md"))
        self.assertTrue(looks_like_documentation_path("./docs/a.txt"))
        self.assertFalse(looks_like_documentation_path("mission_control/run_result.py"))
        self.assertFalse(looks_like_documentation_path("app/api.py"))

    def test_authoritative_result_includes_mode_and_status(self) -> None:
        mission = _base_mission(documentation={"mode": "required"})
        updated = build_documentation_evidence(
            mission,
            files_changed=["docs/CANONICAL_MISSION_SCHEMA.md", "app/api.py"],
            handling_completed=True,
        )
        self.assertEqual(
            updated,
            DocumentationEvidence(
                mode="required",
                status=DOCUMENTATION_STATUS_UPDATED,
            ),
        )

        not_required = build_documentation_evidence(
            mission,
            files_changed=["mission_control/run_result.py"],
            handling_completed=True,
        )
        self.assertEqual(
            not_required.status,
            DOCUMENTATION_STATUS_NOT_REQUIRED,
        )

        failed = build_documentation_evidence(
            mission,
            files_changed=["docs/HAL_OPERATOR_LOG.md"],
            handling_completed=False,
        )
        self.assertEqual(failed.status, DOCUMENTATION_STATUS_FAILED)

        none_mission = _base_mission(documentation={"mode": "none"})
        not_requested = build_documentation_evidence(
            none_mission,
            files_changed=["docs/HAL_OPERATOR_LOG.md"],
            handling_completed=True,
        )
        self.assertEqual(
            not_requested.status,
            DOCUMENTATION_STATUS_NOT_REQUESTED,
        )

    def test_persistence_reporting_remains_correct_with_documentation(
        self,
    ) -> None:
        structured = StructuredRunResult(
            files_changed=["docs/HAL_OPERATOR_LOG.md"],
            persistence=PersistenceEvidence(
                mode="commit",
                attempted=True,
                ok=True,
                commit_sha="abc123",
                pushed=False,
            ),
            documentation=DocumentationEvidence(
                mode="required",
                status=DOCUMENTATION_STATUS_UPDATED,
            ),
        )
        finalize_structured_summary(structured)
        assert structured.summary is not None
        self.assertIn("mode=commit", structured.summary)
        self.assertIn("commit_sha=abc123", structured.summary)
        self.assertIn(WARNING_DOCUMENTATION_PATH_HEURISTIC, structured.warnings)
        payload = structured.to_dict()
        self.assertEqual(payload["persistence"]["mode"], "commit")
        self.assertEqual(payload["persistence"]["commit_sha"], "abc123")
        self.assertEqual(payload["persistence"]["pushed"], False)
        self.assertEqual(payload["documentation"]["mode"], "required")
        self.assertEqual(payload["documentation"]["status"], "updated")

    def test_existing_missions_remain_backward_compatible(self) -> None:
        mission = {
            "version": "1.0",
            "mission_id": "legacy",
            "title": "Legacy",
            "repository": {
                "name": "Mission-Control",
                "path": str(REPO_ROOT),
                "base_branch": "main",
            },
            "execution": {
                "agent": "cursor",
                "mode": "execute",
                "sandbox": True,
                "worktree": False,
            },
            "permissions": {
                "read": True,
                "create_files": True,
                "modify_files": False,
                "delete_files": False,
                "run_commands": True,
                "stage_changes": False,
                "commit": False,
                "push": False,
            },
            "instructions": "Do something.",
            "deliverables": [],
            "approval": {
                "execute_without_approval": True,
                "commit_requires_approval": True,
                "push_requires_approval": True,
            },
        }
        self.assertNotIn("documentation", mission)
        self.assertNotIn("persistence", mission)
        self.assertTrue(validate_mission(mission).ok)
        self.assertTrue(validate_mission_for_execute(mission).ok)
        self.assertEqual(resolve_documentation_mode(mission), "none")
        evidence = build_documentation_evidence(
            mission,
            files_changed=[],
            handling_completed=True,
        )
        self.assertEqual(evidence.mode, "none")
        self.assertEqual(evidence.status, DOCUMENTATION_STATUS_NOT_REQUESTED)


class TestDocumentationInRegisteredRun(SqliteRegistryTestCase):
    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.collect_changed_files")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    def test_registered_run_reports_documentation_updated(
        self,
        mock_prepare,
        mock_execute,
        mock_changed,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        import tempfile

        workspace = tempfile.mkdtemp(prefix="mc-docs-policy-")
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path=workspace,
        )
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout="done; docs updated\n",
            return_code=0,
            command=["cursor-agent", "--force", "<instruction>"],
        )
        mock_changed.return_value = (
            ["docs/HAL_OPERATOR_LOG.md", "mission_control/run_result.py"],
            None,
        )
        mock_persist.return_value = PersistenceResult(
            ok=True,
            commit_sha="docsha01",
            mode="commit",
            pushed=False,
        )

        record = self.registry.create_run()
        mission = _base_mission(
            documentation={"mode": "required"},
            persistence_mode="commit",
        )
        execute_registered_run(record.run_id, mission, self.registry)

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        self.assertEqual(updated.status, RunStatus.COMPLETED)
        self.assertEqual(updated.commit_sha, "docsha01")
        assert updated.result is not None
        assert updated.result.documentation is not None
        self.assertEqual(updated.result.documentation.mode, "required")
        self.assertEqual(
            updated.result.documentation.status,
            DOCUMENTATION_STATUS_UPDATED,
        )
        assert updated.result.persistence is not None
        self.assertEqual(updated.result.persistence.mode, "commit")
        self.assertEqual(updated.result.persistence.commit_sha, "docsha01")
        self.assertFalse(updated.result.persistence.pushed)
        self.assertIn(
            WARNING_DOCUMENTATION_PATH_HEURISTIC,
            updated.result.warnings,
        )

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.collect_changed_files")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    def test_registered_run_omitted_docs_is_not_requested(
        self,
        mock_prepare,
        mock_execute,
        mock_changed,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        import tempfile

        workspace = tempfile.mkdtemp(prefix="mc-docs-none-")
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path=workspace,
        )
        mock_execute.return_value = ExecutionResult(
            ok=True,
            stdout="done\n",
            return_code=0,
            command=["cursor-agent", "--force", "<instruction>"],
        )
        mock_changed.return_value = (["mission_control/run_result.py"], None)
        mock_persist.return_value = PersistenceResult(
            ok=True,
            commit_sha=None,
            mode="none",
            pushed=False,
        )

        record = self.registry.create_run()
        mission = _base_mission(persistence_mode="none")
        self.assertNotIn("documentation", mission)
        execute_registered_run(record.run_id, mission, self.registry)

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        self.assertEqual(updated.status, RunStatus.COMPLETED)
        assert updated.result is not None
        assert updated.result.documentation is not None
        self.assertEqual(updated.result.documentation.mode, "none")
        self.assertEqual(
            updated.result.documentation.status,
            DOCUMENTATION_STATUS_NOT_REQUESTED,
        )
        assert updated.result.persistence is not None
        self.assertEqual(updated.result.persistence.mode, "none")


if __name__ == "__main__":
    unittest.main()
