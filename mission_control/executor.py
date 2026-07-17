"""Cursor Agent execution for Phase 2 read-only missions."""

from dataclasses import dataclass
import subprocess

CURSOR_AGENT = "cursor-agent"
EXECUTION_TIMEOUT_SECONDS = 120

SAFETY_CONSTRAINTS = (
    "This is a read-only mission.",
    "Do not modify files.",
    "Do not run Git commands.",
    "Do not create commits.",
    "Do not use worktrees.",
)


@dataclass
class ExecutionResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


def build_cursor_instruction(mission: dict) -> str:
    """Translate a mission into a Cursor Agent instruction."""
    title = mission.get("title", "")
    instructions = mission.get("instructions", "")
    deliverables = mission.get("deliverables", [])

    lines = [
        f"Mission: {title}",
        "",
        "Constraints:",
    ]
    lines.extend(f"- {constraint}" for constraint in SAFETY_CONSTRAINTS)
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


def build_cursor_agent_command(workspace: str, instruction: str) -> list[str]:
    """Build the cursor-agent argv for a read-only mission."""
    return [
        CURSOR_AGENT,
        "--print",
        "--mode",
        "plan",
        "--output-format",
        "text",
        "--workspace",
        workspace,
        "--trust",
        instruction,
    ]


def run_cursor_agent(mission: dict) -> ExecutionResult:
    """Launch cursor-agent for a validated, run-eligible mission."""
    repository = mission["repository"]
    workspace = repository["path"]
    instruction = build_cursor_instruction(mission)
    command = build_cursor_agent_command(workspace, instruction)

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
            error=f"cursor-agent timed out after {EXECUTION_TIMEOUT_SECONDS} seconds",
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
            message = f"cursor-agent exited with code {completed.returncode}"
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
