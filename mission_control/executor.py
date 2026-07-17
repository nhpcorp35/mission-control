"""Cursor Agent execution for validated missions."""

from dataclasses import dataclass
import subprocess

CURSOR_AGENT = "cursor-agent"
EXECUTION_TIMEOUT_SECONDS = 120

READ_ONLY_CONSTRAINTS = (
    "This is a read-only mission.",
    "Do not modify files.",
    "Do not run Git commands.",
    "Do not create commits.",
    "Do not use worktrees.",
)

CREATE_ONLY_CONSTRAINTS = (
    "This mission may create new files only.",
    "Do not modify or delete existing files.",
    "Do not run Git commands.",
    "Do not stage changes.",
    "Do not create commits.",
    "Do not push changes.",
    "Do not use worktrees.",
)


@dataclass
class ExecutionResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


def build_cursor_instruction(
    mission: dict,
    constraints: tuple[str, ...] = READ_ONLY_CONSTRAINTS,
) -> str:
    title = mission.get("title", "")
    instructions = mission.get("instructions", "")
    deliverables = mission.get("deliverables", [])

    lines = [
        f"Mission: {title}",
        "",
        "Constraints:",
    ]

    lines.extend(f"- {constraint}" for constraint in constraints)

    lines.extend(
        [
            "",
            "Instructions:",
            str(instructions).rstrip(),
            "",
            "Deliverables:",
        ]
    )

    if isinstance(deliverables, list) and deliverables:
        lines.extend(f"- {item}" for item in deliverables)
    else:
        lines.append("- (none specified)")

    return "\n".join(lines).strip()


def build_cursor_agent_command(
    workspace: str,
    instruction: str,
    mode: str = "plan",
) -> list[str]:
    return [
        CURSOR_AGENT,
        "--print",
        "--mode",
        mode,
        "--output-format",
        "text",
        "--workspace",
        workspace,
        "--trust",
        instruction,
    ]


def _run_cursor_agent(
    mission: dict,
    *,
    mode: str,
    constraints: tuple[str, ...],
) -> ExecutionResult:
    repository = mission["repository"]
    workspace = repository["path"]

    instruction = build_cursor_instruction(
        mission,
        constraints=constraints,
    )

    command = build_cursor_agent_command(
        workspace,
        instruction,
        mode=mode,
    )

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=EXECUTION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return ExecutionResult(
            ok=False,
            error=(
                "cursor-agent timed out after "
                f"{EXECUTION_TIMEOUT_SECONDS} seconds"
            ),
        )
    except FileNotFoundError:
        return ExecutionResult(
            ok=False,
            error=f"{CURSOR_AGENT} not found",
        )
    except OSError as exc:
        return ExecutionResult(
            ok=False,
            error=f"Failed to launch {CURSOR_AGENT}: {exc}",
        )

    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()

        if not message:
            message = (
                f"cursor-agent exited with code "
                f"{completed.returncode}"
            )

        return ExecutionResult(
            ok=False,
            stdout=completed.stdout,
            stderr=completed.stderr,
            error=message,
        )

    return ExecutionResult(
        ok=True,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def run_cursor_agent(mission: dict) -> ExecutionResult:
    return _run_cursor_agent(
        mission,
        mode="plan",
        constraints=READ_ONLY_CONSTRAINTS,
    )


def execute_cursor_agent(mission: dict) -> ExecutionResult:
    return _run_cursor_agent(
        mission,
        mode="execute",
        constraints=CREATE_ONLY_CONSTRAINTS,
    )
