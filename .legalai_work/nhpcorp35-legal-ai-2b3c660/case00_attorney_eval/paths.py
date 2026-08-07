"""Resolve Case-00 attorney-feedback artifact locations without inventing data."""

from __future__ import annotations

import os
from pathlib import Path

CASE00_CORPUS_ID = "case-00-triborough"
BENCHMARK_ID = "attorney-gold-benchmark-01"
PACKET_ID = "attorney-review-packet-02-live"

# Mounted executor volume (primary Case-00 derived corpus).
DEFAULT_VOLUME_ROOT = Path("/app/data/case-00-triborough")

ENV_CASE00_ROOT = "CASE00_TRIBOROUGH_ROOT"
ENV_EVAL_OUT = "CASE00_ATTORNEY_EVAL_OUT"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def case00_root(explicit: Path | str | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get(ENV_CASE00_ROOT)
    if env:
        return Path(env)
    volume = DEFAULT_VOLUME_ROOT
    if volume.is_dir():
        return volume
    # Repo-local mirror (inventory / docs only in many checkouts).
    return repo_root() / "data" / "case-00-triborough"


def derived_root(root: Path | None = None) -> Path:
    return case00_root(root) / "derived"


def gold_benchmark_dir(root: Path | None = None) -> Path:
    return derived_root(root) / "attorney-gold-benchmark-01"


def review_packet_dir(root: Path | None = None) -> Path:
    return derived_root(root) / "attorney-review-packet-02-live"


def provisional_answers_dir(root: Path | None = None) -> Path:
    return gold_benchmark_dir(root) / "provisional-gold-answers"


def attorney_approved_answers_dir(root: Path | None = None) -> Path:
    """Directory reserved for expressly attorney-approved gold answers.

    Must remain distinct from provisional-gold-answers/. Absence means no
    attorney-approved reference answers exist yet.
    """
    return gold_benchmark_dir(root) / "attorney-approved-gold-answers"


def default_output_dir(root: Path | None = None) -> Path:
    env = os.environ.get(ENV_EVAL_OUT)
    if env:
        return Path(env)
    # Prefer generated output on the Case-00 volume (outside git).
    volume_out = derived_root(root) / "attorney-feedback-eval"
    if case00_root(root).exists():
        return volume_out
    return repo_root() / "eval_output" / "case00-attorney-feedback"
