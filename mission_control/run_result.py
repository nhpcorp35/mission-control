"""Structured, machine-readable evidence for asynchronous Mission Control runs.

Evidence is collected from Mission Control execution records and repository
state only. Agent-authored stdout/stderr is retained for diagnostics but is
never treated as verified structured evidence. The Mission Control-authored
``summary`` field is the authoritative client-facing narrative for platform
persistence outcomes (which occur after the agent completes).
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
WARNING_STDOUT_PREDATES_PERSISTENCE = (
    "Agent stdout was captured before platform persistence; prefer "
    "result.summary, result.persistence, and commit_sha for the "
    "persistence outcome."
)
WARNING_DOCUMENTATION_PATH_HEURISTIC = (
    "Documentation status updated vs not_required is derived from changed "
    "file paths (docs/ prefix or .md suffix), not from agent stdout claims."
)

DOCUMENTATION_STATUS_NOT_REQUESTED = "not_requested"
DOCUMENTATION_STATUS_UPDATED = "updated"
DOCUMENTATION_STATUS_NOT_REQUIRED = "not_required"
DOCUMENTATION_STATUS_FAILED = "failed"

SUPPORTED_DOCUMENTATION_RESULT_STATUSES = frozenset(
    {
        DOCUMENTATION_STATUS_NOT_REQUESTED,
        DOCUMENTATION_STATUS_UPDATED,
        DOCUMENTATION_STATUS_NOT_REQUIRED,
        DOCUMENTATION_STATUS_FAILED,
    }
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
    # Absolute, home (~), or ..-escaping paths recorded without reading them.
    outside_workspace: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PersistenceEvidence:
    """Platform Git persistence outcome recorded by Mission Control.

    ``mode`` is the authoritative persistence level for this run: the validated
    mission ``persistence.mode`` when persistence was not attempted, or the
    mode actually completed (or attempted and failed) by the platform
    persistence layer. It is never inferred from agent stdout.

    ``pushed`` is True only when a platform push completed successfully.
    """

    mode: str | None
    attempted: bool
    ok: bool | None
    commit_sha: str | None = None
    pushed: bool | None = None


@dataclass(frozen=True)
class DocumentationEvidence:
    """Documentation policy outcome recorded by Mission Control.

    ``mode`` is the validated mission ``documentation.mode`` (default ``none``
    when omitted). ``status`` is derived from that mode plus Mission Control
    execution artifacts (agent success and ``files_changed``), never from
    agent stdout claims alone.
    """

    mode: str
    status: str


@dataclass
class StructuredRunResult:
    """Objective execution and verification evidence for a terminal run."""

    files_changed: list[str] = field(default_factory=list)
    commands: list[CommandEvidence] = field(default_factory=list)
    test_counts: dict[str, int] | None = None
    deliverables: DeliverableEvidence | None = None
    persistence: PersistenceEvidence | None = None
    documentation: DocumentationEvidence | None = None
    warnings: list[str] = field(default_factory=list)
    # Mission Control-authored client summary; authoritative for persistence.
    summary: str | None = None

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
            "documentation": (
                asdict(self.documentation)
                if self.documentation is not None
                else None
            ),
            "warnings": list(self.warnings),
            "summary": self.summary,
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
            outside = deliverables_raw.get("outside_workspace") or []
            deliverables = DeliverableEvidence(
                verified=bool(deliverables_raw.get("verified")),
                passed=deliverables_raw.get("passed"),
                checked_paths=[str(path) for path in checked]
                if isinstance(checked, list)
                else [],
                missing=[str(path) for path in missing]
                if isinstance(missing, list)
                else [],
                outside_workspace=[str(path) for path in outside]
                if isinstance(outside, list)
                else [],
            )

        persistence = None
        persistence_raw = data.get("persistence")
        if isinstance(persistence_raw, dict):
            mode = persistence_raw.get("mode")
            pushed_raw = persistence_raw.get("pushed")
            pushed: bool | None
            if pushed_raw is None and "pushed" not in persistence_raw:
                pushed = None
            else:
                pushed = bool(pushed_raw) if pushed_raw is not None else None
            persistence = PersistenceEvidence(
                mode=str(mode) if mode is not None else None,
                attempted=bool(persistence_raw.get("attempted")),
                ok=persistence_raw.get("ok"),
                commit_sha=persistence_raw.get("commit_sha"),
                pushed=pushed,
            )

        documentation = None
        documentation_raw = data.get("documentation")
        if isinstance(documentation_raw, dict):
            doc_mode = documentation_raw.get("mode")
            doc_status = documentation_raw.get("status")
            if doc_mode is not None and doc_status is not None:
                documentation = DocumentationEvidence(
                    mode=str(doc_mode),
                    status=str(doc_status),
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

        summary_raw = data.get("summary")
        summary = str(summary_raw) if summary_raw is not None else None

        return cls(
            files_changed=[str(path) for path in files_changed],
            commands=commands,
            test_counts=test_counts,
            deliverables=deliverables,
            persistence=persistence,
            documentation=documentation,
            warnings=[str(item) for item in warnings],
            summary=summary,
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


def looks_like_documentation_path(path: str) -> bool:
    """Return True when ``path`` looks like a documentation file path.

    Conservative heuristic used only for structured ``documentation.status``
    reporting (``updated`` vs ``not_required``). Not used to select files for
    the agent to edit.
    """
    normalized = path.replace("\\", "/").lstrip("./")
    if not normalized:
        return False
    if normalized == "docs" or normalized.startswith("docs/"):
        return True
    return normalized.endswith(".md")


def build_documentation_evidence(
    mission: dict,
    *,
    files_changed: list[str] | None = None,
    handling_completed: bool,
) -> DocumentationEvidence:
    """Build authoritative documentation evidence from mission + artifacts.

    ``handling_completed`` is True only when Mission Control considers the
    agent execution successful enough that documentation review could have
    completed (typically agent ``ok`` and, for async runs, deliverables gate
    passed). It is never set from agent stdout claims.
    """
    from mission_control.validator import resolve_documentation_mode

    mode = resolve_documentation_mode(mission)
    if mode == "none":
        return DocumentationEvidence(
            mode="none",
            status=DOCUMENTATION_STATUS_NOT_REQUESTED,
        )

    # mode == "required" (validated missions only reach here with supported modes)
    if not handling_completed:
        return DocumentationEvidence(
            mode=mode,
            status=DOCUMENTATION_STATUS_FAILED,
        )

    changed = files_changed or []
    if any(looks_like_documentation_path(path) for path in changed):
        return DocumentationEvidence(
            mode=mode,
            status=DOCUMENTATION_STATUS_UPDATED,
        )
    return DocumentationEvidence(
        mode=mode,
        status=DOCUMENTATION_STATUS_NOT_REQUIRED,
    )


def build_run_summary(
    *,
    persistence: PersistenceEvidence | None,
    error: str | None = None,
    agent_ok: bool | None = None,
    agent_return_code: int | None = None,
) -> str:
    """Build the authoritative client-facing run summary.

    Composed after platform persistence evidence is recorded so agent prose
    cannot contradict commit/push results. Explicitly separates the agent
    execution outcome from authoritative platform persistence.

    Clients must prefer this summary (and ``result.persistence`` /
    ``commit_sha``) over agent prose for persistence claims.
    """
    if agent_ok is True:
        if agent_return_code is None:
            agent_line = "Agent result: succeeded."
        else:
            agent_line = (
                f"Agent result: succeeded (return_code={agent_return_code})."
            )
    elif agent_ok is False:
        if agent_return_code is None:
            agent_line = "Agent result: failed."
        else:
            agent_line = (
                f"Agent result: failed (return_code={agent_return_code})."
            )
    else:
        agent_line = "Agent result: not executed."

    if persistence is None:
        persistence_line = "Platform persistence evidence is unavailable."
    elif not persistence.attempted:
        mode = persistence.mode or "unknown"
        persistence_line = (
            f"Platform persistence was not attempted (mode={mode})."
        )
    elif persistence.ok is True:
        mode = persistence.mode or "unknown"
        push_note = ""
        if persistence.pushed is True:
            push_note = ", pushed=true"
        elif persistence.pushed is False and mode == "push":
            push_note = ", pushed=false"
        if persistence.commit_sha:
            persistence_line = (
                "Platform persistence succeeded "
                f"(mode={mode}, commit_sha={persistence.commit_sha}"
                f"{push_note})."
            )
        elif mode == "none":
            persistence_line = "Platform persistence skipped (mode=none)."
        else:
            persistence_line = (
                "Platform persistence succeeded with no repository changes "
                f"(mode={mode}{push_note})."
            )
    elif persistence.ok is False:
        mode = persistence.mode or "unknown"
        if error:
            persistence_line = (
                f"Platform persistence failed (mode={mode}): {error}"
            )
        else:
            persistence_line = f"Platform persistence failed (mode={mode})."
    else:
        mode = persistence.mode or "unknown"
        persistence_line = (
            f"Platform persistence outcome is incomplete (mode={mode})."
        )

    trust_line = (
        "Agent stdout is diagnostic only and was captured before platform "
        "persistence when persistence ran; prefer this summary, "
        "result.persistence, and commit_sha for persistence claims — never "
        "treat agent prose as authoritative over platform persistence."
    )
    return f"{agent_line} {persistence_line} {trust_line}"


def _agent_outcome_from_commands(
    result: StructuredRunResult,
) -> tuple[bool | None, int | None]:
    """Derive agent success from Mission Control command evidence."""
    for command in result.commands:
        if command.kind != "cursor_agent":
            continue
        return command.passed, command.exit_code
    return None, None


def finalize_structured_summary(
    result: StructuredRunResult,
    *,
    error: str | None = None,
) -> None:
    """Set ``result.summary`` after persistence evidence is attached.

    The summary is composed only here (post-persistence bookkeeping) and
    clearly separates agent execution from authoritative platform persistence
    so stale agent prose cannot contradict commit/push results.

    When persistence was attempted, also record that agent stdout predates
    the platform persistence step.
    """
    if result.persistence is not None and result.persistence.attempted:
        append_warning(result, WARNING_STDOUT_PREDATES_PERSISTENCE)
    if (
        result.documentation is not None
        and result.documentation.mode == "required"
        and result.documentation.status
        in {
            DOCUMENTATION_STATUS_UPDATED,
            DOCUMENTATION_STATUS_NOT_REQUIRED,
        }
    ):
        append_warning(result, WARNING_DOCUMENTATION_PATH_HEURISTIC)
    agent_ok, agent_return_code = _agent_outcome_from_commands(result)
    result.summary = build_run_summary(
        persistence=result.persistence,
        error=error,
        agent_ok=agent_ok,
        agent_return_code=agent_return_code,
    )
