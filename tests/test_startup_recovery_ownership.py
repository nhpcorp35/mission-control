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
    OWNER_LOST_RUN_ERROR,
    STARTUP_RECOVERY_LEASE_NAME,
    RunRegistry,
    RunStatus,
)

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
