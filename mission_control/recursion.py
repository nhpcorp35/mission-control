"""Guards against recursive Mission Control mission submissions."""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
import threading

_local = threading.local()

RECURSIVE_SUBMISSION_ERROR = (
    "Recursive Mission Control submission rejected. "
    "Missions must not submit new Mission Control runs while executing."
)

# Header a nested local caller may send; also used by regression tests.
RECURSIVE_SUBMISSION_HEADER = "x-mission-control-recursive-submission"


def _depth() -> int:
    return int(getattr(_local, "depth", 0))


def enter_execution() -> None:
    """Mark the current thread as inside Cursor execution."""
    _local.depth = _depth() + 1


def exit_execution() -> None:
    """Leave the current thread's Cursor execution scope."""
    _local.depth = max(0, _depth() - 1)


def is_inside_execution() -> bool:
    """Return True when the current thread is executing a mission."""
    return _depth() > 0


def is_recursive_submission(headers: dict[str, str] | None = None) -> bool:
    """Return True when this submission would nest inside an active run.

    Detects same-thread re-entrancy and explicit recursive-submission headers
    used by nested local callers.
    """
    if is_inside_execution():
        return True
    if not headers:
        return False
    normalized = {
        str(key).lower(): str(value).strip().lower()
        for key, value in headers.items()
    }
    marker = normalized.get(RECURSIVE_SUBMISSION_HEADER, "")
    return marker in {"1", "true", "yes", "blocked", "nested"}


@contextmanager
def execution_scope() -> Iterator[None]:
    """Context manager that marks the thread as inside execution."""
    enter_execution()
    try:
        yield
    finally:
        exit_execution()
