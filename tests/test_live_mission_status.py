"""Focused tests for live mission phase/heartbeat/progress observability."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.api as api_module
from app.api import app
from mission_control.executor import ExecutionResult
from mission_control.run_queue import RunQueue
from mission_control.run_registry import (
    EXECUTION_LEASE_GRACE_SECONDS,
    INTERRUPTED_RUN_ERROR,
    OWNER_LOST_RUN_ERROR,
    RunPhase,
    RunRegistry,
    RunStatus,
    platform_progress,
    sanitize_progress,
)
from mission_control.run_result import DeliverableEvidence
from mission_control.workspace import (
    PersistenceResult,
    WorkspacePrepResult,
    execute_registered_run,
)
from tests.registry_test_utils import SqliteRegistryTestCase

TEST_API_KEY = "mc_test_authentication_key"
AUTH_HEADERS = {
    "Authorization": f"Bearer {TEST_API_KEY}",
}
os.environ["MISSION_CONTROL_API_KEY"] = TEST_API_KEY


def _mission(*, repository_path: str = ".") -> dict:
    return {
        "version": "1.0",
        "mission_id": "2026-08-13-live-status",
        "title": "Live Status",
        "repository": {
            "name": "Mission-Control",
            "path": repository_path,
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
        "persistence": {"mode": "none"},
        "instructions": "Create a file.",
        "deliverables": ["summary"],
        "approval": {
            "execute_without_approval": True,
            "commit_requires_approval": True,
            "push_requires_approval": True,
        },
    }


@contextmanager
def _patched_successful_workspace_run(
    workspace: str,
    *,
    execution_result: ExecutionResult | None = None,
    cleanup_side_effect: BaseException | None = None,
    extra_patches: tuple = (),
) -> Iterator[None]:
    """Patch workspace execution dependencies for an agent path with cleanup."""
    result = execution_result or ExecutionResult(
        ok=True,
        stdout="agent ok",
        stderr="",
        return_code=0,
    )
    cleanup_kwargs: dict = {}
    if cleanup_side_effect is not None:
        cleanup_kwargs["side_effect"] = cleanup_side_effect
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "mission_control.workspace.prepare_isolated_workspace",
                return_value=WorkspacePrepResult(
                    ok=True,
                    workspace_path=workspace,
                    baseline_sha="abc123",
                ),
            )
        )
        stack.enter_context(
            patch(
                "mission_control.workspace.resolve_agent_workspace_path",
                return_value=(workspace, None),
            )
        )
        stack.enter_context(
            patch(
                "mission_control.workspace.disable_agent_git_push",
                return_value=None,
            )
        )
        stack.enter_context(
            patch(
                "mission_control.workspace.execute_cursor_agent",
                return_value=result,
            )
        )
        stack.enter_context(
            patch(
                "mission_control.workspace.collect_changed_files",
                return_value=([], None),
            )
        )
        stack.enter_context(
            patch(
                "mission_control.workspace.collect_deliverable_evidence",
                return_value=DeliverableEvidence(
                    verified=True,
                    passed=True,
                    checked_paths=[],
                    missing=[],
                    outside_workspace=[],
                ),
            )
        )
        stack.enter_context(
            patch(
                "mission_control.workspace.persistence_temp_path_guard_error",
                return_value=None,
            )
        )
        stack.enter_context(
            patch(
                "mission_control.workspace.persist_workspace_changes",
                return_value=PersistenceResult(
                    ok=True,
                    mode="none",
                    commit_sha=None,
                    pushed=False,
                ),
            )
        )
        stack.enter_context(
            patch(
                "mission_control.workspace.cleanup_workspace",
                **cleanup_kwargs,
            )
        )
        for patcher in extra_patches:
            stack.enter_context(patcher)
        yield


def _heartbeat_thread_names() -> list[str]:
    return [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("mc-heartbeat-")
    ]


class TestPlatformProgressRedaction(unittest.TestCase):
    def test_progress_is_bounded_and_redacts_secretish_tokens(self) -> None:
        progress = platform_progress(
            step="agent_execution",
            detail=(
                "Authorization bearer "
                "mc_test_redaction_probe_aaaaaaaa "
                + ("x" * 300)
            ),
        )
        self.assertEqual(set(progress.keys()), {"step", "detail"})
        self.assertNotIn("mc_test_redaction_probe_aaaaaaaa", progress["detail"])
        self.assertIn("[redacted]", progress["detail"])
        self.assertLessEqual(len(progress["detail"]), 160)

        cleaned = sanitize_progress(
            {
                "step": "agent_execution",
                "detail": "ok",
                "prompt": "SECRET PROMPT",
                "stdout": "raw agent output",
            }
        )
        assert cleaned is not None
        self.assertEqual(set(cleaned.keys()), {"step", "detail"})
        self.assertEqual(cleaned["detail"], "ok")


class TestPhaseTransitionsAndHeartbeat(SqliteRegistryTestCase):
    def test_create_run_starts_queued_with_progress(self) -> None:
        record = self.registry.create_run()
        self.assertEqual(record.phase, RunPhase.QUEUED)
        self.assertEqual(record.queued_at, record.created_at)
        self.assertIsNotNone(record.phase_started_at)
        self.assertIsNotNone(record.heartbeat_at)
        assert record.progress is not None
        self.assertEqual(record.progress["step"], "queued")

    def test_phase_transitions_refresh_phase_started_at(self) -> None:
        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        first = self.registry.set_phase(
            record.run_id,
            RunPhase.WORKSPACE_PREPARATION,
            progress=platform_progress(
                step="workspace_preparation",
                detail="Preparing isolated workspace",
            ),
        )
        assert first is not None
        time.sleep(0.01)
        second = self.registry.set_phase(
            record.run_id,
            RunPhase.AGENT_EXECUTION,
            progress=platform_progress(
                step="agent_execution",
                detail="Cursor agent subprocess running",
            ),
        )
        assert second is not None
        assert first.phase_started_at is not None
        assert second.phase_started_at is not None
        self.assertEqual(second.phase, RunPhase.AGENT_EXECUTION)
        self.assertGreaterEqual(second.phase_started_at, first.phase_started_at)
        self.assertEqual(second.progress["step"], "agent_execution")

    def test_heartbeat_refreshes_while_running(self) -> None:
        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        self.registry.set_phase(record.run_id, RunPhase.AGENT_EXECUTION)
        before = self.registry.get_run(record.run_id)
        assert before is not None and before.heartbeat_at is not None
        time.sleep(0.01)
        after = self.registry.touch_heartbeat(record.run_id)
        assert after is not None and after.heartbeat_at is not None
        self.assertGreater(after.heartbeat_at, before.heartbeat_at)

    def test_failure_path_sets_failed_phase(self) -> None:
        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        self.registry.set_phase(record.run_id, RunPhase.WORKSPACE_PREPARATION)
        self.registry.store_result(record.run_id, error="prep failed")
        failed = self.registry.update_status(record.run_id, RunStatus.FAILED)
        assert failed is not None
        self.assertEqual(failed.status, RunStatus.FAILED)
        self.assertEqual(failed.phase, RunPhase.FAILED)
        self.assertEqual(failed.progress["step"], "failed")


class TestTerminalMonotonicity(SqliteRegistryTestCase):
    def test_terminal_status_cannot_regress_to_running(self) -> None:
        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        completed = self.registry.update_status(
            record.run_id, RunStatus.COMPLETED
        )
        assert completed is not None
        completed_at = completed.completed_at

        regress = self.registry.update_status(record.run_id, RunStatus.RUNNING)
        assert regress is not None
        self.assertEqual(regress.status, RunStatus.COMPLETED)
        self.assertEqual(regress.completed_at, completed_at)
        self.assertEqual(regress.phase, RunPhase.COMPLETED)

        other_terminal = self.registry.update_status(
            record.run_id, RunStatus.FAILED
        )
        assert other_terminal is not None
        self.assertEqual(other_terminal.status, RunStatus.COMPLETED)

    def test_terminal_phase_blocks_active_phase_and_heartbeat(
        self,
    ) -> None:
        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        self.registry.store_result(record.run_id, stdout="keep-me")
        failed = self.registry.update_status(record.run_id, RunStatus.FAILED)
        assert failed is not None
        heartbeat_before = failed.heartbeat_at

        blocked_phase = self.registry.set_phase(
            record.run_id,
            RunPhase.AGENT_EXECUTION,
            progress=platform_progress(
                step="agent_execution",
                detail="should not apply",
            ),
        )
        assert blocked_phase is not None
        self.assertEqual(blocked_phase.phase, RunPhase.FAILED)
        self.assertEqual(blocked_phase.status, RunStatus.FAILED)

        blocked_hb = self.registry.touch_heartbeat(record.run_id)
        assert blocked_hb is not None
        self.assertEqual(blocked_hb.phase, RunPhase.FAILED)
        self.assertEqual(blocked_hb.heartbeat_at, heartbeat_before)


class TestStartupRecoveryObservability(SqliteRegistryTestCase):
    def test_recover_interrupted_runs_marks_failed_phase(self) -> None:
        queued = self.registry.create_run()
        healthy = self.registry.create_run()
        self.registry.update_status(healthy.run_id, RunStatus.RUNNING)
        self.registry.set_phase(healthy.run_id, RunPhase.AGENT_EXECUTION)
        orphaned = self.registry.create_run()
        self.registry.update_status(orphaned.run_id, RunStatus.RUNNING)
        self.registry.set_phase(orphaned.run_id, RunPhase.AGENT_EXECUTION)
        stale_at = datetime.now(timezone.utc) - timedelta(
            seconds=EXECUTION_LEASE_GRACE_SECONDS + 5
        )
        with self.registry._lock:
            self.registry._conn.execute(
                "UPDATE runs SET heartbeat_at = ? WHERE run_id = ?",
                (stale_at.isoformat(), orphaned.run_id),
            )
            self.registry._conn.commit()

        recovered = self.registry.recover_interrupted_runs()
        self.assertEqual(recovered, 1)

        queued_record = self.registry.get_run(queued.run_id)
        healthy_record = self.registry.get_run(healthy.run_id)
        orphaned_record = self.registry.get_run(orphaned.run_id)
        assert queued_record is not None
        assert healthy_record is not None
        assert orphaned_record is not None

        self.assertEqual(queued_record.status, RunStatus.QUEUED)
        self.assertEqual(queued_record.phase, RunPhase.QUEUED)
        self.assertIsNone(queued_record.error)
        self.assertNotEqual(queued_record.error, INTERRUPTED_RUN_ERROR)

        self.assertEqual(healthy_record.status, RunStatus.RUNNING)
        self.assertEqual(healthy_record.phase, RunPhase.AGENT_EXECUTION)
        self.assertIsNone(healthy_record.error)
        self.assertNotEqual(healthy_record.error, INTERRUPTED_RUN_ERROR)

        self.assertEqual(orphaned_record.status, RunStatus.FAILED)
        self.assertEqual(orphaned_record.phase, RunPhase.FAILED)
        self.assertEqual(orphaned_record.error, OWNER_LOST_RUN_ERROR)
        self.assertNotEqual(orphaned_record.error, INTERRUPTED_RUN_ERROR)
        assert orphaned_record.progress is not None
        self.assertEqual(orphaned_record.progress["step"], "failed")


class TestLegacySchemaMigration(unittest.TestCase):
    def test_missing_phase_columns_are_added_and_legacy_rows_read(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            conn.execute(
                """
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    elapsed_seconds REAL,
                    stdout TEXT NOT NULL DEFAULT '',
                    stderr TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    return_code INTEGER,
                    commit_sha TEXT,
                    result_json TEXT,
                    mission_yaml TEXT,
                    retried_from TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, status, created_at, started_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    "legacy-run",
                    "running",
                    "2026-08-13T00:00:00+00:00",
                    "2026-08-13T00:00:01+00:00",
                ),
            )
            conn.commit()
            conn.close()

            registry = RunRegistry(path)
            try:
                fetched = registry.get_run("legacy-run")
                assert fetched is not None
                self.assertEqual(fetched.status, RunStatus.RUNNING)
                self.assertEqual(fetched.phase, RunPhase.AGENT_EXECUTION)
                self.assertIsNotNone(fetched.phase_started_at)
                self.assertIsNotNone(fetched.progress)
            finally:
                registry.close()
        finally:
            os.unlink(path)


class TestWorkspacePhaseLifecycle(SqliteRegistryTestCase):
    def test_failure_during_prep_reaches_failed_phase(self) -> None:
        record = self.registry.create_run()
        with patch(
            "mission_control.workspace.prepare_isolated_workspace",
            return_value=WorkspacePrepResult(
                ok=False,
                error="clone failed",
            ),
        ):
            execute_registered_run(record.run_id, _mission(), self.registry)

        fetched = self.registry.get_run(record.run_id)
        assert fetched is not None
        self.assertEqual(fetched.status, RunStatus.FAILED)
        self.assertEqual(fetched.phase, RunPhase.FAILED)
        self.assertEqual(fetched.error, "clone failed")

    def test_successful_path_passes_through_active_phases(self) -> None:
        record = self.registry.create_run()
        seen: list[str] = []
        original_set_phase = self.registry.set_phase

        def _track(run_id: str, phase: RunPhase, **kwargs):
            seen.append(phase.value)
            return original_set_phase(run_id, phase, **kwargs)

        workspace = tempfile.mkdtemp(prefix="mc-live-status-")
        try:
            with (
                patch.object(self.registry, "set_phase", side_effect=_track),
                _patched_successful_workspace_run(workspace),
            ):
                execute_registered_run(
                    record.run_id, _mission(), self.registry
                )
        finally:
            try:
                os.rmdir(workspace)
            except OSError:
                pass

        fetched = self.registry.get_run(record.run_id)
        assert fetched is not None
        self.assertEqual(fetched.status, RunStatus.COMPLETED)
        self.assertEqual(fetched.phase, RunPhase.COMPLETED)
        self.assertIn("workspace_preparation", seen)
        self.assertIn("agent_execution", seen)
        self.assertIn("verification", seen)
        self.assertIn("persistence", seen)
        self.assertIn("cleanup", seen)
        # Terminal phase is applied by update_status, not a separate set_phase.
        self.assertNotIn("completed", seen)
        self.assertEqual(fetched.progress["step"], "completed")
        self.assertEqual(fetched.stdout, "agent ok")

    def test_cleanup_workspace_exception_preserves_completed_result(
        self,
    ) -> None:
        record = self.registry.create_run()
        workspace = tempfile.mkdtemp(prefix="mc-live-status-")
        try:
            with _patched_successful_workspace_run(
                workspace,
                cleanup_side_effect=RuntimeError("cleanup boom"),
            ):
                execute_registered_run(
                    record.run_id, _mission(), self.registry
                )
        finally:
            try:
                os.rmdir(workspace)
            except OSError:
                pass

        fetched = self.registry.get_run(record.run_id)
        assert fetched is not None
        self.assertEqual(fetched.status, RunStatus.COMPLETED)
        self.assertEqual(fetched.phase, RunPhase.COMPLETED)
        self.assertEqual(fetched.stdout, "agent ok")
        self.assertIsNone(fetched.error)

    def test_cleanup_phase_update_exception_still_terminalizes(
        self,
    ) -> None:
        record = self.registry.create_run()
        original_set_phase = self.registry.set_phase

        def _fail_cleanup(run_id: str, phase: RunPhase, **kwargs):
            if phase is RunPhase.CLEANUP:
                raise RuntimeError("cleanup phase boom")
            return original_set_phase(run_id, phase, **kwargs)

        workspace = tempfile.mkdtemp(prefix="mc-live-status-")
        try:
            with (
                patch.object(
                    self.registry, "set_phase", side_effect=_fail_cleanup
                ),
                _patched_successful_workspace_run(workspace),
            ):
                execute_registered_run(
                    record.run_id, _mission(), self.registry
                )
        finally:
            try:
                os.rmdir(workspace)
            except OSError:
                pass

        fetched = self.registry.get_run(record.run_id)
        assert fetched is not None
        self.assertEqual(fetched.status, RunStatus.COMPLETED)
        self.assertEqual(fetched.phase, RunPhase.COMPLETED)

    def test_terminal_phase_update_exception_still_terminalizes(
        self,
    ) -> None:
        """Authoritative status must not depend on a terminal set_phase write."""
        record = self.registry.create_run()
        original_set_phase = self.registry.set_phase

        def _fail_terminal(run_id: str, phase: RunPhase, **kwargs):
            if phase in (RunPhase.COMPLETED, RunPhase.FAILED):
                raise RuntimeError("terminal phase boom")
            return original_set_phase(run_id, phase, **kwargs)

        workspace = tempfile.mkdtemp(prefix="mc-live-status-")
        try:
            with (
                patch.object(
                    self.registry, "set_phase", side_effect=_fail_terminal
                ),
                _patched_successful_workspace_run(workspace),
            ):
                execute_registered_run(
                    record.run_id, _mission(), self.registry
                )
        finally:
            try:
                os.rmdir(workspace)
            except OSError:
                pass

        fetched = self.registry.get_run(record.run_id)
        assert fetched is not None
        self.assertEqual(fetched.status, RunStatus.COMPLETED)
        self.assertEqual(fetched.phase, RunPhase.COMPLETED)

    def test_failure_and_timed_out_paths_remain_truthful(self) -> None:
        failed_record = self.registry.create_run()
        with patch(
            "mission_control.workspace.prepare_isolated_workspace",
            return_value=WorkspacePrepResult(
                ok=False,
                error="clone failed",
            ),
        ):
            execute_registered_run(
                failed_record.run_id, _mission(), self.registry
            )
        failed = self.registry.get_run(failed_record.run_id)
        assert failed is not None
        self.assertEqual(failed.status, RunStatus.FAILED)
        self.assertEqual(failed.phase, RunPhase.FAILED)
        self.assertEqual(failed.error, "clone failed")

        timed_record = self.registry.create_run()
        workspace = tempfile.mkdtemp(prefix="mc-live-status-")
        try:
            with _patched_successful_workspace_run(
                workspace,
                execution_result=ExecutionResult(
                    ok=False,
                    stdout="",
                    stderr="agent timed out waiting",
                    return_code=-1,
                    error="Cursor agent timed out after 1s",
                ),
            ):
                execute_registered_run(
                    timed_record.run_id, _mission(), self.registry
                )
        finally:
            try:
                os.rmdir(workspace)
            except OSError:
                pass

        timed = self.registry.get_run(timed_record.run_id)
        assert timed is not None
        self.assertEqual(timed.status, RunStatus.TIMED_OUT)
        self.assertEqual(timed.phase, RunPhase.FAILED)
        self.assertIn("timed out", timed.error or "")

    def test_stale_worker_cannot_regress_terminal_after_success(
        self,
    ) -> None:
        record = self.registry.create_run()
        workspace = tempfile.mkdtemp(prefix="mc-live-status-")
        try:
            with _patched_successful_workspace_run(workspace):
                execute_registered_run(
                    record.run_id, _mission(), self.registry
                )
        finally:
            try:
                os.rmdir(workspace)
            except OSError:
                pass

        completed = self.registry.get_run(record.run_id)
        assert completed is not None
        completed_at = completed.completed_at

        regress_status = self.registry.update_status(
            record.run_id, RunStatus.RUNNING
        )
        assert regress_status is not None
        self.assertEqual(regress_status.status, RunStatus.COMPLETED)
        self.assertEqual(regress_status.completed_at, completed_at)

        regress_phase = self.registry.set_phase(
            record.run_id,
            RunPhase.CLEANUP,
            progress=platform_progress(
                step="cleanup",
                detail="stale cleanup",
            ),
        )
        assert regress_phase is not None
        self.assertEqual(regress_phase.status, RunStatus.COMPLETED)
        self.assertEqual(regress_phase.phase, RunPhase.COMPLETED)

        other_terminal = self.registry.update_status(
            record.run_id, RunStatus.FAILED
        )
        assert other_terminal is not None
        self.assertEqual(other_terminal.status, RunStatus.COMPLETED)

    def test_successful_path_does_not_leak_heartbeat_thread(self) -> None:
        record = self.registry.create_run()
        workspace = tempfile.mkdtemp(prefix="mc-live-status-")
        before = set(_heartbeat_thread_names())
        try:
            with _patched_successful_workspace_run(workspace):
                execute_registered_run(
                    record.run_id, _mission(), self.registry
                )
        finally:
            try:
                os.rmdir(workspace)
            except OSError:
                pass

        after = set(_heartbeat_thread_names())
        self.assertEqual(after - before, set())
        fetched = self.registry.get_run(record.run_id)
        assert fetched is not None
        self.assertEqual(fetched.status, RunStatus.COMPLETED)


class TestLiveStatusApiSerialization(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        api_module.run_registry = RunRegistry(self._db_path)
        api_module.run_queue = RunQueue()
        api_module.run_queue.configure(api_module._execute_queued_run)
        self.client = TestClient(app, headers=AUTH_HEADERS)

    def tearDown(self) -> None:
        api_module.run_registry.close()
        os.unlink(self._db_path)

    def test_get_run_includes_live_observability_fields(self) -> None:
        record = api_module.run_registry.create_run()
        api_module.run_registry.update_status(record.run_id, RunStatus.RUNNING)
        api_module.run_registry.set_phase(
            record.run_id,
            RunPhase.AGENT_EXECUTION,
            progress=platform_progress(
                step="agent_execution",
                detail="Cursor agent subprocess running",
            ),
        )

        response = self.client.get(f"/runs/{record.run_id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "running")
        self.assertEqual(body["queued_at"], body["created_at"])
        self.assertEqual(body["phase"], "agent_execution")
        self.assertIsNotNone(body["phase_started_at"])
        self.assertIsNotNone(body["heartbeat_at"])
        self.assertEqual(
            body["progress"],
            {
                "step": "agent_execution",
                "detail": "Cursor agent subprocess running",
            },
        )
        parsed = datetime.fromisoformat(body["queued_at"])
        self.assertIsNotNone(parsed.tzinfo)


if __name__ == "__main__":
    unittest.main()
