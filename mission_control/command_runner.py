"""Allowlisted repository command execution in ephemeral checkouts.

Executes one approved argv vector (no shell) inside a fresh isolated
checkout. Persistence is always ``none`` — this path never commits or pushes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
import uuid

from mission_control.workspace import (
    WorkspacePrepResult,
    cleanup_workspace,
    prepare_ephemeral_checkout,
    resolve_safe_workspace_path,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 300.0
MAX_TIMEOUT_SECONDS = 3600.0
MIN_TIMEOUT_SECONDS = 0.1

# Initial allowlist: LegalAI generation CLI only.
ALLOWED_EXECUTABLE = "python3"
ALLOWED_SCRIPT = "scripts/generate_attorney_feedback_candidate.py"

ALLOWED_REPOSITORY_ALIASES: dict[str, str] = {
    "nhpcorp35/legal-ai": "nhpcorp35/legal-ai",
    "legal-ai": "nhpcorp35/legal-ai",
}

# Flags accepted for the allowlisted generation CLI.
_FLAGS_WITH_VALUE = frozenset(
    {
        "--case-root",
        "--question-id",
        "--required-commit",
        "--candidate-output-root",
        "--authorize-private-evidence-transmission",
        "--repo-root",
        "--inventory-path",
    }
)
_FLAGS_NO_VALUE = frozenset({"--generation-only"})
_PATH_FLAGS = frozenset(
    {
        "--case-root",
        "--candidate-output-root",
        "--repo-root",
        "--inventory-path",
    }
)
_SENSITIVE_FLAGS = frozenset(
    {
        "--authorize-private-evidence-transmission",
    }
)

# Shell metacharacters / chaining / redirection / expansion markers.
_SHELL_META_RE = re.compile(r"""[|;&><`$(){}]|&&|\|\||>>|<<|\n|\r""")

# Full SHA for exact checkout verification.
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Env names that must never be forwarded into the command process.
_BLOCKED_ENV_NAMES = frozenset(
    {
        "MISSION_CONTROL_API_KEY",
        "MISSION_CONTROL_URL",
        "GITHUB_TOKEN",
        "CURSOR_API_KEY",
        "GIT_CONFIG_VALUE_0",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_COUNT",
    }
)

# Minimal always-safe names callers may include in allowed_env_names.
_BASE_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "USER",
        "LOGNAME",
    }
)

# Additional names LegalAI generation may need (values never logged).
_GENERATION_ENV_ALLOWLIST = frozenset(
    {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "MODEL_PROVIDER",
        "PYTHONUNBUFFERED",
    }
)

PLATFORM_ENV_NAME_ALLOWLIST = _BASE_ENV_ALLOWLIST | _GENERATION_ENV_ALLOWLIST

MOUNTED_PATHS_ENV = "MISSION_CONTROL_MOUNTED_PATHS"
REPOSITORY_URL_MAP_ENV = "MISSION_CONTROL_REPOSITORY_URL_MAP"
LEGAL_AI_REPOSITORY_URL_ENV = "MISSION_CONTROL_LEGAL_AI_REPOSITORY_URL"

REDACTED = "***REDACTED***"


class CommandRunnerError(Exception):
    """Reject an unsafe or non-allowlisted repository command request."""

    def __init__(self, message: str, *, code: str = "COMMAND_REJECTED") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class RepositoryCommandSpec:
    """Structured fields for one repository command execution."""

    repository: str
    ref: str
    argv: list[str]
    working_directory: str = "."
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    allowed_env_names: list[str] = field(default_factory=list)


@dataclass
class RepositoryCommandResult:
    """Outcome of an allowlisted repository command run."""

    ok: bool
    run_id: str
    checkout_commit: str | None = None
    argv: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    elapsed_seconds: float = 0.0
    artifact_paths: list[str] = field(default_factory=list)
    persistence: dict = field(
        default_factory=lambda: {
            "mode": "none",
            "attempted": False,
            "ok": True,
            "commit_sha": None,
            "pushed": False,
        }
    )
    error: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "run_id": self.run_id,
            "checkout_commit": self.checkout_commit,
            "argv": list(self.argv),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "elapsed_seconds": self.elapsed_seconds,
            "artifact_paths": list(self.artifact_paths),
            "persistence": dict(self.persistence),
            "error": self.error,
            "error_code": self.error_code,
        }


def canonicalize_repository(repository: str) -> str:
    """Return the canonical allowlisted repository id."""
    key = repository.strip()
    canonical = ALLOWED_REPOSITORY_ALIASES.get(key)
    if canonical is None:
        raise CommandRunnerError(
            f"Repository not allowlisted: {repository!r}",
            code="REPOSITORY_NOT_ALLOWLISTED",
        )
    return canonical


def resolve_repository_url(repository: str) -> str:
    """Resolve a clone URL for an allowlisted repository."""
    canonical = canonicalize_repository(repository)

    raw_map = os.environ.get(REPOSITORY_URL_MAP_ENV, "").strip()
    if raw_map:
        try:
            mapping = json.loads(raw_map)
        except json.JSONDecodeError as exc:
            raise CommandRunnerError(
                f"{REPOSITORY_URL_MAP_ENV} must be a JSON object",
                code="REPOSITORY_URL_MAP_INVALID",
            ) from exc
        if not isinstance(mapping, dict):
            raise CommandRunnerError(
                f"{REPOSITORY_URL_MAP_ENV} must be a JSON object",
                code="REPOSITORY_URL_MAP_INVALID",
            )
        for key in (canonical, repository.strip()):
            value = mapping.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    if canonical == "nhpcorp35/legal-ai":
        legal_url = os.environ.get(LEGAL_AI_REPOSITORY_URL_ENV, "").strip()
        if legal_url:
            return legal_url

    return f"https://github.com/{canonical}.git"


def mounted_artifact_paths() -> list[Path]:
    """Return explicitly mounted artifact/data roots available to the executor."""
    raw = os.environ.get(MOUNTED_PATHS_ENV, "").strip()
    if not raw:
        return []
    paths: list[Path] = []
    for part in raw.split(":"):
        part = part.strip()
        if not part:
            continue
        paths.append(Path(part).expanduser().resolve())
    return paths


def _contains_shell_metacharacters(token: str) -> bool:
    if _SHELL_META_RE.search(token):
        return True
    # Reject tokens that would require shell quoting to be safe as a unit.
    try:
        lexed = shlex.split(token, posix=True)
    except ValueError:
        return True
    return len(lexed) != 1 or lexed[0] != token


def _reject_shell_token(token: str, *, role: str) -> None:
    if not isinstance(token, str) or token == "":
        raise CommandRunnerError(
            f"Invalid {role}: empty token",
            code="INVALID_ARGV",
        )
    if "\x00" in token:
        raise CommandRunnerError(
            f"Invalid {role}: NUL byte",
            code="INVALID_ARGV",
        )
    if _contains_shell_metacharacters(token):
        raise CommandRunnerError(
            f"Rejected shell metacharacters in {role}",
            code="SHELL_METACHARACTERS",
        )


def redact_argv(argv: list[str]) -> list[str]:
    """Return argv with sensitive flag values replaced (never log secrets)."""
    redacted: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        redacted.append(token)
        if token in _SENSITIVE_FLAGS and i + 1 < len(argv):
            redacted.append(REDACTED)
            i += 2
            continue
        if "=" in token:
            flag, _, _value = token.partition("=")
            if flag in _SENSITIVE_FLAGS:
                redacted[-1] = f"{flag}={REDACTED}"
        i += 1
    return redacted


def _normalize_repo_relative(path_text: str) -> str | None:
    """Normalize a repo-relative path; return None on traversal / absolute."""
    if not path_text or "\x00" in path_text:
        return None
    if path_text.startswith(("/", "~")):
        return None
    candidate = Path(path_text)
    if candidate.is_absolute():
        return None
    parts: list[str] = []
    for part in candidate.parts:
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    return "/".join(parts)


def _is_under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _resolve_allowed_path(
    path_text: str,
    *,
    workspace: Path,
    cwd: Path,
    mounted: list[Path],
) -> Path:
    """Resolve a path argument to an allowed workspace or mount location."""
    _reject_shell_token(path_text, role="path argument")

    raw = Path(path_text).expanduser()
    if raw.is_absolute():
        resolved = raw.resolve()
    else:
        # Relative paths are resolved from the working directory inside the
        # checkout; they must not escape via .. components.
        if _normalize_repo_relative(path_text) is None and ".." in Path(path_text).parts:
            raise CommandRunnerError(
                "Path traversal rejected",
                code="PATH_TRAVERSAL",
            )
        resolved = (cwd / raw).resolve()

    allowed_roots = [workspace.resolve(), *[m.resolve() for m in mounted]]
    if not any(_is_under_root(resolved, root) for root in allowed_roots):
        raise CommandRunnerError(
            "Path is outside the repository workspace and mounted roots",
            code="PATH_OUTSIDE_ALLOWED_ROOTS",
        )
    return resolved


def validate_and_build_argv(
    argv: list[str],
    *,
    workspace: Path,
    working_directory: str,
    mounted: list[Path] | None = None,
) -> tuple[list[str], Path, Path | None]:
    """Validate argv against the allowlist and resolve the working directory.

    Returns ``(resolved_argv, cwd, candidate_output_root)``.
    """
    if not isinstance(argv, list) or not argv:
        raise CommandRunnerError("argv must be a non-empty list", code="INVALID_ARGV")
    if any(not isinstance(item, str) for item in argv):
        raise CommandRunnerError("argv entries must be strings", code="INVALID_ARGV")

    for token in argv:
        _reject_shell_token(token, role="argv")

    if argv[0] != ALLOWED_EXECUTABLE:
        raise CommandRunnerError(
            f"Executable not allowlisted: {argv[0]!r} "
            f"(only {ALLOWED_EXECUTABLE!r} is permitted)",
            code="EXECUTABLE_NOT_ALLOWLISTED",
        )
    if len(argv) < 2:
        raise CommandRunnerError(
            f"argv must include {ALLOWED_SCRIPT!r}",
            code="SCRIPT_NOT_ALLOWLISTED",
        )

    script_normalized = _normalize_repo_relative(argv[1])
    if script_normalized != ALLOWED_SCRIPT:
        if script_normalized is None and (
            ".." in Path(argv[1]).parts or argv[1].startswith(("/", "~"))
        ):
            raise CommandRunnerError(
                "Path traversal rejected",
                code="PATH_TRAVERSAL",
            )
        raise CommandRunnerError(
            f"Script not allowlisted: {argv[1]!r} "
            f"(only {ALLOWED_SCRIPT!r} is permitted)",
            code="SCRIPT_NOT_ALLOWLISTED",
        )

    script_path = resolve_safe_workspace_path(str(workspace), ALLOWED_SCRIPT)
    if script_path is None or not script_path.is_file():
        raise CommandRunnerError(
            f"Allowlisted script missing from checkout: {ALLOWED_SCRIPT}",
            code="SCRIPT_MISSING",
        )

    if working_directory in (".", ""):
        cwd_path = workspace.resolve()
    else:
        wd_normalized = _normalize_repo_relative(working_directory)
        if wd_normalized is None:
            raise CommandRunnerError(
                "working_directory must be a relative path inside the repository",
                code="INVALID_WORKING_DIRECTORY",
            )
        cwd_path = resolve_safe_workspace_path(str(workspace), wd_normalized)
        if cwd_path is None:
            raise CommandRunnerError(
                "working_directory escapes the repository workspace",
                code="INVALID_WORKING_DIRECTORY",
            )
    if not cwd_path.exists():
        raise CommandRunnerError(
            "working_directory does not exist in the checkout",
            code="INVALID_WORKING_DIRECTORY",
        )
    if cwd_path.is_file():
        raise CommandRunnerError(
            "working_directory must be a directory",
            code="INVALID_WORKING_DIRECTORY",
        )

    mounts = mounted if mounted is not None else mounted_artifact_paths()
    resolved: list[str] = [ALLOWED_EXECUTABLE, str(script_path)]
    candidate_output: Path | None = None

    i = 2
    while i < len(argv):
        token = argv[i]
        if token in _FLAGS_NO_VALUE:
            resolved.append(token)
            i += 1
            continue
        if token in _FLAGS_WITH_VALUE:
            if i + 1 >= len(argv):
                raise CommandRunnerError(
                    f"Flag {token} requires a value",
                    code="INVALID_ARGV",
                )
            value = argv[i + 1]
            if token in _PATH_FLAGS:
                path_resolved = _resolve_allowed_path(
                    value,
                    workspace=workspace,
                    cwd=cwd_path,
                    mounted=mounts,
                )
                resolved.extend([token, str(path_resolved)])
                if token == "--candidate-output-root":
                    candidate_output = path_resolved
            else:
                resolved.extend([token, value])
            i += 2
            continue
        if token.startswith("--") and "=" in token:
            flag, _, value = token.partition("=")
            if flag in _FLAGS_NO_VALUE:
                raise CommandRunnerError(
                    f"Flag {flag} does not take a value",
                    code="INVALID_ARGV",
                )
            if flag not in _FLAGS_WITH_VALUE:
                raise CommandRunnerError(
                    f"Flag not allowlisted: {flag!r}",
                    code="FLAG_NOT_ALLOWLISTED",
                )
            if flag in _PATH_FLAGS:
                path_resolved = _resolve_allowed_path(
                    value,
                    workspace=workspace,
                    cwd=cwd_path,
                    mounted=mounts,
                )
                resolved.append(f"{flag}={path_resolved}")
                if flag == "--candidate-output-root":
                    candidate_output = path_resolved
            else:
                resolved.append(token)
            i += 1
            continue
        raise CommandRunnerError(
            f"Flag or argument not allowlisted: {token!r}",
            code="FLAG_NOT_ALLOWLISTED",
        )

    return resolved, cwd_path, candidate_output


def build_command_env(allowed_env_names: list[str]) -> dict[str, str]:
    """Build a subprocess env from an explicit name allowlist (values not logged)."""
    if any(not isinstance(name, str) or not name for name in allowed_env_names):
        raise CommandRunnerError(
            "allowed_env_names entries must be non-empty strings",
            code="INVALID_ENV_ALLOWLIST",
        )
    for name in allowed_env_names:
        _reject_shell_token(name, role="env name")
        if name in _BLOCKED_ENV_NAMES:
            raise CommandRunnerError(
                f"Environment variable name not permitted: {name!r}",
                code="ENV_NAME_NOT_ALLOWLISTED",
            )
        if name not in PLATFORM_ENV_NAME_ALLOWLIST:
            raise CommandRunnerError(
                f"Environment variable name not allowlisted: {name!r}",
                code="ENV_NAME_NOT_ALLOWLISTED",
            )

    env: dict[str, str] = {}
    # Always provide a minimal PATH so python3 can be resolved when requested.
    if "PATH" not in allowed_env_names:
        path_value = os.environ.get("PATH")
        if path_value:
            env["PATH"] = path_value
    for name in allowed_env_names:
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _read_head_commit(workspace: Path) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        return None
    sha = completed.stdout.strip()
    return sha or None


def _collect_artifact_paths(root: Path | None) -> list[str]:
    if root is None or not root.exists():
        return []
    if root.is_file():
        return [str(root)]
    paths: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            paths.append(str(Path(dirpath) / name))
    paths.sort()
    return paths


def run_repository_command(
    spec: RepositoryCommandSpec,
    *,
    run_id: str | None = None,
) -> RepositoryCommandResult:
    """Checkout ``spec.ref`` and execute the allowlisted argv without a shell."""
    rid = run_id or str(uuid.uuid4())
    started = time.monotonic()
    redacted = redact_argv(list(spec.argv))
    persistence = {
        "mode": "none",
        "attempted": False,
        "ok": True,
        "commit_sha": None,
        "pushed": False,
    }

    def _fail(
        message: str,
        *,
        code: str,
        checkout_commit: str | None = None,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        artifact_paths: list[str] | None = None,
    ) -> RepositoryCommandResult:
        return RepositoryCommandResult(
            ok=False,
            run_id=rid,
            checkout_commit=checkout_commit,
            argv=redacted,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            elapsed_seconds=time.monotonic() - started,
            artifact_paths=list(artifact_paths or []),
            persistence=dict(persistence),
            error=message,
            error_code=code,
        )

    try:
        timeout = float(spec.timeout_seconds)
    except (TypeError, ValueError):
        return _fail("timeout_seconds must be a number", code="INVALID_TIMEOUT")
    if timeout < MIN_TIMEOUT_SECONDS or timeout > MAX_TIMEOUT_SECONDS:
        return _fail(
            (
                "timeout_seconds must be between "
                f"{MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS}"
            ),
            code="INVALID_TIMEOUT",
        )

    workspace_path: str | None = None
    try:
        # Validate repository allowlist before clone.
        repo_url = resolve_repository_url(spec.repository)
        canonicalize_repository(spec.repository)
        _reject_shell_token(spec.ref, role="ref")

        prep: WorkspacePrepResult = prepare_ephemeral_checkout(
            repository_url=repo_url,
            ref=spec.ref,
        )
        if not prep.ok or not prep.workspace_path:
            return _fail(
                prep.error or "Failed to prepare ephemeral checkout",
                code="CHECKOUT_FAILED",
            )
        workspace_path = prep.workspace_path
        workspace = Path(workspace_path)
        head = _read_head_commit(workspace)
        if head is None:
            return _fail(
                "Could not read checkout commit",
                code="CHECKOUT_FAILED",
                checkout_commit=None,
            )

        requested = spec.ref.strip()
        if _FULL_SHA_RE.fullmatch(requested) and head != requested:
            return _fail(
                (
                    "Checkout commit does not match requested ref: "
                    f"requested={requested} actual={head}"
                ),
                code="WRONG_COMMIT",
                checkout_commit=head,
            )

        mounts = mounted_artifact_paths()
        resolved_argv, cwd, candidate_output = validate_and_build_argv(
            list(spec.argv),
            workspace=workspace,
            working_directory=spec.working_directory,
            mounted=mounts,
        )
        redacted = redact_argv(resolved_argv)
        # Keep script path as the allowlisted relative form in evidence.
        if len(redacted) >= 2:
            redacted[1] = ALLOWED_SCRIPT

        env = build_command_env(list(spec.allowed_env_names))

        logger.info(
            (
                "repository_command run_id=%s repository=%s ref=%s "
                "checkout_commit=%s argv=%s timeout=%s persistence=none"
            ),
            rid,
            canonicalize_repository(spec.repository),
            requested,
            head,
            redacted,
            timeout,
        )

        try:
            completed = subprocess.run(
                resolved_argv,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                shell=False,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            artifacts = _collect_artifact_paths(candidate_output)
            return RepositoryCommandResult(
                ok=False,
                run_id=rid,
                checkout_commit=head,
                argv=redacted,
                stdout=stdout,
                stderr=stderr,
                exit_code=None,
                elapsed_seconds=time.monotonic() - started,
                artifact_paths=artifacts,
                persistence=dict(persistence),
                error=f"Command timed out after {timeout} seconds",
                error_code="TIMEOUT",
            )

        artifacts = _collect_artifact_paths(candidate_output)
        ok = completed.returncode == 0
        return RepositoryCommandResult(
            ok=ok,
            run_id=rid,
            checkout_commit=head,
            argv=redacted,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            exit_code=completed.returncode,
            elapsed_seconds=time.monotonic() - started,
            artifact_paths=artifacts,
            persistence=dict(persistence),
            error=None if ok else f"Command exited with code {completed.returncode}",
            error_code=None if ok else "EXIT_NONZERO",
        )
    except CommandRunnerError as exc:
        return _fail(exc.message, code=exc.code)
    except Exception as exc:  # noqa: BLE001 — surface unexpected failures
        logger.exception("repository_command failed run_id=%s", rid)
        return _fail(str(exc), code="INTERNAL_ERROR")
    finally:
        if workspace_path is not None:
            try:
                cleanup_workspace(workspace_path)
            except Exception:
                logger.exception(
                    "Failed to cleanup command-runner workspace: %s",
                    workspace_path,
                )
