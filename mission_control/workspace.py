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


def persist_workspace_changes(
    run_id: str,
    mission: dict,
    workspace_path: str,
) -> PersistenceResult:
    """Commit and push workspace changes after a successful agent execution."""
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

        execution_result = execute_cursor_agent(isolated_mission)
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
    except Exception as exc:  # pragma: no cover - defensive
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
