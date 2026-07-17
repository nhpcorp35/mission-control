#!/usr/bin/env python3
"""Mission Control CLI entry point."""

import sys

from mission_control.executor import run_cursor_agent
from mission_control.validator import (
    load_mission_file,
    validate_mission_file,
    validate_mission_for_run,
)


def _usage() -> str:
    return (
        "Usage: python3 mc.py validate <mission-file>\n"
        "       python3 mc.py run <mission-file>"
    )


def _run_validate(path: str) -> int:
    result = validate_mission_file(path)

    if result.ok:
        print("\u2713 Mission valid")
        return 0

    print("\u2717 Mission invalid")
    print(result.error)
    return 1


def _run_mission(path: str) -> int:
    structural_result, mission = load_mission_file(path)
    if not structural_result.ok:
        print("\u2717 Mission invalid", file=sys.stderr)
        print(structural_result.error, file=sys.stderr)
        return 1

    run_result = validate_mission_for_run(mission)
    if not run_result.ok:
        print("\u2717 Mission not runnable", file=sys.stderr)
        print(run_result.error, file=sys.stderr)
        return 1

    execution_result = run_cursor_agent(mission)
    if not execution_result.ok:
        print("\u2717 Mission execution failed", file=sys.stderr)
        print(execution_result.error, file=sys.stderr)
        return 1

    sys.stdout.write(execution_result.stdout)
    if execution_result.stdout and not execution_result.stdout.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in {"validate", "run"}:
        print(_usage(), file=sys.stderr)
        return 2

    command, path = argv
    if command == "validate":
        return _run_validate(path)
    return _run_mission(path)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
