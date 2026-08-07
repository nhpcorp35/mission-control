#!/usr/bin/env python3
"""Entry point: Case-00 attorney-feedback evaluation.

Usage:
  python run_case00_attorney_eval.py
  python -m case00_attorney_eval
  python -m case00_attorney_eval.cli
"""

from case00_attorney_eval.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
