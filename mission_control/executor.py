"""Cursor Agent execution for validated missions."""

from dataclasses import dataclass
import logging
import subprocess

from app.cursor_cli import cursor_cli_env, find_cursor_agent_binary

CURSOR_AGENT = "cursor-agent"
EXECUTION_TIMEOUT_SECONDS = 120

logger = logging.getLogger(__name__)

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

MODIFY_ONLY_CONSTRAINTS = (
    "This mission may modify existing files only.",
    "Modify only the files explicitly identified in the mission instructions.",
    "Do not create or delete files.",
    "Do not run Git commands.",
    "Do not stage changes.",
    "Do not create commits.",
    "Do not push changes.",
    "Do not use worktrees.",
)

CREATE_AND_MODIFY_CONSTRAINTS = (
    "This mission may create new files and modify existing files.",
    "Modify only the files explicitly identified in the mission instructions.",
    "Do not delete files.",
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
    binary: str = CURSOR_AGENT,
) -> list[str]:
    command = [
        binary,
        "--print",
    ]

    if mode in {"plan", "ask"}:
        command.extend(["--mode", mode])
    elif mode != "execute":
        raise ValueError(f"Unsupported Cursor Agent mode: {mode}")

    command.extend(
        [
            "--output-format",
            "text",
            "--workspace",
            workspace,
            "--trust",
            instruction,
        ]
    )

    return command


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

    mission_id = mission.get("mission_id", "unknown")
    title = mission.get("title", "untitled")

    cursor_binary = find_cursor_agent_binary()

    if cursor_binary is None:
        logger.error(
            "Cursor Agent binary not found: mission_id=%s binary=%s",
            mission_id,
            CURSOR_AGENT,
        )

        return ExecutionResult(
            ok=False,
            error=f"{CURSOR_AGENT} not found",
        )

    command = build_cursor_agent_command(
        workspace,
        instruction,
        mode=mode,
        binary=cursor_binary,
    )

    logger.info(
        "Starting Cursor mission: mission_id=%s title=%s mode=%s workspace=%s",
        mission_id,
        title,
        mode,
        workspace,
    )

    logger.info(
        "Cursor command prepared: binary=%s mode=%s workspace=%s",
        cursor_binary,
        mode,
        workspace,
    )

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=EXECUTION_TIMEOUT_SECONDS,
            cwd=workspace,
            env=cursor_cli_env(),
        )
    except subprocess.TimeoutExpired:
        logger.error(
            "Cursor mission timed out: mission_id=%s timeout_seconds=%s",
            mission_id,
            EXECUTION_TIMEOUT_SECONDS,
        )

        return ExecutionResult(
            ok=False,
            error=(
                "cursor-agent timed out after "
                f"{EXECUTION_TIMEOUT_SECONDS} seconds"
            ),
        )
    except FileNotFoundError:
        logger.error(
            "Cursor Agent binary not found: mission_id=%s binary=%s",
            mission_id,
            CURSOR_AGENT,
        )

        return ExecutionResult(
            ok=False,
            error=f"{CURSOR_AGENT} not found",
        )
    except NotADirectoryError:
        logger.error(
            "Cursor workspace is not a directory: mission_id=%s workspace=%s",
            mission_id,
            workspace,
        )

        return ExecutionResult(
            ok=False,
            error=f"Repository workspace is not a directory: {workspace}",
        )
    except OSError as exc:
        logger.exception(
            "Failed to launch Cursor Agent: mission_id=%s workspace=%s",
            mission_id,
            workspace,
        )

        return ExecutionResult(
            ok=False,
            error=f"Failed to launch {CURSOR_AGENT}: {exc}",
        )

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""

    logger.info(
        (
            "Cursor mission completed: mission_id=%s returncode=%s "
            "stdout_chars=%s stderr_chars=%s"
        ),
        mission_id,
        completed.returncode,
        len(stdout),
        len(stderr),
    )

    if completed.returncode != 0:
        message = stderr.strip() or stdout.strip()

        if not message:
            message = (
                "cursor-agent exited with code "
                f"{completed.returncode}"
            )

        logger.error(
            "Cursor mission failed: mission_id=%s returncode=%s error=%s",
            mission_id,
            completed.returncode,
            message[:500],
        )

        return ExecutionResult(
            ok=False,
            stdout=stdout,
            stderr=stderr,
            error=message,
        )

    if not stdout.strip():
        logger.warning(
            "Cursor mission succeeded with empty stdout: mission_id=%s",
            mission_id,
        )

    return ExecutionResult(
        ok=True,
        stdout=stdout,
        stderr=stderr,
    )


def _execution_constraints(
    mission: dict,
) -> tuple[str, ...]:
    permissions = mission.get("permissions", {})

    if not isinstance(permissions, dict):
        return CREATE_ONLY_CONSTRAINTS

    create_files = bool(permissions.get("create_files"))
    modify_files = bool(permissions.get("modify_files"))

    if create_files and modify_files:
        return CREATE_AND_MODIFY_CONSTRAINTS

    if modify_files:
        return MODIFY_ONLY_CONSTRAINTS

    return CREATE_ONLY_CONSTRAINTS


def run_cursor_agent(mission: dict) -> ExecutionResult:
    return _run_cursor_agent(
        mission,
        mode="ask",
        constraints=READ_ONLY_CONSTRAINTS,
    )


def execute_cursor_agent(mission: dict) -> ExecutionResult:
    return _run_cursor_agent(
        mission,
        mode="execute",
        constraints=_execution_constraints(mission),
    )