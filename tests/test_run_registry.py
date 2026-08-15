"""Focused tests for the SQLite-backed run registry."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import threading
import unittest
import uuid

from mission_control.run_registry import (
    CONFLICT_EXECUTION_MISMATCH,
    CONFLICT_EXISTING_RUN_COLLISION,
    CONFLICT_INVALID_RUN_ID,
    CONFLICT_MISSING_BINDING_IDENTITY,
    CONFLICT_MISSION_YAML_MISMATCH,
    CONFLICT_NONCANONICAL_RUN_ID,
    CONFLICT_OWNERSHIP_MISMATCH,
    CONFLICT_PERMISSIONS_MISMATCH,
    CONFLICT_REPOSITORY_MISMATCH,
    ReservedRunOutcome,
    RunRegistry,
    RunStatus,
)
from tests.registry_test_utils import SqliteRegistryTestCase


def _mission_yaml(
    *,
    repository_name: str = "demo-repo",
    agent: str = "cursor",
    mode: str = "execute",
    create_files: bool = False,
    instructions: str = "do the thing",
) -> str:
    create = "true" if create_files else "false"
    return (
        "version: '1.0'\n"
        "mission_id: reserved-test\n"
        "title: Reserved ID test\n"
        "repository:\n"
        f"  name: {repository_name}\n"
        "  path: .\n"
        "  base_branch: main\n"
        "execution:\n"
        f"  agent: {agent}\n"
        f"  mode: {mode}\n"
        "permissions:\n"
        "  read: true\n"
        f"  create_files: {create}\n"
        "  modify_files: false\n"
        "  delete_files: false\n"
        "  run_commands: false\n"
        "  stage_changes: false\n"
        "  commit: false\n"
        "  push: false\n"
        f"instructions: |\n  {instructions}\n"
        "deliverables: []\n"
        "approval:\n"
        "  execute_without_approval: true\n"
        "  commit_requires_approval: true\n"
        "  push_requires_approval: true\n"
    )


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


class TestReservedRunIds(SqliteRegistryTestCase):
    def test_reserved_create_uses_caller_id(self) -> None:
        reserved = str(uuid.uuid4())
        yaml_text = _mission_yaml()
        result = self.registry.create_run(
            run_id=reserved,
            mission_yaml=yaml_text,
            retried_from="parent-1",
        )

        self.assertEqual(result.outcome, ReservedRunOutcome.CREATED)
        self.assertIsNotNone(result.record)
        assert result.record is not None
        self.assertEqual(result.record.run_id, reserved)
        self.assertEqual(result.record.status, RunStatus.QUEUED)
        self.assertEqual(result.record.mission_yaml, yaml_text)
        self.assertEqual(result.record.retried_from, "parent-1")
        self.assertIsNone(result.conflict_class)

        fetched = self.registry.get_run(reserved)
        assert fetched is not None
        self.assertEqual(fetched.mission_yaml, yaml_text)

    def test_default_allocation_unchanged_without_run_id(self) -> None:
        record = self.registry.create_run(mission_yaml=_mission_yaml())
        self.assertIsInstance(record.run_id, str)
        self.assertEqual(uuid.UUID(record.run_id).version, 4)

    def test_malformed_run_id_conflicts(self) -> None:
        result = self.registry.create_run(
            run_id="not-a-uuid",
            mission_yaml=_mission_yaml(),
        )
        self.assertEqual(result.outcome, ReservedRunOutcome.CONFLICT)
        self.assertIsNone(result.record)
        self.assertEqual(result.conflict_class, CONFLICT_INVALID_RUN_ID)
        self.assertEqual(self.registry.count_runs(), 0)

    def test_noncanonical_run_id_conflicts(self) -> None:
        canonical = str(uuid.uuid4())
        upper = canonical.upper()
        result = self.registry.create_run(
            run_id=upper,
            mission_yaml=_mission_yaml(),
        )
        self.assertEqual(result.outcome, ReservedRunOutcome.CONFLICT)
        self.assertEqual(result.conflict_class, CONFLICT_NONCANONICAL_RUN_ID)
        self.assertEqual(self.registry.count_runs(), 0)

        compact = canonical.replace("-", "")
        compact_result = self.registry.create_run(
            run_id=compact,
            mission_yaml=_mission_yaml(),
        )
        self.assertEqual(
            compact_result.conflict_class, CONFLICT_NONCANONICAL_RUN_ID
        )

    def test_exact_idempotent_replay(self) -> None:
        reserved = str(uuid.uuid4())
        yaml_text = _mission_yaml()
        first = self.registry.create_run(
            run_id=reserved,
            mission_yaml=yaml_text,
            retried_from="wf-parent",
        )
        self.assertEqual(first.outcome, ReservedRunOutcome.CREATED)
        created_at = first.record.created_at if first.record else None

        second = self.registry.create_run(
            run_id=reserved,
            mission_yaml=yaml_text,
            retried_from="wf-parent",
        )
        self.assertEqual(
            second.outcome, ReservedRunOutcome.RECOVERED_IDEMPOTENTLY
        )
        assert second.record is not None
        self.assertEqual(second.record.run_id, reserved)
        self.assertEqual(second.record.created_at, created_at)
        self.assertEqual(self.registry.count_runs(), 1)

    def test_mission_yaml_mismatch(self) -> None:
        reserved = str(uuid.uuid4())
        base = _mission_yaml(instructions="first")
        self.registry.create_run(
            run_id=reserved,
            mission_yaml=base,
            retried_from="parent",
        )
        conflict = self.registry.create_run(
            run_id=reserved,
            mission_yaml=_mission_yaml(instructions="second"),
            retried_from="parent",
        )
        self.assertEqual(conflict.outcome, ReservedRunOutcome.CONFLICT)
        self.assertEqual(
            conflict.conflict_class, CONFLICT_MISSION_YAML_MISMATCH
        )
        assert conflict.record is not None
        self.assertEqual(conflict.record.mission_yaml, base)

    def test_permissions_mismatch(self) -> None:
        reserved = str(uuid.uuid4())
        self.registry.create_run(
            run_id=reserved,
            mission_yaml=_mission_yaml(create_files=False),
            retried_from="parent",
        )
        conflict = self.registry.create_run(
            run_id=reserved,
            mission_yaml=_mission_yaml(create_files=True),
            retried_from="parent",
        )
        self.assertEqual(
            conflict.conflict_class, CONFLICT_PERMISSIONS_MISMATCH
        )

    def test_execution_mismatch(self) -> None:
        reserved = str(uuid.uuid4())
        self.registry.create_run(
            run_id=reserved,
            mission_yaml=_mission_yaml(mode="execute"),
            retried_from="parent",
        )
        conflict = self.registry.create_run(
            run_id=reserved,
            mission_yaml=_mission_yaml(mode="plan"),
            retried_from="parent",
        )
        self.assertEqual(conflict.conflict_class, CONFLICT_EXECUTION_MISMATCH)

        agent_conflict = self.registry.create_run(
            run_id=reserved,
            mission_yaml=_mission_yaml(agent="other-agent"),
            retried_from="parent",
        )
        self.assertEqual(
            agent_conflict.conflict_class, CONFLICT_EXECUTION_MISMATCH
        )

    def test_repository_mismatch(self) -> None:
        reserved = str(uuid.uuid4())
        self.registry.create_run(
            run_id=reserved,
            mission_yaml=_mission_yaml(repository_name="repo-a"),
            retried_from="parent",
        )
        conflict = self.registry.create_run(
            run_id=reserved,
            mission_yaml=_mission_yaml(repository_name="repo-b"),
            retried_from="parent",
        )
        self.assertEqual(
            conflict.conflict_class, CONFLICT_REPOSITORY_MISMATCH
        )

    def test_ownership_mismatch(self) -> None:
        reserved = str(uuid.uuid4())
        yaml_text = _mission_yaml()
        self.registry.create_run(
            run_id=reserved,
            mission_yaml=yaml_text,
            retried_from="workflow-a",
        )
        conflict = self.registry.create_run(
            run_id=reserved,
            mission_yaml=yaml_text,
            retried_from="workflow-b",
        )
        self.assertEqual(conflict.conflict_class, CONFLICT_OWNERSHIP_MISMATCH)

    def test_unrelated_existing_run_collision(self) -> None:
        existing = self.registry.create_run()
        conflict = self.registry.create_run(
            run_id=existing.run_id,
            mission_yaml=_mission_yaml(),
            retried_from="workflow-child",
        )
        self.assertEqual(conflict.outcome, ReservedRunOutcome.CONFLICT)
        self.assertEqual(
            conflict.conflict_class, CONFLICT_EXISTING_RUN_COLLISION
        )
        assert conflict.record is not None
        self.assertIsNone(conflict.record.mission_yaml)
        # Existing row must remain untouched.
        fetched = self.registry.get_run(existing.run_id)
        assert fetched is not None
        self.assertIsNone(fetched.mission_yaml)
        self.assertEqual(fetched.status, RunStatus.QUEUED)

    def test_concurrent_same_id_two_connections(self) -> None:
        reserved = str(uuid.uuid4())
        yaml_text = _mission_yaml()
        barrier = threading.Barrier(2)
        outcomes: list[ReservedRunOutcome] = []
        lock = threading.Lock()

        def worker() -> None:
            registry = RunRegistry(self._db_path)
            try:
                barrier.wait(timeout=5)
                result = registry.create_run(
                    run_id=reserved,
                    mission_yaml=yaml_text,
                    retried_from="parent",
                )
                with lock:
                    outcomes.append(result.outcome)
            finally:
                registry.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

        self.assertEqual(len(outcomes), 2)
        self.assertEqual(
            set(outcomes),
            {
                ReservedRunOutcome.CREATED,
                ReservedRunOutcome.RECOVERED_IDEMPOTENTLY,
            },
        )
        self.assertEqual(self.registry.count_runs(), 1)
        fetched = self.registry.get_run(reserved)
        assert fetched is not None
        self.assertEqual(fetched.mission_yaml, yaml_text)

    def test_blank_legacy_row_omitted_identity_cannot_recover(self) -> None:
        legacy = self.registry.create_run()
        conflict = self.registry.create_run(run_id=legacy.run_id)

        self.assertEqual(conflict.outcome, ReservedRunOutcome.CONFLICT)
        self.assertEqual(
            conflict.conflict_class, CONFLICT_MISSING_BINDING_IDENTITY
        )
        self.assertIsNone(conflict.record)
        fetched = self.registry.get_run(legacy.run_id)
        assert fetched is not None
        self.assertIsNone(fetched.mission_yaml)
        self.assertIsNone(fetched.retried_from)
        self.assertEqual(self.registry.count_runs(), 1)

    def test_reserved_missing_or_blank_identity_rejected_without_insert(
        self,
    ) -> None:
        reserved = str(uuid.uuid4())
        yaml_text = _mission_yaml()
        cases = [
            {"mission_yaml": None, "retried_from": "owner"},
            {"mission_yaml": "", "retried_from": "owner"},
            {"mission_yaml": "   \n", "retried_from": "owner"},
            {"mission_yaml": yaml_text, "retried_from": None},
            {"mission_yaml": yaml_text, "retried_from": ""},
            {"mission_yaml": yaml_text, "retried_from": "  \t"},
            {},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                result = self.registry.create_run(run_id=reserved, **kwargs)
                self.assertEqual(result.outcome, ReservedRunOutcome.CONFLICT)
                self.assertEqual(
                    result.conflict_class, CONFLICT_MISSING_BINDING_IDENTITY
                )
                self.assertIsNone(result.record)
                self.assertEqual(self.registry.count_runs(), 0)
                self.assertIsNone(self.registry.get_run(reserved))

    def test_valid_binding_creates_and_recovers(self) -> None:
        reserved = str(uuid.uuid4())
        yaml_text = _mission_yaml()
        first = self.registry.create_run(
            run_id=reserved,
            mission_yaml=yaml_text,
            retried_from="owner-1",
        )
        self.assertEqual(first.outcome, ReservedRunOutcome.CREATED)
        assert first.record is not None
        self.assertEqual(first.record.retried_from, "owner-1")

        second = self.registry.create_run(
            run_id=reserved,
            mission_yaml=yaml_text,
            retried_from="owner-1",
        )
        self.assertEqual(
            second.outcome, ReservedRunOutcome.RECOVERED_IDEMPOTENTLY
        )
        assert second.record is not None
        self.assertEqual(second.record.run_id, reserved)
        self.assertEqual(second.record.retried_from, "owner-1")
        self.assertEqual(self.registry.count_runs(), 1)

    def test_status_persist_does_not_mutate_immutable_identity(self) -> None:
        reserved = str(uuid.uuid4())
        yaml_text = _mission_yaml()
        created = self.registry.create_run(
            run_id=reserved,
            mission_yaml=yaml_text,
            retried_from="immutable-owner",
        )
        assert created.record is not None
        updated = self.registry.update_status(reserved, RunStatus.RUNNING)
        assert updated is not None
        self.assertEqual(updated.mission_yaml, yaml_text)
        self.assertEqual(updated.retried_from, "immutable-owner")
        fetched = self.registry.get_run(reserved)
        assert fetched is not None
        self.assertEqual(fetched.mission_yaml, yaml_text)
        self.assertEqual(fetched.retried_from, "immutable-owner")


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
