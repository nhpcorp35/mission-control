"""Build Mission Spec v1.0 YAML from structured fields.

Thin adapter for HAL / API callers that prefer structured submission over
hand-authored raw YAML. Builder-controlled fields are fixed for execute-mode
async runs and cannot be overridden by callers in v1.
"""

from __future__ import annotations

from typing import Any

import yaml

# Read-only structured default (and raw YAML omitted-block default elsewhere).
DEFAULT_PERSISTENCE_MODE = "none"
# Structured missions that create/modify/delete files default here when the
# caller omits persistence_mode. Explicit caller values are never overridden.
MUTATING_STRUCTURED_PERSISTENCE_MODE = "push"
DEFAULT_REPOSITORY_NAME = "Mission-Control"
DEFAULT_REPOSITORY_PATH = "."
DEFAULT_BASE_BRANCH = "main"
DEFAULT_RUN_COMMANDS = True
DEFAULT_PLATFORM_PUSH_APPROVED = False
DEFAULT_ALLOW_AUTOMATIC_PLATFORM_PUSH = False


def resolve_structured_persistence_mode(
    *,
    create_files: bool,
    modify_files: bool,
    delete_files: bool = False,
    persistence_mode: str | None = None,
) -> str:
    """Resolve ``persistence.mode`` for structured mission submission.

    Explicit ``persistence_mode`` is authoritative and never overridden.
    When omitted (``None``), repository-mutating permission flags
    (create / modify / delete) default to ``push``; read-only structured
    missions default to ``none``.

    This does not change raw Mission Spec YAML resolution for an omitted
    ``persistence`` block (still ``none`` via ``resolve_persistence_mode``).
    """
    if persistence_mode is not None:
        return persistence_mode
    if create_files or modify_files or delete_files:
        return MUTATING_STRUCTURED_PERSISTENCE_MODE
    return DEFAULT_PERSISTENCE_MODE


def build_mission_spec(
    *,
    mission_id: str,
    title: str,
    instructions: str,
    deliverables: list[Any],
    create_files: bool,
    modify_files: bool,
    persistence_mode: str | None = None,
    repository_name: str = DEFAULT_REPOSITORY_NAME,
    repository_path: str = DEFAULT_REPOSITORY_PATH,
    base_branch: str = DEFAULT_BASE_BRANCH,
    run_commands: bool = DEFAULT_RUN_COMMANDS,
    platform_push_approved: bool = DEFAULT_PLATFORM_PUSH_APPROVED,
    allow_automatic_platform_push: bool = (
        DEFAULT_ALLOW_AUTOMATIC_PLATFORM_PUSH
    ),
) -> dict[str, Any]:
    """Build a Mission Spec v1.0 dictionary with safe execute defaults."""
    resolved_persistence_mode = resolve_structured_persistence_mode(
        create_files=create_files,
        modify_files=modify_files,
        persistence_mode=persistence_mode,
    )
    return {
        "version": "1.0",
        "mission_id": mission_id,
        "title": title,
        "repository": {
            "name": repository_name,
            "path": repository_path,
            "base_branch": base_branch,
        },
        "execution": {
            "agent": "cursor",
            "mode": "execute",
            "sandbox": True,
            "worktree": False,
        },
        "permissions": {
            "read": True,
            "create_files": create_files,
            "modify_files": modify_files,
            "delete_files": False,
            "run_commands": run_commands,
            "stage_changes": False,
            "commit": False,
            "push": False,
        },
        "instructions": instructions,
        "deliverables": list(deliverables),
        "approval": {
            "execute_without_approval": True,
            "commit_requires_approval": True,
            "push_requires_approval": True,
            "platform_push_approved": platform_push_approved,
            "allow_automatic_platform_push": allow_automatic_platform_push,
        },
        "persistence": {
            "mode": resolved_persistence_mode,
        },
    }


def render_mission_yaml(
    *,
    mission_id: str,
    title: str,
    instructions: str,
    deliverables: list[Any],
    create_files: bool,
    modify_files: bool,
    persistence_mode: str | None = None,
    repository_name: str = DEFAULT_REPOSITORY_NAME,
    repository_path: str = DEFAULT_REPOSITORY_PATH,
    base_branch: str = DEFAULT_BASE_BRANCH,
    run_commands: bool = DEFAULT_RUN_COMMANDS,
    platform_push_approved: bool = DEFAULT_PLATFORM_PUSH_APPROVED,
    allow_automatic_platform_push: bool = (
        DEFAULT_ALLOW_AUTOMATIC_PLATFORM_PUSH
    ),
) -> str:
    """Render Mission Spec v1.0 YAML text via ``yaml.safe_dump``."""
    spec = build_mission_spec(
        mission_id=mission_id,
        title=title,
        instructions=instructions,
        deliverables=deliverables,
        create_files=create_files,
        modify_files=modify_files,
        persistence_mode=persistence_mode,
        repository_name=repository_name,
        repository_path=repository_path,
        base_branch=base_branch,
        run_commands=run_commands,
        platform_push_approved=platform_push_approved,
        allow_automatic_platform_push=allow_automatic_platform_push,
    )
    return yaml.safe_dump(
        spec,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
