"""Structured, machine-readable evidence for asynchronous Mission Control runs.

Evidence is collected from Mission Control execution records and repository
state only. Agent-authored stdout/stderr is retained for diagnostics but is
never treated as verified structured evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from typing import Any

from mission_control.executor import CURSOR_AGENT, ExecutionResult

WARNING_NO_TEST_COUNTS = (
    "Aggregate test counts are unavailable; Mission Control does not parse "
    "agent stdout for test results."
)
WARNING_NO_SEPARATE_VERIFICATION_COMMANDS = (
    "No separate Mission Control verification shell commands were executed; "
    "only the Cursor agent subprocess and platform checks are recorded."
)
WARNING_FILES_CHANGED_UNAVAILABLE = (
    "Changed files are unavailable; workspace Git status could not be read."
)
WARNING_PREP_FAILED = (
    "Workspace preparation failed before agent execution; evidence is limited."
)
WARNING_DELIVERABLES_NOT_CHECKED = (
    "Declared file deliverables were not checked because agent execution "
    "did not succeed."
)
WARNING_PERSISTENCE_NOT_ATTEMPTED = (
    "Platform persistence was not attempted for this run."
)


@dataclass(frozen=True)
class CommandEvidence:
    """One command Mission Control itself executed (not agent-claimed)."""

    argv: list[str]
    exit_code: int | None
    passed: bool | None
    kind: str


@dataclass(frozen=True)
class DeliverableEvidence:
    """Declared file-deliverable verification performed by Mission Control."""

    verified: bool
    passed: bool | None
    checked_paths: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PersistenceEvidence:
    """Platform Git persistence outcome recorded by Mission Control."""

    mode: str | None
    attempted: bool
    ok: bool | None
    commit_sha: str | None = None


@dataclass
class StructuredRunResult:
    """Objective execution and verification evidence for a terminal run."""

    files_changed: list[str] = field(default_factory=list)
    commands: list[CommandEvidence] = field(default_factory=list)
    test_counts: dict[str, int] | None = None
    deliverables: DeliverableEvidence | None = None
    persistence: PersistenceEvidence | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_changed": list(self.files_changed),
            "commands": [asdict(command) for command in self.commands],
            "test_counts": self.test_counts,
            "deliverables": (
                asdict(self.deliverables) if self.deliverables is not None else None
            ),
            "persistence": (
                asdict(self.persistence) if self.persistence is not None else None
            ),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> StructuredRunResult | None:
        if data is None:
            return None
        if not isinstance(data, dict):
            return None

        commands_raw = data.get("commands") or []
        commands: list[CommandEvidence] = []
        if isinstance(commands_raw, list):
            for item in commands_raw:
                if not isinstance(item, dict):
                    continue
                argv = item.get("argv") or []
                if not isinstance(argv, list):
                    argv = []
                commands.append(
                    CommandEvidence(
                        argv=[str(part) for part in argv],
                        exit_code=item.get("exit_code"),
                        passed=item.get("passed"),
                        kind=str(item.get("kind") or "unknown"),
                    )
                )

        deliverables = None
        deliverables_raw = data.get("deliverables")
        if isinstance(deliverables_raw, dict):
            checked = deliverables_raw.get("checked_paths") or []
            missing = deliverables_raw.get("missing") or []
            deliverables = DeliverableEvidence(
                verified=bool(deliverables_raw.get("verified")),
                passed=deliverables_raw.get("passed"),
                checked_paths=[str(path) for path in checked]
                if isinstance(checked, list)
                else [],
                missing=[str(path) for path in missing]
                if isinstance(missing, list)
                else [],
            )

        persistence = None
        persistence_raw = data.get("persistence")
        if isinstance(persistence_raw, dict):
            mode = persistence_raw.get("mode")
            persistence = PersistenceEvidence(
                mode=str(mode) if mode is not None else None,
                attempted=bool(persistence_raw.get("attempted")),
                ok=persistence_raw.get("ok"),
                commit_sha=persistence_raw.get("commit_sha"),
            )

        files_changed = data.get("files_changed") or []
        if not isinstance(files_changed, list):
            files_changed = []

        warnings = data.get("warnings") or []
        if not isinstance(warnings, list):
            warnings = []

        test_counts = data.get("test_counts")
        if test_counts is not None and not isinstance(test_counts, dict):
            test_counts = None

        return cls(
            files_changed=[str(path) for path in files_changed],
            commands=commands,
            test_counts=test_counts,
            deliverables=deliverables,
            persistence=persistence,
            warnings=[str(item) for item in warnings],
        )


def serialize_structured_result(
    result: StructuredRunResult | None,
) -> str | None:
    """Serialize a structured result to JSON text for SQLite storage."""
    if result is None:
        return None
    return json.dumps(result.to_dict(), separators=(",", ":"), sort_keys=True)


def deserialize_structured_result(
    raw: str | None,
) -> StructuredRunResult | None:
    """Load a structured result from SQLite JSON text."""
    if raw is None or raw == "":
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return StructuredRunResult.from_dict(data)


def empty_structured_result(
    *,
    warnings: list[str] | None = None,
) -> StructuredRunResult:
    """Return a result shell with standard unavailable-evidence warnings."""
    merged = [
        WARNING_NO_TEST_COUNTS,
        WARNING_NO_SEPARATE_VERIFICATION_COMMANDS,
    ]
    if warnings:
        for warning in warnings:
            if warning not in merged:
                merged.append(warning)
    return StructuredRunResult(
        test_counts=None,
        warnings=merged,
    )


def parse_git_status_porcelain_paths(stdout: str) -> list[str]:
    """Parse repository-relative paths from ``git status --porcelain`` output."""
    paths: list[str] = []
    for line in stdout.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:]
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        if len(entry) >= 2 and entry[0] == '"' and entry[-1] == '"':
            entry = entry[1:-1]
        if entry:
            paths.append(entry)
    return sorted(set(paths))


def command_evidence_from_execution(
    execution_result: ExecutionResult,
) -> CommandEvidence:
    """Build command evidence from a Cursor agent ``ExecutionResult``."""
    argv = list(execution_result.command or [CURSOR_AGENT])
    exit_code = execution_result.return_code
    if exit_code is not None:
        passed = exit_code == 0
    elif execution_result.ok:
        passed = True
    else:
        # Timeout / launch failures may omit a process exit code.
        passed = False
    return CommandEvidence(
        argv=argv,
        exit_code=exit_code,
        passed=passed,
        kind="cursor_agent",
    )


def append_warning(result: StructuredRunResult, warning: str) -> None:
    """Append ``warning`` when not already present."""
    if warning not in result.warnings:
        result.warnings.append(warning)
