"""Cursor CLI availability and authentication checks for cloud deployment."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import shutil

CURSOR_AGENT = "cursor-agent"
CURSOR_API_KEY_ENV = "CURSOR_API_KEY"
CURSOR_LOCAL_BIN = Path.home() / ".local" / "bin"
CURSOR_RUNTIME_BIN = Path("/app/.cursor-runtime")

ERROR_CURSOR_AGENT_UNAVAILABLE = "CURSOR_AGENT_UNAVAILABLE"
ERROR_CURSOR_API_KEY_MISSING = "CURSOR_API_KEY_MISSING"


@dataclass(frozen=True)
class StructuredError:
    code: str
    message: str
    stage: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class CursorCliStatus:
    installed: bool
    authenticated: bool
    binary_path: str | None
    api_key_configured: bool


def augment_path(path: str | None = None) -> str:
    """Prepend ~/.local/bin so the official installer location is visible."""
    local_bin = str(CURSOR_LOCAL_BIN)
    current = path if path is not None else os.environ.get("PATH", "")
    if not current:
        return local_bin
    parts = current.split(os.pathsep)
    if local_bin in parts:
        return current
    return os.pathsep.join([local_bin, current])


def cursor_cli_env() -> dict[str, str]:
    """Return a copy of the process environment with Cursor CLI PATH applied."""
    env = os.environ.copy()
    env["PATH"] = augment_path(env.get("PATH"))
    return env


def find_cursor_agent_binary() -> str | None:
    """Resolve cursor-agent from Railway runtime or the normal CLI PATH."""
    search_path = cursor_cli_env()["PATH"]
    runtime_bin = str(CURSOR_RUNTIME_BIN)

    if runtime_bin not in search_path.split(os.pathsep):
        search_path = os.pathsep.join([runtime_bin, search_path])

    return shutil.which(CURSOR_AGENT, path=search_path)


def is_api_key_configured() -> bool:
    """Return True when CURSOR_API_KEY is set to a non-empty value."""
    return bool(os.environ.get(CURSOR_API_KEY_ENV, "").strip())


def check_cursor_cli_status() -> CursorCliStatus:
    """Inspect Cursor CLI installation and API key configuration."""
    binary_path = find_cursor_agent_binary()
    api_key_configured = is_api_key_configured()
    return CursorCliStatus(
        installed=binary_path is not None,
        authenticated=api_key_configured,
        binary_path=binary_path,
        api_key_configured=api_key_configured,
    )


def preflight_for_execution() -> StructuredError | None:
    """Return a structured preflight error when Cursor CLI cannot run."""
    if not find_cursor_agent_binary():
        return StructuredError(
            code=ERROR_CURSOR_AGENT_UNAVAILABLE,
            message=(
                f"{CURSOR_AGENT} is not installed or not on PATH. "
                f"Install with: curl -fsS https://cursor.com/install | bash "
                f"and ensure {CURSOR_LOCAL_BIN} is on PATH."
            ),
            stage="preflight",
        )

    if not is_api_key_configured():
        return StructuredError(
            code=ERROR_CURSOR_API_KEY_MISSING,
            message=(
                f"{CURSOR_API_KEY_ENV} environment variable is not set. "
                "Create a key at https://cursor.com/dashboard/api and configure "
                "it as a Railway service variable."
            ),
            stage="preflight",
        )

    return None
