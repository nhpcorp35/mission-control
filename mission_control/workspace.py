"""Isolated workspace preparation and Git persistence for asynchronous runs."""

from __future__ import annotations

import base64
import copy
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from mission_control.executor import execute_cursor_agent
from mission_control.run_registry import RunRegistry, RunStatus
from mission_control.run_result import (
    DeliverableEvidence,
    PersistenceEvidence,
    WARNING_DELIVERABLES_NOT_CHECKED,
    WARNING_FILES_CHANGED_UNAVAILABLE,
    WARNING_PERSISTENCE_NOT_ATTEMPTED,
    WARNING_PREP_FAILED,
    append_warning,
    build_documentation_evidence,
    command_evidence_from_execution,
    empty_structured_result,
    finalize_structured_summary,
    parse_git_status_porcelain_paths,
)

logger = logging.getLogger(__name__)

DEFAULT_PERSISTENCE_MODE = "none"
SUPPORTED_PERSISTENCE_MODES = frozenset({"none", "commit", "push"})

# Basename with a short alphanumeric extension (e.g. README.md, app.py).
_FILE_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9]{1,16}$")

# Machine-readable gate for privileged platform persistence.mode=push.
# Distinct from agent permissions.push.
PLATFORM_PUSH_APPROVAL_REQUIRED = (
    "PLATFORM_PUSH_APPROVAL_REQUIRED: persistence.mode=push requires "
    "explicit approval.platform_push_approved=true (or the "
    "allow_automatic_platform_push=true policy)"
)

# Optional JSON object mapping repository.name → clone URL.
REPOSITORY_URL_MAP_ENV = "MISSION_CONTROL_REPOSITORY_URL_MAP"
# Optional override when repository.name selects Mission Control itself.
SELF_REPOSITORY_URL_ENV = "MISSION_CONTROL_SELF_REPOSITORY_URL"
# Optional override when repository.name selects Legal AI.
LEGAL_AI_REPOSITORY_URL_ENV = "MISSION_CONTROL_LEGAL_AI_REPOSITORY_URL"
DEFAULT_MISSION_CONTROL_CLONE_URL = (
    "https://github.com/nhpcorp35/mission-control.git"
)
DEFAULT_LEGAL_AI_CLONE_URL = "https://github.com/nhpcorp35/legal-ai.git"

# Names that must clone Mission Control rather than the legacy single-repo
# MISSION_CONTROL_REPOSITORY_URL.
_MISSION_CONTROL_REPOSITORY_NAMES = frozenset(
    {
        "mission-control",
        "nhpcorp35/mission-control",
    }
)

# Explicit Legal AI identities — never fall back to Mission Control's clone URL.
_LEGAL_AI_REPOSITORY_NAMES = frozenset(
    {
        "legal-ai",
        "nhpcorp35/legal-ai",
    }
)

# Nested checkout directory agents previously used inside Mission Control.
# Changes under this path must not legitimize wrong-repo persistence.
NESTED_LEGALAI_WORK_DIR = ".legalai_work"

REPOSITORY_ORIGIN_MISMATCH_PREFIX = "REPOSITORY_ORIGIN_MISMATCH:"
NESTED_WORKSPACE_CONTAMINATION_PREFIX = "NESTED_WORKSPACE_CONTAMINATION:"
PERSISTENCE_TEMP_PATH_BLOCKED_PREFIX = "PERSISTENCE_TEMP_PATH_BLOCKED:"

# Top-level repository directories that must not be platform-persisted.
# System absolute /tmp outside the checkout is unrelated and unaffected.
_PERSISTENCE_BLOCKED_TOP_LEVEL_DIRS = frozenset(
    {
        "tmp",
        ".tmp",
        "scratch",
        "extracted",
    }
)

# Exact repository-relative paths that may override the temp-path guard.
# Keep empty by default; add only deliberate, narrow exceptions.
PERSISTENCE_TEMP_PATH_ALLOWLIST: frozenset[str] = frozenset()


@dataclass(frozen=True)
class WorkspacePrepResult:
    ok: bool
    workspace_path: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class PersistenceResult:
    """Outcome of platform Git persistence for one run.

    ``mode`` is the persistence level applied by this call (from the validated
    mission configuration and the actions actually performed). When omitted
    (``None``), structured reporting falls back to
    ``resolve_persistence_mode(mission)`` so callers never silently treat a
    missing execution-result mode as ``none`` while a push/commit succeeded.

    ``pushed`` is True only after a successful ``git push``, False when push
    did not succeed, and None when unknown (legacy/partial results).
    """

    ok: bool
    commit_sha: str | None = None
    error: str | None = None
    mode: str | None = None
    pushed: bool | None = None


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


def _repository_name(mission: dict) -> str:
    repository = mission.get("repository")
    if not isinstance(repository, dict):
        return ""
    name = repository.get("name")
    if not isinstance(name, str):
        return ""
    return name.strip()


def _is_mission_control_repository_name(name: str) -> bool:
    """Return whether ``name`` selects the Mission Control GitHub repository."""
    if not name:
        return False
    return name.casefold() in {
        alias.casefold() for alias in _MISSION_CONTROL_REPOSITORY_NAMES
    }


def _is_legal_ai_repository_name(name: str) -> bool:
    """Return whether ``name`` selects the Legal AI GitHub repository."""
    if not name:
        return False
    return name.casefold() in {
        alias.casefold() for alias in _LEGAL_AI_REPOSITORY_NAMES
    }


def _looks_like_github_owner_repo(name: str) -> bool:
    """Return whether ``name`` looks like an explicit ``owner/repo`` identity."""
    if not name or " " in name or name.count("/") != 1:
        return False
    owner, repo = name.split("/", 1)
    if not owner or not repo:
        return False
    if owner.startswith(".") or repo.startswith("."):
        return False
    return True


def normalize_remote_url_identity(url: str) -> str:
    """Normalize a Git remote URL for origin/target comparison.

    HTTPS, SSH, and local/file remotes compare as the same repository when they
    refer to the same host/owner/repo (case-insensitive) or the same realpath.
    """
    raw = (url or "").strip()
    if not raw:
        return ""

    # Local / file remotes: compare canonical paths.
    if raw.startswith("file://"):
        path = raw[len("file://") :]
        if path.startswith("//"):
            # file://localhost/path or file:///path
            without_host = path.split("/", 2)
            if len(without_host) == 3:
                path = "/" + without_host[2]
            else:
                path = path.lstrip("/")
        return os.path.realpath(path)
    if "://" not in raw and not raw.startswith("git@"):
        return os.path.realpath(raw)

    normalized = raw
    if normalized.startswith("git@"):
        # git@host:owner/repo.git → host/owner/repo
        try:
            host_and_path = normalized[len("git@") :]
            host, path = host_and_path.split(":", 1)
            normalized = f"{host}/{path}"
        except ValueError:
            normalized = normalized[len("git@") :]
    else:
        # https://host/owner/repo.git (optionally with userinfo)
        without_scheme = normalized.split("://", 1)[1]
        if "@" in without_scheme.split("/", 1)[0]:
            without_scheme = without_scheme.split("@", 1)[1]
        normalized = without_scheme

    normalized = normalized.rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[: -len(".git")]
    return normalized.casefold()


def verify_workspace_origin_matches_mission(
    mission: dict,
    workspace_path: str,
) -> str | None:
    """Return an error when workspace origin is not the mission target remote.

    Fail closed before mutation/persistence so Mission Control never reports
    persistence success against a different repository than ``repository.name``.
    """
    expected_url, url_error = resolve_mission_clone_url(mission)
    if url_error is not None or not expected_url:
        return url_error or (
            f"{REPOSITORY_ORIGIN_MISMATCH_PREFIX} cannot resolve expected "
            "clone URL for repository.name"
        )

    actual_url = get_origin_url(workspace_path)
    if not actual_url:
        name = _repository_name(mission) or "<unknown>"
        return (
            f"{REPOSITORY_ORIGIN_MISMATCH_PREFIX} workspace origin is missing "
            f"for repository.name={name!r}; expected {expected_url}"
        )

    expected_id = normalize_remote_url_identity(expected_url)
    actual_id = normalize_remote_url_identity(actual_url)
    if expected_id and actual_id and expected_id == actual_id:
        return None

    name = _repository_name(mission) or "<unknown>"
    return (
        f"{REPOSITORY_ORIGIN_MISMATCH_PREFIX} repository.name={name!r} "
        f"expected origin {expected_url} but workspace origin is {actual_url}"
    )


def resolve_agent_workspace_path(
    checkout_root: str,
    repository_path: str | None,
) -> tuple[str | None, str | None]:
    """Resolve the agent ``--workspace`` directory inside ``checkout_root``.

    ``repository.name`` selects which repository to clone. ``repository.path``
    may select a subdirectory only inside that checkout; ``'.'`` (and absolute
    submit-time host paths used for validation) mean the repository root.
    """
    root = os.path.realpath(checkout_root)
    if not repository_path or not str(repository_path).strip():
        return root, None

    requested = str(repository_path).strip()
    if requested in {".", "./"}:
        return root, None

    # Absolute paths are submit-time validation roots on the API host, not
    # clone-relative locations. Isolated runs bind the agent to checkout root.
    if os.path.isabs(requested) or requested.startswith("~"):
        return root, None

    resolved = resolve_safe_workspace_path(root, requested)
    if resolved is None:
        return None, (
            "repository.path must be '.' or a relative subdirectory inside "
            f"the selected repository checkout (got {requested!r})"
        )
    if not resolved.is_dir():
        return None, (
            "repository.path subdirectory does not exist inside checkout: "
            f"{requested}"
        )
    return str(resolved), None


def nested_workspace_contamination_error(paths: list[str]) -> str | None:
    """Reject ``.legalai_work`` nesting that must not satisfy deliverables."""
    for raw in paths:
        if not isinstance(raw, str) or not raw:
            continue
        parts = Path(raw).parts
        if NESTED_LEGALAI_WORK_DIR in parts:
            return (
                f"{NESTED_WORKSPACE_CONTAMINATION_PREFIX} path {raw!r} is under "
                f"{NESTED_LEGALAI_WORK_DIR}/ and cannot legitimize persistence "
                "for the selected repository"
            )
    return None


def normalize_repository_relative_path(path: str) -> str:
    """Normalize a repo-relative path for guard comparisons (POSIX-style)."""
    text = str(path).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def is_blocked_persistence_temp_path(
    relative_path: str,
    *,
    allowlist: frozenset[str] | None = None,
) -> bool:
    """Return whether a repo-relative path is blocked from platform persistence.

    Fail closed for:

    - paths under top-level ``tmp/``, ``.tmp/``, ``scratch/``, or ``extracted/``
    - paths containing a ``__pycache__`` segment

    Exact allowlisted relative paths are exempt. Absolute system ``/tmp`` paths
    are outside this check (Git status reports repository-relative paths only).
    """
    if not isinstance(relative_path, str) or not relative_path.strip():
        return False
    # Absolute / home paths are not repository-relative persistence inputs.
    if relative_path.startswith(("/", "~")):
        return False
    if (
        len(relative_path) >= 3
        and relative_path[1] == ":"
        and relative_path[0].isalpha()
        and relative_path[2] in "/\\"
    ):
        return False
    normalized = normalize_repository_relative_path(relative_path)
    normalized = normalized.strip("/")
    if not normalized:
        return False
    effective_allowlist = (
        PERSISTENCE_TEMP_PATH_ALLOWLIST if allowlist is None else allowlist
    )
    if normalized in effective_allowlist:
        return False
    parts = tuple(part for part in normalized.split("/") if part and part != ".")
    if not parts:
        return False
    if parts[0] in _PERSISTENCE_BLOCKED_TOP_LEVEL_DIRS:
        return True
    if "__pycache__" in parts:
        return True
    return False


def persistence_temp_path_guard_error(
    paths: list[str],
    *,
    allowlist: frozenset[str] | None = None,
) -> str | None:
    """Return a fail-closed error when blocked temp paths would be persisted.

    Reports every blocked relative path. Never deletes files.
    """
    blocked: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        if not isinstance(raw, str) or not raw:
            continue
        if not is_blocked_persistence_temp_path(raw, allowlist=allowlist):
            continue
        normalized = normalize_repository_relative_path(raw).strip("/")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        blocked.append(normalized)
    if not blocked:
        return None
    blocked_display = ", ".join(blocked)
    return (
        f"{PERSISTENCE_TEMP_PATH_BLOCKED_PREFIX} blocked relative paths: "
        f"{blocked_display}. Use mktemp -d or absolute system /tmp outside the "
        "repository for inspection/extraction; platform persistence does not "
        "delete blocked paths."
    )


def resolve_mission_clone_url(mission: dict) -> tuple[str | None, str | None]:
    """Return ``(clone_url, error)`` for the mission's isolated workspace.

    Persistence inspects the clone prepared here. If that clone is a different
    repository than the one identified by ``repository.name``, the coding agent
    may modify the intended repo while platform persistence pushes elsewhere.
    Resolve the clone URL from ``repository.name`` (and optional URL map) so
    agent and persistence share one checkout of the selected remote.

    Explicit Legal AI / ``owner/repo`` names never fall back silently to
    Mission Control. Missions that omit optional fields still default to
    Mission Control via the structured builder's ``Mission-Control`` name.
    """
    name = _repository_name(mission)
    name_key = name.casefold()

    raw_map = os.environ.get(REPOSITORY_URL_MAP_ENV, "").strip()
    if raw_map:
        try:
            mapping = json.loads(raw_map)
        except json.JSONDecodeError:
            return None, f"{REPOSITORY_URL_MAP_ENV} must be a JSON object"
        if not isinstance(mapping, dict):
            return None, f"{REPOSITORY_URL_MAP_ENV} must be a JSON object"
        for key in (name, name_key):
            if not key:
                continue
            value = mapping.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), None
        for map_key, value in mapping.items():
            if (
                isinstance(map_key, str)
                and map_key.casefold() == name_key
                and isinstance(value, str)
                and value.strip()
            ):
                return value.strip(), None

    if _is_mission_control_repository_name(name):
        self_url = os.environ.get(SELF_REPOSITORY_URL_ENV, "").strip()
        return (self_url or DEFAULT_MISSION_CONTROL_CLONE_URL), None

    if _is_legal_ai_repository_name(name):
        legal_url = os.environ.get(LEGAL_AI_REPOSITORY_URL_ENV, "").strip()
        return (legal_url or DEFAULT_LEGAL_AI_CLONE_URL), None

    # Explicit owner/repo clone identity: do not reuse the legacy single-repo
    # MISSION_CONTROL_REPOSITORY_URL (often Mission Control or Legal AI).
    if _looks_like_github_owner_repo(name):
        return f"https://github.com/{name}.git", None

    repository_url = os.environ.get("MISSION_CONTROL_REPOSITORY_URL", "").strip()
    if not repository_url:
        if name:
            return None, (
                f"Cannot resolve clone URL for repository.name={name!r}. "
                f"Set {REPOSITORY_URL_MAP_ENV} or MISSION_CONTROL_REPOSITORY_URL."
            )
        return None, (
            "MISSION_CONTROL_REPOSITORY_URL is not configured. "
            "Set it to the Git clone URL for the repository."
        )
    return repository_url, None


def prepare_isolated_workspace(mission: dict) -> WorkspacePrepResult:
    """Clone the mission's target repository into a temporary workspace."""
    repository = mission["repository"]
    base_branch = repository["base_branch"]
    repo_name = _repository_name(mission) or "<unknown>"
    repository_url, url_error = resolve_mission_clone_url(mission)

    if url_error is not None or not repository_url:
        return WorkspacePrepResult(
            ok=False,
            error=url_error
            or (
                "MISSION_CONTROL_REPOSITORY_URL is not configured. "
                "Set it to the Git clone URL for the repository."
            ),
        )

    workspace_path = tempfile.mkdtemp(prefix="mission-control-run-")

    # Prefer GitHub HTTPS auth when available so private allowed repositories
    # can clone; missing token is fine for local/file remotes in tests.
    clone_env, _auth_error = _github_push_environment()
    clone = _run_git(
        [
            "clone",
            "--branch",
            str(base_branch),
            "--single-branch",
            repository_url,
            workspace_path,
        ],
        env=clone_env,
    )
    if clone.returncode != 0:
        _safe_cleanup(workspace_path)
        message = clone.stderr.strip() or clone.stdout.strip()
        if not message:
            message = f"git clone failed with code {clone.returncode}"
        return WorkspacePrepResult(
            ok=False,
            error=(
                f"Failed to clone repository.name={repo_name!r} "
                f"at ref {base_branch!r} from {repository_url}: {message}"
            ),
        )

    # Canonicalize so agent --workspace / cwd and persistence git -C agree
    # even when /tmp (or the mkdtemp path) involves symlinks.
    real_workspace = os.path.realpath(workspace_path)
    mismatch = verify_workspace_origin_matches_mission(mission, real_workspace)
    if mismatch is not None:
        _safe_cleanup(real_workspace)
        return WorkspacePrepResult(ok=False, error=mismatch)

    return WorkspacePrepResult(
        ok=True,
        workspace_path=real_workspace,
    )


def prepare_ephemeral_checkout(
    *,
    repository_url: str,
    ref: str,
) -> WorkspacePrepResult:
    """Clone ``repository_url`` and check out ``ref`` in a temporary workspace.

    Used by the repository command runner. Does not stage, commit, or push.
    Authentication reuses the same GitHub HTTPS extraheader environment as
    platform push when ``GITHUB_TOKEN`` is configured.
    """
    if not isinstance(repository_url, str) or not repository_url.strip():
        return WorkspacePrepResult(
            ok=False,
            error="repository_url is required for ephemeral checkout",
        )
    if not isinstance(ref, str) or not ref.strip():
        return WorkspacePrepResult(
            ok=False,
            error="ref is required for ephemeral checkout",
        )

    workspace_path = tempfile.mkdtemp(prefix="mission-control-cmd-")
    clone_env, _auth_error = _github_push_environment()
    # Missing GITHUB_TOKEN is fine for local/file remotes used in tests.
    clone = _run_git(
        [
            "clone",
            repository_url.strip(),
            workspace_path,
        ],
        env=clone_env,
    )
    if clone.returncode != 0:
        _safe_cleanup(workspace_path)
        message = clone.stderr.strip() or clone.stdout.strip()
        if not message:
            message = f"git clone failed with code {clone.returncode}"
        return WorkspacePrepResult(ok=False, error=message)

    checkout = _run_git(
        [
            "-C",
            workspace_path,
            "checkout",
            "--detach",
            ref.strip(),
        ],
        env=clone_env,
    )
    if checkout.returncode != 0:
        _safe_cleanup(workspace_path)
        message = checkout.stderr.strip() or checkout.stdout.strip()
        if not message:
            message = f"git checkout failed with code {checkout.returncode}"
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


def _read_head_commit_sha(
    workspace_path: str,
    *,
    mode: str,
    pushed: bool = False,
) -> PersistenceResult:
    rev_parse = _run_git(["-C", workspace_path, "rev-parse", "HEAD"])
    if rev_parse.returncode != 0:
        message = rev_parse.stderr.strip() or rev_parse.stdout.strip()
        if not message:
            message = f"git rev-parse failed with code {rev_parse.returncode}"
        return PersistenceResult(
            ok=False,
            error=message,
            mode=mode,
            pushed=False,
        )

    commit_sha = rev_parse.stdout.strip()
    if not commit_sha:
        return PersistenceResult(
            ok=False,
            error="git rev-parse returned an empty commit SHA",
            mode=mode,
            pushed=False,
        )

    return PersistenceResult(
        ok=True,
        commit_sha=commit_sha,
        mode=mode,
        pushed=pushed,
    )


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

    Agent ``permissions.commit`` / ``permissions.push`` / ``permissions.stage_changes``
    are legacy agent-facing fields only. They do not control this platform
    persistence path; ``persistence.mode`` is authoritative. Approval is
    enforced again here so a run cannot bypass the gate merely because earlier
    validation succeeded.
    """
    mode = resolve_persistence_mode(mission)
    if mode not in SUPPORTED_PERSISTENCE_MODES:
        return PersistenceResult(
            ok=False,
            error=(
                f"Unsupported persistence.mode: {mode} "
                "(expected one of: none, commit, push)"
            ),
            mode=mode,
            pushed=False,
        )

    if mode == "none":
        return PersistenceResult(
            ok=True,
            commit_sha=None,
            mode="none",
            pushed=False,
        )

    if mode == "push":
        approval_error = require_platform_push_approval(mission)
        if approval_error is not None:
            return PersistenceResult(
                ok=False,
                error=approval_error,
                mode="push",
                pushed=False,
            )

    mismatch = verify_workspace_origin_matches_mission(mission, workspace_path)
    if mismatch is not None:
        return PersistenceResult(
            ok=False,
            error=mismatch,
            mode=mode,
            pushed=False,
        )

    status = _git_status_porcelain(workspace_path)
    if status.returncode != 0:
        message = status.stderr.strip() or status.stdout.strip()
        if not message:
            message = f"git status failed with code {status.returncode}"
        return PersistenceResult(
            ok=False,
            error=message,
            mode=mode,
            pushed=False,
        )

    if not status.stdout.strip():
        return PersistenceResult(
            ok=True,
            commit_sha=None,
            mode=mode,
            pushed=False,
        )

    changed_paths = parse_git_status_porcelain_paths(status.stdout)
    temp_path_error = persistence_temp_path_guard_error(changed_paths)
    if temp_path_error is not None:
        return PersistenceResult(
            ok=False,
            error=temp_path_error,
            mode=mode,
            pushed=False,
        )

    add = _run_git(["-C", workspace_path, "add", "-A"])
    if add.returncode != 0:
        message = add.stderr.strip() or add.stdout.strip()
        if not message:
            message = f"git add failed with code {add.returncode}"
        return PersistenceResult(
            ok=False,
            error=message,
            mode=mode,
            pushed=False,
        )

    identity_error = configure_git_identity(workspace_path)
    if identity_error is not None:
        return PersistenceResult(
            ok=False,
            error=identity_error,
            mode=mode,
            pushed=False,
        )

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
        return PersistenceResult(
            ok=False,
            error=message,
            mode=mode,
            pushed=False,
        )

    if mode == "commit":
        return _read_head_commit_sha(
            workspace_path,
            mode="commit",
            pushed=False,
        )

    push_env, push_auth_error = _github_push_environment()
    if push_auth_error is not None:
        sha_result = _read_head_commit_sha(
            workspace_path,
            mode="push",
            pushed=False,
        )
        return PersistenceResult(
            ok=False,
            error=push_auth_error,
            mode="push",
            pushed=False,
            commit_sha=sha_result.commit_sha if sha_result.ok else None,
        )

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
        sha_result = _read_head_commit_sha(
            workspace_path,
            mode="push",
            pushed=False,
        )
        return PersistenceResult(
            ok=False,
            error=message,
            mode="push",
            pushed=False,
            commit_sha=sha_result.commit_sha if sha_result.ok else None,
        )

    return _read_head_commit_sha(
        workspace_path,
        mode="push",
        pushed=True,
    )


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


def looks_like_file_path_deliverable(deliverable: str) -> bool:
    """Return whether a bare-string deliverable clearly resembles a path.

    Compatibility rule for untyped string deliverables (documented contract):

    - Non-empty string without NUL bytes.
    - Absolute forms (``/…``, ``~/…``, Windows drive paths) are classified as
      path-like so they can be rejected from workspace checks rather than
      mistaken for descriptive text.
    - Otherwise treated as path-like when either:

      - the final path segment has a short alphanumeric file extension
        (``.[A-Za-z0-9]{1,16}``), e.g. ``MISSION_SPEC.md``, ``docs/out.txt``,
        or
      - it contains a ``/`` separator **and** contains no whitespace.

    Descriptive phrases — including ``summary``, ``report``,
    ``repository status``, and slash-containing prose such as
    ``API/OpenAPI documentation updates`` — are **not** path-like and are not
    verified on disk. A lone ``/`` inside free text is not enough.

    Prefer explicit typed entries (``file:`` / ``description:``) for new
    missions; see :func:`file_path_from_deliverable`.
    """
    if not isinstance(deliverable, str) or not deliverable:
        return False
    if "\x00" in deliverable:
        return False

    if deliverable.startswith(("/", "~")):
        return True
    if (
        len(deliverable) >= 3
        and deliverable[1] == ":"
        and deliverable[0].isalpha()
        and deliverable[2] in "/\\"
    ):
        return True
    if Path(deliverable).is_absolute():
        return True

    basename = deliverable.rsplit("/", 1)[-1]
    if basename in ("", ".", ".."):
        return False
    if _FILE_EXTENSION_RE.search(basename):
        return True

    if "/" in deliverable and not any(ch.isspace() for ch in deliverable):
        return True
    return False


def file_path_from_deliverable(item: object) -> str | None:
    """Return the filesystem path to verify for ``item``, or ``None``.

    Supported shapes:

    - **Typed file** mapping: ``{file: <path>}`` or
      ``{kind: file, path: <path>}`` — always treated as a file deliverable
      (subject to workspace safety resolution).
    - **Typed descriptive** mapping: ``{description: <text>}`` or
      ``{kind: descriptive|description, ...}`` — never checked on disk.
    - **Bare string** — checked only when
      :func:`looks_like_file_path_deliverable` is true.

    Unknown shapes are skipped (not treated as paths).
    """
    if isinstance(item, str):
        if looks_like_file_path_deliverable(item):
            return item
        return None

    if not isinstance(item, dict):
        return None

    kind_raw = item.get("kind")
    kind = str(kind_raw).strip().lower() if kind_raw is not None else ""

    if kind in {"descriptive", "description"}:
        return None
    if "description" in item and kind != "file" and "file" not in item:
        return None

    if kind == "file":
        path = item.get("path", item.get("file"))
        if isinstance(path, str) and path and "\x00" not in path:
            return path
        return None

    file_value = item.get("file")
    if isinstance(file_value, str) and file_value and "\x00" not in file_value:
        return file_value

    return None


def resolve_safe_workspace_path(
    workspace_path: str,
    relative_path: str,
) -> Path | None:
    """Resolve ``relative_path`` under ``workspace_path``, or ``None``.

    Returns ``None`` for absolute paths, home paths, NUL, or ``..`` escapes.
    Does not apply the bare-string path heuristic; callers decide whether a
    deliverable is a file path first.
    """
    if not isinstance(relative_path, str) or not relative_path:
        return None
    if "\x00" in relative_path:
        return None
    if relative_path.startswith(("/", "~")):
        return None
    if (
        len(relative_path) >= 3
        and relative_path[1] == ":"
        and relative_path[0].isalpha()
        and relative_path[2] in "/\\"
    ):
        return None

    candidate = Path(relative_path)
    if candidate.is_absolute():
        return None

    workspace = Path(workspace_path).resolve()
    resolved = (workspace / candidate).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError:
        return None
    return resolved


def resolve_safe_workspace_deliverable(
    workspace_path: str,
    deliverable: str,
) -> Path | None:
    """Resolve a bare-string deliverable under ``workspace_path``, or None.

    Returns ``None`` when the deliverable is not path-like or is not a safe
    relative path inside the workspace. Callers must treat ``None`` as “do not
    perform a filesystem check” so Mission Control never reads outside the
    isolated run workspace.
    """
    if not looks_like_file_path_deliverable(deliverable):
        return None
    return resolve_safe_workspace_path(workspace_path, deliverable)


def verify_declared_file_deliverables(
    mission: dict,
    workspace_path: str,
) -> str | None:
    """Verify declared file deliverables exist as regular files.

    Runs after successful agent execution and before platform persistence.
    Only file deliverables (explicit ``file:`` / ``kind: file`` entries, or
    bare strings that pass :func:`looks_like_file_path_deliverable`) are
    considered. Paths that resolve safely inside the workspace must exist as
    regular files. Absolute, home (``~``), or ``..``-escaping paths are
    recorded without reading outside the workspace and fail closed:

    ``Declared file deliverable outside workspace: <path>``

    Missing in-workspace files produce:

    ``Missing declared file deliverable: <path>``

    Empty deliverable lists, non-list values, and descriptive (non-file)
    items do not fail this gate.
    """
    evidence = collect_deliverable_evidence(mission, workspace_path)
    if evidence.outside_workspace:
        return (
            "Declared file deliverable outside workspace: "
            f"{evidence.outside_workspace[0]}"
        )
    if evidence.missing:
        return f"Missing declared file deliverable: {evidence.missing[0]}"
    return None


def collect_deliverable_evidence(
    mission: dict,
    workspace_path: str,
) -> DeliverableEvidence:
    """Collect structured declared file-deliverable verification evidence."""
    deliverables = mission.get("deliverables", [])
    if not isinstance(deliverables, list) or not deliverables:
        return DeliverableEvidence(
            verified=True,
            passed=True,
            checked_paths=[],
            missing=[],
            outside_workspace=[],
        )

    checked_paths: list[str] = []
    missing: list[str] = []
    outside_workspace: list[str] = []
    for item in deliverables:
        path = file_path_from_deliverable(item)
        if path is None:
            continue
        target = resolve_safe_workspace_path(workspace_path, path)
        if target is None:
            # Absolute/home/escaping: record without reading outside workspace.
            outside_workspace.append(path)
            continue
        checked_paths.append(path)
        if not target.is_file():
            missing.append(path)

    return DeliverableEvidence(
        verified=True,
        passed=len(missing) == 0 and len(outside_workspace) == 0,
        checked_paths=checked_paths,
        missing=missing,
        outside_workspace=outside_workspace,
    )


def collect_changed_files(
    workspace_path: str,
) -> tuple[list[str], str | None]:
    """Return changed/untracked repo-relative paths from Git status.

    Returns ``(paths, warning)``. ``warning`` is set when status cannot be read.
    """
    status = _git_status_porcelain(workspace_path)
    if status.returncode != 0:
        return [], WARNING_FILES_CHANGED_UNAVAILABLE
    return parse_git_status_porcelain_paths(status.stdout), None


def build_persistence_evidence(
    mission: dict,
    *,
    attempted: bool,
    ok: bool | None = None,
    commit_sha: str | None = None,
    mode: str | None = None,
    pushed: bool | None = None,
) -> PersistenceEvidence:
    """Record platform persistence outcome for the structured result.

    Prefer ``mode`` / ``pushed`` from ``PersistenceResult`` when persistence
    ran. When ``mode`` is omitted, use the validated mission configuration —
    never invent ``none`` solely because the execution result omitted mode.
    """
    reported_mode = (
        mode if mode is not None else resolve_persistence_mode(mission)
    )
    return PersistenceEvidence(
        mode=reported_mode,
        attempted=attempted,
        ok=ok,
        commit_sha=commit_sha,
        pushed=pushed,
    )


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
    structured = empty_structured_result()

    def _attach_documentation(*, handling_completed: bool) -> None:
        structured.documentation = build_documentation_evidence(
            mission,
            files_changed=structured.files_changed,
            handling_completed=handling_completed,
        )

    try:
        prep = prepare_isolated_workspace(mission)
        if not prep.ok:
            append_warning(structured, WARNING_PREP_FAILED)
            append_warning(structured, WARNING_PERSISTENCE_NOT_ATTEMPTED)
            structured.persistence = build_persistence_evidence(
                mission,
                attempted=False,
                ok=None,
            )
            structured.deliverables = DeliverableEvidence(
                verified=False,
                passed=None,
            )
            _attach_documentation(handling_completed=False)
            finalize_structured_summary(structured, error=prep.error)
            registry.store_result(
                run_id,
                error=prep.error,
                result=structured,
            )
            registry.update_status(run_id, RunStatus.FAILED)
            return

        # realpath: same absolute checkout for agent --workspace and persistence.
        workspace_path = os.path.realpath(prep.workspace_path)
        assert workspace_path is not None

        # repository.path from the mission selects a subdirectory inside the
        # checkout ('.' → repository root). Git persistence always uses the
        # clone top-level so nested paths cannot retarget origin.
        requested_path = mission.get("repository", {}).get("path")
        agent_workspace, path_error = resolve_agent_workspace_path(
            workspace_path,
            requested_path if isinstance(requested_path, str) else ".",
        )
        if path_error is not None or not agent_workspace:
            append_warning(structured, WARNING_PREP_FAILED)
            append_warning(structured, WARNING_PERSISTENCE_NOT_ATTEMPTED)
            structured.persistence = build_persistence_evidence(
                mission,
                attempted=False,
                ok=None,
            )
            structured.deliverables = DeliverableEvidence(
                verified=False,
                passed=None,
            )
            _attach_documentation(handling_completed=False)
            finalize_structured_summary(structured, error=path_error)
            registry.store_result(
                run_id,
                error=path_error,
                result=structured,
            )
            registry.update_status(run_id, RunStatus.FAILED)
            return

        isolated_mission = copy.deepcopy(mission)
        isolated_mission["repository"] = {
            **mission["repository"],
            "path": agent_workspace,
        }

        execution_result = execute_cursor_agent(
            isolated_mission,
            run_id=run_id,
        )
        structured.commands = [
            command_evidence_from_execution(execution_result),
        ]
        changed_files, files_warning = collect_changed_files(workspace_path)
        structured.files_changed = changed_files
        if files_warning is not None:
            append_warning(structured, files_warning)

        contamination = nested_workspace_contamination_error(changed_files)
        if contamination is not None and execution_result.ok:
            append_warning(structured, WARNING_PERSISTENCE_NOT_ATTEMPTED)
            structured.deliverables = DeliverableEvidence(
                verified=False,
                passed=False,
                checked_paths=[],
                missing=[],
                outside_workspace=[],
            )
            structured.persistence = build_persistence_evidence(
                mission,
                attempted=False,
                ok=None,
            )
            _attach_documentation(handling_completed=False)
            finalize_structured_summary(structured, error=contamination)
            registry.store_result(
                run_id,
                stdout=execution_result.stdout,
                stderr=execution_result.stderr,
                error=contamination,
                return_code=execution_result.return_code,
                result=structured,
            )
            registry.update_status(run_id, RunStatus.FAILED)
            return

        if not execution_result.ok:
            append_warning(structured, WARNING_DELIVERABLES_NOT_CHECKED)
            append_warning(structured, WARNING_PERSISTENCE_NOT_ATTEMPTED)
            structured.deliverables = DeliverableEvidence(
                verified=False,
                passed=None,
            )
            structured.persistence = build_persistence_evidence(
                mission,
                attempted=False,
                ok=None,
            )
            _attach_documentation(handling_completed=False)
            finalize_structured_summary(
                structured,
                error=execution_result.error,
            )
            registry.store_result(
                run_id,
                stdout=execution_result.stdout,
                stderr=execution_result.stderr,
                error=execution_result.error,
                return_code=execution_result.return_code,
                result=structured,
            )
            registry.update_status(
                run_id,
                _execution_run_status(
                    execution_result.ok,
                    execution_result.error,
                ),
            )
            return

        deliverable_evidence = collect_deliverable_evidence(
            mission,
            workspace_path,
        )
        structured.deliverables = deliverable_evidence
        deliverable_error: str | None = None
        contamination = nested_workspace_contamination_error(
            list(deliverable_evidence.checked_paths)
            + list(deliverable_evidence.missing)
            + list(deliverable_evidence.outside_workspace)
        )
        if contamination is not None:
            deliverable_error = contamination
        elif deliverable_evidence.outside_workspace:
            deliverable_error = (
                "Declared file deliverable outside workspace: "
                f"{deliverable_evidence.outside_workspace[0]}"
            )
        elif deliverable_evidence.missing:
            deliverable_error = (
                "Missing declared file deliverable: "
                f"{deliverable_evidence.missing[0]}"
            )
        if deliverable_error is not None:
            append_warning(structured, WARNING_PERSISTENCE_NOT_ATTEMPTED)
            structured.persistence = build_persistence_evidence(
                mission,
                attempted=False,
                ok=None,
            )
            # Agent completed; documentation status uses files_changed.
            _attach_documentation(handling_completed=True)
            finalize_structured_summary(structured, error=deliverable_error)
            registry.store_result(
                run_id,
                stdout=execution_result.stdout,
                stderr=execution_result.stderr,
                error=deliverable_error,
                return_code=execution_result.return_code,
                result=structured,
            )
            registry.update_status(run_id, RunStatus.FAILED)
            return

        temp_path_error = persistence_temp_path_guard_error(
            structured.files_changed,
        )
        if temp_path_error is not None:
            append_warning(structured, WARNING_PERSISTENCE_NOT_ATTEMPTED)
            structured.persistence = build_persistence_evidence(
                mission,
                attempted=False,
                ok=None,
            )
            _attach_documentation(handling_completed=True)
            finalize_structured_summary(structured, error=temp_path_error)
            registry.store_result(
                run_id,
                stdout=execution_result.stdout,
                stderr=execution_result.stderr,
                error=temp_path_error,
                return_code=execution_result.return_code,
                result=structured,
            )
            registry.update_status(run_id, RunStatus.FAILED)
            return

        persistence_result = persist_workspace_changes(
            run_id,
            mission,
            workspace_path,
        )
        structured.persistence = build_persistence_evidence(
            mission,
            attempted=True,
            ok=persistence_result.ok,
            commit_sha=persistence_result.commit_sha,
            mode=persistence_result.mode,
            pushed=persistence_result.pushed,
        )
        # Re-read changed files after persistence so commit-only cleanliness
        # does not erase the pre-persist change list already captured.
        if not structured.files_changed:
            changed_files, files_warning = collect_changed_files(workspace_path)
            if changed_files:
                structured.files_changed = changed_files
            if files_warning is not None:
                append_warning(structured, files_warning)

        if not persistence_result.ok:
            # Agent succeeded and deliverables passed; documentation review
            # completed even though platform persistence failed.
            _attach_documentation(handling_completed=True)
            finalize_structured_summary(
                structured,
                error=persistence_result.error,
            )
            registry.store_result(
                run_id,
                stdout=execution_result.stdout,
                stderr=execution_result.stderr,
                error=persistence_result.error,
                return_code=execution_result.return_code,
                result=structured,
            )
            registry.update_status(run_id, RunStatus.FAILED)
            return

        _attach_documentation(handling_completed=True)
        finalize_structured_summary(structured)
        registry.store_result(
            run_id,
            stdout=execution_result.stdout,
            stderr=execution_result.stderr,
            return_code=execution_result.return_code,
            commit_sha=persistence_result.commit_sha,
            result=structured,
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
        append_warning(structured, WARNING_PERSISTENCE_NOT_ATTEMPTED)
        if structured.persistence is None:
            structured.persistence = build_persistence_evidence(
                mission,
                attempted=False,
                ok=None,
            )
        if structured.documentation is None:
            _attach_documentation(handling_completed=False)
        finalize_structured_summary(structured, error=str(exc))
        registry.store_result(
            run_id,
            error=str(exc),
            result=structured,
        )
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
