"""Focused tests for the SQLite-backed run registry."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import unittest
import uuid

from mission_control.run_registry import RunRegistry, RunStatus
from tests.registry_test_utils import SqliteRegistryTestCase


class TestRunCreation(SqliteRegistryTestCase):
    def test_create_run_returns_queued_record(self) -> None:
        record = self.registry.create_run()

        self.assertEqual(record.status, RunStatus.QUEUED)
        self.assertIsInstance(record.created_at, datetime)
        self.assertEqual(record.created_at.tzinfo, timezone.utc)
        self.assertIsNone(record.started_at)
        self.assertIsNone(record.completed_at)
        self.assertIsNone(record.elapsed_seconds)
        self.assertEqual(record.stdout, "")
        self.assertEqual(record.stderr, "")
        self.assertIsNone(record.error)

    def test_create_run_uses_uuid4(self) -> None:
        record = self.registry.create_run()
        parsed = uuid.UUID(record.run_id)

        self.assertEqual(parsed.version, 4)
        self.assertEqual(str(parsed), record.run_id)

    def test_create_run_generates_unique_ids(self) -> None:
        ids = {self.registry.create_run().run_id for _ in range(50)}
        self.assertEqual(len(ids), 50)


class TestRetrieveAndUnknown(SqliteRegistryTestCase):
    def test_get_run_returns_created_record(self) -> None:
        created = self.registry.create_run()
        fetched = self.registry.get_run(created.run_id)

        self.assertIsNotNone(fetched)
        assert fetched is not None
        self.assertEqual(fetched.run_id, created.run_id)
        self.assertEqual(fetched.status, RunStatus.QUEUED)

    def test_get_unknown_id_returns_none(self) -> None:
        self.assertIsNone(self.registry.get_run("missing-run-id"))

    def test_update_unknown_id_returns_none(self) -> None:
        self.assertIsNone(
            self.registry.update_status("missing-run-id", RunStatus.RUNNING)
        )

    def test_store_result_unknown_id_returns_none(self) -> None:
        self.assertIsNone(
            self.registry.store_result("missing-run-id", stdout="x")
        )


class TestStatusTransitionsAndTimestamps(SqliteRegistryTestCase):
    def test_queued_to_running_sets_started_at(self) -> None:
        record = self.registry.create_run()
        updated = self.registry.update_status(record.run_id, RunStatus.RUNNING)

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.status, RunStatus.RUNNING)
        self.assertIsNotNone(updated.started_at)
        assert updated.started_at is not None
        self.assertEqual(updated.started_at.tzinfo, timezone.utc)
        self.assertIsNone(updated.completed_at)
        self.assertIsNone(updated.elapsed_seconds)

    def test_running_to_completed_sets_completed_and_elapsed(self) -> None:
        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        started = self.registry.get_run(record.run_id)
        assert started is not None and started.started_at is not None

        completed = self.registry.update_status(
            record.run_id, RunStatus.COMPLETED
        )
        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertEqual(completed.status, RunStatus.COMPLETED)
        self.assertIsNotNone(completed.completed_at)
        assert completed.completed_at is not None
        self.assertEqual(completed.completed_at.tzinfo, timezone.utc)
        self.assertIsNotNone(completed.elapsed_seconds)
        assert completed.elapsed_seconds is not None
        self.assertGreaterEqual(completed.elapsed_seconds, 0.0)
        expected = (
            completed.completed_at - started.started_at
        ).total_seconds()
        self.assertAlmostEqual(completed.elapsed_seconds, expected)

    def test_failed_and_timed_out_transitions(self) -> None:
        failed_run = self.registry.create_run()
        self.registry.update_status(failed_run.run_id, RunStatus.RUNNING)
        failed = self.registry.update_status(failed_run.run_id, RunStatus.FAILED)
        self.assertIsNotNone(failed)
        assert failed is not None
        self.assertEqual(failed.status, RunStatus.FAILED)
        self.assertIsNotNone(failed.completed_at)
        self.assertIsNotNone(failed.elapsed_seconds)

        timed_out_run = self.registry.create_run()
        self.registry.update_status(timed_out_run.run_id, RunStatus.RUNNING)
        timed_out = self.registry.update_status(
            timed_out_run.run_id, RunStatus.TIMED_OUT
        )
        self.assertIsNotNone(timed_out)
        assert timed_out is not None
        self.assertEqual(timed_out.status, RunStatus.TIMED_OUT)
        self.assertIsNotNone(timed_out.completed_at)
        self.assertIsNotNone(timed_out.elapsed_seconds)


class TestResultStorage(SqliteRegistryTestCase):
    def test_store_result_persists_stdout_stderr_error(self) -> None:
        record = self.registry.create_run()
        updated = self.registry.store_result(
            record.run_id,
            stdout="out",
            stderr="err",
            error="boom",
            return_code=7,
        )

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.stdout, "out")
        self.assertEqual(updated.stderr, "err")
        self.assertEqual(updated.error, "boom")
        self.assertEqual(updated.return_code, 7)
        self.assertIsNone(updated.commit_sha)
        self.assertEqual(updated.status, RunStatus.QUEUED)

        fetched = self.registry.get_run(record.run_id)
        assert fetched is not None
        self.assertEqual(fetched.stdout, "out")
        self.assertEqual(fetched.stderr, "err")
        self.assertEqual(fetched.error, "boom")
        self.assertEqual(fetched.return_code, 7)
        self.assertIsNone(fetched.commit_sha)

    def test_terminal_failure_retains_existing_record(self) -> None:
        record = self.registry.create_run()
        run_id = record.run_id
        self.registry.update_status(run_id, RunStatus.RUNNING)
        self.registry.store_result(
            run_id,
            stdout="partial",
            stderr="boom-stderr",
            error="boom",
            return_code=3,
        )
        self.registry.update_status(run_id, RunStatus.FAILED)

        self.assertEqual(self.registry.count_runs(), 1)
        fetched = self.registry.get_run(run_id)
        assert fetched is not None
        self.assertEqual(fetched.status, RunStatus.FAILED)
        self.assertEqual(fetched.error, "boom")
        self.assertEqual(fetched.stderr, "boom-stderr")
        self.assertEqual(fetched.stdout, "partial")
        self.assertEqual(fetched.return_code, 3)
        self.assertIsNotNone(fetched.completed_at)


class TestCommitShaStorage(SqliteRegistryTestCase):
    def test_store_result_persists_commit_sha(self) -> None:
        record = self.registry.create_run()
        updated = self.registry.store_result(
            record.run_id,
            stdout="done",
            commit_sha="abc123def4567890",
        )

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.commit_sha, "abc123def4567890")

        fetched = self.registry.get_run(record.run_id)
        assert fetched is not None
        self.assertEqual(fetched.commit_sha, "abc123def4567890")

    def test_store_result_leaves_commit_sha_none_by_default(self) -> None:
        record = self.registry.create_run()
        self.registry.store_result(record.run_id, stdout="done")

        fetched = self.registry.get_run(record.run_id)
        assert fetched is not None
        self.assertIsNone(fetched.commit_sha)


class TestConcurrentAccess(SqliteRegistryTestCase):
    def test_concurrent_create_is_safe(self) -> None:
        count = 100

        def create_one(_: int) -> str:
            return self.registry.create_run().run_id

        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(create_one, i) for i in range(count)]
            ids = [future.result() for future in as_completed(futures)]

        self.assertEqual(len(ids), count)
        self.assertEqual(len(set(ids)), count)
        for run_id in ids:
            self.assertIsNotNone(self.registry.get_run(run_id))


if __name__ == "__main__":
    unittest.main()
