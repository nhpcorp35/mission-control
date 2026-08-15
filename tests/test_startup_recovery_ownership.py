"""Replica-safe startup recovery: ownership, lease, and requeue regressions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import multiprocessing
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from mission_control.run_queue import RunQueue
from mission_control.run_registry import (
    EXECUTION_LEASE_FUTURE_SKEW_TOLERANCE_SECONDS,
    EXECUTION_LEASE_GRACE_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    INTERRUPTED_RUN_ERROR,
    LEGACY_INTERRUPT_REFUSED_ERROR,
    OBSERVED_FALSE_INTERRUPT_SECONDS,
    OWNER_LOST_RUN_ERROR,
    STARTUP_RECOVERY_LEASE_NAME,
    STARTUP_RECOVERY_POLICY_VERSION,
    RunPhase,
    RunRegistry,
    RunStatus,
    _BUILD_FINGERPRINT_LEN,
    _LEGACY_INTERRUPT_INSERT_TRIGGER,
    _LEGACY_INTERRUPT_UPDATE_TRIGGER,
    _PYTHON_STRIP_WHITESPACE_CODEPOINTS,
    _SQL_STRIP_CHARS,
    _sql_trim_chars_expr,
    bound_terminal_provenance,
    build_terminal_provenance,
    deployed_build_marker,
    is_legacy_interrupt_error,
    refuse_legacy_interrupt_error,
    sanitize_build_fingerprint,
    sanitize_progress,
    startup_recovery_module_identity,
    startup_recovery_policy_diagnostics,
)
from tests.registry_test_utils import SqliteRegistryTestCase

_LEASE_TABLE = "startup_recovery_leases"
_RUNS_TABLE = "runs"

# Observed production run ids (false interruption under prior recovery).
_OBS_RUNNING_ID = "d48bf6ef-f783-43e7-8327-a5d6a0966557"
_OBS_QUEUED_ID = "fcaf5a05-5ac0-4128-8609-1350df3baa30"

_MINIMAL_MISSION_YAML = """
version: "1.0"
mission_id: startup-recovery-fixture
title: Startup recovery fixture
repository:
  name: example/repo
  path: .
execution:
  agent: cursor
  mode: plan
  sandbox: true
  worktree: true
permissions:
  read: true
  create_files: false
  modify_files: false
  delete_files: false
  run_commands: false
  stage_changes: false
  commit: false
  push: false
instructions: |
  no-op fixture
deliverables:
  - none
approval:
  execute_without_approval: true
  commit_requires_approval: true
  push_requires_approval: true
"""


def _recover_worker(
    db_path: str,
    ready: multiprocessing.synchronize.Event,
    start: multiprocessing.synchronize.Event,
    result_queue: multiprocessing.Queue,
) -> None:
    registry = RunRegistry(db_path)
    ready.set()
    start.wait(timeout=10)
    try:
        recovered = registry.recover_interrupted_runs()
        result_queue.put(("ok", recovered, os.getpid()))
    except Exception as exc:  # pragma: no cover - surfaced to parent
        result_queue.put(("err", f"{type(exc).__name__}: {exc}", os.getpid()))
    finally:
        registry.close()


def _claim_worker(
    db_path: str,
    run_id: str,
    ready: multiprocessing.synchronize.Event,
    start: multiprocessing.synchronize.Event,
    result_queue: multiprocessing.Queue,
) -> None:
    registry = RunRegistry(db_path)
    ready.set()
    start.wait(timeout=10)
    try:
        claimed = registry.try_claim_run(run_id)
        result_queue.put(
            (
                "ok",
                claimed is not None,
                None if claimed is None else claimed.execution_owner,
                os.getpid(),
            )
        )
    except Exception as exc:  # pragma: no cover
        result_queue.put(("err", f"{type(exc).__name__}: {exc}", None, os.getpid()))
    finally:
        registry.close()


def _insert_lease(
    db_path: str,
    *,
    owner_token: str,
    expires_at: datetime,
    acquired_at: datetime | None = None,
) -> None:
    acquired = acquired_at or (expires_at - timedelta(seconds=30))
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"""
            INSERT INTO {_LEASE_TABLE} (
                name, owner_token, acquired_at, expires_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                owner_token = excluded.owner_token,
                acquired_at = excluded.acquired_at,
                expires_at = excluded.expires_at
            """,
            (
                STARTUP_RECOVERY_LEASE_NAME,
                owner_token,
                acquired.astimezone(timezone.utc).isoformat(),
                expires_at.astimezone(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _age_run_heartbeat(
    db_path: str,
    run_id: str,
    *,
    age_seconds: float,
) -> None:
    stale_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"UPDATE {_RUNS_TABLE} SET heartbeat_at = ? WHERE run_id = ?",
            (stale_at.isoformat(), run_id),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_run(
    db_path: str,
    *,
    run_id: str,
    status: str,
    mission_yaml: str | None = None,
    heartbeat_at: datetime | None = None,
    started_at: datetime | None = None,
    execution_owner: str | None = None,
    error: str | None = None,
    completed_at: datetime | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"""
            INSERT INTO {_RUNS_TABLE} (
                run_id, status, created_at, started_at, completed_at,
                error, mission_yaml, heartbeat_at, execution_owner, phase
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                status,
                now.isoformat(),
                None if started_at is None else started_at.isoformat(),
                None if completed_at is None else completed_at.isoformat(),
                error,
                mission_yaml,
                None if heartbeat_at is None else heartbeat_at.isoformat(),
                execution_owner,
                status if status in {"queued", "failed", "completed"} else "agent_execution",
            ),
        )
        conn.commit()
    finally:
        conn.close()


class TestStartupRecoveryOwnership(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)

    def tearDown(self) -> None:
        self.registry.close()
        os.unlink(self._db_path)

    def test_active_lease_holder_skips_recovery(self) -> None:
        queued = self.registry.create_run()
        running = self.registry.create_run()
        self.registry.update_status(running.run_id, RunStatus.RUNNING)
        self.registry.close()

        priming = RunRegistry(self._db_path)
        priming.close()
        _insert_lease(
            self._db_path,
            owner_token="other-process:lease",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        )

        non_owner = RunRegistry(self._db_path)
        try:
            recovered = non_owner.recover_interrupted_runs()
            self.assertEqual(recovered, 0)

            queued_record = non_owner.get_run(queued.run_id)
            running_record = non_owner.get_run(running.run_id)
            assert queued_record is not None
            assert running_record is not None
            self.assertEqual(queued_record.status, RunStatus.QUEUED)
            self.assertEqual(running_record.status, RunStatus.RUNNING)
            self.assertIsNone(queued_record.error)
            self.assertIsNone(running_record.error)
        finally:
            non_owner.close()

    def test_queued_run_survives_startup_and_is_requeueable(self) -> None:
        queued = self.registry.create_run(mission_yaml=_MINIMAL_MISSION_YAML)
        self.registry.close()

        recovered_registry = RunRegistry(self._db_path)
        try:
            self.assertEqual(recovered_registry.recover_interrupted_runs(), 0)
            record = recovered_registry.get_run(queued.run_id)
            assert record is not None
            self.assertEqual(record.status, RunStatus.QUEUED)
            self.assertIsNone(record.error)
            candidates = recovered_registry.list_requeueable_queued_runs()
            self.assertEqual(candidates, [(queued.run_id, _MINIMAL_MISSION_YAML)])
        finally:
            recovered_registry.close()

    def test_healthy_running_run_not_interrupted_by_other_replica_startup(
        self,
    ) -> None:
        running = self.registry.create_run()
        self.registry.update_status(running.run_id, RunStatus.RUNNING)
        owner = self.registry.get_run(running.run_id)
        assert owner is not None
        self.assertIsNotNone(owner.execution_owner)
        self.registry.close()

        replica_b = RunRegistry(self._db_path)
        try:
            self.assertEqual(replica_b.recover_interrupted_runs(), 0)
            record = replica_b.get_run(running.run_id)
            assert record is not None
            self.assertEqual(record.status, RunStatus.RUNNING)
            self.assertIsNone(record.error)
            self.assertEqual(record.execution_owner, owner.execution_owner)
        finally:
            replica_b.close()

    def test_expired_lease_terminalizes_running_with_owner_lost_reason(
        self,
    ) -> None:
        queued = self.registry.create_run(mission_yaml=_MINIMAL_MISSION_YAML)
        running = self.registry.create_run()
        self.registry.update_status(running.run_id, RunStatus.RUNNING)
        self.registry.close()
        _age_run_heartbeat(
            self._db_path,
            running.run_id,
            age_seconds=EXECUTION_LEASE_GRACE_SECONDS + 5,
        )

        priming = RunRegistry(self._db_path)
        priming.close()
        _insert_lease(
            self._db_path,
            owner_token="crashed-process:lease",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        )

        successor = RunRegistry(self._db_path)
        try:
            recovered = successor.recover_interrupted_runs()
            self.assertEqual(recovered, 1)

            queued_record = successor.get_run(queued.run_id)
            running_record = successor.get_run(running.run_id)
            assert queued_record is not None
            assert running_record is not None
            self.assertEqual(queued_record.status, RunStatus.QUEUED)
            self.assertIsNone(queued_record.error)
            self.assertEqual(running_record.status, RunStatus.FAILED)
            self.assertEqual(running_record.error, OWNER_LOST_RUN_ERROR)
        finally:
            successor.close()

    def test_multiprocess_only_one_owner_recovers_dead_run(self) -> None:
        queued = self.registry.create_run(mission_yaml=_MINIMAL_MISSION_YAML)
        running = self.registry.create_run()
        self.registry.update_status(running.run_id, RunStatus.RUNNING)
        self.registry.close()
        _age_run_heartbeat(
            self._db_path,
            running.run_id,
            age_seconds=EXECUTION_LEASE_GRACE_SECONDS + 5,
        )

        ctx = multiprocessing.get_context("spawn")
        result_queue: multiprocessing.Queue = ctx.Queue()
        start = ctx.Event()
        ready_events = [ctx.Event() for _ in range(4)]
        workers = [
            ctx.Process(
                target=_recover_worker,
                args=(self._db_path, ready, start, result_queue),
            )
            for ready in ready_events
        ]
        for worker in workers:
            worker.start()
        try:
            for ready in ready_events:
                self.assertTrue(ready.wait(timeout=10))
            start.set()

            results: list[tuple[str, int | str, int]] = []
            for _ in workers:
                results.append(result_queue.get(timeout=30))
        finally:
            for worker in workers:
                worker.join(timeout=30)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)

        self.assertTrue(all(item[0] == "ok" for item in results), results)
        recovered_counts = [int(item[1]) for item in results]
        self.assertEqual(sum(recovered_counts), 1)
        self.assertEqual(sum(1 for count in recovered_counts if count > 0), 1)

        verify = RunRegistry(self._db_path)
        try:
            queued_record = verify.get_run(queued.run_id)
            running_record = verify.get_run(running.run_id)
            assert queued_record is not None
            assert running_record is not None
            self.assertEqual(queued_record.status, RunStatus.QUEUED)
            self.assertEqual(running_record.status, RunStatus.FAILED)
            self.assertEqual(running_record.error, OWNER_LOST_RUN_ERROR)
        finally:
            verify.close()

    def test_claim_is_exclusive_across_processes(self) -> None:
        queued = self.registry.create_run(mission_yaml=_MINIMAL_MISSION_YAML)
        self.registry.close()

        ctx = multiprocessing.get_context("spawn")
        result_queue: multiprocessing.Queue = ctx.Queue()
        start = ctx.Event()
        ready_events = [ctx.Event() for _ in range(4)]
        workers = [
            ctx.Process(
                target=_claim_worker,
                args=(self._db_path, queued.run_id, ready, start, result_queue),
            )
            for ready in ready_events
        ]
        for worker in workers:
            worker.start()
        try:
            for ready in ready_events:
                self.assertTrue(ready.wait(timeout=10))
            start.set()
            results = [result_queue.get(timeout=30) for _ in workers]
        finally:
            for worker in workers:
                worker.join(timeout=30)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)

        self.assertTrue(all(item[0] == "ok" for item in results), results)
        wins = [item for item in results if item[1] is True]
        self.assertEqual(len(wins), 1)

        verify = RunRegistry(self._db_path)
        try:
            record = verify.get_run(queued.run_id)
            assert record is not None
            self.assertEqual(record.status, RunStatus.RUNNING)
            self.assertEqual(record.execution_owner, wins[0][2])
        finally:
            verify.close()

    def test_repeated_requeue_is_idempotent_in_local_queue(self) -> None:
        queued = self.registry.create_run(mission_yaml=_MINIMAL_MISSION_YAML)
        executed: list[str] = []
        claimed_owners: list[str | None] = []
        done = threading.Event()

        def _execute(run_id: str, mission: dict, registry: RunRegistry) -> None:
            claimed = registry.try_claim_run(run_id)
            claimed_owners.append(
                None if claimed is None else claimed.execution_owner
            )
            if claimed is not None:
                executed.append(run_id)
                registry.update_status(run_id, RunStatus.COMPLETED)
            done.set()

        queue = RunQueue()
        queue.configure(_execute)
        try:
            mission = {"mission_id": "startup-recovery-fixture"}
            self.assertTrue(queue.enqueue(queued.run_id, mission, self.registry))
            self.assertFalse(queue.enqueue(queued.run_id, mission, self.registry))
            self.assertTrue(done.wait(timeout=5))
            # Second recover/requeue cycle must not duplicate execution.
            self.assertEqual(self.registry.recover_interrupted_runs(), 0)
            for run_id, _yaml in self.registry.list_requeueable_queued_runs():
                queue.enqueue(run_id, mission, self.registry)
            self.assertEqual(executed, [queued.run_id])
            self.assertEqual(len([o for o in claimed_owners if o]), 1)
        finally:
            queue.stop()

    def test_crash_between_claim_and_heartbeat_recovers_after_grace(self) -> None:
        queued = self.registry.create_run(mission_yaml=_MINIMAL_MISSION_YAML)
        claimed = self.registry.try_claim_run(queued.run_id)
        assert claimed is not None
        self.assertEqual(claimed.status, RunStatus.RUNNING)
        # Simulate crash before heartbeat loop: age lease past grace.
        self.registry.close()
        _age_run_heartbeat(
            self._db_path,
            queued.run_id,
            age_seconds=EXECUTION_LEASE_GRACE_SECONDS + 1,
        )
        registry = RunRegistry(self._db_path)
        try:
            self.assertEqual(registry.recover_interrupted_runs(), 1)
            record = registry.get_run(queued.run_id)
            assert record is not None
            self.assertEqual(record.status, RunStatus.FAILED)
            self.assertEqual(record.error, OWNER_LOST_RUN_ERROR)
        finally:
            registry.close()

    def test_crash_before_claim_leaves_queued_for_requeue(self) -> None:
        # Dequeued from process-local queue but never claimed/persisted running.
        queued = self.registry.create_run(mission_yaml=_MINIMAL_MISSION_YAML)
        self.assertEqual(self.registry.recover_interrupted_runs(), 0)
        record = self.registry.get_run(queued.run_id)
        assert record is not None
        self.assertEqual(record.status, RunStatus.QUEUED)
        self.assertEqual(
            self.registry.list_requeueable_queued_runs(),
            [(queued.run_id, _MINIMAL_MISSION_YAML)],
        )

    def test_terminal_and_timed_out_runs_unchanged(self) -> None:
        completed = self.registry.create_run()
        self.registry.update_status(completed.run_id, RunStatus.RUNNING)
        self.registry.update_status(completed.run_id, RunStatus.COMPLETED)

        failed = self.registry.create_run()
        self.registry.update_status(failed.run_id, RunStatus.RUNNING)
        self.registry.store_result(failed.run_id, error="boom")
        self.registry.update_status(failed.run_id, RunStatus.FAILED)

        timed_out = self.registry.create_run()
        self.registry.update_status(timed_out.run_id, RunStatus.RUNNING)
        self.registry.update_status(timed_out.run_id, RunStatus.TIMED_OUT)

        before = {
            run_id: self.registry.get_run(run_id)
            for run_id in (
                completed.run_id,
                failed.run_id,
                timed_out.run_id,
            )
        }
        self.assertEqual(self.registry.recover_interrupted_runs(), 0)
        for run_id, prior in before.items():
            assert prior is not None
            after = self.registry.get_run(run_id)
            assert after is not None
            self.assertEqual(after.status, prior.status)
            self.assertEqual(after.error, prior.error)
            self.assertEqual(after.completed_at, prior.completed_at)

    def test_observed_run_timelines_as_regression_fixtures(self) -> None:
        """d48bf6ef was running; fcaf5a05 was queued — neither is failed by restart."""
        self.registry.close()
        now = datetime.now(timezone.utc)
        _insert_run(
            self._db_path,
            run_id=_OBS_RUNNING_ID,
            status=RunStatus.RUNNING.value,
            mission_yaml=_MINIMAL_MISSION_YAML,
            started_at=now - timedelta(minutes=2),
            heartbeat_at=now - timedelta(seconds=5),
            execution_owner="replica-a:alive",
        )
        _insert_run(
            self._db_path,
            run_id=_OBS_QUEUED_ID,
            status=RunStatus.QUEUED.value,
            mission_yaml=_MINIMAL_MISSION_YAML,
            heartbeat_at=now - timedelta(minutes=1),
        )

        registry = RunRegistry(self._db_path)
        try:
            self.assertEqual(registry.recover_interrupted_runs(), 0)
            running = registry.get_run(_OBS_RUNNING_ID)
            queued = registry.get_run(_OBS_QUEUED_ID)
            assert running is not None and queued is not None
            self.assertEqual(running.status, RunStatus.RUNNING)
            self.assertIsNone(running.error)
            self.assertEqual(queued.status, RunStatus.QUEUED)
            self.assertIsNone(queued.error)
            self.assertIn(
                (_OBS_QUEUED_ID, _MINIMAL_MISSION_YAML),
                registry.list_requeueable_queued_runs(),
            )
        finally:
            registry.close()


class TestLeaseRecoveryCasAndClockHardening(unittest.TestCase):
    """Adversarial regressions for review lease-recovery findings."""

    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)

    def tearDown(self) -> None:
        self.registry.close()
        os.unlink(self._db_path)

    def test_grace_exceeds_heartbeat_cadence(self) -> None:
        self.assertGreater(
            EXECUTION_LEASE_GRACE_SECONDS,
            HEARTBEAT_INTERVAL_SECONDS,
        )
        self.assertEqual(EXECUTION_LEASE_GRACE_SECONDS, 90.0)
        self.assertEqual(HEARTBEAT_INTERVAL_SECONDS, 5.0)

    def test_late_heartbeat_between_read_and_update_does_not_false_kill(
        self,
    ) -> None:
        running = self.registry.create_run()
        self.registry.update_status(running.run_id, RunStatus.RUNNING)
        self.registry.close()
        _age_run_heartbeat(
            self._db_path,
            running.run_id,
            age_seconds=EXECUTION_LEASE_GRACE_SECONDS + 1,
        )

        owner = RunRegistry(self._db_path)
        real_age = RunRegistry._execution_lease_age_seconds

        def _age_then_refresh(self, record, *, now=None):
            age = real_age(self, record, now=now)
            owner.touch_heartbeat(running.run_id)
            return age

        RunRegistry._execution_lease_age_seconds = _age_then_refresh  # type: ignore[method-assign]
        try:
            successor = RunRegistry(self._db_path)
            try:
                recovered = successor.recover_interrupted_runs()
                after = successor.get_run(running.run_id)
            finally:
                successor.close()
        finally:
            RunRegistry._execution_lease_age_seconds = real_age  # type: ignore[method-assign]
            owner.close()

        self.assertEqual(recovered, 0)
        assert after is not None
        self.assertEqual(after.status, RunStatus.RUNNING)
        self.assertIsNone(after.error)

    def test_malformed_row_isolated_sibling_still_recovers(self) -> None:
        bad = self.registry.create_run()
        good = self.registry.create_run()
        self.registry.update_status(bad.run_id, RunStatus.RUNNING)
        self.registry.update_status(good.run_id, RunStatus.RUNNING)
        self.registry.close()

        stale = datetime.now(timezone.utc) - timedelta(
            seconds=EXECUTION_LEASE_GRACE_SECONDS + 5
        )
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                f"UPDATE {_RUNS_TABLE} SET heartbeat_at = ? WHERE run_id = ?",
                ("not-a-timestamp", bad.run_id),
            )
            conn.execute(
                f"UPDATE {_RUNS_TABLE} SET heartbeat_at = ? WHERE run_id = ?",
                (stale.astimezone(timezone.utc).isoformat(), good.run_id),
            )
            conn.commit()
        finally:
            conn.close()

        registry = RunRegistry(self._db_path)
        try:
            recovered = registry.recover_interrupted_runs()
            self.assertEqual(recovered, 2)
            bad_rec = registry.get_run(bad.run_id)
            good_rec = registry.get_run(good.run_id)
            assert bad_rec is not None and good_rec is not None
            self.assertEqual(bad_rec.status, RunStatus.FAILED)
            self.assertEqual(bad_rec.error, OWNER_LOST_RUN_ERROR)
            self.assertEqual(good_rec.status, RunStatus.FAILED)
            self.assertEqual(good_rec.error, OWNER_LOST_RUN_ERROR)
        finally:
            registry.close()

    def test_ten_year_future_heartbeat_does_not_pin_dead_work(self) -> None:
        running = self.registry.create_run()
        self.registry.update_status(running.run_id, RunStatus.RUNNING)
        self.registry.close()
        future = datetime.now(timezone.utc) + timedelta(days=3650)
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                f"UPDATE {_RUNS_TABLE} SET heartbeat_at = ? WHERE run_id = ?",
                (future.isoformat(), running.run_id),
            )
            conn.commit()
        finally:
            conn.close()

        registry = RunRegistry(self._db_path)
        try:
            self.assertEqual(registry.recover_interrupted_runs(), 1)
            record = registry.get_run(running.run_id)
            assert record is not None
            self.assertEqual(record.status, RunStatus.FAILED)
            self.assertEqual(record.error, OWNER_LOST_RUN_ERROR)
        finally:
            registry.close()

    def test_future_skew_tolerance_boundaries(self) -> None:
        now = datetime.now(timezone.utc)
        at_tolerance = self.registry.create_run()
        beyond = self.registry.create_run()
        self.registry.update_status(at_tolerance.run_id, RunStatus.RUNNING)
        self.registry.update_status(beyond.run_id, RunStatus.RUNNING)
        self.registry.close()

        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                f"UPDATE {_RUNS_TABLE} SET heartbeat_at = ? WHERE run_id = ?",
                (
                    (
                        now
                        + timedelta(
                            seconds=EXECUTION_LEASE_FUTURE_SKEW_TOLERANCE_SECONDS
                        )
                    ).isoformat(),
                    at_tolerance.run_id,
                ),
            )
            conn.execute(
                f"UPDATE {_RUNS_TABLE} SET heartbeat_at = ? WHERE run_id = ?",
                (
                    (
                        now
                        + timedelta(
                            seconds=EXECUTION_LEASE_FUTURE_SKEW_TOLERANCE_SECONDS
                            + 1
                        )
                    ).isoformat(),
                    beyond.run_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        registry = RunRegistry(self._db_path)
        try:
            with patch(
                "mission_control.run_registry._utc_now",
                return_value=now,
            ):
                recovered = registry.recover_interrupted_runs()
            self.assertEqual(recovered, 1)
            kept = registry.get_run(at_tolerance.run_id)
            killed = registry.get_run(beyond.run_id)
            assert kept is not None and killed is not None
            self.assertEqual(kept.status, RunStatus.RUNNING)
            self.assertIsNone(kept.error)
            self.assertEqual(killed.status, RunStatus.FAILED)
            self.assertEqual(killed.error, OWNER_LOST_RUN_ERROR)
        finally:
            registry.close()

    def test_legacy_null_owner_valid_lease_not_stealable(self) -> None:
        running = self.registry.create_run()
        self.registry.update_status(running.run_id, RunStatus.RUNNING)
        self.registry.close()
        now = datetime.now(timezone.utc)
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                f"""
                UPDATE {_RUNS_TABLE}
                SET execution_owner = NULL, heartbeat_at = ?
                WHERE run_id = ?
                """,
                (now.isoformat(), running.run_id),
            )
            conn.commit()
        finally:
            conn.close()

        thief = RunRegistry(self._db_path)
        try:
            stolen = thief.try_claim_run(
                running.run_id, owner_token="replica-b:thief"
            )
            self.assertIsNone(stolen)
            record = thief.get_run(running.run_id)
            assert record is not None
            self.assertEqual(record.status, RunStatus.RUNNING)
            self.assertIsNone(record.execution_owner)
        finally:
            thief.close()

    def test_concurrent_replica_recovery_single_terminalize(self) -> None:
        running = self.registry.create_run()
        self.registry.update_status(running.run_id, RunStatus.RUNNING)
        self.registry.close()
        _age_run_heartbeat(
            self._db_path,
            running.run_id,
            age_seconds=EXECUTION_LEASE_GRACE_SECONDS + 5,
        )

        ctx = multiprocessing.get_context("spawn")
        result_queue: multiprocessing.Queue = ctx.Queue()
        start = ctx.Event()
        ready_events = [ctx.Event() for _ in range(4)]
        workers = [
            ctx.Process(
                target=_recover_worker,
                args=(self._db_path, ready, start, result_queue),
            )
            for ready in ready_events
        ]
        for worker in workers:
            worker.start()
        try:
            for ready in ready_events:
                self.assertTrue(ready.wait(timeout=10))
            start.set()
            results = [result_queue.get(timeout=30) for _ in workers]
        finally:
            for worker in workers:
                worker.join(timeout=30)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)

        self.assertTrue(all(item[0] == "ok" for item in results), results)
        recovered_counts = [int(item[1]) for item in results]
        self.assertEqual(sum(recovered_counts), 1)
        verify = RunRegistry(self._db_path)
        try:
            record = verify.get_run(running.run_id)
            assert record is not None
            self.assertEqual(record.status, RunStatus.FAILED)
            self.assertEqual(record.error, OWNER_LOST_RUN_ERROR)
        finally:
            verify.close()


class TestFalseInterruptProvenanceGuards(SqliteRegistryTestCase):
    """Post-hotfix secondary-writer guards and 30s timeline regression."""

    def test_observed_30s_timeline_cannot_reach_legacy_interrupt_error(
        self,
    ) -> None:
        """Reproduce run 019bf53f…: created→failed in 30s must not legacy-fail.

        A running run whose lease is only OBSERVED_FALSE_INTERRUPT_SECONDS old
        (30s) is still within the 90s grace window. Recovery must leave it
        running, and no path may attribute INTERRUPTED_RUN_ERROR.
        """
        self.assertLess(
            OBSERVED_FALSE_INTERRUPT_SECONDS,
            EXECUTION_LEASE_GRACE_SECONDS,
        )
        running = self.registry.create_run()
        self.registry.update_status(running.run_id, RunStatus.RUNNING)
        owner = self.registry.get_run(running.run_id)
        assert owner is not None
        self.registry.close()

        _age_run_heartbeat(
            self._db_path,
            running.run_id,
            age_seconds=OBSERVED_FALSE_INTERRUPT_SECONDS,
        )

        successor = RunRegistry(self._db_path)
        try:
            recovered = successor.recover_interrupted_runs()
            self.assertEqual(recovered, 0)
            record = successor.get_run(running.run_id)
            assert record is not None
            self.assertEqual(record.status, RunStatus.RUNNING)
            self.assertIsNone(record.error)
            self.assertNotEqual(record.error, INTERRUPTED_RUN_ERROR)
            self.assertEqual(record.execution_owner, owner.execution_owner)
        finally:
            successor.close()

    def test_store_result_refuses_legacy_interrupt_error(self) -> None:
        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        updated = self.registry.store_result(
            record.run_id,
            error=INTERRUPTED_RUN_ERROR,
        )
        assert updated is not None
        self.assertEqual(updated.error, LEGACY_INTERRUPT_REFUSED_ERROR)
        self.assertNotEqual(updated.error, INTERRUPTED_RUN_ERROR)
        fetched = self.registry.get_run(record.run_id)
        assert fetched is not None
        self.assertEqual(fetched.error, LEGACY_INTERRUPT_REFUSED_ERROR)

    def test_owner_lost_terminalize_attaches_provenance(self) -> None:
        running = self.registry.create_run()
        self.registry.update_status(running.run_id, RunStatus.RUNNING)
        before = self.registry.get_run(running.run_id)
        assert before is not None
        prior_owner = before.execution_owner
        self.registry.close()
        _age_run_heartbeat(
            self._db_path,
            running.run_id,
            age_seconds=EXECUTION_LEASE_GRACE_SECONDS + 5,
        )

        railway_sha = "a1b2c3d4e5f6789012345678abcdef0123456789"
        with patch.dict(
            os.environ,
            {"RAILWAY_GIT_COMMIT_SHA": railway_sha},
            clear=False,
        ):
            successor = RunRegistry(self._db_path)
            try:
                self.assertEqual(successor.recover_interrupted_runs(), 1)
                record = successor.get_run(running.run_id)
                assert record is not None
                self.assertEqual(record.status, RunStatus.FAILED)
                self.assertEqual(record.error, OWNER_LOST_RUN_ERROR)
                self.assertNotEqual(record.error, INTERRUPTED_RUN_ERROR)
                self.assertEqual(record.phase, RunPhase.FAILED)
                assert record.progress is not None
                provenance = record.progress.get("provenance")
                assert isinstance(provenance, dict)
                self.assertEqual(
                    provenance.get("event"), "terminalize_owner_lost"
                )
                self.assertEqual(
                    provenance.get("source"),
                    "startup_recovery.owner_lease_cas",
                )
                self.assertEqual(
                    provenance.get("execution_owner"), prior_owner or "none"
                )
                self.assertEqual(
                    provenance.get("policy_version"),
                    STARTUP_RECOVERY_POLICY_VERSION,
                )
                self.assertEqual(
                    provenance.get("build_marker"),
                    railway_sha[:_BUILD_FINGERPRINT_LEN],
                )
                self.assertTrue(provenance.get("process_instance_id"))
                # Public step/detail remain compatible.
                self.assertEqual(
                    record.progress.get("step"), RunPhase.FAILED.value
                )
            finally:
                successor.close()

    def test_refuse_helper_and_policy_diagnostics(self) -> None:
        self.assertEqual(
            refuse_legacy_interrupt_error(INTERRUPTED_RUN_ERROR),
            LEGACY_INTERRUPT_REFUSED_ERROR,
        )
        self.assertEqual(
            refuse_legacy_interrupt_error(
                f"  {INTERRUPTED_RUN_ERROR.upper()}  "
            ),
            LEGACY_INTERRUPT_REFUSED_ERROR,
        )
        self.assertTrue(
            is_legacy_interrupt_error(f"\t{INTERRUPTED_RUN_ERROR} \n")
        )
        self.assertEqual(refuse_legacy_interrupt_error("other"), "other")
        self.assertIsNone(refuse_legacy_interrupt_error(None))
        diag = startup_recovery_policy_diagnostics()
        self.assertEqual(
            diag["policy_version"], STARTUP_RECOVERY_POLICY_VERSION
        )
        self.assertEqual(
            diag["module_path"], startup_recovery_module_identity()
        )
        self.assertEqual(diag["module_path"], "mission_control.run_registry")
        self.assertNotIn(os.sep, str(diag["module_path"]))
        self.assertEqual(diag["grace_seconds"], EXECUTION_LEASE_GRACE_SECONDS)
        self.assertTrue(diag["legacy_interrupt_quarantined"])
        self.assertEqual(diag["owner_lost_error"], OWNER_LOST_RUN_ERROR)
        self.assertNotEqual(OWNER_LOST_RUN_ERROR, INTERRUPTED_RUN_ERROR)

    def test_legacy_interrupt_constant_not_written_by_registry_source(
        self,
    ) -> None:
        """Quarantine: production writers must not assign INTERRUPTED_RUN_ERROR."""
        import mission_control.run_registry as rr_mod
        import pathlib

        source = pathlib.Path(rr_mod.__file__).read_text(encoding="utf-8")
        # Allow the constant definition and explicit comparisons / quarantine docs.
        write_patterns = (
            "error = INTERRUPTED_RUN_ERROR",
            "error=INTERRUPTED_RUN_ERROR",
            "error= INTERRUPTED_RUN_ERROR",
            ",\n                        INTERRUPTED_RUN_ERROR,",
            ", INTERRUPTED_RUN_ERROR,",
        )
        for pattern in write_patterns:
            self.assertNotIn(pattern, source)

    def test_concurrent_recovery_never_writes_legacy_interrupt(
        self,
    ) -> None:
        running = self.registry.create_run()
        self.registry.update_status(running.run_id, RunStatus.RUNNING)
        self.registry.close()
        _age_run_heartbeat(
            self._db_path,
            running.run_id,
            age_seconds=EXECUTION_LEASE_GRACE_SECONDS + 5,
        )

        ctx = multiprocessing.get_context("spawn")
        result_queue: multiprocessing.Queue = ctx.Queue()
        start = ctx.Event()
        ready_events = [ctx.Event() for _ in range(4)]
        workers = [
            ctx.Process(
                target=_recover_worker,
                args=(self._db_path, ready, start, result_queue),
            )
            for ready in ready_events
        ]
        for worker in workers:
            worker.start()
        try:
            for ready in ready_events:
                self.assertTrue(ready.wait(timeout=10))
            start.set()
            results = [result_queue.get(timeout=30) for _ in workers]
        finally:
            for worker in workers:
                worker.join(timeout=30)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)

        self.assertTrue(all(item[0] == "ok" for item in results), results)
        verify = RunRegistry(self._db_path)
        try:
            record = verify.get_run(running.run_id)
            assert record is not None
            self.assertEqual(record.status, RunStatus.FAILED)
            self.assertEqual(record.error, OWNER_LOST_RUN_ERROR)
            self.assertNotEqual(record.error, INTERRUPTED_RUN_ERROR)
        finally:
            verify.close()


class TestLegacyInterruptSqliteBoundary(SqliteRegistryTestCase):
    """Adversarial guards: DB trigger, variants, build fingerprint, no path leak."""

    _RAILWAY_SHA = "deadbeefcafebabe0123456789abcdef01234567"

    def _trigger_names(self) -> set[str]:
        conn = sqlite3.connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
            return {row[0] for row in rows}
        finally:
            conn.close()

    def test_direct_sqlite_update_blocked_atomically(self) -> None:
        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        before = self.registry.get_run(record.run_id)
        assert before is not None
        self.assertEqual(before.status, RunStatus.RUNNING)
        self.assertIsNone(before.error)

        conn = sqlite3.connect(self._db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                conn.execute(
                    f"""
                    UPDATE {_RUNS_TABLE}
                    SET error = ?, status = ?, phase = ?
                    WHERE run_id = ?
                    """,
                    (
                        INTERRUPTED_RUN_ERROR,
                        RunStatus.FAILED.value,
                        RunPhase.FAILED.value,
                        record.run_id,
                    ),
                )
            self.assertIn(LEGACY_INTERRUPT_REFUSED_ERROR, str(ctx.exception))
            conn.rollback()
        finally:
            conn.close()

        after = self.registry.get_run(record.run_id)
        assert after is not None
        self.assertEqual(after.status, RunStatus.RUNNING)
        self.assertEqual(after.phase, before.phase)
        self.assertIsNone(after.error)
        self.assertNotEqual(after.error, INTERRUPTED_RUN_ERROR)

    def test_direct_sqlite_insert_blocked(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self._db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                conn.execute(
                    f"""
                    INSERT INTO {_RUNS_TABLE} (
                        run_id, status, created_at, error, phase
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "adversarial-insert-legacy",
                        RunStatus.FAILED.value,
                        now,
                        INTERRUPTED_RUN_ERROR,
                        RunPhase.FAILED.value,
                    ),
                )
            self.assertIn(LEGACY_INTERRUPT_REFUSED_ERROR, str(ctx.exception))
            conn.rollback()
            found = conn.execute(
                f"SELECT 1 FROM {_RUNS_TABLE} WHERE run_id = ?",
                ("adversarial-insert-legacy",),
            ).fetchone()
            self.assertIsNone(found)
        finally:
            conn.close()

    def test_sql_strip_chars_generated_from_canonical_python_whitespace(
        self,
    ) -> None:
        """SQLite trim set must be derived from one Python strip() list."""
        expected = tuple(i for i in range(0x110000) if chr(i).isspace())
        self.assertEqual(_PYTHON_STRIP_WHITESPACE_CODEPOINTS, expected)
        self.assertEqual(
            _SQL_STRIP_CHARS,
            _sql_trim_chars_expr(_PYTHON_STRIP_WHITESPACE_CODEPOINTS),
        )
        # Named families required by the quarantine contract.
        named = {
            0x0009,
            0x000A,
            0x000B,
            0x000C,
            0x000D,
            0x0020,
            0x00A0,  # NBSP
            0x2000,
            0x2001,
            0x2002,
            0x2003,  # EN/EM SPACE family
            0x2004,
            0x2005,
            0x2006,
            0x2007,  # FIGURE SPACE
            0x2008,
            0x2009,
            0x200A,
            0x2028,  # LINE SEPARATOR
            0x2029,  # PARAGRAPH SEPARATOR
            0x202F,  # NARROW NO-BREAK SPACE
            0x205F,
            0x3000,  # IDEOGRAPHIC SPACE
        }
        self.assertTrue(named.issubset(_PYTHON_STRIP_WHITESPACE_CODEPOINTS))
        # BOM is not Python strip whitespace — must not enter SQLite trim set.
        self.assertNotIn(0xFEFF, _PYTHON_STRIP_WHITESPACE_CODEPOINTS)

    def _assert_app_and_sql_reject_legacy(self, variant: str) -> None:
        self.assertTrue(is_legacy_interrupt_error(variant))
        self.assertEqual(
            refuse_legacy_interrupt_error(variant),
            LEGACY_INTERRUPT_REFUSED_ERROR,
        )
        run = self.registry.create_run()
        self.registry.update_status(run.run_id, RunStatus.RUNNING)
        refused = self.registry.store_result(run.run_id, error=variant)
        assert refused is not None
        self.assertEqual(refused.error, LEGACY_INTERRUPT_REFUSED_ERROR)

        conn = sqlite3.connect(self._db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                conn.execute(
                    f"UPDATE {_RUNS_TABLE} SET error = ? WHERE run_id = ?",
                    (variant, run.run_id),
                )
            self.assertIn(LEGACY_INTERRUPT_REFUSED_ERROR, str(ctx.exception))
            conn.rollback()
        finally:
            conn.close()
        fetched = self.registry.get_run(run.run_id)
        assert fetched is not None
        self.assertEqual(fetched.error, LEGACY_INTERRUPT_REFUSED_ERROR)
        self.assertEqual(fetched.status, RunStatus.RUNNING)

    def _assert_app_and_sql_allow_non_legacy(self, variant: str) -> None:
        self.assertFalse(is_legacy_interrupt_error(variant))
        self.assertEqual(refuse_legacy_interrupt_error(variant), variant)
        run = self.registry.create_run()
        self.registry.update_status(run.run_id, RunStatus.RUNNING)
        stored = self.registry.store_result(run.run_id, error=variant)
        assert stored is not None
        self.assertEqual(stored.error, variant)

        other = self.registry.create_run()
        self.registry.update_status(other.run_id, RunStatus.RUNNING)
        before = self.registry.get_run(other.run_id)
        assert before is not None
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                f"UPDATE {_RUNS_TABLE} SET error = ? WHERE run_id = ?",
                (variant, other.run_id),
            )
            conn.commit()
        finally:
            conn.close()
        fetched = self.registry.get_run(other.run_id)
        assert fetched is not None
        self.assertEqual(fetched.error, variant)
        self.assertEqual(fetched.status, before.status)

    def test_canonical_unicode_whitespace_parity_app_and_sql(self) -> None:
        """Each Python strip() whitespace padding is refused by app and SQL."""
        for cp in _PYTHON_STRIP_WHITESPACE_CODEPOINTS:
            ch = chr(cp)
            variant = f"{ch}{INTERRUPTED_RUN_ERROR}{ch}"
            with self.subTest(codepoint=f"U+{cp:04X}", variant=variant):
                self.assertEqual(variant.strip(), INTERRUPTED_RUN_ERROR)
                self._assert_app_and_sql_reject_legacy(variant)

    def test_whitespace_case_variants_blocked_app_and_sql(self) -> None:
        variants = (
            INTERRUPTED_RUN_ERROR,
            f"  {INTERRUPTED_RUN_ERROR}  ",
            INTERRUPTED_RUN_ERROR.upper(),
            INTERRUPTED_RUN_ERROR.swapcase(),
            f"\n{INTERRUPTED_RUN_ERROR}\t",
            f"\u00a0{INTERRUPTED_RUN_ERROR.upper()}\u3000",
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self._assert_app_and_sql_reject_legacy(variant)

    def test_non_whitespace_lookalikes_not_treated_as_legacy(self) -> None:
        """Zero-width / BOM lookalikes stay outside strip() on both layers."""
        lookalikes = (
            "\u200b",  # ZERO WIDTH SPACE
            "\u200c",  # ZERO WIDTH NON-JOINER
            "\u200d",  # ZERO WIDTH JOINER
            "\u2060",  # WORD JOINER
            "\ufeff",  # BOM / ZWNBSP (not Python strip whitespace)
        )
        for ch in lookalikes:
            variant = f"{ch}{INTERRUPTED_RUN_ERROR}"
            with self.subTest(codepoint=f"U+{ord(ch):04X}"):
                self.assertNotEqual(variant.strip(), INTERRUPTED_RUN_ERROR)
                self._assert_app_and_sql_allow_non_legacy(variant)

    def test_direct_sqlite_replace_and_upsert_blocked(self) -> None:
        """REPLACE/UPSERT writers must hit the same legacy-error guards."""
        now = datetime.now(timezone.utc).isoformat()
        padded = f"\u00a0{INTERRUPTED_RUN_ERROR}\u3000"
        self.assertTrue(is_legacy_interrupt_error(padded))

        conn = sqlite3.connect(self._db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                conn.execute(
                    f"""
                    REPLACE INTO {_RUNS_TABLE} (
                        run_id, status, created_at, error, phase
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "adversarial-replace-legacy",
                        RunStatus.FAILED.value,
                        now,
                        padded,
                        RunPhase.FAILED.value,
                    ),
                )
            self.assertIn(LEGACY_INTERRUPT_REFUSED_ERROR, str(ctx.exception))
            conn.rollback()

            seed = self.registry.create_run()
            self.registry.update_status(seed.run_id, RunStatus.RUNNING)
            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                conn.execute(
                    f"""
                    INSERT INTO {_RUNS_TABLE} (
                        run_id, status, created_at, error, phase
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        error = excluded.error,
                        status = excluded.status,
                        phase = excluded.phase
                    """,
                    (
                        seed.run_id,
                        RunStatus.FAILED.value,
                        now,
                        padded,
                        RunPhase.FAILED.value,
                    ),
                )
            self.assertIn(LEGACY_INTERRUPT_REFUSED_ERROR, str(ctx.exception))
            conn.rollback()
        finally:
            conn.close()

        self.assertIsNone(self.registry.get_run("adversarial-replace-legacy"))
        after = self.registry.get_run(seed.run_id)
        assert after is not None
        self.assertEqual(after.status, RunStatus.RUNNING)
        self.assertIsNone(after.error)

    def test_historical_legacy_error_remains_readable(self) -> None:
        historical_id = "historical-legacy-interrupt"
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                f"DROP TRIGGER IF EXISTS {_LEGACY_INTERRUPT_INSERT_TRIGGER}"
            )
            conn.execute(
                f"DROP TRIGGER IF EXISTS {_LEGACY_INTERRUPT_UPDATE_TRIGGER}"
            )
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                f"""
                INSERT INTO {_RUNS_TABLE} (
                    run_id, status, created_at, completed_at, error, phase
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    historical_id,
                    RunStatus.FAILED.value,
                    now,
                    now,
                    INTERRUPTED_RUN_ERROR,
                    RunPhase.FAILED.value,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        # Re-open installs triggers again; historical row stays readable.
        self.registry.close()
        registry = RunRegistry(self._db_path)
        try:
            row = registry.get_run(historical_id)
            assert row is not None
            self.assertEqual(row.error, INTERRUPTED_RUN_ERROR)
            self.assertEqual(row.status, RunStatus.FAILED)
            # Unrelated repair on historical row must still succeed.
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    f"""
                    UPDATE {_RUNS_TABLE}
                    SET stderr = 'operator note'
                    WHERE run_id = ?
                    """,
                    (historical_id,),
                )
                conn.commit()
            finally:
                conn.close()
            repaired = registry.get_run(historical_id)
            assert repaired is not None
            self.assertEqual(repaired.error, INTERRUPTED_RUN_ERROR)
            self.assertEqual(repaired.stderr, "operator note")
        finally:
            registry.close()
            self.registry = RunRegistry(self._db_path)

    def test_trigger_install_idempotent_across_reopen(self) -> None:
        first = self._trigger_names()
        self.assertIn(_LEGACY_INTERRUPT_INSERT_TRIGGER, first)
        self.assertIn(_LEGACY_INTERRUPT_UPDATE_TRIGGER, first)
        self.registry.close()
        again = RunRegistry(self._db_path)
        try:
            second = self._trigger_names()
            self.assertEqual(
                first & {
                    _LEGACY_INTERRUPT_INSERT_TRIGGER,
                    _LEGACY_INTERRUPT_UPDATE_TRIGGER,
                },
                second & {
                    _LEGACY_INTERRUPT_INSERT_TRIGGER,
                    _LEGACY_INTERRUPT_UPDATE_TRIGGER,
                },
            )
            # Third ensure_schema path (new connection) still rejects.
            conn = sqlite3.connect(self._db_path)
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        f"""
                        INSERT INTO {_RUNS_TABLE} (
                            run_id, status, created_at, error
                        ) VALUES ('idempotent-legacy', 'failed', ?, ?)
                        """,
                        (
                            datetime.now(timezone.utc).isoformat(),
                            INTERRUPTED_RUN_ERROR,
                        ),
                    )
                conn.rollback()
            finally:
                conn.close()
        finally:
            again.close()
            self.registry = RunRegistry(self._db_path)

    def test_concurrent_stale_and_new_connections_reject(self) -> None:
        record = self.registry.create_run()
        self.registry.update_status(record.run_id, RunStatus.RUNNING)
        # Stale-shaped peer: connection opened without going through RunRegistry.
        stale = sqlite3.connect(self._db_path)
        fresh = sqlite3.connect(self._db_path)
        try:
            for conn in (stale, fresh):
                with self.assertRaises(sqlite3.IntegrityError) as ctx:
                    conn.execute(
                        f"""
                        UPDATE {_RUNS_TABLE}
                        SET error = ?, status = ?
                        WHERE run_id = ?
                        """,
                        (
                            INTERRUPTED_RUN_ERROR,
                            RunStatus.FAILED.value,
                            record.run_id,
                        ),
                    )
                self.assertIn(LEGACY_INTERRUPT_REFUSED_ERROR, str(ctx.exception))
                conn.rollback()
        finally:
            stale.close()
            fresh.close()
        fetched = self.registry.get_run(record.run_id)
        assert fetched is not None
        self.assertEqual(fetched.status, RunStatus.RUNNING)
        self.assertIsNone(fetched.error)

    def test_legitimate_terminal_errors_still_writable(self) -> None:
        allowed = (
            OWNER_LOST_RUN_ERROR,
            LEGACY_INTERRUPT_REFUSED_ERROR,
            "Worker boom",
            "mission timed out after 120s",
            "operator repair: reset attribution",
        )
        for message in allowed:
            with self.subTest(message=message):
                run = self.registry.create_run()
                self.registry.update_status(run.run_id, RunStatus.RUNNING)
                updated = self.registry.store_result(run.run_id, error=message)
                assert updated is not None
                self.assertEqual(updated.error, message)
                failed = self.registry.update_status(
                    run.run_id, RunStatus.FAILED
                )
                assert failed is not None
                self.assertEqual(failed.status, RunStatus.FAILED)
                self.assertEqual(failed.error, message)

    def test_railway_sha_fingerprint_and_malicious_marker_rejection(
        self,
    ) -> None:
        self.assertEqual(
            sanitize_build_fingerprint(self._RAILWAY_SHA),
            self._RAILWAY_SHA[:_BUILD_FINGERPRINT_LEN],
        )
        self.assertEqual(
            sanitize_build_fingerprint("abc1234"),
            "abc1234",
        )
        malicious = (
            "not-a-git-sha",
            "abc123deadbeef",  # 14 hex: neither full nor short fallback
            "g" * 40,  # non-hex
            "supersecret_token_abcdefghijklmnopqrstuv",
            "A" * 64,
        )
        for value in malicious:
            with self.subTest(value=value):
                self.assertIsNone(sanitize_build_fingerprint(value))

        with patch.dict(
            os.environ,
            {"RAILWAY_GIT_COMMIT_SHA": self._RAILWAY_SHA},
            clear=False,
        ):
            self.assertEqual(
                deployed_build_marker(),
                self._RAILWAY_SHA[:_BUILD_FINGERPRINT_LEN],
            )
            prov = build_terminal_provenance(
                event="terminalize_owner_lost",
                source="startup_recovery.owner_lease_cas",
                execution_owner="owner-1",
            )
            self.assertEqual(
                prov.get("build_marker"),
                self._RAILWAY_SHA[:_BUILD_FINGERPRINT_LEN],
            )

        with patch.dict(
            os.environ,
            {"RAILWAY_GIT_COMMIT_SHA": "password=hunter2_abcdefghijklmnopqrstuvwxyz"},
            clear=False,
        ):
            self.assertIsNone(deployed_build_marker())
            diag = startup_recovery_policy_diagnostics()
            self.assertEqual(diag["build_marker"], "unknown")

        cleaned = bound_terminal_provenance(
            {
                "event": "terminalize_owner_lost",
                "source": "x",
                "build_marker": "password=hunter2_abcdefghijklmnopqrstuvwxyz",
                "policy_version": STARTUP_RECOVERY_POLICY_VERSION,
            }
        )
        assert cleaned is not None
        self.assertNotIn("build_marker", cleaned)
        self.assertNotIn("password", str(cleaned))

    def test_public_api_progress_strips_provenance(self) -> None:
        from app.api import _run_status_response

        running = self.registry.create_run()
        self.registry.update_status(running.run_id, RunStatus.RUNNING)
        self.registry.close()
        _age_run_heartbeat(
            self._db_path,
            running.run_id,
            age_seconds=EXECUTION_LEASE_GRACE_SECONDS + 5,
        )
        with patch.dict(
            os.environ,
            {"RAILWAY_GIT_COMMIT_SHA": self._RAILWAY_SHA},
            clear=False,
        ):
            successor = RunRegistry(self._db_path)
            try:
                self.assertEqual(successor.recover_interrupted_runs(), 1)
                record = successor.get_run(running.run_id)
                assert record is not None
                assert record.progress is not None
                self.assertIn("provenance", record.progress)
                public = _run_status_response(record).model_dump()
                self.assertEqual(
                    set(public["progress"].keys()), {"step", "detail"}
                )
                blob = str(public)
                self.assertNotIn("provenance", blob)
                self.assertNotIn("build_marker", blob)
                self.assertNotIn("process_instance_id", blob)
                self.assertNotIn(self._RAILWAY_SHA, blob)
                # sanitize_progress also drops provenance for monitoring surfaces.
                cleaned = sanitize_progress(record.progress)
                assert cleaned is not None
                self.assertNotIn("provenance", cleaned)
            finally:
                successor.close()
                self.registry = RunRegistry(self._db_path)


class TestStartupRequeueApiHelpers(unittest.TestCase):
    def test_lifespan_requeue_helper_enqueues_once(self) -> None:
        from app import api as api_module

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        registry = RunRegistry(db_path)
        queued = registry.create_run(mission_yaml=_MINIMAL_MISSION_YAML)
        queue = RunQueue()
        executed: list[str] = []
        done = threading.Event()

        def _execute(run_id: str, mission: dict, reg: RunRegistry) -> None:
            if reg.try_claim_run(run_id) is not None:
                executed.append(run_id)
                reg.update_status(run_id, RunStatus.COMPLETED)
            done.set()

        queue.configure(_execute)
        try:
            with patch.object(api_module, "run_registry", registry), patch.object(
                api_module, "run_queue", queue
            ):
                first = api_module._requeue_persisted_queued_runs()
                self.assertTrue(done.wait(timeout=5))
                second = api_module._requeue_persisted_queued_runs()
            self.assertEqual(first, 1)
            self.assertEqual(second, 0)
            self.assertEqual(executed, [queued.run_id])
        finally:
            queue.stop()
            registry.close()
            os.unlink(db_path)


if __name__ == "__main__":
    unittest.main()
