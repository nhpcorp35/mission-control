"""Bounded read-only commit resolver for nhpcorp35/legal-ai (case.resolve_commit)."""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Mapping

import httpx

GATEWAY_TOOL = "case.resolve_commit"
FIXED_REPOSITORY = "nhpcorp35/legal-ai"
MAIN_REF = "main"
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
GITHUB_API = "https://api.github.com"

ERROR_INVALID_REF = "invalid_ref"
ERROR_NOT_FOUND = "not_found"
ERROR_RESOLUTION_FAILED = "resolution_failed"
ERROR_UNAUTHORIZED = "unauthorized"

SUCCESS_KEYS = frozenset({"ok", "repository", "ref", "commit_sha"})
FAILURE_KEYS = frozenset({"ok", "error"})


class CaseResolveCommitContractError(ValueError):
    """Public contract validation failure."""


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def validate_public_input(arguments: Mapping[str, Any]) -> dict[str, str]:
    """Accept only ``ref``; reject repository/URL/path/owner fields."""
    if not isinstance(arguments, Mapping):
        raise CaseResolveCommitContractError("input must be an object")
    extra = set(arguments) - {"ref"}
    if extra:
        raise CaseResolveCommitContractError(
            "input accepts ref only; repository, URL, path, and owner are rejected"
        )
    ref = arguments.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        raise CaseResolveCommitContractError("ref must be a non-empty string")
    return {"ref": ref.strip()}


def validate_ref(ref: str) -> str:
    """Allow exactly ``main`` or a lowercase 40-character SHA."""
    if ref == MAIN_REF:
        return ref
    if COMMIT_SHA_RE.fullmatch(ref):
        return ref
    raise CaseResolveCommitContractError(
        'ref must be exactly "main" or a lowercase 40-character commit SHA'
    )


def validate_public_success(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Ensure success payload uses the exact public keys only."""
    if not isinstance(payload, Mapping):
        raise CaseResolveCommitContractError("output must be an object")
    keys = set(payload)
    if keys != SUCCESS_KEYS:
        raise CaseResolveCommitContractError(
            "success output keys must be exactly ok, repository, ref, commit_sha"
        )
    if payload.get("ok") is not True:
        raise CaseResolveCommitContractError("success output requires ok=true")
    repository = payload.get("repository")
    if repository != FIXED_REPOSITORY:
        raise CaseResolveCommitContractError(
            f"repository must be exactly {FIXED_REPOSITORY!r}"
        )
    ref = payload.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        raise CaseResolveCommitContractError("ref must be a non-empty string")
    commit_sha = payload.get("commit_sha")
    if not isinstance(commit_sha, str) or not COMMIT_SHA_RE.fullmatch(commit_sha):
        raise CaseResolveCommitContractError(
            "commit_sha must be an exact lowercase 40-character hex SHA"
        )
    return {
        "ok": True,
        "repository": FIXED_REPOSITORY,
        "ref": ref,
        "commit_sha": commit_sha,
    }


def failure_response(*, error: str, ref: str | None = None) -> dict[str, Any]:
    """Bounded fail-closed error without arbitrary GitHub content."""
    payload: dict[str, Any] = {"ok": False, "error": error}
    if ref is not None:
        payload["ref"] = ref[:64]
    extra = set(payload) - (FAILURE_KEYS | {"ref"})
    if extra:
        raise CaseResolveCommitContractError("failure output has undocumented fields")
    return payload


async def _github_get_commit(
    ref: str,
    *,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> tuple[int | None, str | None, str | None]:
    """Return ``(status_code, commit_sha, transport_error)`` for the fixed repo."""
    path = f"/repos/{FIXED_REPOSITORY}/commits/{ref}"

    async def _request(client: httpx.AsyncClient) -> tuple[int | None, str | None, str | None]:
        try:
            response = await client.get(
                f"{GITHUB_API}{path}",
                headers=_github_headers(),
            )
        except httpx.HTTPError as exc:
            return None, None, exc.__class__.__name__
        sha: str | None = None
        if response.status_code < 400:
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                raw_sha = body.get("sha")
                if isinstance(raw_sha, str):
                    sha = raw_sha.lower()
        return response.status_code, sha, None

    if client_factory is not None:
        async with client_factory() as client:
            return await _request(client)
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        return await _request(client)


async def resolve_legalai_commit(
    ref: str,
    *,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> dict[str, Any]:
    """Resolve ``main`` or verify an immutable SHA in nhpcorp35/legal-ai."""
    try:
        validated_ref = validate_ref(ref)
    except CaseResolveCommitContractError:
        return failure_response(error=ERROR_INVALID_REF, ref=ref)

    status, sha, transport_error = await _github_get_commit(
        validated_ref,
        client_factory=client_factory,
    )
    if transport_error is not None:
        return failure_response(error=ERROR_RESOLUTION_FAILED, ref=validated_ref)
    if status is None:
        return failure_response(error=ERROR_RESOLUTION_FAILED, ref=validated_ref)

    if validated_ref == MAIN_REF:
        if status == 404:
            return failure_response(error=ERROR_NOT_FOUND, ref=validated_ref)
        if status >= 400 or not sha or not COMMIT_SHA_RE.fullmatch(sha):
            return failure_response(error=ERROR_RESOLUTION_FAILED, ref=validated_ref)
        return validate_public_success(
            {
                "ok": True,
                "repository": FIXED_REPOSITORY,
                "ref": validated_ref,
                "commit_sha": sha,
            }
        )

    # Explicit immutable SHA: GitHub 404/422 means absent from the fixed repository.
    if status in (404, 422):
        return failure_response(error=ERROR_NOT_FOUND, ref=validated_ref)
    if status >= 400:
        return failure_response(error=ERROR_RESOLUTION_FAILED, ref=validated_ref)
    commit_sha = sha if sha and COMMIT_SHA_RE.fullmatch(sha) else validated_ref
    if not COMMIT_SHA_RE.fullmatch(commit_sha):
        return failure_response(error=ERROR_RESOLUTION_FAILED, ref=validated_ref)
    return validate_public_success(
        {
            "ok": True,
            "repository": FIXED_REPOSITORY,
            "ref": validated_ref,
            "commit_sha": commit_sha,
        }
    )
