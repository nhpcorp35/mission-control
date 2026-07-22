"""Tests for durable SQLite-backed run persistence."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from mission_control.run_registry import (
    INTERRUPTED_RUN_ERROR,
    RunRegistry,
    RunStatus,
    resolve_db_path,
)
from tests.registry_test_utils import SqliteRegistryTestCase


class TestRegistryPersistenceAcrossInstances(SqliteRegistryTestCase):
    def test_run_written_by_one_registry_is_read_by_another(self) -> None:
        created = self.registry.create_run()
        self.registry.update_status(created.run_id, RunStatus.RUNNING)
        self.registry.store_result(
            created.run_id,
            stdout="hello",
            stderr="warn",
            error="boom",
            commit_sha="abc123",
        )
        self.registry.update_status(created.run_id, RunStatus.COMPLETED)
        self.registry.close()

        other = RunRegistry(self._db_path, recover=False)
        try:
            fetched = other.get_run(created.run_id)
            self.assertIsNotNone(fetched)
            assert fetched is not None
            self.assertEqual(fetched.status, RunStatus.COMPLETED)
            self.assertEqual(fetched.stdout, "hello")
            self.assertEqual(fetched.stderr, "warn")
            self.assertEqual(fetched.error, "boom")
            self.assertEqual(fetched.commit_sha, "abc123")
            self.assertIsNotNone(fetched.started_at)
            self.assertIsNotNone(fetched.completed_at)
            self.assertIsNotNone(fetched.elapsed_seconds)
        finally:
            other.close()

    def test_state_updates_persist_immediately(self) -> None:
        created = self.registry.create_run()
        self.registry.close()

        other = RunRegistry(self._db_path, recover=False)
        try:
            other.update_status(created.run_id, RunStatus.RUNNING)
            other.close()

            third = RunRegistry(self._db_path, recover=False)
            try:
                fetched = third.get_run(created.run_id)
                assert fetched is not None
                self.assertEqual(fetched.status, RunStatus.RUNNING)
                self.assertIsNotNone(fetched.started_at)
            finally:
                third.close()
        finally:
            pass


class TestInterruptedRunRecovery(SqliteRegistryTestCase):
    def test_unfinished_runs_are_marked_failed_on_recovery(self) -> None:
        queued = self.registry.create_run()
        running = self.registry.create_run()
        self.registry.update_status(running.run_id, RunStatus.RUNNING)
        self.registry.close()

        recovered_registry = RunRegistry(self._db_path, recover=True)
        try:
            queued_record = recovered_registry.get_run(queued.run_id)
            running_record = recovered_registry.get_run(running.run_id)
            assert queued_record is not None
            assert running_record is not None

            self.assertEqual(queued_record.status, RunStatus.FAILED)
            self.assertEqual(queued_record.error, INTERRUPTED_RUN_ERROR)
            self.assertIsNotNone(queued_record.completed_at)
            self.assertIsNone(queued_record.elapsed_seconds)

            self.assertEqual(running_record.status, RunStatus.FAILED)
            self.assertEqual(running_record.error, INTERRUPTED_RUN_ERROR)
            self.assertIsNotNone(running_record.completed_at)
            self.assertIsNotNone(running_record.elapsed_seconds)
        finally:
            recovered_registry.close()


class TestRegistryDefaults(unittest.TestCase):
    def test_resolve_db_path_uses_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_db_path(), "./data/mission-control.db")

    def test_resolve_db_path_honors_environment(self) -> None:
        with patch.dict(
            os.environ,
            {"MISSION_CONTROL_DB_PATH": "/tmp/custom.db"},
            clear=True,
        ):
            self.assertEqual(resolve_db_path(), "/tmp/custom.db")

    def test_registry_creates_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "nested", "mission-control.db")
            registry = RunRegistry(db_path, recover=False)
            try:
                self.assertTrue(os.path.isdir(os.path.dirname(db_path)))
                registry.create_run()
                self.assertEqual(registry.count_runs(), 1)
            finally:
                registry.close()


if __name__ == "__main__":
    unittest.main()
