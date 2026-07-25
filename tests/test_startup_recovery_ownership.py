"""Multi-process ownership tests for interrupted-run startup recovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import multiprocessing
import os
import sqlite3
import tempfile
import unittest

from mission_control.run_registry import (
    INTERRUPTED_RUN_ERROR,
    STARTUP_RECOVERY_LEASE_NAME,
    RunRegistry,
    RunStatus,
)

_LEASE_TABLE = "startup_recovery_leases"


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

        # Ensure lease table exists, then plant a non-expired foreign lease.
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

    def test_expired_lease_allows_recovery(self) -> None:
        queued = self.registry.create_run()
        running = self.registry.create_run()
        self.registry.update_status(running.run_id, RunStatus.RUNNING)
        self.registry.close()

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
            self.assertEqual(recovered, 2)

            queued_record = successor.get_run(queued.run_id)
            running_record = successor.get_run(running.run_id)
            assert queued_record is not None
            assert running_record is not None
            self.assertEqual(queued_record.status, RunStatus.FAILED)
            self.assertEqual(running_record.status, RunStatus.FAILED)
            self.assertEqual(queued_record.error, INTERRUPTED_RUN_ERROR)
            self.assertEqual(running_record.error, INTERRUPTED_RUN_ERROR)
        finally:
            successor.close()

    def test_multiprocess_only_one_owner_recovers(self) -> None:
        queued = self.registry.create_run()
        running = self.registry.create_run()
        self.registry.update_status(running.run_id, RunStatus.RUNNING)
        self.registry.close()

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
        self.assertEqual(sum(recovered_counts), 2)
        self.assertEqual(sum(1 for count in recovered_counts if count > 0), 1)
        self.assertIn(2, recovered_counts)

        verify = RunRegistry(self._db_path)
        try:
            queued_record = verify.get_run(queued.run_id)
            running_record = verify.get_run(running.run_id)
            assert queued_record is not None
            assert running_record is not None
            self.assertEqual(queued_record.status, RunStatus.FAILED)
            self.assertEqual(running_record.status, RunStatus.FAILED)
            self.assertEqual(queued_record.error, INTERRUPTED_RUN_ERROR)
            self.assertEqual(running_record.error, INTERRUPTED_RUN_ERROR)
        finally:
            verify.close()


if __name__ == "__main__":
    unittest.main()
