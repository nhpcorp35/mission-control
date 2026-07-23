"""Cursor Agent execution for validated missions."""

from dataclasses import dataclass
import logging
import os
import subprocess

from app.cursor_cli import cursor_cli_env, find_cursor_agent_binary

CURSOR_AGENT = "cursor-agent"
EXECUTION_TIMEOUT_SECONDS = 600
_MAX_ERROR_LOG_CHARS = 500

logger = logging.getLogger(__name__)


def _bound_error_text(text: str | None) -> str:
    """Bound and redact subprocess error text for safe INFO/ERROR logs."""
    if not text:
        return ""
    cleaned = " ".join(text.split())
    if len(cleaned) > _MAX_ERROR_LOG_CHARS:
        return f"{cleaned[:_MAX_ERROR_LOG_CHARS]}...[truncated]"
    return cleaned

_NO_RECURSIVE_MISSIONS = (
    "Do not submit recursive Mission Control missions.",
)

READ_ONLY_CONSTRAINTS = (
    "This is a read-only mission.",
    "Do not modify files.",
    "Do not run Git commands.",
    "Do not create commits.",
    "Do not use worktrees.",
    *_NO_RECURSIVE_MISSIONS,
)

CREATE_ONLY_CONSTRAINTS = (
    "This mission may create new files only.",
    "Do not modify or delete existing files.",
    "Do not run Git commands.",
    "Do not stage changes.",
    "Do not create commits.",
    "Do not push changes.",
    "Do not use worktrees.",
    *_NO_RECURSIVE_MISSIONS,
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
    *_NO_RECURSIVE_MISSIONS,
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
    *_NO_RECURSIVE_MISSIONS,
)


@dataclass
class ExecutionResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    return_code: int | None = None
    # Redacted argv Mission Control actually launched (instruction omitted).
    command: list[str] | None = None


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
    elif mode == "execute":
        command.append("--force")
    else:
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
    run_id: str | None = None,
) -> ExecutionResult:
    repository = mission["repository"]
    workspace = repository["path"]

    instruction = build_cursor_instruction(
        mission,
        constraints=constraints,
    )

    mission_id = mission.get("mission_id", "unknown")
    title = mission.get("title", "untitled")
    run_label = run_id or "sync"

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
            command=[CURSOR_AGENT],
        )

    command = build_cursor_agent_command(
        workspace,
        instruction,
        mode=mode,
        binary=cursor_binary,
    )
    # Persist redacted argv for structured evidence (omit mission instruction).
    command_evidence = list(command[:-1]) + ["<instruction>"]

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

    logger.info(
        (
            "lifecycle run_id=%s event=subprocess_create_start "
            "api_pid=%s mission_id=%s mode=%s workspace=%s binary=%s"
        ),
        run_label,
        os.getpid(),
        mission_id,
        mode,
        workspace,
        cursor_binary,
    )

    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=workspace,
            env=cursor_cli_env(),
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
            command=command_evidence,
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
            command=command_evidence,
        )
    except OSError as exc:
        logger.exception(
            (
                "lifecycle run_id=%s event=exception "
                "api_pid=%s stage=subprocess_create mission_id=%s workspace=%s"
            ),
            run_label,
            os.getpid(),
            mission_id,
            workspace,
        )

        return ExecutionResult(
            ok=False,
            error=f"Failed to launch {CURSOR_AGENT}: {exc}",
            command=command_evidence,
        )

    child_pid = proc.pid
    logger.info(
        (
            "lifecycle run_id=%s event=subprocess_created "
            "api_pid=%s child_pid=%s mission_id=%s"
        ),
        run_label,
        os.getpid(),
        child_pid,
        mission_id,
    )
    logger.info(
        (
            "lifecycle run_id=%s event=subprocess_wait_start "
            "api_pid=%s child_pid=%s timeout_seconds=%s"
        ),
        run_label,
        os.getpid(),
        child_pid,
        EXECUTION_TIMEOUT_SECONDS,
    )

    try:
        stdout, stderr = proc.communicate(timeout=EXECUTION_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        logger.error(
            (
                "lifecycle run_id=%s event=subprocess_completed "
                "api_pid=%s child_pid=%s returncode=timeout "
                "mission_id=%s timeout_seconds=%s"
            ),
            run_label,
            os.getpid(),
            child_pid,
            mission_id,
            EXECUTION_TIMEOUT_SECONDS,
        )
        logger.error(
            "Cursor mission timed out: mission_id=%s timeout_seconds=%s",
            mission_id,
            EXECUTION_TIMEOUT_SECONDS,
        )

        return ExecutionResult(
            ok=False,
            stdout=stdout or "",
            stderr=stderr or "",
            error=(
                "cursor-agent timed out after "
                f"{EXECUTION_TIMEOUT_SECONDS} seconds"
            ),
            command=command_evidence,
        )
    except Exception:
        logger.exception(
            (
                "lifecycle run_id=%s event=exception "
                "api_pid=%s child_pid=%s stage=subprocess_wait mission_id=%s"
            ),
            run_label,
            os.getpid(),
            child_pid,
            mission_id,
        )
        raise

    stdout = stdout or ""
    stderr = stderr or ""
    returncode = proc.returncode

    logger.info(
        (
            "lifecycle run_id=%s event=subprocess_completed "
            "api_pid=%s child_pid=%s returncode=%s "
            "stdout_chars=%s stderr_chars=%s"
        ),
        run_label,
        os.getpid(),
        child_pid,
        returncode,
        len(stdout),
        len(stderr),
    )

    logger.info(
        (
            "Cursor mission completed: mission_id=%s returncode=%s "
            "stdout_chars=%s stderr_chars=%s"
        ),
        mission_id,
        returncode,
        len(stdout),
        len(stderr),
    )

    if returncode != 0:
        message = stderr.strip() or stdout.strip()

        if not message:
            message = (
                "cursor-agent exited with code "
                f"{returncode}"
            )

        logger.error(
            "Cursor mission failed: mission_id=%s returncode=%s error=%s",
            mission_id,
            returncode,
            _bound_error_text(message),
        )

        return ExecutionResult(
            ok=False,
            stdout=stdout,
            stderr=stderr,
            error=message,
            return_code=returncode,
            command=command_evidence,
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
        return_code=returncode,
        command=command_evidence,
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


def execute_cursor_agent(
    mission: dict,
    *,
    run_id: str | None = None,
) -> ExecutionResult:
    return _run_cursor_agent(
        mission,
        mode="execute",
        constraints=_execution_constraints(mission),
        run_id=run_id,
    )
