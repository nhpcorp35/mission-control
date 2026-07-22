"""Shared helpers for SQLite-backed run registry tests."""

from __future__ import annotations

import os
import tempfile
import unittest

from mission_control.run_registry import RunRegistry


class SqliteRegistryTestCase(unittest.TestCase):
    """Provide an isolated temporary SQLite database per test."""

    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path, recover=False)

    def tearDown(self) -> None:
        self.registry.close()
        os.unlink(self._db_path)
