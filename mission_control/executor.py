"""Cursor Agent execution for validated missions."""

from dataclasses import dataclass
import logging
import os
import signal
import subprocess
import time

from app.cursor_cli import cursor_cli_env, find_cursor_agent_binary

CURSOR_AGENT = "cursor-agent"
CURSOR_EXECUTION_TIMEOUT_ENV = "CURSOR_EXECUTION_TIMEOUT_SECONDS"
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 600
# Conservative ceiling: 1 hour. A higher operator value would pin a run worker.
MAX_EXECUTION_TIMEOUT_SECONDS = 3600
MIN_EXECUTION_TIMEOUT_SECONDS = 1
# Compatibility alias for the default hard timeout (runtime uses the resolver).
EXECUTION_TIMEOUT_SECONDS = DEFAULT_EXECUTION_TIMEOUT_SECONDS
# After killing a timed-out agent, wait at most this long for pipes to close.
# Unbounded communicate() can hang forever when grandchildren keep stdio open.
CLEANUP_TIMEOUT_SECONDS = 10
_MAX_ERROR_LOG_CHARS = 500

logger = logging.getLogger(__name__)


def resolve_execution_timeout_seconds() -> int:
    """Return the Cursor-agent hard timeout in seconds.

    Reads ``CURSOR_EXECUTION_TIMEOUT_SECONDS``. Absent or blank values use
    ``DEFAULT_EXECUTION_TIMEOUT_SECONDS`` (600). Invalid, non-positive, or
    values above ``MAX_EXECUTION_TIMEOUT_SECONDS`` (3600) fall back to 600.
    """
    raw = os.environ.get(CURSOR_EXECUTION_TIMEOUT_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_EXECUTION_TIMEOUT_SECONDS
    try:
        value = int(str(raw).strip(), 10)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid %s=%r; falling back to %s seconds",
            CURSOR_EXECUTION_TIMEOUT_ENV,
            raw,
            DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        )
        return DEFAULT_EXECUTION_TIMEOUT_SECONDS
    if (
        value < MIN_EXECUTION_TIMEOUT_SECONDS
        or value > MAX_EXECUTION_TIMEOUT_SECONDS
    ):
        logger.warning(
            (
                "%s=%s is outside [%s, %s]; falling back to %s seconds"
            ),
            CURSOR_EXECUTION_TIMEOUT_ENV,
            value,
            MIN_EXECUTION_TIMEOUT_SECONDS,
            MAX_EXECUTION_TIMEOUT_SECONDS,
            DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        )
        return DEFAULT_EXECUTION_TIMEOUT_SECONDS
    return value


def _bound_error_text(text: str | None) -> str:
    """Bound and redact subprocess error text for safe INFO/ERROR logs."""
    if not text:
        return ""
    cleaned = " ".join(text.split())
    if len(cleaned) > _MAX_ERROR_LOG_CHARS:
        return f"{cleaned[:_MAX_ERROR_LOG_CHARS]}...[truncated]"
    return cleaned


def _decode_pipe_output(value: str | bytes | None) -> str:
    """Normalize subprocess pipe bytes/str (or None) to str."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the agent process group, falling back to the direct child.

    Cursor Agent may spawn children that inherit stdout/stderr. Killing only
    the parent leaves those children holding pipes open, so a later
    ``communicate()`` never sees EOF.
    """
    pid = proc.pid
    if pid is None:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except (PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            return
    try:
        # Keep Popen's internal state consistent when killpg already reaped it.
        proc.kill()
    except ProcessLookupError:
        pass


def _close_process_pipes(proc: subprocess.Popen[str]) -> None:
    """Best-effort close of child stdio pipes after a cleanup timeout."""
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


_CANCEL_POLL_SECONDS = 0.5


def _communicate_with_cancellation(
    proc: subprocess.Popen[str],
    *,
    timeout_seconds: int,
    run_id: str | None,
) -> tuple[str, str, bool]:
    """Wait for subprocess completion; return (stdout, stderr, cancelled)."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout_seconds)
        try:
            stdout, stderr = proc.communicate(timeout=min(_CANCEL_POLL_SECONDS, remaining))
            return stdout or "", stderr or "", False
        except subprocess.TimeoutExpired:
            if run_id is None:
                continue
            from mission_control.run_cancellation import active_execution_registry

            if active_execution_registry.get(run_id) is not proc:
                continue
            # Subprocess still running; loop until deadline or external kill.
            continue

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

DOCUMENTATION_REQUIRED_INSTRUCTIONS = (
    "Review repository documentation affected by the implementation.",
    "Update relevant documentation when behavior, architecture, scope, "
    "workflow, or significant decisions change.",
    "Explicitly report when no documentation update is required and explain "
    "why.",
    "Treat documentation review as part of completion.",
)

# Soft budget before the hard execution-timeout ceiling.
SPLIT_RUN_SOFT_TIMEOUT_SECONDS = 360

RECOMMENDED_SPLIT_PHASES = (
    "implementation (one objective, four files or fewer)",
    "focused testing for that change only",
    "documentation updates",
    "deployment/verification",
)


def _split_run_policy_instructions(timeout_seconds: int) -> tuple[str, ...]:
    return (
        "Pursue exactly one objective per mission.",
        "Target four files or fewer.",
        "Separate implementation, broad testing, documentation, and "
        "deployment/verification into distinct missions.",
        "Never blindly retry a timed-out mission; split remaining work into "
        "smaller follow-up missions instead.",
        (
            f"At {SPLIT_RUN_SOFT_TIMEOUT_SECONDS // 60} minutes of agent work, "
            "treat remaining broad scope as oversized: stop expanding scope and "
            "return a structured split recommendation before the "
            f"{timeout_seconds}-second hard timeout when possible."
        ),
    )


SPLIT_RUN_POLICY_INSTRUCTIONS = _split_run_policy_instructions(
    DEFAULT_EXECUTION_TIMEOUT_SECONDS,
)

# Injected into every agent prompt. Coding agents may edit/test the workspace;
# only Mission Control platform persistence may commit, push, or record SHA.
PLATFORM_PERSISTENCE_OWNERSHIP_INSTRUCTIONS = (
    "Do not commit, push, create pull requests, or mutate remotes; leave "
    "tested changes in the workspace for Mission Control persistence.",
)


def build_timeout_split_guidance(
    *,
    timeout_stage: str = "agent_execution",
    observed_changed_paths: list[str] | None = None,
) -> dict[str, object]:
    """Structured next-step guidance when agent execution times out.

    Observed workspace paths are diagnostic only. Mission Control does not
    persist partial agent work after a timeout.
    """
    return {
        "timeout_stage": timeout_stage,
        "observed_changed_paths": list(observed_changed_paths or []),
        "persistence_not_attempted": True,
        "recommended_phases": list(RECOMMENDED_SPLIT_PHASES),
        "guidance": (
            "Do not claim partial work was persisted. Do not blindly retry "
            "the same mission; submit smaller follow-up missions for the "
            "recommended phases."
        ),
    }


@dataclass
class ExecutionResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    return_code: int | None = None
    # Redacted argv Mission Control actually launched (instruction omitted).
    command: list[str] | None = None
    # Present only on agent hard-timeout; authoritative copy lives on the
    # structured run result after finalize.
    timeout_split_guidance: dict[str, object] | None = None


def _workspace_binding_constraints(mission: dict) -> tuple[str, ...]:
    """Bind agent writes to the concrete isolated workspace path.

    ``repository.path`` is the only filesystem root for edits.
    ``repository.name`` selects which remote to clone; it is not a path.
    """
    repository = mission.get("repository")
    if not isinstance(repository, dict):
        return ()
    workspace_path = repository.get("path")
    if not isinstance(workspace_path, str) or not workspace_path.strip():
        return ()
    path = workspace_path.strip()
    return (
        f"All file writes must stay inside this Mission Control workspace: {path}",
        "Edit ONLY repository-relative paths in that workspace "
        "(for example `mission_control/executor.py`); never absolute paths "
        "such as `/tmp/.../mission_control/executor.py`.",
        "repository.name is clone identity only — do NOT infer a filesystem "
        "path from it, and do NOT create, clone, or edit any other repository "
        "checkout outside this workspace.",
        "For ephemeral inspection or extraction, use `mktemp -d` or absolute "
        "system `/tmp` paths outside the repository. Do not write temporary "
        "files under repository-relative `tmp/`, `.tmp/`, `scratch/`, "
        "`extracted/`, or any `__pycache__` path; platform persistence fails "
        "closed on those paths and never deletes them.",
    )


def build_cursor_instruction(
    mission: dict,
    constraints: tuple[str, ...] = READ_ONLY_CONSTRAINTS,
    *,
    execution_timeout_seconds: int | None = None,
) -> str:
    from mission_control.validator import resolve_documentation_mode

    title = mission.get("title", "")
    instructions = mission.get("instructions", "")
    deliverables = mission.get("deliverables", [])
    timeout_seconds = (
        execution_timeout_seconds
        if execution_timeout_seconds is not None
        else resolve_execution_timeout_seconds()
    )

    lines = [
        f"Mission: {title}",
        "",
        "Constraints:",
    ]

    lines.extend(f"- {constraint}" for constraint in constraints)
    lines.extend(
        f"- {constraint}" for constraint in _workspace_binding_constraints(mission)
    )
    lines.extend(
        f"- {item}" for item in PLATFORM_PERSISTENCE_OWNERSHIP_INSTRUCTIONS
    )

    lines.extend(
        [
            "",
            "Split-run scope policy:",
        ]
    )
    lines.extend(
        f"- {item}" for item in _split_run_policy_instructions(timeout_seconds)
    )

    if resolve_documentation_mode(mission) == "required":
        lines.extend(
            [
                "",
                "Documentation:",
            ]
        )
        lines.extend(
            f"- {item}" for item in DOCUMENTATION_REQUIRED_INSTRUCTIONS
        )

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

    timeout_seconds = resolve_execution_timeout_seconds()
    instruction = build_cursor_instruction(
        mission,
        constraints=constraints,
        execution_timeout_seconds=timeout_seconds,
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
            # New session ⇒ process group id == pid, so timeout cleanup can
            # SIGKILL grandchildren that would otherwise keep stdio pipes open.
            start_new_session=True,
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
    if run_id is not None:
        from mission_control.run_cancellation import active_execution_registry

        active_execution_registry.register(run_id, proc)
    logger.info(
        (
            "lifecycle run_id=%s event=subprocess_wait_start "
            "api_pid=%s child_pid=%s timeout_seconds=%s"
        ),
        run_label,
        os.getpid(),
        child_pid,
        timeout_seconds,
    )

    cancelled = False
    try:
        try:
            stdout, stderr, cancelled = _communicate_with_cancellation(
                proc,
                timeout_seconds=timeout_seconds,
                run_id=run_id,
            )
        except subprocess.TimeoutExpired as timed_out:
            _terminate_process_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=CLEANUP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                # Grandchildren (or a stuck agent) still hold pipes open. Do not
                # block the run worker indefinitely — return with partial output.
                stdout = _decode_pipe_output(timed_out.stdout)
                stderr = _decode_pipe_output(timed_out.stderr)
                _close_process_pipes(proc)
                logger.error(
                    (
                        "lifecycle run_id=%s event=subprocess_cleanup_timeout "
                        "api_pid=%s child_pid=%s cleanup_timeout_seconds=%s"
                    ),
                    run_label,
                    os.getpid(),
                    child_pid,
                    CLEANUP_TIMEOUT_SECONDS,
                )
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
                timeout_seconds,
            )
            logger.error(
                "Cursor mission timed out: mission_id=%s timeout_seconds=%s",
                mission_id,
                timeout_seconds,
            )

            return ExecutionResult(
                ok=False,
                stdout=stdout or "",
                stderr=stderr or "",
                error=(
                    "cursor-agent timed out after "
                    f"{timeout_seconds} seconds"
                ),
                command=command_evidence,
                timeout_split_guidance=build_timeout_split_guidance(
                    timeout_stage="agent_execution",
                ),
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
    finally:
        if run_id is not None:
            from mission_control.run_cancellation import active_execution_registry

            active_execution_registry.unregister(run_id)

    if cancelled:
        return ExecutionResult(
            ok=False,
            stdout=stdout or "",
            stderr=stderr or "",
            error="Run cancelled during agent execution",
            command=command_evidence,
        )

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

    if create_files:
        return CREATE_ONLY_CONSTRAINTS

    # create_files and modify_files both false: read-only execute (inspection)
    # or push-only with no agent file writes.
    return READ_ONLY_CONSTRAINTS


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
