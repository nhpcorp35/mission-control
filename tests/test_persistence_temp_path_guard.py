"""Focused synthetic tests for persistence temp-path guard."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mission_control.executor import ExecutionResult, build_cursor_instruction
from mission_control.run_registry import RunRegistry, RunStatus
from mission_control.run_result import (
    CommandEvidence,
    PersistenceEvidence,
    build_run_summary,
    empty_structured_result,
    finalize_structured_summary,
)
from mission_control.workspace import (
    PERSISTENCE_TEMP_PATH_BLOCKED_PREFIX,
    SELF_REPOSITORY_URL_ENV,
    cleanup_workspace,
    execute_registered_run,
    is_blocked_persistence_temp_path,
    persist_workspace_changes,
    persistence_temp_path_guard_error,
    prepare_isolated_workspace,
)


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )


class _BareRepoFixture:
    def __init__(self) -> None:
        self._root = tempfile.TemporaryDirectory(prefix="mc-temp-guard-")
        root = Path(self._root.name)
        self.bare_remote = root / "remote.git"
        self.seed = root / "seed"
        _run_git(["init", "--bare", str(self.bare_remote)])
        _run_git(["clone", str(self.bare_remote), str(self.seed)])
        (self.seed / "README.md").write_text("seed\n", encoding="utf-8")
        (self.seed / "mission_control").mkdir()
        (self.seed / "mission_control" / "workspace.py").write_text(
            "x = 1\n",
            encoding="utf-8",
        )
        (self.seed / "tests").mkdir()
        (self.seed / "tests" / "test_sample.py").write_text(
            "def test_ok():\n    assert True\n",
            encoding="utf-8",
        )
        (self.seed / "docs").mkdir()
        (self.seed / "docs" / "README.md").write_text("docs\n", encoding="utf-8")
        _run_git(["-C", str(self.seed), "add", "-A"])
        _run_git(
            [
                "-C",
                str(self.seed),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "seed",
            ]
        )
        _run_git(["-C", str(self.seed), "branch", "-M", "main"])
        _run_git(["-C", str(self.seed), "push", "-u", "origin", "main"])

    def close(self) -> None:
        self._root.cleanup()

    def mission(self, *, persistence_mode: str = "commit") -> dict:
        return {
            "mission_id": "2026-08-11-temp-path-guard",
            "title": "Temp path guard",
            "repository": {
                "name": "nhpcorp35/mission-control",
                "path": ".",
                "base_branch": "main",
            },
            "permissions": {
                "create_files": True,
                "modify_files": True,
                "push": False,
            },
            "persistence": {"mode": persistence_mode},
            "instructions": "test",
            "deliverables": ["summary"],
            "approval": {},
        }


class TestPersistenceTempPathClassification(unittest.TestCase):
    def test_repo_tmp_blocked(self) -> None:
        self.assertTrue(is_blocked_persistence_temp_path("tmp/foo.json"))
        self.assertTrue(is_blocked_persistence_temp_path("tmp/nested/a.py"))
        self.assertTrue(is_blocked_persistence_temp_path(".tmp/cache.bin"))
        self.assertTrue(is_blocked_persistence_temp_path("scratch/out.txt"))
        self.assertTrue(is_blocked_persistence_temp_path("extracted/x.py"))
        error = persistence_temp_path_guard_error(
            ["tmp/foo.json", "mission_control/workspace.py"]
        )
        self.assertIsNotNone(error)
        assert error is not None
        self.assertTrue(error.startswith(PERSISTENCE_TEMP_PATH_BLOCKED_PREFIX))
        self.assertIn("tmp/foo.json", error)
        self.assertNotIn("mission_control/workspace.py", error)

    def test_nested_pycache_blocked(self) -> None:
        self.assertTrue(
            is_blocked_persistence_temp_path(
                "mission_control/__pycache__/workspace.cpython-313.pyc"
            )
        )
        error = persistence_temp_path_guard_error(
            ["tests/__pycache__/test_x.cpython-313.pyc"]
        )
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("__pycache__", error)

    def test_normal_source_tests_docs_allowed(self) -> None:
        allowed = [
            "mission_control/workspace.py",
            "tests/test_workspace.py",
            "docs/CANONICAL_MISSION_SCHEMA.md",
            "README.md",
            "app/api.py",
        ]
        for path in allowed:
            self.assertFalse(
                is_blocked_persistence_temp_path(path),
                path,
            )
        self.assertIsNone(persistence_temp_path_guard_error(allowed))

    def test_narrow_allowlist_works(self) -> None:
        path = "tmp/policy-allowed.txt"
        self.assertTrue(is_blocked_persistence_temp_path(path))
        self.assertFalse(
            is_blocked_persistence_temp_path(
                path,
                allowlist=frozenset({path}),
            )
        )
        self.assertIsNone(
            persistence_temp_path_guard_error(
                [path, "docs/README.md"],
                allowlist=frozenset({path}),
            )
        )

    def test_external_system_tmp_irrelevant(self) -> None:
        """Absolute system /tmp is outside repo-relative Git status paths."""
        # Guard only classifies repository-relative paths. Absolute /tmp never
        # appears in porcelain paths and is not treated as top-level repo tmp/.
        self.assertFalse(
            is_blocked_persistence_temp_path("/tmp/inspection-scratch")
        )
        self.assertFalse(
            is_blocked_persistence_temp_path("/tmp/mktemp-dir/extract.py")
        )
        self.assertIsNone(
            persistence_temp_path_guard_error(
                [
                    "mission_control/workspace.py",
                    # Absolute paths are not repo-relative persistence inputs.
                    "/tmp/outside-repo.txt",
                ]
            )
        )


class TestPersistBlocksTempPaths(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _BareRepoFixture()
        self._env = patch.dict(
            os.environ,
            {
                SELF_REPOSITORY_URL_ENV: str(self.fixture.bare_remote),
                "MISSION_CONTROL_REPOSITORY_URL": str(self.fixture.bare_remote),
                "MISSION_CONTROL_GIT_NAME": "Mission Control",
                "MISSION_CONTROL_GIT_EMAIL": "mc@example.com",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self.fixture.close()

    def test_persist_fails_closed_on_repo_tmp_without_deleting(self) -> None:
        mission = self.fixture.mission()
        prep = prepare_isolated_workspace(mission)
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        workspace = Path(prep.workspace_path)
        try:
            tmp_dir = workspace / "tmp"
            tmp_dir.mkdir()
            blocked = tmp_dir / "accidental.json"
            blocked.write_text('{"x": 1}\n', encoding="utf-8")
            source = workspace / "mission_control" / "workspace.py"
            source.write_text("x = 2\n", encoding="utf-8")

            result = persist_workspace_changes(
                "run-temp-guard",
                mission,
                prep.workspace_path,
            )
            self.assertFalse(result.ok)
            self.assertIsNotNone(result.error)
            assert result.error is not None
            self.assertTrue(
                result.error.startswith(PERSISTENCE_TEMP_PATH_BLOCKED_PREFIX),
                result.error,
            )
            # Untracked directories appear as top-level ``tmp`` in porcelain.
            self.assertIn("tmp", result.error)
            self.assertTrue(blocked.is_file())
            self.assertEqual(blocked.read_text(encoding="utf-8"), '{"x": 1}\n')
            self.assertIsNone(result.commit_sha)
        finally:
            cleanup_workspace(prep.workspace_path)

    def test_persist_allows_normal_source_changes(self) -> None:
        mission = self.fixture.mission()
        prep = prepare_isolated_workspace(mission)
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            target = (
                Path(prep.workspace_path) / "mission_control" / "workspace.py"
            )
            target.write_text("x = 3\n", encoding="utf-8")
            result = persist_workspace_changes(
                "run-temp-guard-ok",
                mission,
                prep.workspace_path,
            )
            self.assertTrue(result.ok, result.error)
            self.assertIsNotNone(result.commit_sha)
        finally:
            cleanup_workspace(prep.workspace_path)


class TestRegisteredRunTempPathGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _BareRepoFixture()
        self._db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db.close()
        self.registry = RunRegistry(self._db.name)
        self._env = patch.dict(
            os.environ,
            {
                SELF_REPOSITORY_URL_ENV: str(self.fixture.bare_remote),
                "MISSION_CONTROL_REPOSITORY_URL": str(self.fixture.bare_remote),
                "MISSION_CONTROL_GIT_NAME": "Mission Control",
                "MISSION_CONTROL_GIT_EMAIL": "mc@example.com",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self.registry.close()
        self.fixture.close()
        Path(self._db.name).unlink(missing_ok=True)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    def test_registered_run_blocks_before_persist(
        self,
        mock_persist,
        _mock_cleanup,
    ) -> None:
        mission = self.fixture.mission(persistence_mode="push")
        mission["approval"] = {"platform_push_approved": True}
        mission["deliverables"] = ["mission_control/workspace.py"]

        def fake_agent(mission_arg: dict, run_id: str | None = None) -> ExecutionResult:
            workspace = Path(mission_arg["repository"]["path"])
            (workspace / "tmp").mkdir(exist_ok=True)
            (workspace / "tmp" / "leak.json").write_text("{}\n", encoding="utf-8")
            target = workspace / "mission_control" / "workspace.py"
            target.write_text("x = 9\n", encoding="utf-8")
            return ExecutionResult(
                ok=True,
                stdout="agent claims push succeeded\n",
                return_code=0,
                command=["cursor-agent", "--workspace", str(workspace)],
            )

        with patch(
            "mission_control.workspace.execute_cursor_agent",
            side_effect=fake_agent,
        ):
            created = self.registry.create_run(mission_yaml="title: x\n")
            execute_registered_run(created.run_id, mission, self.registry)

        updated = self.registry.get_run(created.run_id)
        assert updated is not None
        self.assertEqual(updated.status, RunStatus.FAILED)
        self.assertTrue(
            (updated.error or "").startswith(PERSISTENCE_TEMP_PATH_BLOCKED_PREFIX),
            updated.error,
        )
        mock_persist.assert_not_called()
        assert updated.result is not None
        self.assertIsNotNone(updated.result.summary)
        assert updated.result.summary is not None
        self.assertIn("Agent result: succeeded", updated.result.summary)
        self.assertIn(
            "Platform persistence was not attempted",
            updated.result.summary,
        )
        self.assertNotIn(
            "agent claims push succeeded",
            updated.result.summary,
        )


class TestConsolidatedStatusSeparatesAgentAndPersistence(unittest.TestCase):
    def test_summary_separates_agent_from_platform_persistence(self) -> None:
        summary = build_run_summary(
            persistence=PersistenceEvidence(
                mode="push",
                attempted=True,
                ok=True,
                commit_sha="abc123",
                pushed=True,
            ),
            agent_ok=True,
            agent_return_code=0,
        )
        self.assertTrue(summary.startswith("Agent result: succeeded"))
        self.assertIn("Platform persistence succeeded", summary)
        self.assertIn("commit_sha=abc123", summary)
        self.assertIn("pushed=true", summary)
        self.assertIn(
            "never treat agent prose as authoritative over platform persistence",
            summary,
        )

    def test_finalize_uses_command_evidence_for_agent_line(self) -> None:
        structured = empty_structured_result()
        structured.commands = [
            CommandEvidence(
                argv=["cursor-agent"],
                exit_code=0,
                passed=True,
                kind="cursor_agent",
            )
        ]
        structured.persistence = PersistenceEvidence(
            mode="commit",
            attempted=True,
            ok=True,
            commit_sha="deadbeef",
        )
        finalize_structured_summary(structured)
        assert structured.summary is not None
        self.assertIn("Agent result: succeeded (return_code=0).", structured.summary)
        self.assertIn(
            "Platform persistence succeeded (mode=commit, commit_sha=deadbeef).",
            structured.summary,
        )


class TestAgentGuidanceTempScratch(unittest.TestCase):
    def test_instruction_guides_mktemp_and_system_tmp(self) -> None:
        mission = {
            "title": "Scratch guidance",
            "instructions": "Inspect only.",
            "deliverables": ["summary"],
            "repository": {"path": "/tmp/mission-control-run-example"},
        }
        instruction = build_cursor_instruction(mission)
        self.assertIn("mktemp -d", instruction)
        self.assertIn("absolute system `/tmp`", instruction)
        self.assertIn("tmp/", instruction)
        self.assertIn("__pycache__", instruction)
        self.assertIn("fails closed", instruction)


if __name__ == "__main__":
    unittest.main()
