#!/usr/bin/env python3
"""Mission Control CLI entry point."""

import sys

from mission_control.validator import validate_mission_file


def _usage() -> str:
    return "Usage: python3 mc.py validate <mission-file>"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "validate":
        print(_usage(), file=sys.stderr)
        return 2

    path = argv[1]
    result = validate_mission_file(path)

    if result.ok:
        print("\u2713 Mission valid")
        return 0

    print("\u2717 Mission invalid")
    print(result.error)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
