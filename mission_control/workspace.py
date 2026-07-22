"""Isolated workspace preparation and Git persistence for asynchronous runs."""

from __future__ import annotations

import base64
import copy
from dataclasses import dataclass
import logging
import os
import shutil
import subprocess
import tempfile

from mission_control.executor import execute_cursor_agent
from mission_control.run_registry import RunRegistry, RunStatus

logger = logging.getLogger(__name__)

DEFAULT_PERSISTENCE_MODE = "none"
SUPPORTED_PERSISTENCE_MODES = frozenset({"none", "commit", "push"})

# Machine-readable gate for privileged platform persistence.mode=push.
# Distinct from agent permissions.push.
PLATFORM_PUSH_APPROVAL_REQUIRED = (
    "PLATFORM_PUSH_APPROVAL_REQUIRED: persistence.mode=push requires "
    "explicit approval.platform_push_approved=true (or the "
    "allow_automatic_platform_push=true policy)"
)


@dataclass(frozen=True)
class WorkspacePrepResult:
    ok: bool
    workspace_path: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class PersistenceResult:
    ok: bool
    commit_sha: str | None = None
    error: str | None = None


def resolve_persistence_mode(mission: dict) -> str:
    """Return the platform persistence mode for ``mission``.

    When the top-level ``persistence`` block is omitted, or when ``mode`` is
    omitted inside that block, the mode defaults to ``none``.
    """
    persistence = mission.get("persistence")
    if not isinstance(persistence, dict):
        return DEFAULT_PERSISTENCE_MODE
    mode = persistence.get("mode", DEFAULT_PERSISTENCE_MODE)
    if mode is None:
        return DEFAULT_PERSISTENCE_MODE
    return str(mode)


def is_platform_push_authorized(mission: dict) -> bool:
    """Return whether platform ``persistence.mode=push`` is authorized.

    Authorization is granted only by:

    - ``approval.platform_push_approved: true`` (explicit per-mission approval)
    - ``approval.allow_automatic_platform_push: true`` (named automatic policy)

    Agent ``permissions.push`` does not authorize platform push.
    """
    approval = mission.get("approval")
    if not isinstance(approval, dict):
        return False
    if approval.get("platform_push_approved") is True:
        return True
    if approval.get("allow_automatic_platform_push") is True:
        return True
    return False


def require_platform_push_approval(mission: dict) -> str | None:
    """Return a machine-readable error when platform push is not approved.

    Modes ``none`` and ``commit`` never require platform-push approval.
    """
    if resolve_persistence_mode(mission) != "push":
        return None
    if is_platform_push_authorized(mission):
        return None
    return PLATFORM_PUSH_APPROVAL_REQUIRED


def _run_git(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        shell=False,
        env=env,
    )


def get_origin_url(repository_path: str) -> str | None:
    """Return the origin remote URL for ``repository_path``, if configured."""
    completed = _run_git(["-C", repository_path, "remote", "get-url", "origin"])
    if completed.returncode != 0:
        return None
    url = completed.stdout.strip()
    return url or None


def configure_workspace_origin(
    workspace_path: str,
    origin_url: str,
) -> subprocess.CompletedProcess[str]:
    """Point the isolated workspace's origin remote at ``origin_url``."""
    return _run_git(["-C", workspace_path, "remote", "set-url", "origin", origin_url])


def prepare_isolated_workspace(mission: dict) -> WorkspacePrepResult:
    """Clone the configured repository into a temporary isolated workspace."""
    repository = mission["repository"]
    base_branch = repository["base_branch"]
    repository_url = os.environ.get("MISSION_CONTROL_REPOSITORY_URL", "").strip()

    if not repository_url:
        return WorkspacePrepResult(
            ok=False,
            error=(
                "MISSION_CONTROL_REPOSITORY_URL is not configured. "
                "Set it to the Git clone URL for the repository."
            ),
        )

    workspace_path = tempfile.mkdtemp(prefix="mission-control-run-")

    clone = _run_git(
        [
            "clone",
            "--branch",
            base_branch,
            "--single-branch",
            repository_url,
            workspace_path,
        ]
    )
    if clone.returncode != 0:
        _safe_cleanup(workspace_path)
        message = clone.stderr.strip() or clone.stdout.strip()
        if not message:
            message = f"git clone failed with code {clone.returncode}"
        return WorkspacePrepResult(ok=False, error=message)

    return WorkspacePrepResult(ok=True, workspace_path=workspace_path)


def _git_status_porcelain(workspace_path: str) -> subprocess.CompletedProcess[str]:
    return _run_git(["-C", workspace_path, "status", "--porcelain"])



def configure_git_identity(workspace_path: str) -> str | None:
    """Configure the repository-local Git author identity."""
    name = os.environ.get("MISSION_CONTROL_GIT_NAME", "").strip()
    email = os.environ.get("MISSION_CONTROL_GIT_EMAIL", "").strip()

    if not name:
        return "MISSION_CONTROL_GIT_NAME is not configured."

    if not email:
        return "MISSION_CONTROL_GIT_EMAIL is not configured."

    for key, value in (("user.name", name), ("user.email", email)):
        result = _run_git(
            [
                "-C",
                workspace_path,
                "config",
                key,
                value,
            ]
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip()
            if not message:
                message = f"git config {key} failed with code {result.returncode}"
            return message

    return None

def _github_push_environment() -> tuple[dict[str, str] | None, str | None]:
    """Return a Git environment containing GitHub HTTPS authentication."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        return None, (
            "GITHUB_TOKEN is not configured. Set a GitHub token with "
            "read/write access to the repository."
        )

    credentials = base64.b64encode(
        f"x-access-token:{token}".encode("utf-8")
    ).decode("ascii")

    env = os.environ.copy()
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
    env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {credentials}"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env, None


def _read_head_commit_sha(workspace_path: str) -> PersistenceResult:
    rev_parse = _run_git(["-C", workspace_path, "rev-parse", "HEAD"])
    if rev_parse.returncode != 0:
        message = rev_parse.stderr.strip() or rev_parse.stdout.strip()
        if not message:
            message = f"git rev-parse failed with code {rev_parse.returncode}"
        return PersistenceResult(ok=False, error=message)

    commit_sha = rev_parse.stdout.strip()
    if not commit_sha:
        return PersistenceResult(
            ok=False,
            error="git rev-parse returned an empty commit SHA",
        )

    return PersistenceResult(ok=True, commit_sha=commit_sha)


def persist_workspace_changes(
    run_id: str,
    mission: dict,
    workspace_path: str,
) -> PersistenceResult:
    """Apply platform Git persistence according to ``persistence.mode``.

    Modes:

    - ``none``: do not stage, commit, or push
    - ``commit``: stage and create a local commit, but do not push
    - ``push``: stage, commit, and push to the mission base branch
      (requires explicit platform-push approval; see
      ``require_platform_push_approval``)

    Agent ``permissions.commit`` / ``permissions.push`` are separate and do not
    control this platform persistence path. Approval is enforced again here so
    a run cannot bypass the gate merely because earlier validation succeeded.
    """
    mode = resolve_persistence_mode(mission)
    if mode not in SUPPORTED_PERSISTENCE_MODES:
        return PersistenceResult(
            ok=False,
            error=(
                f"Unsupported persistence.mode: {mode} "
                "(expected one of: none, commit, push)"
            ),
        )

    if mode == "none":
        return PersistenceResult(ok=True, commit_sha=None)

    if mode == "push":
        approval_error = require_platform_push_approval(mission)
        if approval_error is not None:
            return PersistenceResult(ok=False, error=approval_error)

    status = _git_status_porcelain(workspace_path)
    if status.returncode != 0:
        message = status.stderr.strip() or status.stdout.strip()
        if not message:
            message = f"git status failed with code {status.returncode}"
        return PersistenceResult(ok=False, error=message)

    if not status.stdout.strip():
        return PersistenceResult(ok=True, commit_sha=None)

    add = _run_git(["-C", workspace_path, "add", "-A"])
    if add.returncode != 0:
        message = add.stderr.strip() or add.stdout.strip()
        if not message:
            message = f"git add failed with code {add.returncode}"
        return PersistenceResult(ok=False, error=message)

    identity_error = configure_git_identity(workspace_path)
    if identity_error is not None:
        return PersistenceResult(ok=False, error=identity_error)

    commit = _run_git(
        [
            "-C",
            workspace_path,
            "commit",
            "-m",
            f"Mission Control run {run_id}",
        ]
    )
    if commit.returncode != 0:
        message = commit.stderr.strip() or commit.stdout.strip()
        if not message:
            message = f"git commit failed with code {commit.returncode}"
        return PersistenceResult(ok=False, error=message)

    if mode == "commit":
        return _read_head_commit_sha(workspace_path)

    push_env, push_auth_error = _github_push_environment()
    if push_auth_error is not None:
        return PersistenceResult(ok=False, error=push_auth_error)

    base_branch = mission["repository"]["base_branch"]
    push = _run_git(
        [
            "-C",
            workspace_path,
            "push",
            "origin",
            f"HEAD:{base_branch}",
        ],
        env=push_env,
    )
    if push.returncode != 0:
        message = push.stderr.strip() or push.stdout.strip()
        if not message:
            message = f"git push failed with code {push.returncode}"
        return PersistenceResult(ok=False, error=message)

    return _read_head_commit_sha(workspace_path)


def cleanup_workspace(workspace_path: str) -> None:
    """Remove a temporary workspace directory."""
    shutil.rmtree(workspace_path)


def _safe_cleanup(workspace_path: str) -> None:
    try:
        cleanup_workspace(workspace_path)
    except Exception:
        logger.exception(
            "Failed to cleanup workspace during preparation: workspace=%s",
            workspace_path,
        )


def _execution_run_status(ok: bool, error: str | None) -> RunStatus:
    if ok:
        return RunStatus.COMPLETED
    if error is not None and "timed out" in error:
        return RunStatus.TIMED_OUT
    return RunStatus.FAILED


def execute_registered_run(
    run_id: str,
    mission: dict,
    registry: RunRegistry,
) -> None:
    """Run a registered mission in an isolated workspace and persist changes."""
    count, keys = registry.diagnostic_state()
    logger.info(
        (
            "lifecycle run_id=%s event=registered_run_entered "
            "api_pid=%s registry_id=%s registry_count=%s registry_keys=%s"
        ),
        run_id,
        os.getpid(),
        id(registry),
        count,
        keys,
    )
    registry.update_status(run_id, RunStatus.RUNNING)
    workspace_path: str | None = None

    try:
        prep = prepare_isolated_workspace(mission)
        if not prep.ok:
            registry.store_result(run_id, error=prep.error)
            registry.update_status(run_id, RunStatus.FAILED)
            return

        workspace_path = prep.workspace_path
        assert workspace_path is not None

        isolated_mission = copy.deepcopy(mission)
        isolated_mission["repository"] = {
            **mission["repository"],
            "path": workspace_path,
        }

        execution_result = execute_cursor_agent(
            isolated_mission,
            run_id=run_id,
        )
        if not execution_result.ok:
            registry.store_result(
                run_id,
                stdout=execution_result.stdout,
                stderr=execution_result.stderr,
                error=execution_result.error,
                return_code=execution_result.return_code,
            )
            registry.update_status(
                run_id,
                _execution_run_status(
                    execution_result.ok,
                    execution_result.error,
                ),
            )
            return

        persistence_result = persist_workspace_changes(
            run_id,
            mission,
            workspace_path,
        )
        if not persistence_result.ok:
            registry.store_result(
                run_id,
                stdout=execution_result.stdout,
                stderr=execution_result.stderr,
                error=persistence_result.error,
                return_code=execution_result.return_code,
            )
            registry.update_status(run_id, RunStatus.FAILED)
            return

        registry.store_result(
            run_id,
            stdout=execution_result.stdout,
            stderr=execution_result.stderr,
            return_code=execution_result.return_code,
            commit_sha=persistence_result.commit_sha,
        )
        registry.update_status(run_id, RunStatus.COMPLETED)
    except Exception as exc:
        logger.exception(
            (
                "lifecycle run_id=%s event=exception "
                "api_pid=%s registry_id=%s stage=registered_run"
            ),
            run_id,
            os.getpid(),
            id(registry),
        )
        registry.store_result(run_id, error=str(exc))
        registry.update_status(run_id, RunStatus.FAILED)
    finally:
        if workspace_path is not None:
            try:
                cleanup_workspace(workspace_path)
            except Exception:
                logger.exception(
                    "Failed to cleanup workspace: run_id=%s workspace=%s",
                    run_id,
                    workspace_path,
                )
