"""Load existing Case-00 benchmark artifacts; never invent gold data."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from case00_attorney_eval import paths as pathmod

# Status tokens that mean a provisional file is not a usable corrected answer.
_PLACEHOLDER_STATUSES = frozenset(
    {
        "placeholder_pending_reviewer_completion",
        "placeholder",
        "drafting_blocked",
    }
)

_PROVISIONAL_STATUS_RE = re.compile(
    r"^`([a-z0-9_]+)`\s*$",
    re.MULTILINE,
)

_SECTION_3_RE = re.compile(
    r"^##\s+3\.\s+Provisional gold answer\s*$",
    re.MULTILINE | re.IGNORECASE,
)


@dataclass
class ProvisionalAnswerArtifact:
    question_id: str
    path: Path
    status: str
    is_placeholder: bool
    body: Optional[str]
    raw_markdown: str


@dataclass
class AttorneyApprovedAnswerArtifact:
    question_id: str
    path: Path
    body: str
    approval_marker: str


@dataclass
class Case00QuestionBundle:
    question_id: str
    question_text: str
    original_legalai_answer: Optional[str]
    original_packet_meta: dict[str, Any] = field(default_factory=dict)
    label_record: Optional[dict[str, Any]] = None
    provisional: Optional[ProvisionalAnswerArtifact] = None
    attorney_approved: Optional[AttorneyApprovedAnswerArtifact] = None


@dataclass
class Case00BenchmarkCorpus:
    corpus_id: str
    benchmark_id: str
    packet_id: str
    case00_root: Path
    labels_path: Path
    packet_path: Path
    labels: dict[str, Any]
    packet: dict[str, Any]
    questions: list[Case00QuestionBundle]
    rubric: dict[str, Any]
    review_status: dict[str, Any]
    raw_feedback: dict[str, Any]
    missing_artifacts: list[str] = field(default_factory=list)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object at {path}")
    return data


def _parse_provisional_markdown(
    question_id: str, path: Path
) -> ProvisionalAnswerArtifact:
    raw = path.read_text(encoding="utf-8")
    status_match = _PROVISIONAL_STATUS_RE.search(raw)
    status = status_match.group(1) if status_match else "unknown_provisional_status"
    is_placeholder = status in _PLACEHOLDER_STATUSES or (
        "DRAFTING BLOCKED" in raw.upper() and "NO SUBSTANTIVE REPLACEMENT" in raw.upper()
    )

    body: Optional[str] = None
    if not is_placeholder:
        section = _SECTION_3_RE.search(raw)
        if section:
            start = section.end()
            next_heading = re.search(r"^##\s+\d+\.", raw[start:], re.MULTILINE)
            end = start + next_heading.start() if next_heading else len(raw)
            body = raw[start:end].strip() or None
        if body is None:
            # Fall back to full file only when status indicates a real provisional.
            body = raw.strip() or None

    return ProvisionalAnswerArtifact(
        question_id=question_id,
        path=path,
        status=status,
        is_placeholder=is_placeholder,
        body=None if is_placeholder else body,
        raw_markdown=raw,
    )


def _load_provisional_answers(
    directory: Path,
) -> dict[str, ProvisionalAnswerArtifact]:
    out: dict[str, ProvisionalAnswerArtifact] = {}
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("Q*_provisional_gold_answer.md")):
        m = re.match(r"(Q\d+)_provisional_gold_answer\.md$", path.name)
        if not m:
            continue
        qid = m.group(1)
        out[qid] = _parse_provisional_markdown(qid, path)
    return out


def _load_attorney_approved_answers(
    directory: Path,
) -> dict[str, AttorneyApprovedAnswerArtifact]:
    """Load only expressly attorney-approved gold answers.

    Accepted filenames:
      QN_attorney_approved_gold_answer.md
      QN_final_attorney_approved_answer.md

    File must contain an explicit approval marker. Provisional files are never
    read from this directory and are never promoted here by this loader.
    """
    out: dict[str, AttorneyApprovedAnswerArtifact] = {}
    if not directory.is_dir():
        return out

    patterns = (
        "*_attorney_approved_gold_answer.md",
        "*_final_attorney_approved_answer.md",
    )
    approval_markers = (
        "attorney_approved_gold",
        "final_attorney_approved",
        "attorney-approved gold",
        "expressly attorney-approved",
    )
    for pattern in patterns:
        for path in sorted(directory.glob(pattern)):
            m = re.match(
                r"(Q\d+)_(?:attorney_approved_gold_answer|final_attorney_approved_answer)\.md$",
                path.name,
            )
            if not m:
                continue
            text = path.read_text(encoding="utf-8")
            lower = text.lower()
            marker = next((mk for mk in approval_markers if mk in lower), None)
            if marker is None:
                # Refuse to treat unmarked files as attorney-approved.
                continue
            out[m.group(1)] = AttorneyApprovedAnswerArtifact(
                question_id=m.group(1),
                path=path,
                body=text.strip(),
                approval_marker=marker,
            )
    return out


def load_case00_benchmark(
    case00_root: Path | str | None = None,
) -> Case00BenchmarkCorpus:
    root = pathmod.case00_root(case00_root)
    labels_path = pathmod.gold_benchmark_dir(root) / "attorney_gold_labels_01.json"
    packet_path = (
        pathmod.review_packet_dir(root) / "attorney_review_packet_02.json"
    )
    provisional_dir = pathmod.provisional_answers_dir(root)
    approved_dir = pathmod.attorney_approved_answers_dir(root)

    missing: list[str] = []
    if not labels_path.is_file():
        missing.append(str(labels_path))
    if not packet_path.is_file():
        missing.append(str(packet_path))
    if missing:
        raise FileNotFoundError(
            "Case-00 attorney-feedback artifacts missing: " + "; ".join(missing)
        )

    labels = _load_json(labels_path)
    packet = _load_json(packet_path)
    provisional = _load_provisional_answers(provisional_dir)
    approved = _load_attorney_approved_answers(approved_dir)

    label_by_id = {
        rec["question_id"]: rec
        for rec in labels.get("question_records") or []
        if isinstance(rec, dict) and rec.get("question_id")
    }
    packet_by_id = {
        rec["question_id"]: rec
        for rec in packet.get("questions") or []
        if isinstance(rec, dict) and rec.get("question_id")
    }

    # Evaluate every question that has an original LegalAI answer in the packet.
    question_ids = sorted(
        packet_by_id.keys(),
        key=lambda q: (len(q), q),
    )

    bundles: list[Case00QuestionBundle] = []
    for qid in question_ids:
        pkt = packet_by_id[qid]
        original = pkt.get("proposed_answer")
        if original is not None and not str(original).strip():
            original = None
        question_text = (
            pkt.get("text")
            or (label_by_id.get(qid) or {}).get("question_text")
            or ""
        )
        bundles.append(
            Case00QuestionBundle(
                question_id=qid,
                question_text=question_text,
                original_legalai_answer=original if original is not None else None,
                original_packet_meta={
                    "answer_status": pkt.get("answer_status"),
                    "reasoner_status": pkt.get("reasoner_status"),
                    "confidence": pkt.get("confidence"),
                    "packet_answer_status": pkt.get("answer_status"),
                },
                label_record=label_by_id.get(qid),
                provisional=provisional.get(qid),
                attorney_approved=approved.get(qid),
            )
        )

    if not provisional_dir.is_dir():
        missing.append(f"provisional answers dir absent: {provisional_dir}")

    return Case00BenchmarkCorpus(
        corpus_id=labels.get("corpus_id") or pathmod.CASE00_CORPUS_ID,
        benchmark_id=labels.get("benchmark_id") or pathmod.BENCHMARK_ID,
        packet_id=labels.get("packet_id") or pathmod.PACKET_ID,
        case00_root=root,
        labels_path=labels_path,
        packet_path=packet_path,
        labels=labels,
        packet=packet,
        questions=bundles,
        rubric=labels.get("rubric") or {},
        review_status=labels.get("review_status") or {},
        raw_feedback=labels.get("raw_feedback") or {},
        missing_artifacts=missing,
    )
