"""Tests for isolated workspace preparation and Git persistence."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import os

from mission_control.executor import ExecutionResult
from mission_control.run_registry import RunRegistry, RunStatus
from mission_control.validator import validate_mission_for_execute
from mission_control.workspace import (
    DEFAULT_LEGAL_AI_CLONE_URL,
    DEFAULT_MISSION_CONTROL_CLONE_URL,
    CLONE_STRATEGY_FULL,
    CLONE_STRATEGY_SHALLOW,
    LEGAL_AI_REPOSITORY_URL_ENV,
    NESTED_WORKSPACE_CONTAMINATION_PREFIX,
    PLATFORM_MAIN_WRITE_ACK_REQUIRED,
    PLATFORM_PUSH_APPROVAL_REQUIRED,
    PLATFORM_TARGET_BRANCH_REQUIRED,
    POST_PUSH_RECONCILIATION_FAILURE_STAGE,
    REMOTE_RECONCILIATION_FAILURE_STAGE,
    REPOSITORY_ORIGIN_MISMATCH_PREFIX,
    REPOSITORY_URL_MAP_ENV,
    SELF_REPOSITORY_URL_ENV,
    WORKSPACE_CLONE_DEPTH_ENV,
    PersistenceResult,
    WorkspacePrepResult,
    _argv_safe_repository_url,
    _ls_remote_branch_sha,
    _redact_secret_text,
    build_persistence_evidence,
    cleanup_workspace,
    collect_deliverable_evidence,
    configure_workspace_origin,
    execute_registered_run,
    file_path_from_deliverable,
    get_origin_url,
    is_platform_main_write_acknowledged,
    is_platform_push_authorized,
    is_protected_default_branch,
    looks_like_file_path_deliverable,
    nested_workspace_contamination_error,
    normalize_remote_url_identity,
    persist_workspace_changes,
    prepare_ephemeral_checkout,
    prepare_isolated_workspace,
    require_persistence_push_target,
    require_platform_push_approval,
    resolve_agent_workspace_path,
    resolve_mission_clone_url,
    resolve_persistence_target_branch,
    resolve_safe_workspace_deliverable,
    resolve_workspace_clone_depth,
    verify_declared_file_deliverables,
    verify_workspace_origin_matches_mission,
)
from mission_control.run_result import (
    PersistenceEvidence,
    build_run_summary,
)


def _run_git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=check,
        shell=False,
    )


def _cleanup_mocked_workspaces(mock_cleanup) -> None:
    """Run real cleanup for paths observed while cleanup_workspace was mocked.

    Handoff tests mock cleanup so post-run filesystem assertions can inspect the
    isolated checkout. Without a follow-up real cleanup, ``mkdtemp`` workspaces
    leak across the suite and amplify process/file pressure under low NPROC.
    """
    seen: set[str] = set()
    for call in mock_cleanup.call_args_list:
        path = call.args[0] if call.args else call.kwargs.get("workspace_path")
        if not isinstance(path, str) or not path or path in seen:
            continue
        # Fully mocked execute paths use a sentinel that is not a real checkout.
        if path == "/tmp/workspace":
            continue
        seen.add(path)
        cleanup_workspace(path)


class GitRepoFixture:
    """Create a source repo and bare remote for workspace tests."""

    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.bare_remote = root / "remote.git"
        self.source_repo = root / "source"
        self.base_branch = "main"

        _run_git(["init", "--bare", str(self.bare_remote)])
        self.source_repo.mkdir()
        _run_git(["init", str(self.source_repo)])
        _run_git(
            ["-C", str(self.source_repo), "config", "user.email", "test@example.com"]
        )
        _run_git(["-C", str(self.source_repo), "config", "user.name", "Test User"])
        (self.source_repo / "README.md").write_text("initial\n", encoding="utf-8")
        _run_git(["-C", str(self.source_repo), "add", "README.md"])
        _run_git(["-C", str(self.source_repo), "commit", "-m", "init"])
        _run_git(
            [
                "-C",
                str(self.source_repo),
                "branch",
                "-M",
                self.base_branch,
            ]
        )
        _run_git(
            [
                "-C",
                str(self.source_repo),
                "remote",
                "add",
                "origin",
                str(self.bare_remote),
            ]
        )
        _run_git(["-C", str(self.source_repo), "push", "-u", "origin", self.base_branch])

        self._previous_repo_url = os.environ.get("MISSION_CONTROL_REPOSITORY_URL")
        self._previous_git_name = os.environ.get("MISSION_CONTROL_GIT_NAME")
        self._previous_git_email = os.environ.get("MISSION_CONTROL_GIT_EMAIL")
        self._previous_clone_depth = os.environ.get(WORKSPACE_CLONE_DEPTH_ENV)
        os.environ["MISSION_CONTROL_REPOSITORY_URL"] = str(self.bare_remote)
        os.environ["MISSION_CONTROL_GIT_NAME"] = "Test User"
        os.environ["MISSION_CONTROL_GIT_EMAIL"] = "test@example.com"

    def add_branch(self, branch: str, *, content: str = "branch\n") -> str:
        """Create and push ``branch`` from the current source tip; return tip SHA."""
        _run_git(["-C", str(self.source_repo), "checkout", "-B", branch])
        (self.source_repo / f"{branch.replace('/', '_')}.txt").write_text(
            content,
            encoding="utf-8",
        )
        _run_git(["-C", str(self.source_repo), "add", "-A"])
        _run_git(
            ["-C", str(self.source_repo), "commit", "-m", f"add {branch}"]
        )
        _run_git(
            [
                "-C",
                str(self.source_repo),
                "push",
                "-u",
                "origin",
                f"refs/heads/{branch}:refs/heads/{branch}",
            ]
        )
        tip = _run_git(
            ["-C", str(self.source_repo), "rev-parse", "HEAD"]
        ).stdout.strip()
        _run_git(
            ["-C", str(self.source_repo), "checkout", self.base_branch]
        )
        return tip

    def add_tag(
        self,
        tag: str,
        *,
        annotated: bool = False,
        message: str = "tag",
        content: str | None = None,
    ) -> tuple[str, str]:
        """Create and push ``tag``; return ``(commit_sha, tag_sha)``.

        For lightweight tags both SHAs are equal. For annotated tags
        ``tag_sha`` is the tag object and ``commit_sha`` is the peeled commit.
        """
        if content is not None:
            marker = self.source_repo / f"{tag.replace('/', '_')}.txt"
            marker.write_text(content, encoding="utf-8")
            _run_git(["-C", str(self.source_repo), "add", "-A"])
            _run_git(
                ["-C", str(self.source_repo), "commit", "-m", f"tag base {tag}"]
            )
            _run_git(
                [
                    "-C",
                    str(self.source_repo),
                    "push",
                    "origin",
                    self.base_branch,
                ]
            )
        if annotated:
            _run_git(
                [
                    "-C",
                    str(self.source_repo),
                    "tag",
                    "-a",
                    tag,
                    "-m",
                    message,
                ]
            )
        else:
            _run_git(["-C", str(self.source_repo), "tag", tag])
        _run_git(["-C", str(self.source_repo), "push", "origin", tag])
        commit_sha = _run_git(
            ["-C", str(self.source_repo), "rev-parse", f"{tag}^{{}}"]
        ).stdout.strip()
        tag_sha = _run_git(
            ["-C", str(self.source_repo), "rev-parse", tag]
        ).stdout.strip()
        return commit_sha, tag_sha

    def mission(
        self,
        *,
        persistence_mode: str | None = "push",
        platform_push_approved: bool | None = None,
        allow_automatic_platform_push: bool | None = None,
        platform_main_write_acknowledged: bool | None = None,
        target_branch: str | None = None,
        permissions_push: bool = False,
        include_default_push_target: bool = True,
        base_branch: str | None = None,
    ) -> dict:
        resolved_base = self.base_branch if base_branch is None else base_branch
        mission = {
            "mission_id": "2026-07-19-workspace",
            "repository": {
                "name": "test-repo",
                "path": str(self.source_repo),
                "base_branch": resolved_base,
            },
            "permissions": {
                "push": permissions_push,
            },
        }
        if persistence_mode is not None:
            persistence: dict[str, str] = {"mode": persistence_mode}
            if persistence_mode == "push" and include_default_push_target:
                persistence["target_branch"] = (
                    resolved_base if target_branch is None else target_branch
                )
            elif target_branch is not None:
                persistence["target_branch"] = target_branch
            mission["persistence"] = persistence
        approval: dict[str, bool] = {}
        if platform_push_approved is not None:
            approval["platform_push_approved"] = platform_push_approved
        if allow_automatic_platform_push is not None:
            approval["allow_automatic_platform_push"] = (
                allow_automatic_platform_push
            )
        if platform_main_write_acknowledged is not None:
            approval["platform_main_write_acknowledged"] = (
                platform_main_write_acknowledged
            )
        elif (
            persistence_mode == "push"
            and include_default_push_target
            and (
                platform_push_approved is True
                or allow_automatic_platform_push is True
            )
        ):
            # Fixture base_branch is main; approved push tests need the
            # distinct main-write acknowledgement unless a test overrides it.
            resolved_target = (
                resolved_base if target_branch is None else target_branch
            )
            if is_protected_default_branch(resolved_target):
                approval["platform_main_write_acknowledged"] = True
        if approval:
            mission["approval"] = approval
        return mission

    def cleanup(self) -> None:
        if self._previous_repo_url is None:
            os.environ.pop("MISSION_CONTROL_REPOSITORY_URL", None)
        else:
            os.environ["MISSION_CONTROL_REPOSITORY_URL"] = self._previous_repo_url

        if self._previous_git_name is None:
            os.environ.pop("MISSION_CONTROL_GIT_NAME", None)
        else:
            os.environ["MISSION_CONTROL_GIT_NAME"] = self._previous_git_name

        if self._previous_git_email is None:
            os.environ.pop("MISSION_CONTROL_GIT_EMAIL", None)
        else:
            os.environ["MISSION_CONTROL_GIT_EMAIL"] = self._previous_git_email

        if self._previous_clone_depth is None:
            os.environ.pop(WORKSPACE_CLONE_DEPTH_ENV, None)
        else:
            os.environ[WORKSPACE_CLONE_DEPTH_ENV] = self._previous_clone_depth

        self.temp.cleanup()


class TestOriginDiscovery(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitRepoFixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def test_get_origin_url_returns_configured_remote(self) -> None:
        origin = get_origin_url(str(self.fixture.source_repo))
        self.assertEqual(origin, str(self.fixture.bare_remote))

    def test_get_origin_url_returns_none_when_missing(self) -> None:
        repo = Path(self.fixture.temp.name) / "no-remote"
        repo.mkdir()
        _run_git(["init", str(repo)])
        self.assertIsNone(get_origin_url(str(repo)))


class TestWorkspacePreparation(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitRepoFixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def test_prepare_isolated_workspace_clones_and_configures_origin(self) -> None:
        prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None

        try:
            self.assertEqual(
                get_origin_url(prep.workspace_path),
                str(self.fixture.bare_remote),
            )
            configure = configure_workspace_origin(
                prep.workspace_path,
                "https://example.com/org/repo.git",
            )
            self.assertEqual(configure.returncode, 0)
            self.assertEqual(
                get_origin_url(prep.workspace_path),
                "https://example.com/org/repo.git",
            )
        finally:
            cleanup_workspace(prep.workspace_path)

    def test_prepare_isolated_workspace_fails_when_origin_missing(self) -> None:
        with patch.dict(os.environ, {"MISSION_CONTROL_REPOSITORY_URL": ""}, clear=False):
            prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertFalse(prep.ok)
        self.assertIn(
            "mission_control_repository_url",
            (prep.error or "").lower(),
        )


class TestShallowWorkspaceClone(unittest.TestCase):
    """Phase-3 shallow clone + HEAD verification + full-clone fallback."""

    def setUp(self) -> None:
        self.fixture = GitRepoFixture()
        # Path-style local clones skip shallow (Git ignores --depth and native
        # local clones are faster). Use file:// so depth-1 is actually applied.
        self._path_url = os.environ["MISSION_CONTROL_REPOSITORY_URL"]
        self._file_url = Path(self.fixture.bare_remote).resolve().as_uri()
        os.environ["MISSION_CONTROL_REPOSITORY_URL"] = self._file_url

    def tearDown(self) -> None:
        os.environ["MISSION_CONTROL_REPOSITORY_URL"] = self._path_url
        self.fixture.cleanup()

    def test_path_local_clone_skips_shallow(self) -> None:
        os.environ["MISSION_CONTROL_REPOSITORY_URL"] = self._path_url
        prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            self.assertEqual(prep.clone_strategy, CLONE_STRATEGY_FULL)
            is_shallow = _run_git(
                [
                    "-C",
                    prep.workspace_path,
                    "rev-parse",
                    "--is-shallow-repository",
                ]
            ).stdout.strip()
            self.assertEqual(is_shallow, "false")
        finally:
            cleanup_workspace(prep.workspace_path)

    def test_shallow_clone_main_matches_remote_tip(self) -> None:
        remote_tip = _run_git(
            [
                "-C",
                str(self.fixture.bare_remote),
                "rev-parse",
                self.fixture.base_branch,
            ]
        ).stdout.strip()
        prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            self.assertEqual(prep.clone_strategy, CLONE_STRATEGY_SHALLOW)
            self.assertEqual(prep.baseline_sha, remote_tip)
            head = _run_git(
                ["-C", prep.workspace_path, "rev-parse", "HEAD"]
            ).stdout.strip()
            self.assertEqual(head, remote_tip)
            is_shallow = _run_git(
                [
                    "-C",
                    prep.workspace_path,
                    "rev-parse",
                    "--is-shallow-repository",
                ]
            ).stdout.strip()
            self.assertEqual(is_shallow, "true")
            # Origin identity matches the path-form remote.
            self.assertEqual(
                normalize_remote_url_identity(
                    get_origin_url(prep.workspace_path) or ""
                ),
                normalize_remote_url_identity(self._path_url),
            )
        finally:
            cleanup_workspace(prep.workspace_path)

    def test_shallow_clone_non_main_base_branch(self) -> None:
        tip = self.fixture.add_branch("release/phase3", content="non-main\n")
        prep = prepare_isolated_workspace(
            self.fixture.mission(base_branch="release/phase3")
        )
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            self.assertEqual(prep.clone_strategy, CLONE_STRATEGY_SHALLOW)
            self.assertEqual(prep.baseline_sha, tip)
            branch = _run_git(
                ["-C", prep.workspace_path, "rev-parse", "--abbrev-ref", "HEAD"]
            ).stdout.strip()
            self.assertEqual(branch, "release/phase3")
            marker = Path(prep.workspace_path) / "release_phase3.txt"
            self.assertTrue(marker.is_file())
            self.assertEqual(marker.read_text(encoding="utf-8"), "non-main\n")
        finally:
            cleanup_workspace(prep.workspace_path)

    def test_missing_base_branch_fails_without_workspace(self) -> None:
        prep = prepare_isolated_workspace(
            self.fixture.mission(base_branch="does-not-exist")
        )
        self.assertFalse(prep.ok)
        self.assertIn("does-not-exist", prep.error or "")
        self.assertIsNone(prep.workspace_path)

    def test_force_full_clone_via_env(self) -> None:
        with patch.dict(
            os.environ,
            {WORKSPACE_CLONE_DEPTH_ENV: "full"},
            clear=False,
        ):
            self.assertIsNone(resolve_workspace_clone_depth())
            prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            self.assertEqual(prep.clone_strategy, CLONE_STRATEGY_FULL)
            is_shallow = _run_git(
                [
                    "-C",
                    prep.workspace_path,
                    "rev-parse",
                    "--is-shallow-repository",
                ]
            ).stdout.strip()
            self.assertEqual(is_shallow, "false")
        finally:
            cleanup_workspace(prep.workspace_path)

    def test_shallow_failure_falls_back_to_full_clone(self) -> None:
        real_clone = prepare_isolated_workspace.__globals__["_clone_at_base_branch"]
        calls: list[int | None] = []

        def _flaky_clone(*, depth: int | None, **kwargs):
            calls.append(depth)
            if depth is not None:
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=1,
                    stdout="",
                    stderr="shallow clone unsupported",
                )
            return real_clone(depth=depth, **kwargs)

        with patch(
            "mission_control.workspace._clone_at_base_branch",
            side_effect=_flaky_clone,
        ):
            prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            self.assertEqual(calls, [1, None])
            self.assertEqual(prep.clone_strategy, CLONE_STRATEGY_FULL)
            remote_tip = _run_git(
                [
                    "-C",
                    str(self.fixture.bare_remote),
                    "rev-parse",
                    self.fixture.base_branch,
                ]
            ).stdout.strip()
            self.assertEqual(prep.baseline_sha, remote_tip)
        finally:
            cleanup_workspace(prep.workspace_path)

    def test_head_mismatch_after_shallow_triggers_full_fallback(self) -> None:
        real_verify = prepare_isolated_workspace.__globals__[
            "_verify_workspace_head_matches_ref"
        ]
        calls = {"n": 0}

        def _verify_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return None, "Workspace HEAD does not match requested remote ref"
            return real_verify(*args, **kwargs)

        with patch(
            "mission_control.workspace._verify_workspace_head_matches_ref",
            side_effect=_verify_once,
        ):
            prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            self.assertEqual(calls["n"], 2)
            self.assertEqual(prep.clone_strategy, CLONE_STRATEGY_FULL)
        finally:
            cleanup_workspace(prep.workspace_path)

    def test_target_branch_persistence_from_shallow_workspace(self) -> None:
        target = "mission/phase3-persist"
        _run_git(
            [
                "-C",
                str(self.fixture.source_repo),
                "push",
                "origin",
                f"{self.fixture.base_branch}:{target}",
            ]
        )
        prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        self.assertEqual(prep.clone_strategy, CLONE_STRATEGY_SHALLOW)
        try:
            (Path(prep.workspace_path) / "phase3.txt").write_text(
                "persist\n",
                encoding="utf-8",
            )
            with patch(
                "mission_control.workspace._github_push_environment",
                return_value=(os.environ.copy(), None),
            ):
                result = persist_workspace_changes(
                    "run-shallow-persist",
                    self.fixture.mission(
                        persistence_mode="push",
                        platform_push_approved=True,
                        platform_main_write_acknowledged=False,
                        target_branch=target,
                    ),
                    prep.workspace_path,
                    baseline_sha=prep.baseline_sha,
                )
            self.assertTrue(result.ok, result.error)
            self.assertTrue(result.pushed)
            self.assertEqual(result.target_branch, target)
            remote_tip = _run_git(
                ["-C", str(self.fixture.bare_remote), "rev-parse", target]
            ).stdout.strip()
            self.assertEqual(remote_tip, result.commit_sha)
            main_tip = _run_git(
                [
                    "-C",
                    str(self.fixture.bare_remote),
                    "rev-parse",
                    self.fixture.base_branch,
                ]
            ).stdout.strip()
            self.assertEqual(main_tip, prep.baseline_sha)
            self.assertNotEqual(main_tip, result.commit_sha)
        finally:
            cleanup_workspace(prep.workspace_path)


class TestTagRefResolution(unittest.TestCase):
    """Annotated/lightweight tags, collisions, peel gaps, and movement."""

    def setUp(self) -> None:
        self.fixture = GitRepoFixture()
        self._path_url = os.environ["MISSION_CONTROL_REPOSITORY_URL"]
        self._file_url = Path(self.fixture.bare_remote).resolve().as_uri()
        os.environ["MISSION_CONTROL_REPOSITORY_URL"] = self._file_url

    def tearDown(self) -> None:
        os.environ["MISSION_CONTROL_REPOSITORY_URL"] = self._path_url
        self.fixture.cleanup()

    def test_annotated_tag_resolves_to_peeled_commit(self) -> None:
        commit_sha, tag_sha = self.fixture.add_tag(
            "v-annot",
            annotated=True,
            message="annotated release",
            content="annot\n",
        )
        self.assertNotEqual(commit_sha, tag_sha)
        resolved, err, missing = _ls_remote_branch_sha(
            self._file_url, "v-annot", env=None
        )
        self.assertIsNone(err)
        self.assertFalse(missing)
        self.assertEqual(resolved, commit_sha)
        self.assertNotEqual(resolved, tag_sha)

        prep = prepare_isolated_workspace(
            self.fixture.mission(base_branch="v-annot")
        )
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            self.assertEqual(prep.baseline_sha, commit_sha)
            head = _run_git(
                ["-C", prep.workspace_path, "rev-parse", "HEAD"]
            ).stdout.strip()
            self.assertEqual(head, commit_sha)
            self.assertNotEqual(head, tag_sha)
        finally:
            cleanup_workspace(prep.workspace_path)

    def test_lightweight_tag_resolves_to_commit(self) -> None:
        commit_sha, tag_sha = self.fixture.add_tag(
            "v-lite",
            annotated=False,
            content="lite\n",
        )
        self.assertEqual(commit_sha, tag_sha)
        resolved, err, missing = _ls_remote_branch_sha(
            self._file_url, "v-lite", env=None
        )
        self.assertIsNone(err)
        self.assertFalse(missing)
        self.assertEqual(resolved, commit_sha)

        prep = prepare_isolated_workspace(
            self.fixture.mission(base_branch="v-lite")
        )
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            self.assertEqual(prep.baseline_sha, commit_sha)
            head = _run_git(
                ["-C", prep.workspace_path, "rev-parse", "HEAD"]
            ).stdout.strip()
            self.assertEqual(head, commit_sha)
        finally:
            cleanup_workspace(prep.workspace_path)

    def test_branch_wins_over_same_named_tag(self) -> None:
        tag_commit, _tag_sha = self.fixture.add_tag(
            "release",
            annotated=True,
            message="tag side",
            content="from-tag\n",
        )
        branch_tip = self.fixture.add_branch(
            "release", content="from-branch\n"
        )
        self.assertNotEqual(tag_commit, branch_tip)
        resolved, err, missing = _ls_remote_branch_sha(
            self._file_url, "release", env=None
        )
        self.assertIsNone(err)
        self.assertFalse(missing)
        self.assertEqual(resolved, branch_tip)

        prep = prepare_isolated_workspace(
            self.fixture.mission(base_branch="release")
        )
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            self.assertEqual(prep.baseline_sha, branch_tip)
            marker = Path(prep.workspace_path) / "release.txt"
            self.assertEqual(marker.read_text(encoding="utf-8"), "from-branch\n")
        finally:
            cleanup_workspace(prep.workspace_path)

    def test_annotated_tag_missing_peel_advertisement_fails_closed(self) -> None:
        commit_sha, tag_sha = self.fixture.add_tag(
            "v-nopeel",
            annotated=True,
            message="no peel",
            content="nopeel\n",
        )
        self.assertNotEqual(commit_sha, tag_sha)
        real_run_git = prepare_isolated_workspace.__globals__["_run_git"]

        def _ls_remote_without_peel(args, **kwargs):
            if args and args[0] == "ls-remote":
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout=f"{tag_sha}\trefs/tags/v-nopeel\n",
                    stderr="",
                )
            return real_run_git(args, **kwargs)

        with patch(
            "mission_control.workspace._run_git",
            side_effect=_ls_remote_without_peel,
        ):
            prep = prepare_isolated_workspace(
                self.fixture.mission(base_branch="v-nopeel")
            )
        self.assertFalse(prep.ok)
        self.assertIsNone(prep.workspace_path)
        self.assertIn("does not match", (prep.error or "").lower())

    def test_remote_movement_between_ls_remote_and_clone_fails(self) -> None:
        stale = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        with patch(
            "mission_control.workspace._ls_remote_branch_sha",
            return_value=(stale, None, False),
        ):
            prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertFalse(prep.ok)
        self.assertIsNone(prep.workspace_path)
        error = (prep.error or "").lower()
        self.assertTrue(
            "does not match" in error or "failed to clone" in error,
            prep.error,
        )

    def test_annotated_tag_shallow_failure_falls_back_to_full(self) -> None:
        commit_sha, tag_sha = self.fixture.add_tag(
            "v-fallback",
            annotated=True,
            message="fallback",
            content="fallback\n",
        )
        self.assertNotEqual(commit_sha, tag_sha)
        real_clone = prepare_isolated_workspace.__globals__["_clone_at_base_branch"]
        calls: list[int | None] = []

        def _flaky_clone(*, depth: int | None, **kwargs):
            calls.append(depth)
            if depth is not None:
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=1,
                    stdout="",
                    stderr="shallow clone unsupported for tag",
                )
            return real_clone(depth=depth, **kwargs)

        with patch(
            "mission_control.workspace._clone_at_base_branch",
            side_effect=_flaky_clone,
        ):
            prep = prepare_isolated_workspace(
                self.fixture.mission(base_branch="v-fallback")
            )
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            self.assertEqual(calls, [1, None])
            self.assertEqual(prep.clone_strategy, CLONE_STRATEGY_FULL)
            self.assertEqual(prep.baseline_sha, commit_sha)
        finally:
            cleanup_workspace(prep.workspace_path)


class TestCloneErrorRedaction(unittest.TestCase):
    """Credential-safe workspace preparation / clone / ref errors."""

    def setUp(self) -> None:
        self.fixture = GitRepoFixture()
        self._path_url = os.environ["MISSION_CONTROL_REPOSITORY_URL"]
        self._file_url = Path(self.fixture.bare_remote).resolve().as_uri()
        os.environ["MISSION_CONTROL_REPOSITORY_URL"] = self._file_url

    def tearDown(self) -> None:
        os.environ["MISSION_CONTROL_REPOSITORY_URL"] = self._path_url
        self.fixture.cleanup()

    def test_redact_https_userinfo_and_encoded_credentials(self) -> None:
        raw = (
            "clone failed for "
            "https://user:s3cret-pass@github.com/org/repo.git "
            "and https://user%3Aname:%70%61%73%73@example.com/r.git"
        )
        redacted = _redact_secret_text(raw)
        self.assertNotIn("s3cret-pass", redacted)
        self.assertNotIn("%70%61%73%73", redacted)
        self.assertNotIn("user%3Aname", redacted)
        self.assertIn("https://***@", redacted)
        self.assertIn("github.com/org/repo.git", redacted)

    def test_redact_token_query_and_bearer_stderr(self) -> None:
        raw = (
            "fatal: unable to access "
            "'https://github.com/org/repo.git?access_token=ghs_LEAK1234567890abcd"
            "&token=abc123': "
            "Authorization: Bearer ghs_LEAK1234567890abcd "
            "authorization=ghs_LEAK1234567890abcd"
        )
        redacted = _redact_secret_text(raw)
        self.assertNotIn("ghs_LEAK1234567890abcd", redacted)
        self.assertNotIn("token=abc123", redacted)
        self.assertIn("access_token=***", redacted)
        self.assertIn("Bearer ***", redacted)

    def test_redact_basic_authorization_credential(self) -> None:
        """Basic must redact the credential, not only the scheme word."""
        sentinel = "dXNlcjpTRU5USU5FTF9CQVNJQ19TRUNSRVQ="
        cases = (
            f"Authorization: Basic {sentinel}",
            f"AUTHORIZATION: BASIC {sentinel}",
            f"authorization = Basic  {sentinel}",
            f"authorization:basic {sentinel}",
            f'Authorization: "Basic {sentinel}"',
            f"Authorization: Basic {sentinel}.",
            f"Authorization: Basic {sentinel})",
            f"Authorization:\nBasic\t{sentinel}",
            f"Authorization: Basic {sentinel} and Authorization: Basic {sentinel}",
            f"Authorization: Basic !!not-base64!!{sentinel}",
            "Authorization: Basic",
            "Authorization: Basic ",
        )
        for raw in cases:
            with self.subTest(raw=raw):
                redacted = _redact_secret_text(raw)
                self.assertNotIn(sentinel, redacted)
                self.assertNotIn("SENTINEL_BASIC_SECRET", redacted)
                if sentinel in raw or "!!not-base64!!" in raw:
                    self.assertRegex(redacted, r"(?i)basic\s+\*\*\*")
                    self.assertNotRegex(
                        redacted,
                        r"(?i)Authorization:\s*\*\*\*\s+\S+",
                    )

    def test_argv_safe_url_strips_userinfo_and_secret_query(self) -> None:
        safe, userinfo = _argv_safe_repository_url(
            "https://x-access-token:ghs_SECRET@github.com/org/repo.git"
            "?token=leak&path=src"
        )
        self.assertEqual(userinfo, "x-access-token:ghs_SECRET")
        self.assertNotIn("ghs_SECRET", safe)
        self.assertNotIn("token=leak", safe)
        self.assertIn("github.com/org/repo.git", safe)
        self.assertIn("path=src", safe)

    def test_clone_failure_redacts_credential_url_and_stderr(self) -> None:
        secret_url = (
            "https://user:passw0rd_LEAK@github.com/org/private.git?token=leak_token"
        )
        os.environ["MISSION_CONTROL_REPOSITORY_URL"] = secret_url
        captured_argv: list[list[str]] = []

        def _fake_run_git(args, **kwargs):
            captured_argv.append(list(args))
            if args and args[0] == "ls-remote":
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout=(
                        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\t"
                        "refs/heads/main\n"
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=args,
                returncode=1,
                stdout="",
                stderr=(
                    f"fatal: could not read Username for '{secret_url}': "
                    "Authorization: Bearer ghs_NESTED_SECRET "
                    "Authorization: Basic dXNlcjpORVNURURfQkFTSUNfU0VDUkVU "
                    "x-access-token:ghs_NESTED_SECRET"
                ),
            )

        with patch(
            "mission_control.workspace._run_git",
            side_effect=_fake_run_git,
        ):
            prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertFalse(prep.ok)
        error = prep.error or ""
        self.assertNotIn("passw0rd_LEAK", error)
        self.assertNotIn("leak_token", error)
        self.assertNotIn("ghs_NESTED_SECRET", error)
        self.assertNotIn("NESTED_BASIC_SECRET", error)
        self.assertNotIn("dXNlcjpORVNURURfQkFTSUNfU0VDUkVU", error)
        self.assertIn("https://***@", error)
        self.assertIn("Basic ***", error)
        for args in captured_argv:
            joined = " ".join(args)
            self.assertNotIn("passw0rd_LEAK", joined)
            self.assertNotIn("leak_token", joined)

    def test_ls_remote_failure_redacts_secrets(self) -> None:
        secret_url = "https://bot:tok_VALUE@github.com/org/repo.git"
        os.environ["MISSION_CONTROL_REPOSITORY_URL"] = secret_url

        def _fake_run_git(args, **kwargs):
            if args and args[0] == "ls-remote":
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=128,
                    stdout="",
                    stderr=(
                        f"fatal: Authentication failed for '{secret_url}' "
                        "Bearer tok_VALUE"
                    ),
                )
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="unexpected"
            )

        with patch(
            "mission_control.workspace._run_git",
            side_effect=_fake_run_git,
        ):
            # Soft ls-remote failure still attempts clone; force clone fail too.
            prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertFalse(prep.ok)
        error = prep.error or ""
        self.assertNotIn("tok_VALUE", error)
        self.assertNotIn("bot:tok_VALUE", error)

    def test_fallback_failure_redacts_nested_exception_text(self) -> None:
        secret = "super_secret_clone_token_xyz"
        os.environ["MISSION_CONTROL_REPOSITORY_URL"] = (
            f"https://user:{secret}@example.com/r.git"
        )

        def _always_fail(*, depth: int | None = None, **kwargs):
            return subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr=(
                    f"CalledProcessError: clone failed nested "
                    f"https://user:{secret}@example.com/r.git "
                    f"Authorization: Bearer {secret}"
                ),
            )

        with patch(
            "mission_control.workspace._ls_remote_branch_sha",
            return_value=(
                "cccccccccccccccccccccccccccccccccccccccc",
                None,
                False,
            ),
        ), patch(
            "mission_control.workspace._clone_at_base_branch",
            side_effect=_always_fail,
        ):
            prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertFalse(prep.ok)
        error = prep.error or ""
        self.assertNotIn(secret, error)
        self.assertIn("https://***@", error)

    def test_ephemeral_checkout_errors_are_redacted(self) -> None:
        secret_url = "https://user:ephem_SECRET@github.com/org/repo.git"
        with patch(
            "mission_control.workspace._run_git",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr=f"fatal: '{secret_url}' Bearer ephem_SECRET",
            ),
        ):
            prep = prepare_ephemeral_checkout(
                repository_url=secret_url, ref="main"
            )
        self.assertFalse(prep.ok)
        error = prep.error or ""
        self.assertNotIn("ephem_SECRET", error)
        self.assertNotIn("user:ephem_SECRET", error)

    def test_missing_ref_error_redacts_credential_url(self) -> None:
        secret_url = (
            "https://user:missing_ref_SECRET@github.com/org/repo.git"
            "?access_token=missing_ref_SECRET"
        )
        os.environ["MISSION_CONTROL_REPOSITORY_URL"] = secret_url
        with patch(
            "mission_control.workspace._ls_remote_branch_sha",
            return_value=(
                None,
                _redact_secret_text(
                    f"Remote ref 'refs/heads/missing' (or tag) not found at "
                    f"{secret_url}"
                ),
                True,
            ),
        ):
            prep = prepare_isolated_workspace(
                self.fixture.mission(base_branch="missing")
            )
        self.assertFalse(prep.ok)
        error = prep.error or ""
        self.assertNotIn("missing_ref_SECRET", error)
        self.assertIn("missing", error)


class TestWorkspacePersistence(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitRepoFixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def _prepare_workspace(self) -> str:
        prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        return prep.workspace_path

    def test_persist_workspace_changes_with_no_changes(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            result = persist_workspace_changes(
                "run-no-change",
                self.fixture.mission(
                    persistence_mode="push",
                    platform_push_approved=True,
                ),
                workspace_path,
            )
            self.assertTrue(result.ok, result.error)
            self.assertIsNone(result.commit_sha)
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_mode_none_invokes_no_git_add_commit_or_push(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            with patch("mission_control.workspace._run_git") as mock_git:
                result = persist_workspace_changes(
                    "run-none",
                    self.fixture.mission(persistence_mode="none"),
                    workspace_path,
                )
            self.assertTrue(result.ok, result.error)
            self.assertIsNone(result.commit_sha)
            self.assertEqual(result.mode, "none")
            self.assertFalse(result.pushed)
            mock_git.assert_not_called()
            status = _run_git(["-C", workspace_path, "status", "--porcelain"])
            self.assertIn("created.txt", status.stdout)
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_omitted_persistence_defaults_to_none(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            with patch("mission_control.workspace._run_git") as mock_git:
                result = persist_workspace_changes(
                    "run-default-none",
                    self.fixture.mission(persistence_mode=None),
                    workspace_path,
                )
            self.assertTrue(result.ok, result.error)
            self.assertIsNone(result.commit_sha)
            self.assertEqual(result.mode, "none")
            self.assertFalse(result.pushed)
            mock_git.assert_not_called()
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_mode_commit_never_pushes(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            remote_before = _run_git(
                [
                    "-C",
                    str(self.fixture.bare_remote),
                    "rev-parse",
                    self.fixture.base_branch,
                ]
            ).stdout.strip()

            recorded_args: list[list[str]] = []
            real_run_git = persist_workspace_changes.__globals__["_run_git"]

            def tracking_run_git(
                args: list[str],
                *,
                env: dict[str, str] | None = None,
            ) -> subprocess.CompletedProcess[str]:
                recorded_args.append(list(args))
                return real_run_git(args, env=env)

            with patch(
                "mission_control.workspace._run_git",
                side_effect=tracking_run_git,
            ):
                result = persist_workspace_changes(
                    "run-commit-only",
                    self.fixture.mission(persistence_mode="commit"),
                    workspace_path,
                )

            self.assertTrue(result.ok, result.error)
            self.assertIsNotNone(result.commit_sha)
            self.assertEqual(result.mode, "commit")
            self.assertFalse(result.pushed)
            self.assertFalse(
                any("push" in args for args in recorded_args),
                recorded_args,
            )

            remote_after = _run_git(
                [
                    "-C",
                    str(self.fixture.bare_remote),
                    "rev-parse",
                    self.fixture.base_branch,
                ]
            ).stdout.strip()
            self.assertEqual(remote_before, remote_after)
            self.assertNotEqual(remote_after, result.commit_sha)
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_workspace_changes_commits_and_pushes(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )

            with patch(
                "mission_control.workspace._github_push_environment",
                return_value=(os.environ.copy(), None),
            ):
                result = persist_workspace_changes(
                    "run-with-change",
                    self.fixture.mission(
                        persistence_mode="push",
                        platform_push_approved=True,
                    ),
                    workspace_path,
                )
            self.assertTrue(result.ok, result.error)
            self.assertIsNotNone(result.commit_sha)
            self.assertEqual(result.mode, "push")
            self.assertTrue(result.pushed)

            remote_head = _run_git(
                [
                    "-C",
                    str(self.fixture.bare_remote),
                    "rev-parse",
                    self.fixture.base_branch,
                ]
            )
            self.assertEqual(remote_head.stdout.strip(), result.commit_sha)
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_push_rejected_without_platform_push_approval(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            with patch("mission_control.workspace._run_git") as mock_git:
                result = persist_workspace_changes(
                    "run-push-unapproved",
                    self.fixture.mission(persistence_mode="push"),
                    workspace_path,
                )
            self.assertFalse(result.ok)
            self.assertEqual(result.mode, "push")
            self.assertFalse(result.pushed)
            self.assertTrue(
                (result.error or "").startswith("PLATFORM_PUSH_APPROVAL_REQUIRED"),
                result.error,
            )
            mock_git.assert_not_called()
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_push_succeeds_when_platform_push_approved(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            with patch(
                "mission_control.workspace._github_push_environment",
                return_value=(os.environ.copy(), None),
            ):
                result = persist_workspace_changes(
                    "run-push-approved",
                    self.fixture.mission(
                        persistence_mode="push",
                        platform_push_approved=True,
                    ),
                    workspace_path,
                )
            self.assertTrue(result.ok, result.error)
            self.assertIsNotNone(result.commit_sha)
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_push_succeeds_with_automatic_platform_push_policy(
        self,
    ) -> None:
        workspace_path = self._prepare_workspace()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            with patch(
                "mission_control.workspace._github_push_environment",
                return_value=(os.environ.copy(), None),
            ):
                result = persist_workspace_changes(
                    "run-push-auto-policy",
                    self.fixture.mission(
                        persistence_mode="push",
                        allow_automatic_platform_push=True,
                    ),
                    workspace_path,
                )
            self.assertTrue(result.ok, result.error)
            self.assertIsNotNone(result.commit_sha)
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_mode_none_does_not_require_platform_push_approval(
        self,
    ) -> None:
        workspace_path = self._prepare_workspace()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            mission = self.fixture.mission(persistence_mode="none")
            self.assertIsNone(require_platform_push_approval(mission))
            with patch("mission_control.workspace._run_git") as mock_git:
                result = persist_workspace_changes(
                    "run-none-no-approval",
                    mission,
                    workspace_path,
                )
            self.assertTrue(result.ok, result.error)
            mock_git.assert_not_called()
        finally:
            cleanup_workspace(workspace_path)

    def test_agent_permissions_push_does_not_authorize_platform_push(
        self,
    ) -> None:
        workspace_path = self._prepare_workspace()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            mission = self.fixture.mission(
                persistence_mode="push",
                permissions_push=True,
            )
            self.assertFalse(is_platform_push_authorized(mission))
            with patch("mission_control.workspace._run_git") as mock_git:
                result = persist_workspace_changes(
                    "run-agent-push-not-enough",
                    mission,
                    workspace_path,
                )
            self.assertFalse(result.ok)
            self.assertEqual(result.error, PLATFORM_PUSH_APPROVAL_REQUIRED)
            mock_git.assert_not_called()
        finally:
            cleanup_workspace(workspace_path)

    def test_persistence_layer_enforces_approval_independently(self) -> None:
        """Boundary check rejects push even if a caller skipped queue validation."""
        workspace_path = self._prepare_workspace()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            mission = self.fixture.mission(persistence_mode="push")
            # Simulate a caller that did not run validate_mission_for_execute.
            self.assertFalse(is_platform_push_authorized(mission))
            with patch("mission_control.workspace._run_git") as mock_git:
                result = persist_workspace_changes(
                    "run-boundary-only",
                    mission,
                    workspace_path,
                )
            self.assertFalse(result.ok)
            self.assertTrue(
                (result.error or "").startswith("PLATFORM_PUSH_APPROVAL_REQUIRED"),
                result.error,
            )
            mock_git.assert_not_called()
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_workspace_changes_commit_failure(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            baseline = _run_git(
                ["-C", workspace_path, "rev-parse", "HEAD"]
            ).stdout.strip()
            real_run_git = persist_workspace_changes.__globals__["_run_git"]

            def fake_run_git(
                args: list[str],
                *,
                env: dict[str, str] | None = None,
            ) -> subprocess.CompletedProcess[str]:
                if "commit" in args:
                    return subprocess.CompletedProcess(
                        args=args,
                        returncode=1,
                        stdout="",
                        stderr="commit failed",
                    )
                return real_run_git(args, env=env)

            with patch(
                "mission_control.workspace._github_push_environment",
                return_value=(os.environ.copy(), None),
            ), patch(
                "mission_control.workspace._run_git",
                side_effect=fake_run_git,
            ):
                (Path(workspace_path) / "README.md").write_text(
                    "changed\n",
                    encoding="utf-8",
                )
                result = persist_workspace_changes(
                    "run-commit-fail",
                    self.fixture.mission(
                        persistence_mode="push",
                        platform_push_approved=True,
                    ),
                    workspace_path,
                    baseline_sha=baseline,
                )
            self.assertFalse(result.ok)
            self.assertIn("commit failed", result.error or "")
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_workspace_changes_push_failure(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            baseline = _run_git(
                ["-C", workspace_path, "rev-parse", "HEAD"]
            ).stdout.strip()
            real_run_git = persist_workspace_changes.__globals__["_run_git"]

            def fake_run_git(
                args: list[str],
                *,
                env: dict[str, str] | None = None,
            ) -> subprocess.CompletedProcess[str]:
                if "push" in args:
                    return subprocess.CompletedProcess(
                        args=args,
                        returncode=1,
                        stdout="",
                        stderr="push rejected",
                    )
                return real_run_git(args, env=env)

            with patch(
                "mission_control.workspace._github_push_environment",
                return_value=(os.environ.copy(), None),
            ), patch(
                "mission_control.workspace._run_git",
                side_effect=fake_run_git,
            ):
                (Path(workspace_path) / "README.md").write_text(
                    "changed\n",
                    encoding="utf-8",
                )
                result = persist_workspace_changes(
                    "run-push-fail",
                    self.fixture.mission(
                        persistence_mode="push",
                        platform_push_approved=True,
                    ),
                    workspace_path,
                    baseline_sha=baseline,
                )
            self.assertFalse(result.ok)
            self.assertIn("push rejected", result.error or "")
        finally:
            cleanup_workspace(workspace_path)

    def test_persist_unsupported_mode_fails_inside_persist(self) -> None:
        workspace_path = self._prepare_workspace()
        try:
            with patch("mission_control.workspace._run_git") as mock_git:
                result = persist_workspace_changes(
                    "run-bad-mode",
                    self.fixture.mission(persistence_mode="rebase"),
                    workspace_path,
                )
            self.assertFalse(result.ok)
            self.assertIn("Unsupported persistence.mode", result.error or "")
            mock_git.assert_not_called()
        finally:
            cleanup_workspace(workspace_path)


class TestDeclaredFileDeliverables(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        (self.workspace / "README.md").write_text("ok\n", encoding="utf-8")
        (self.workspace / "docs").mkdir()
        (self.workspace / "docs" / "out.txt").write_text("out\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_looks_like_file_path_deliverable_detection(self) -> None:
        self.assertTrue(looks_like_file_path_deliverable("MISSION_SPEC.md"))
        self.assertTrue(looks_like_file_path_deliverable("docs/out.txt"))
        self.assertTrue(looks_like_file_path_deliverable("src/app.py"))
        self.assertTrue(looks_like_file_path_deliverable("/etc/passwd"))
        self.assertTrue(looks_like_file_path_deliverable("../outside.txt"))
        self.assertTrue(looks_like_file_path_deliverable("docs/subdir/file"))

        self.assertFalse(looks_like_file_path_deliverable("summary"))
        self.assertFalse(looks_like_file_path_deliverable("report"))
        self.assertFalse(looks_like_file_path_deliverable("confirmation"))
        self.assertFalse(looks_like_file_path_deliverable("repository status"))
        self.assertFalse(
            looks_like_file_path_deliverable(
                "API/OpenAPI documentation updates"
            )
        )
        self.assertFalse(looks_like_file_path_deliverable(""))

    def test_file_path_from_deliverable_typed_and_string_forms(self) -> None:
        self.assertEqual(
            file_path_from_deliverable("docs/out.txt"),
            "docs/out.txt",
        )
        self.assertIsNone(
            file_path_from_deliverable("API/OpenAPI documentation updates")
        )
        self.assertEqual(
            file_path_from_deliverable({"file": "docs/out.txt"}),
            "docs/out.txt",
        )
        self.assertEqual(
            file_path_from_deliverable(
                {"kind": "file", "path": "mission_control/workspace.py"}
            ),
            "mission_control/workspace.py",
        )
        self.assertIsNone(
            file_path_from_deliverable(
                {"description": "API/OpenAPI documentation updates"}
            )
        )
        self.assertIsNone(
            file_path_from_deliverable(
                {
                    "kind": "descriptive",
                    "text": "API/OpenAPI documentation updates",
                }
            )
        )
        self.assertIsNone(file_path_from_deliverable({"unknown": True}))

    def test_existing_declared_file_deliverable_passes(self) -> None:
        mission = {"deliverables": ["README.md", "docs/out.txt"]}
        self.assertIsNone(
            verify_declared_file_deliverables(mission, str(self.workspace))
        )

    def test_missing_declared_file_deliverable_fails(self) -> None:
        mission = {"deliverables": ["missing-output.txt"]}
        error = verify_declared_file_deliverables(mission, str(self.workspace))
        self.assertEqual(
            error,
            "Missing declared file deliverable: missing-output.txt",
        )

    def test_multiple_file_deliverables_identify_missing_item(self) -> None:
        mission = {
            "deliverables": [
                "README.md",
                "docs/out.txt",
                "docs/missing.txt",
            ]
        }
        error = verify_declared_file_deliverables(mission, str(self.workspace))
        self.assertEqual(
            error,
            "Missing declared file deliverable: docs/missing.txt",
        )

    def test_descriptive_only_deliverables_preserve_current_behavior(self) -> None:
        mission = {
            "deliverables": [
                "summary",
                "report",
                "confirmation",
                "repository status",
            ]
        }
        self.assertIsNone(
            verify_declared_file_deliverables(mission, str(self.workspace))
        )

    def test_slash_containing_descriptive_deliverable_does_not_fail(
        self,
    ) -> None:
        """Regression: prose with '/' must not be treated as a file path."""
        mission = {
            "deliverables": [
                "API/OpenAPI documentation updates",
                "summary",
            ]
        }
        self.assertIsNone(
            verify_declared_file_deliverables(mission, str(self.workspace))
        )
        evidence = collect_deliverable_evidence(mission, str(self.workspace))
        self.assertTrue(evidence.passed)
        self.assertEqual(evidence.checked_paths, [])
        self.assertEqual(evidence.missing, [])

    def test_typed_descriptive_deliverable_does_not_fail(self) -> None:
        mission = {
            "deliverables": [
                {"description": "API/OpenAPI documentation updates"},
                {"kind": "descriptive", "text": "release notes"},
            ]
        }
        self.assertIsNone(
            verify_declared_file_deliverables(mission, str(self.workspace))
        )

    def test_typed_file_deliverable_missing_still_fails(self) -> None:
        mission = {
            "deliverables": [
                {"file": "missing-typed.txt"},
                {"description": "API/OpenAPI documentation updates"},
            ]
        }
        error = verify_declared_file_deliverables(mission, str(self.workspace))
        self.assertEqual(
            error,
            "Missing declared file deliverable: missing-typed.txt",
        )

    def test_typed_file_deliverable_existing_passes(self) -> None:
        mission = {
            "deliverables": [
                {"file": "README.md"},
                {"kind": "file", "path": "docs/out.txt"},
                {"description": "API/OpenAPI documentation updates"},
            ]
        }
        self.assertIsNone(
            verify_declared_file_deliverables(mission, str(self.workspace))
        )
        evidence = collect_deliverable_evidence(mission, str(self.workspace))
        self.assertTrue(evidence.passed)
        self.assertEqual(
            evidence.checked_paths,
            ["README.md", "docs/out.txt"],
        )

    def test_empty_deliverables_preserve_current_behavior(self) -> None:
        self.assertIsNone(
            verify_declared_file_deliverables(
                {"deliverables": []},
                str(self.workspace),
            )
        )
        self.assertIsNone(
            verify_declared_file_deliverables({}, str(self.workspace))
        )

    def test_unsafe_escaping_paths_are_not_read_outside_workspace(self) -> None:
        outside = Path(self.temp.name) / "outside_secret.txt"
        outside.write_text("secret\n", encoding="utf-8")
        workspace = self.workspace.resolve()
        real_is_file = Path.is_file

        def guarded_is_file(self: Path) -> bool:
            resolved = self if self.is_absolute() else (Path.cwd() / self)
            resolved = resolved.resolve()
            try:
                resolved.relative_to(workspace)
            except ValueError as exc:
                raise AssertionError(
                    f"attempted filesystem check outside workspace: {resolved}"
                ) from exc
            return real_is_file(self)

        mission = {
            "deliverables": [
                str(outside),
                f"../{outside.name}",
                "/etc/passwd",
                "~/secret.txt",
            ]
        }
        with patch.object(Path, "is_file", guarded_is_file):
            error = verify_declared_file_deliverables(
                mission, str(self.workspace)
            )
            self.assertEqual(
                error,
                f"Declared file deliverable outside workspace: {outside}",
            )
            evidence = collect_deliverable_evidence(
                mission, str(self.workspace)
            )
            self.assertTrue(evidence.verified)
            self.assertFalse(evidence.passed)
            self.assertEqual(evidence.checked_paths, [])
            self.assertEqual(evidence.missing, [])
            self.assertEqual(
                evidence.outside_workspace,
                [
                    str(outside),
                    f"../{outside.name}",
                    "/etc/passwd",
                    "~/secret.txt",
                ],
            )
            self.assertIsNone(
                resolve_safe_workspace_deliverable(
                    str(self.workspace),
                    f"../{outside.name}",
                )
            )
            self.assertIsNone(
                resolve_safe_workspace_deliverable(str(self.workspace), str(outside))
            )


class TestExecuteRegisteredRun(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitRepoFixture()
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)
        # Mocked /tmp/workspace paths are not git repos; skip push-deny
        # config that now runs before mocked persistence.
        self._disable_push_patcher = patch(
            "mission_control.workspace.disable_agent_git_push",
            return_value=None,
        )
        self._disable_push_patcher.start()
        self.addCleanup(self._disable_push_patcher.stop)

    def tearDown(self) -> None:
        self.fixture.cleanup()
        self.registry.close()
        os.unlink(self._db_path)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    def test_execute_registered_run_stores_commit_sha(
        self,
        mock_prepare,
        mock_execute,
        mock_persist,
        mock_cleanup,
    ) -> None:
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path="/tmp/workspace",
        )
        mock_execute.return_value = ExecutionResult(ok=True, stdout="done\n")
        mock_persist.return_value = PersistenceResult(
            ok=True,
            commit_sha="abc123def456",
        )

        record = self.registry.create_run()
        execute_registered_run(record.run_id, self.fixture.mission(), self.registry)

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        self.assertEqual(updated.status, RunStatus.COMPLETED)
        self.assertEqual(updated.commit_sha, "abc123def456")
        mock_cleanup.assert_called_once_with("/tmp/workspace")

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    def test_execute_registered_run_marks_commit_failure_as_failed(
        self,
        mock_prepare,
        mock_execute,
        mock_persist,
        mock_cleanup,
    ) -> None:
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path="/tmp/workspace",
        )
        mock_execute.return_value = ExecutionResult(ok=True, stdout="done\n")
        mock_persist.return_value = PersistenceResult(
            ok=False,
            error="git commit failed",
        )

        record = self.registry.create_run()
        execute_registered_run(record.run_id, self.fixture.mission(), self.registry)

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        self.assertEqual(updated.status, RunStatus.FAILED)
        self.assertEqual(updated.error, "git commit failed")
        mock_cleanup.assert_called_once_with("/tmp/workspace")

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    def test_execute_registered_run_cleans_up_after_execution_failure(
        self,
        mock_prepare,
        mock_cleanup,
    ) -> None:
        mock_prepare.return_value = WorkspacePrepResult(
            ok=True,
            workspace_path="/tmp/workspace",
        )

        with patch(
            "mission_control.workspace.execute_cursor_agent",
            return_value=ExecutionResult(ok=False, error="agent failed"),
        ):
            record = self.registry.create_run()
            execute_registered_run(
                record.run_id,
                self.fixture.mission(),
                self.registry,
            )

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        self.assertEqual(updated.status, RunStatus.FAILED)
        mock_cleanup.assert_called_once_with("/tmp/workspace")

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.prepare_isolated_workspace")
    def test_execute_registered_run_fails_when_origin_missing(
        self,
        mock_prepare,
        mock_cleanup,
    ) -> None:
        mock_prepare.return_value = WorkspacePrepResult(
            ok=False,
            error="MISSION_CONTROL_REPOSITORY_URL is not configured.",
        )

        record = self.registry.create_run()
        execute_registered_run(record.run_id, self.fixture.mission(), self.registry)

        updated = self.registry.get_run(record.run_id)
        assert updated is not None
        self.assertEqual(updated.status, RunStatus.FAILED)
        self.assertIn("repository_url", (updated.error or "").lower())
        mock_cleanup.assert_not_called()

    @patch("mission_control.workspace._safe_cleanup")
    @patch("mission_control.workspace._run_git")
    def test_cleanup_runs_after_prepare_failure_leaves_no_workspace(
        self,
        mock_run_git,
        mock_safe_cleanup,
    ) -> None:
        clone = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="clone failed",
        )
        mock_run_git.return_value = clone

        prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertFalse(prep.ok)
        self.assertGreaterEqual(mock_safe_cleanup.call_count, 1)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    def test_existing_file_deliverable_allows_persistence(
        self,
        mock_execute,
        mock_persist,
        mock_cleanup,
    ) -> None:
        prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            (Path(prep.workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            mock_execute.return_value = ExecutionResult(ok=True, stdout="done\n")
            mock_persist.return_value = PersistenceResult(ok=True, commit_sha=None)

            mission = self.fixture.mission(persistence_mode="none")
            mission["deliverables"] = ["created.txt"]
            with patch(
                "mission_control.workspace.prepare_isolated_workspace",
                return_value=prep,
            ):
                record = self.registry.create_run()
                execute_registered_run(record.run_id, mission, self.registry)

            updated = self.registry.get_run(record.run_id)
            assert updated is not None
            self.assertEqual(updated.status, RunStatus.COMPLETED)
            mock_persist.assert_called_once()
            mock_cleanup.assert_called_once_with(prep.workspace_path)
        finally:
            cleanup_workspace(prep.workspace_path)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    def test_missing_file_deliverable_fails_before_persistence(
        self,
        mock_execute,
        mock_persist,
        mock_cleanup,
    ) -> None:
        prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            mock_execute.return_value = ExecutionResult(ok=True, stdout="done\n")

            mission = self.fixture.mission(persistence_mode="none")
            mission["deliverables"] = ["missing-output.txt"]
            with patch(
                "mission_control.workspace.prepare_isolated_workspace",
                return_value=prep,
            ):
                record = self.registry.create_run()
                execute_registered_run(record.run_id, mission, self.registry)

            updated = self.registry.get_run(record.run_id)
            assert updated is not None
            self.assertEqual(updated.status, RunStatus.FAILED)
            self.assertEqual(
                updated.error,
                "Missing declared file deliverable: missing-output.txt",
            )
            mock_persist.assert_not_called()
            mock_cleanup.assert_called_once_with(prep.workspace_path)
        finally:
            cleanup_workspace(prep.workspace_path)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    def test_outside_workspace_deliverable_fails_before_persistence(
        self,
        mock_execute,
        mock_persist,
        mock_cleanup,
    ) -> None:
        prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            mock_execute.return_value = ExecutionResult(ok=True, stdout="done\n")

            mission = self.fixture.mission(persistence_mode="commit")
            mission["deliverables"] = [
                "/tmp/not-in-workspace.txt",
                "~/escaped.txt",
                "../escape.txt",
            ]
            with patch(
                "mission_control.workspace.prepare_isolated_workspace",
                return_value=prep,
            ):
                record = self.registry.create_run()
                execute_registered_run(record.run_id, mission, self.registry)

            updated = self.registry.get_run(record.run_id)
            assert updated is not None
            self.assertEqual(updated.status, RunStatus.FAILED)
            self.assertEqual(
                updated.error,
                "Declared file deliverable outside workspace: "
                "/tmp/not-in-workspace.txt",
            )
            mock_persist.assert_not_called()
            assert updated.result is not None
            assert updated.result.deliverables is not None
            self.assertFalse(updated.result.deliverables.passed)
            self.assertEqual(
                updated.result.deliverables.outside_workspace,
                [
                    "/tmp/not-in-workspace.txt",
                    "~/escaped.txt",
                    "../escape.txt",
                ],
            )
            assert updated.result.persistence is not None
            self.assertFalse(updated.result.persistence.attempted)
            mock_cleanup.assert_called_once_with(prep.workspace_path)
        finally:
            cleanup_workspace(prep.workspace_path)

    @patch("mission_control.workspace.cleanup_workspace")
    @patch("mission_control.workspace.persist_workspace_changes")
    @patch("mission_control.workspace.execute_cursor_agent")
    def test_descriptive_and_empty_deliverables_still_persist(
        self,
        mock_execute,
        mock_persist,
        mock_cleanup,
    ) -> None:
        prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            mock_execute.return_value = ExecutionResult(ok=True, stdout="done\n")
            mock_persist.return_value = PersistenceResult(ok=True, commit_sha=None)

            for deliverables in (
                ["summary", "report", "confirmation"],
                ["API/OpenAPI documentation updates"],
                [{"description": "API/OpenAPI documentation updates"}],
                [],
            ):
                mock_persist.reset_mock()
                mock_cleanup.reset_mock()
                mission = self.fixture.mission(persistence_mode="none")
                mission["deliverables"] = list(deliverables)
                with patch(
                    "mission_control.workspace.prepare_isolated_workspace",
                    return_value=WorkspacePrepResult(
                        ok=True,
                        workspace_path=prep.workspace_path,
                    ),
                ):
                    record = self.registry.create_run()
                    execute_registered_run(record.run_id, mission, self.registry)

                updated = self.registry.get_run(record.run_id)
                assert updated is not None
                self.assertEqual(updated.status, RunStatus.COMPLETED)
                mock_persist.assert_called_once()

            mock_cleanup.assert_called_with(prep.workspace_path)
        finally:
            cleanup_workspace(prep.workspace_path)
    def test_persistence_modes_none_commit_push_unchanged_with_file_gate(
        self,
    ) -> None:
        """Deliverable verification must not alter none/commit/push semantics."""
        for mode, expect_sha in (
            ("none", False),
            ("commit", True),
            ("push", True),
        ):
            workspace_path = prepare_isolated_workspace(
                self.fixture.mission()
            )
            self.assertTrue(workspace_path.ok, workspace_path.error)
            assert workspace_path.workspace_path is not None
            path = workspace_path.workspace_path
            try:
                (Path(path) / "created.txt").write_text(
                    "mission output\n",
                    encoding="utf-8",
                )
                mission = self.fixture.mission(
                    persistence_mode=mode,
                    platform_push_approved=(mode == "push"),
                )
                mission["deliverables"] = ["created.txt"]
                self.assertIsNone(
                    verify_declared_file_deliverables(mission, path)
                )
                with patch(
                    "mission_control.workspace._github_push_environment",
                    return_value=(os.environ.copy(), None),
                ):
                    result = persist_workspace_changes(
                        f"run-mode-{mode}",
                        mission,
                        path,
                    )
                self.assertTrue(result.ok, result.error)
                if expect_sha:
                    self.assertIsNotNone(result.commit_sha)
                else:
                    self.assertIsNone(result.commit_sha)
            finally:
                cleanup_workspace(path)


class TestMissionCloneUrlResolution(unittest.TestCase):
    """repository.name must select the clone URL persistence will inspect."""

    def setUp(self) -> None:
        self._previous_repo_url = os.environ.get("MISSION_CONTROL_REPOSITORY_URL")
        self._previous_map = os.environ.get(REPOSITORY_URL_MAP_ENV)
        self._previous_self = os.environ.get(SELF_REPOSITORY_URL_ENV)
        self._previous_legal = os.environ.get(LEGAL_AI_REPOSITORY_URL_ENV)
        # Simulate production where the legacy env points at Mission Control.
        os.environ["MISSION_CONTROL_REPOSITORY_URL"] = (
            "https://github.com/nhpcorp35/Mission-Control.git"
        )
        os.environ.pop(REPOSITORY_URL_MAP_ENV, None)
        os.environ.pop(SELF_REPOSITORY_URL_ENV, None)
        os.environ.pop(LEGAL_AI_REPOSITORY_URL_ENV, None)

    def tearDown(self) -> None:
        if self._previous_repo_url is None:
            os.environ.pop("MISSION_CONTROL_REPOSITORY_URL", None)
        else:
            os.environ["MISSION_CONTROL_REPOSITORY_URL"] = self._previous_repo_url
        if self._previous_map is None:
            os.environ.pop(REPOSITORY_URL_MAP_ENV, None)
        else:
            os.environ[REPOSITORY_URL_MAP_ENV] = self._previous_map
        if self._previous_self is None:
            os.environ.pop(SELF_REPOSITORY_URL_ENV, None)
        else:
            os.environ[SELF_REPOSITORY_URL_ENV] = self._previous_self
        if self._previous_legal is None:
            os.environ.pop(LEGAL_AI_REPOSITORY_URL_ENV, None)
        else:
            os.environ[LEGAL_AI_REPOSITORY_URL_ENV] = self._previous_legal

    def test_mission_control_name_does_not_use_legal_ai_env_url(self) -> None:
        mission = {
            "repository": {
                "name": "nhpcorp35/mission-control",
                "path": ".",
                "base_branch": "main",
            }
        }
        url, error = resolve_mission_clone_url(mission)
        self.assertIsNone(error)
        self.assertEqual(url, DEFAULT_MISSION_CONTROL_CLONE_URL)
        self.assertNotIn("legal-ai", url or "")

    def test_mission_control_alias_casefold(self) -> None:
        mission = {
            "repository": {
                "name": "Mission-Control",
                "path": ".",
                "base_branch": "main",
            }
        }
        url, error = resolve_mission_clone_url(mission)
        self.assertIsNone(error)
        self.assertEqual(url, DEFAULT_MISSION_CONTROL_CLONE_URL)

    def test_legal_ai_name_does_not_use_mission_control_env_url(self) -> None:
        """Explicit LegalAI must not silently clone Mission-Control."""
        mission = {
            "repository": {
                "name": "nhpcorp35/legal-ai",
                "path": ".",
                "base_branch": "main",
            }
        }
        url, error = resolve_mission_clone_url(mission)
        self.assertIsNone(error)
        self.assertEqual(url, DEFAULT_LEGAL_AI_CLONE_URL)
        self.assertNotIn("mission-control", (url or "").casefold())
        self.assertNotEqual(
            normalize_remote_url_identity(url or ""),
            normalize_remote_url_identity(
                os.environ["MISSION_CONTROL_REPOSITORY_URL"]
            ),
        )

    def test_legal_ai_short_alias_uses_dedicated_url(self) -> None:
        os.environ[LEGAL_AI_REPOSITORY_URL_ENV] = (
            "https://github.com/nhpcorp35/legal-ai.git"
        )
        mission = {
            "repository": {
                "name": "legal-ai",
                "path": ".",
                "base_branch": "main",
            }
        }
        url, error = resolve_mission_clone_url(mission)
        self.assertIsNone(error)
        self.assertEqual(url, "https://github.com/nhpcorp35/legal-ai.git")

    def test_url_map_overrides_mission_control_default(self) -> None:
        os.environ[REPOSITORY_URL_MAP_ENV] = json.dumps(
            {"nhpcorp35/mission-control": "file:///tmp/mc-mirror.git"}
        )
        mission = {
            "repository": {
                "name": "nhpcorp35/mission-control",
                "path": ".",
                "base_branch": "main",
            }
        }
        url, error = resolve_mission_clone_url(mission)
        self.assertIsNone(error)
        self.assertEqual(url, "file:///tmp/mc-mirror.git")


class TestPersistenceHandoff(unittest.TestCase):
    """Agent file modifications must be visible to platform persistence."""

    def setUp(self) -> None:
        self.fixture = GitRepoFixture()
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)

    def tearDown(self) -> None:
        self.fixture.cleanup()
        self.registry.close()
        os.unlink(self._db_path)

    def _fake_agent_write(
        self,
        seen: dict[str, str],
        relative_path: str = "agent_created.txt",
        content: str = "from agent\n",
    ):
        def fake_agent(mission: dict, run_id: str | None = None) -> ExecutionResult:
            workspace = mission["repository"]["path"]
            seen["agent_workspace"] = os.path.realpath(workspace)
            target = Path(workspace) / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return ExecutionResult(
                ok=True,
                stdout="agent done\n",
                return_code=0,
                command=["cursor-agent", "--workspace", workspace],
            )

        return fake_agent

    @patch("mission_control.workspace.cleanup_workspace")
    def test_agent_modifications_are_committed_on_same_workspace(
        self,
        mock_cleanup,
    ) -> None:
        seen: dict[str, str] = {}
        mission = self.fixture.mission(persistence_mode="commit")
        mission["deliverables"] = ["agent_created.txt"]

        try:
            with patch(
                "mission_control.workspace.execute_cursor_agent",
                side_effect=self._fake_agent_write(seen),
            ):
                record = self.registry.create_run()
                execute_registered_run(record.run_id, mission, self.registry)

            updated = self.registry.get_run(record.run_id)
            assert updated is not None
            self.assertEqual(updated.status, RunStatus.COMPLETED)
            self.assertIsNotNone(updated.commit_sha)
            assert updated.result is not None
            self.assertEqual(updated.result.files_changed, ["agent_created.txt"])
            assert updated.result.persistence is not None
            self.assertTrue(updated.result.persistence.attempted)
            self.assertTrue(updated.result.persistence.ok)
            self.assertEqual(updated.result.persistence.mode, "commit")
            self.assertFalse(updated.result.persistence.pushed)
            self.assertEqual(
                updated.result.persistence.commit_sha,
                updated.commit_sha,
            )
            self.assertIn("agent_workspace", seen)
        finally:
            _cleanup_mocked_workspaces(mock_cleanup)

    @patch("mission_control.workspace.cleanup_workspace")
    def test_cross_repository_name_writes_stay_in_isolated_workspace(
        self,
        mock_cleanup,
    ) -> None:
        """repository.name selects clone URL; agent edits stay in isolated path."""
        other = GitRepoFixture()
        seen: dict[str, str] = {}
        try:
            os.environ[REPOSITORY_URL_MAP_ENV] = json.dumps(
                {"nhpcorp35/mission-control": str(other.bare_remote)}
            )
            os.environ["MISSION_CONTROL_REPOSITORY_URL"] = str(
                self.fixture.bare_remote
            )
            mission = other.mission(persistence_mode="commit")
            mission["repository"]["name"] = "nhpcorp35/mission-control"
            mission["deliverables"] = ["agent_created.txt"]

            with patch(
                "mission_control.workspace.execute_cursor_agent",
                side_effect=self._fake_agent_write(seen),
            ):
                record = self.registry.create_run()
                execute_registered_run(record.run_id, mission, self.registry)

            updated = self.registry.get_run(record.run_id)
            assert updated is not None
            self.assertEqual(updated.status, RunStatus.COMPLETED, updated.error)
            self.assertIsNotNone(updated.commit_sha)
            assert updated.result is not None
            self.assertEqual(updated.result.files_changed, ["agent_created.txt"])
            assert updated.result.persistence is not None
            self.assertTrue(updated.result.persistence.ok)
            self.assertEqual(updated.result.persistence.mode, "commit")
            self.assertFalse(updated.result.persistence.pushed)

            agent_ws = Path(seen["agent_workspace"])
            self.assertTrue((agent_ws / "agent_created.txt").is_file())
            self.assertEqual(
                get_origin_url(str(agent_ws)),
                str(other.bare_remote),
            )
            # Must not land in the legacy Legal AI source tree.
            self.assertFalse(
                (self.fixture.source_repo / "agent_created.txt").exists()
            )
            # Isolated checkout is not either fixture's source path.
            self.assertNotEqual(
                os.path.realpath(agent_ws),
                os.path.realpath(self.fixture.source_repo),
            )
            self.assertNotEqual(
                os.path.realpath(agent_ws),
                os.path.realpath(other.source_repo),
            )
        finally:
            _cleanup_mocked_workspaces(mock_cleanup)
            os.environ.pop(REPOSITORY_URL_MAP_ENV, None)
            other.cleanup()

    @patch("mission_control.workspace.cleanup_workspace")
    def test_approved_push_handoff_records_pushed_true(
        self,
        mock_cleanup,
    ) -> None:
        seen: dict[str, str] = {}
        mission = self.fixture.mission(
            persistence_mode="push",
            platform_push_approved=True,
            allow_automatic_platform_push=True,
        )
        mission["deliverables"] = ["agent_created.txt"]

        try:
            with patch(
                "mission_control.workspace.execute_cursor_agent",
                side_effect=self._fake_agent_write(seen),
            ), patch(
                "mission_control.workspace._github_push_environment",
                return_value=(os.environ.copy(), None),
            ):
                record = self.registry.create_run()
                execute_registered_run(record.run_id, mission, self.registry)

            updated = self.registry.get_run(record.run_id)
            assert updated is not None
            self.assertEqual(updated.status, RunStatus.COMPLETED, updated.error)
            self.assertIsNotNone(updated.commit_sha)
            assert updated.result is not None
            self.assertEqual(updated.result.files_changed, ["agent_created.txt"])
            assert updated.result.persistence is not None
            self.assertTrue(updated.result.persistence.ok)
            self.assertEqual(updated.result.persistence.mode, "push")
            self.assertTrue(updated.result.persistence.pushed)

            # Bare remote received the commit.
            remote_sha = _run_git(
                ["--git-dir", str(self.fixture.bare_remote), "rev-parse", "main"]
            ).stdout.strip()
            self.assertEqual(remote_sha, updated.commit_sha)
        finally:
            _cleanup_mocked_workspaces(mock_cleanup)

    @patch("mission_control.workspace.cleanup_workspace")
    def test_unapproved_push_handoff_is_rejected(
        self,
        mock_cleanup,
    ) -> None:
        seen: dict[str, str] = {}
        mission = self.fixture.mission(
            persistence_mode="push",
            platform_push_approved=False,
            allow_automatic_platform_push=False,
        )
        mission["deliverables"] = ["agent_created.txt"]

        try:
            with patch(
                "mission_control.workspace.execute_cursor_agent",
                side_effect=self._fake_agent_write(seen),
            ):
                record = self.registry.create_run()
                execute_registered_run(record.run_id, mission, self.registry)

            updated = self.registry.get_run(record.run_id)
            assert updated is not None
            self.assertEqual(updated.status, RunStatus.FAILED)
            self.assertEqual(updated.error, PLATFORM_PUSH_APPROVAL_REQUIRED)
            assert updated.result is not None
            # Agent edits were detected even though push was rejected.
            self.assertEqual(updated.result.files_changed, ["agent_created.txt"])
            assert updated.result.persistence is not None
            self.assertTrue(updated.result.persistence.attempted)
            self.assertFalse(updated.result.persistence.ok)
            self.assertFalse(updated.result.persistence.pushed)
        finally:
            _cleanup_mocked_workspaces(mock_cleanup)

    def test_prepare_clones_mapped_url_for_mission_control_name(self) -> None:
        """Mission Control names must not silently clone the Legal AI env URL."""
        other = GitRepoFixture()
        try:
            os.environ[REPOSITORY_URL_MAP_ENV] = json.dumps(
                {"nhpcorp35/mission-control": str(other.bare_remote)}
            )
            # Point legacy env at a different remote; name must win.
            os.environ["MISSION_CONTROL_REPOSITORY_URL"] = str(
                self.fixture.bare_remote
            )
            mission = {
                "repository": {
                    "name": "nhpcorp35/mission-control",
                    "path": str(self.fixture.source_repo),
                    "base_branch": other.base_branch,
                }
            }
            prep = prepare_isolated_workspace(mission)
            self.assertTrue(prep.ok, prep.error)
            assert prep.workspace_path is not None
            try:
                origin = get_origin_url(prep.workspace_path)
                self.assertEqual(origin, str(other.bare_remote))
                self.assertEqual(
                    os.path.realpath(prep.workspace_path),
                    prep.workspace_path,
                )
            finally:
                cleanup_workspace(prep.workspace_path)
        finally:
            os.environ.pop(REPOSITORY_URL_MAP_ENV, None)
            other.cleanup()


class TestExplicitLegalAiRepositoryRouting(unittest.TestCase):
    """Structured LegalAI missions must check out legal-ai, not Mission-Control."""

    def setUp(self) -> None:
        self.mc = GitRepoFixture()
        self.legal = GitRepoFixture()
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        self.registry = RunRegistry(self._db_path)
        self._previous_map = os.environ.get(REPOSITORY_URL_MAP_ENV)
        # Legacy env points at Mission Control — the production footgun.
        os.environ["MISSION_CONTROL_REPOSITORY_URL"] = str(self.mc.bare_remote)
        os.environ[REPOSITORY_URL_MAP_ENV] = json.dumps(
            {
                "nhpcorp35/legal-ai": str(self.legal.bare_remote),
                "legal-ai": str(self.legal.bare_remote),
                "nhpcorp35/mission-control": str(self.mc.bare_remote),
                "Mission-Control": str(self.mc.bare_remote),
            }
        )

    def tearDown(self) -> None:
        self.registry.close()
        os.unlink(self._db_path)
        if self._previous_map is None:
            os.environ.pop(REPOSITORY_URL_MAP_ENV, None)
        else:
            os.environ[REPOSITORY_URL_MAP_ENV] = self._previous_map
        self.legal.cleanup()
        self.mc.cleanup()

    def _legal_mission(self, *, persistence_mode: str = "commit") -> dict:
        return {
            "mission_id": "2026-08-08-legalai-routing",
            "repository": {
                "name": "nhpcorp35/legal-ai",
                "path": ".",
                "base_branch": "main",
            },
            "permissions": {"push": False},
            "persistence": {"mode": persistence_mode},
            "approval": {"platform_push_approved": True},
            "deliverables": ["agent_created.txt"],
        }

    def test_prepare_clones_legal_ai_not_mission_control(self) -> None:
        mission = self._legal_mission()
        prep = prepare_isolated_workspace(mission)
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            origin = get_origin_url(prep.workspace_path)
            self.assertEqual(origin, str(self.legal.bare_remote))
            self.assertNotEqual(
                normalize_remote_url_identity(origin or ""),
                normalize_remote_url_identity(str(self.mc.bare_remote)),
            )
            agent_ws, path_error = resolve_agent_workspace_path(
                prep.workspace_path,
                ".",
            )
            self.assertIsNone(path_error)
            self.assertEqual(agent_ws, prep.workspace_path)
            branch = _run_git(
                ["-C", prep.workspace_path, "rev-parse", "--abbrev-ref", "HEAD"]
            ).stdout.strip()
            self.assertEqual(branch, "main")
        finally:
            cleanup_workspace(prep.workspace_path)

    @patch("mission_control.workspace.cleanup_workspace")
    def test_execute_binds_agent_to_legal_ai_checkout_root(
        self,
        mock_cleanup,
    ) -> None:
        seen: dict[str, str] = {}
        mission = self._legal_mission()

        def fake_agent(mission_arg: dict, run_id: str | None = None) -> ExecutionResult:
            workspace = mission_arg["repository"]["path"]
            seen["agent_workspace"] = os.path.realpath(workspace)
            seen["origin"] = get_origin_url(workspace) or ""
            (Path(workspace) / "agent_created.txt").write_text(
                "legal\n",
                encoding="utf-8",
            )
            return ExecutionResult(
                ok=True,
                stdout="agent done\n",
                return_code=0,
                command=["cursor-agent", "--workspace", workspace],
            )

        try:
            with patch(
                "mission_control.workspace.execute_cursor_agent",
                side_effect=fake_agent,
            ):
                record = self.registry.create_run()
                execute_registered_run(record.run_id, mission, self.registry)

            updated = self.registry.get_run(record.run_id)
            assert updated is not None
            self.assertEqual(updated.status, RunStatus.COMPLETED, updated.error)
            self.assertEqual(
                normalize_remote_url_identity(seen["origin"]),
                normalize_remote_url_identity(str(self.legal.bare_remote)),
            )
            self.assertNotEqual(
                normalize_remote_url_identity(seen["origin"]),
                normalize_remote_url_identity(str(self.mc.bare_remote)),
            )
            assert updated.result is not None
            self.assertEqual(updated.result.files_changed, ["agent_created.txt"])
            self.assertFalse(
                any(
                    ".legalai_work" in path
                    for path in updated.result.files_changed
                )
            )
        finally:
            _cleanup_mocked_workspaces(mock_cleanup)

    def test_origin_mismatch_fails_closed_before_persist(self) -> None:
        mission = self._legal_mission(persistence_mode="commit")
        prep = prepare_isolated_workspace(mission)
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        try:
            # Simulate the regression: workspace origin retargeted to MC.
            configure_workspace_origin(
                prep.workspace_path,
                str(self.mc.bare_remote),
            )
            mismatch = verify_workspace_origin_matches_mission(
                mission,
                prep.workspace_path,
            )
            self.assertIsNotNone(mismatch)
            assert mismatch is not None
            self.assertTrue(
                mismatch.startswith(REPOSITORY_ORIGIN_MISMATCH_PREFIX),
                mismatch,
            )

            (Path(prep.workspace_path) / "nested.txt").write_text(
                "should not commit\n",
                encoding="utf-8",
            )
            result = persist_workspace_changes(
                "run-mismatch",
                mission,
                prep.workspace_path,
            )
            self.assertFalse(result.ok)
            self.assertTrue(
                (result.error or "").startswith(REPOSITORY_ORIGIN_MISMATCH_PREFIX),
                result.error,
            )
            self.assertIsNone(result.commit_sha)
            legal_head = _run_git(
                [
                    "--git-dir",
                    str(self.legal.bare_remote),
                    "rev-parse",
                    "main",
                ]
            ).stdout.strip()
            workspace_head = _run_git(
                ["-C", prep.workspace_path, "rev-parse", "HEAD"]
            ).stdout.strip()
            self.assertEqual(workspace_head, legal_head)
        finally:
            cleanup_workspace(prep.workspace_path)

    @patch("mission_control.workspace.cleanup_workspace")
    def test_nested_legalai_work_changes_cannot_persist_to_mission_control(
        self,
        mock_cleanup,
    ) -> None:
        """Edits under .legalai_work must not legitimize MC persistence."""
        mission = {
            "mission_id": "2026-08-08-nested-contamination",
            "repository": {
                "name": "nhpcorp35/mission-control",
                "path": ".",
                "base_branch": "main",
            },
            "permissions": {"push": False},
            "persistence": {"mode": "commit"},
            "deliverables": [
                ".legalai_work/nhpcorp35-legal-ai-2b3c660/created.txt",
            ],
        }

        def fake_agent(mission_arg: dict, run_id: str | None = None) -> ExecutionResult:
            workspace = mission_arg["repository"]["path"]
            nested = (
                Path(workspace)
                / ".legalai_work"
                / "nhpcorp35-legal-ai-2b3c660"
                / "created.txt"
            )
            nested.parent.mkdir(parents=True, exist_ok=True)
            nested.write_text("nested legalai\n", encoding="utf-8")
            return ExecutionResult(
                ok=True,
                stdout="agent done\n",
                return_code=0,
                command=["cursor-agent", "--workspace", workspace],
            )

        try:
            with patch(
                "mission_control.workspace.execute_cursor_agent",
                side_effect=fake_agent,
            ):
                record = self.registry.create_run()
                execute_registered_run(record.run_id, mission, self.registry)

            updated = self.registry.get_run(record.run_id)
            assert updated is not None
            self.assertEqual(updated.status, RunStatus.FAILED)
            self.assertTrue(
                (updated.error or "").startswith(NESTED_WORKSPACE_CONTAMINATION_PREFIX),
                updated.error,
            )
            self.assertIsNone(updated.commit_sha)
            assert updated.result is not None
            assert updated.result.persistence is not None
            self.assertFalse(updated.result.persistence.attempted)
        finally:
            _cleanup_mocked_workspaces(mock_cleanup)

    def test_nested_contamination_helper_detects_legalai_work(self) -> None:
        error = nested_workspace_contamination_error(
            [".legalai_work/nhpcorp35-legal-ai-2b3c660/foo.py"]
        )
        self.assertIsNotNone(error)
        self.assertTrue(
            (error or "").startswith(NESTED_WORKSPACE_CONTAMINATION_PREFIX)
        )
        self.assertIsNone(nested_workspace_contamination_error(["app/api.py"]))


class TestRemoteReconciliationPhase2(unittest.TestCase):
    """Phase-2 ownership: reconcile unexpected remote advancement."""

    def setUp(self) -> None:
        self.fixture = GitRepoFixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def _prepare(self) -> tuple[str, str]:
        prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        self.assertIsNotNone(prep.baseline_sha)
        assert prep.baseline_sha is not None
        return prep.workspace_path, prep.baseline_sha

    def _push_mission(self) -> dict:
        return self.fixture.mission(
            persistence_mode="push",
            platform_push_approved=True,
        )

    def _configure_identity(self, workspace_path: str) -> None:
        _run_git(
            ["-C", workspace_path, "config", "user.email", "test@example.com"]
        )
        _run_git(["-C", workspace_path, "config", "user.name", "Test User"])

    def test_unchanged_remote_normal_platform_push(self) -> None:
        workspace_path, baseline_sha = self._prepare()
        try:
            (Path(workspace_path) / "owned.txt").write_text(
                "platform\n",
                encoding="utf-8",
            )
            with patch(
                "mission_control.workspace._github_push_environment",
                return_value=(os.environ.copy(), None),
            ):
                result = persist_workspace_changes(
                    "recon-normal-push",
                    self._push_mission(),
                    workspace_path,
                    baseline_sha=baseline_sha,
                )
            self.assertTrue(result.ok, result.error)
            self.assertTrue(result.pushed)
            self.assertIsNotNone(result.commit_sha)
            self.assertEqual(result.baseline_sha, baseline_sha)
            self.assertEqual(result.remote_sha, baseline_sha)
            self.assertNotEqual(result.commit_sha, baseline_sha)
            self.assertIsNone(result.failure_stage)
            self.assertFalse(bool(result.pushed_by_external_or_agent))
            remote_sha = _run_git(
                [
                    "-C",
                    str(self.fixture.bare_remote),
                    "rev-parse",
                    self.fixture.base_branch,
                ]
            ).stdout.strip()
            self.assertEqual(remote_sha, result.commit_sha)
        finally:
            cleanup_workspace(workspace_path)

    def test_remote_already_equals_workspace_commit(self) -> None:
        workspace_path, baseline_sha = self._prepare()
        try:
            self._configure_identity(workspace_path)
            (Path(workspace_path) / "external.txt").write_text(
                "already pushed\n",
                encoding="utf-8",
            )
            _run_git(["-C", workspace_path, "add", "external.txt"])
            _run_git(
                ["-C", workspace_path, "commit", "-m", "external-or-agent"]
            )
            workspace_sha = _run_git(
                ["-C", workspace_path, "rev-parse", "HEAD"]
            ).stdout.strip()
            origin = get_origin_url(workspace_path)
            assert origin is not None
            _run_git(
                [
                    "-C",
                    workspace_path,
                    "push",
                    origin,
                    f"HEAD:{self.fixture.base_branch}",
                ]
            )
            remote_before = _run_git(
                [
                    "-C",
                    str(self.fixture.bare_remote),
                    "rev-parse",
                    self.fixture.base_branch,
                ]
            ).stdout.strip()
            self.assertEqual(remote_before, workspace_sha)
            self.assertNotEqual(remote_before, baseline_sha)

            with patch(
                "mission_control.workspace._github_push_environment",
                return_value=(os.environ.copy(), None),
            ):
                result = persist_workspace_changes(
                    "recon-already-pushed",
                    self._push_mission(),
                    workspace_path,
                    baseline_sha=baseline_sha,
                )

            self.assertTrue(result.ok, result.error)
            self.assertFalse(result.pushed)
            self.assertTrue(result.reconciled)
            self.assertTrue(result.pushed_by_external_or_agent)
            self.assertEqual(result.commit_sha, workspace_sha)
            self.assertEqual(result.remote_sha, workspace_sha)
            self.assertEqual(result.workspace_sha, workspace_sha)
            self.assertEqual(result.baseline_sha, baseline_sha)
            self.assertFalse(result.dirty)
            self.assertIsNone(result.failure_stage)
            remote_after = _run_git(
                [
                    "-C",
                    str(self.fixture.bare_remote),
                    "rev-parse",
                    self.fixture.base_branch,
                ]
            ).stdout.strip()
            self.assertEqual(remote_after, remote_before)
        finally:
            cleanup_workspace(workspace_path)

    def test_unrelated_remote_advancement_fails_closed_without_overwrite(
        self,
    ) -> None:
        workspace_path, baseline_sha = self._prepare()
        try:
            (Path(workspace_path) / "local-only.txt").write_text(
                "workspace change\n",
                encoding="utf-8",
            )
            # Advance remote with an unrelated commit via a sibling clone.
            sibling = Path(self.fixture.temp.name) / "sibling"
            _run_git(
                [
                    "clone",
                    "--branch",
                    self.fixture.base_branch,
                    str(self.fixture.bare_remote),
                    str(sibling),
                ]
            )
            _run_git(
                ["-C", str(sibling), "config", "user.email", "sib@example.com"]
            )
            _run_git(["-C", str(sibling), "config", "user.name", "Sibling"])
            (sibling / "unrelated.txt").write_text("remote\n", encoding="utf-8")
            _run_git(["-C", str(sibling), "add", "unrelated.txt"])
            _run_git(["-C", str(sibling), "commit", "-m", "unrelated remote"])
            _run_git(
                ["-C", str(sibling), "push", "origin", self.fixture.base_branch]
            )
            advanced_remote = _run_git(
                [
                    "-C",
                    str(self.fixture.bare_remote),
                    "rev-parse",
                    self.fixture.base_branch,
                ]
            ).stdout.strip()
            self.assertNotEqual(advanced_remote, baseline_sha)

            recorded_args: list[list[str]] = []
            real_run_git = persist_workspace_changes.__globals__["_run_git"]

            def tracking_run_git(
                args: list[str],
                *,
                env: dict[str, str] | None = None,
            ) -> subprocess.CompletedProcess[str]:
                recorded_args.append(list(args))
                return real_run_git(args, env=env)

            with patch(
                "mission_control.workspace._github_push_environment",
                return_value=(os.environ.copy(), None),
            ), patch(
                "mission_control.workspace._run_git",
                side_effect=tracking_run_git,
            ):
                result = persist_workspace_changes(
                    "recon-unrelated",
                    self._push_mission(),
                    workspace_path,
                    baseline_sha=baseline_sha,
                )

            self.assertFalse(result.ok)
            self.assertFalse(result.pushed)
            self.assertEqual(
                result.failure_stage,
                REMOTE_RECONCILIATION_FAILURE_STAGE,
            )
            self.assertEqual(result.baseline_sha, baseline_sha)
            self.assertEqual(result.remote_sha, advanced_remote)
            self.assertTrue(result.dirty)
            self.assertFalse(bool(result.reconciled))
            self.assertFalse(bool(result.pushed_by_external_or_agent))
            self.assertIsNotNone(result.recommended_next_action)
            self.assertIn("refusing to overwrite", (result.error or "").lower())
            self.assertFalse(
                any("push" in args for args in recorded_args),
                recorded_args,
            )
            remote_after = _run_git(
                [
                    "-C",
                    str(self.fixture.bare_remote),
                    "rev-parse",
                    self.fixture.base_branch,
                ]
            ).stdout.strip()
            self.assertEqual(remote_after, advanced_remote)
        finally:
            cleanup_workspace(workspace_path)

    def test_authoritative_summary_matches_structured_persistence_fields(
        self,
    ) -> None:
        workspace_path, baseline_sha = self._prepare()
        try:
            self._configure_identity(workspace_path)
            (Path(workspace_path) / "external.txt").write_text(
                "already pushed\n",
                encoding="utf-8",
            )
            _run_git(["-C", workspace_path, "add", "external.txt"])
            _run_git(
                ["-C", workspace_path, "commit", "-m", "external-or-agent"]
            )
            workspace_sha = _run_git(
                ["-C", workspace_path, "rev-parse", "HEAD"]
            ).stdout.strip()
            origin = get_origin_url(workspace_path)
            assert origin is not None
            _run_git(
                [
                    "-C",
                    workspace_path,
                    "push",
                    origin,
                    f"HEAD:{self.fixture.base_branch}",
                ]
            )

            with patch(
                "mission_control.workspace._github_push_environment",
                return_value=(os.environ.copy(), None),
            ):
                result = persist_workspace_changes(
                    "recon-summary",
                    self._push_mission(),
                    workspace_path,
                    baseline_sha=baseline_sha,
                )

            evidence = build_persistence_evidence(
                self._push_mission(),
                attempted=True,
                ok=result.ok,
                commit_sha=result.commit_sha,
                mode=result.mode,
                pushed=result.pushed,
                baseline_sha=result.baseline_sha,
                remote_sha=result.remote_sha,
                workspace_sha=result.workspace_sha,
                dirty=result.dirty,
                reconciled=result.reconciled,
                pushed_by_external_or_agent=result.pushed_by_external_or_agent,
                failure_stage=result.failure_stage,
                recommended_next_action=result.recommended_next_action,
            )
            summary = build_run_summary(persistence=evidence)
            self.assertTrue(evidence.reconciled)
            self.assertTrue(evidence.pushed_by_external_or_agent)
            self.assertFalse(evidence.pushed)
            self.assertEqual(evidence.commit_sha, workspace_sha)
            self.assertIn("reconciled", summary)
            self.assertIn("pushed_by_external_or_agent=true", summary)
            self.assertIn(f"commit_sha={workspace_sha}", summary)
            self.assertIn("pushed=false", summary)
            self.assertNotIn("no repository changes", summary)

            failed = PersistenceEvidence(
                mode="push",
                attempted=True,
                ok=False,
                commit_sha=None,
                pushed=False,
                baseline_sha=baseline_sha,
                remote_sha="abc",
                workspace_sha="def",
                dirty=True,
                reconciled=False,
                pushed_by_external_or_agent=False,
                failure_stage=REMOTE_RECONCILIATION_FAILURE_STAGE,
                recommended_next_action="inspect_diverged_remote_then_resubmit_or_rebase",
            )
            failed_summary = build_run_summary(
                persistence=failed,
                error="Remote branch advanced unexpectedly",
            )
            self.assertIn("failure_stage=remote_reconciliation", failed_summary)
            self.assertIn(f"baseline_sha={baseline_sha}", failed_summary)
            self.assertIn("dirty=true", failed_summary)
            self.assertNotIn("no repository changes", failed_summary)
        finally:
            cleanup_workspace(workspace_path)


class TestSafePushTarget(unittest.TestCase):
    """Fail-closed push target, main-write ack, and post-push tip checks."""

    def setUp(self) -> None:
        self.fixture = GitRepoFixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def _prepare(self) -> str:
        prep = prepare_isolated_workspace(self.fixture.mission())
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        return prep.workspace_path

    def test_push_fails_closed_when_target_branch_missing(self) -> None:
        workspace_path = self._prepare()
        try:
            (Path(workspace_path) / "created.txt").write_text(
                "mission output\n",
                encoding="utf-8",
            )
            mission = self.fixture.mission(
                persistence_mode="push",
                platform_push_approved=True,
                platform_main_write_acknowledged=True,
                include_default_push_target=False,
            )
            self.assertNotIn("target_branch", mission.get("persistence", {}))
            self.assertEqual(
                require_persistence_push_target(mission),
                PLATFORM_TARGET_BRANCH_REQUIRED,
            )
            with patch("mission_control.workspace._run_git") as mock_git:
                result = persist_workspace_changes(
                    "run-missing-target",
                    mission,
                    workspace_path,
                )
            self.assertFalse(result.ok)
            self.assertFalse(result.pushed)
            self.assertTrue(
                (result.error or "").startswith("PLATFORM_TARGET_BRANCH_REQUIRED"),
                result.error,
            )
            self.assertIsNone(result.target_branch)
            mock_git.assert_not_called()
            execute = validate_mission_for_execute(
                {
                    **mission,
                    "version": "1.0",
                    "title": "Missing target",
                    "execution": {
                        "agent": "cursor",
                        "mode": "execute",
                        "sandbox": True,
                        "worktree": False,
                    },
                    "permissions": {
                        "read": True,
                        "create_files": True,
                        "modify_files": False,
                        "delete_files": False,
                        "run_commands": True,
                        "stage_changes": False,
                        "commit": False,
                        "push": False,
                    },
                    "instructions": "Do not choose a push branch in prose.",
                    "deliverables": ["summary"],
                    "approval": mission.get("approval", {}),
                }
            )
            self.assertFalse(execute.ok)
            self.assertTrue(
                (execute.error or "").startswith("PLATFORM_TARGET_BRANCH_REQUIRED"),
                execute.error,
            )
        finally:
            cleanup_workspace(workspace_path)

    def test_non_main_refspec_pushes_only_approved_target_branch(self) -> None:
        target = "mission/safe-target"
        # Seed remote target at the same tip as base so pre-push reconciliation
        # sees an unchanged remote for the approved target_branch.
        _run_git(
            [
                "-C",
                str(self.fixture.source_repo),
                "push",
                "origin",
                f"{self.fixture.base_branch}:{target}",
            ]
        )
        workspace_path = self._prepare()
        try:
            (Path(workspace_path) / "feature.txt").write_text(
                "non-main\n",
                encoding="utf-8",
            )
            mission = self.fixture.mission(
                persistence_mode="push",
                platform_push_approved=True,
                platform_main_write_acknowledged=False,
                target_branch=target,
            )
            self.assertEqual(resolve_persistence_target_branch(mission), target)
            self.assertFalse(is_platform_main_write_acknowledged(mission))
            recorded: list[list[str]] = []
            real_run_git = persist_workspace_changes.__globals__["_run_git"]

            def _tracking_run_git(args, **kwargs):
                recorded.append(list(args))
                return real_run_git(args, **kwargs)

            with patch(
                "mission_control.workspace._github_push_environment",
                return_value=(os.environ.copy(), None),
            ), patch(
                "mission_control.workspace._run_git",
                side_effect=_tracking_run_git,
            ):
                result = persist_workspace_changes(
                    "run-non-main-target",
                    mission,
                    workspace_path,
                )
            self.assertTrue(result.ok, result.error)
            self.assertTrue(result.pushed)
            self.assertEqual(result.target_branch, target)
            self.assertEqual(result.remote_tip_sha, result.commit_sha)
            self.assertTrue(result.reconciled)
            push_args = [args for args in recorded if "push" in args]
            self.assertTrue(push_args)
            self.assertIn(f"HEAD:{target}", push_args[0])
            self.assertNotIn(
                f"HEAD:{self.fixture.base_branch}",
                push_args[0],
            )
            remote_target = _run_git(
                ["-C", str(self.fixture.bare_remote), "rev-parse", target]
            ).stdout.strip()
            self.assertEqual(remote_target, result.commit_sha)
            main_tip = _run_git(
                [
                    "-C",
                    str(self.fixture.bare_remote),
                    "rev-parse",
                    self.fixture.base_branch,
                ]
            ).stdout.strip()
            self.assertNotEqual(main_tip, result.commit_sha)
        finally:
            cleanup_workspace(workspace_path)

    def test_main_push_requires_distinct_acknowledgement(self) -> None:
        workspace_path = self._prepare()
        try:
            (Path(workspace_path) / "main.txt").write_text(
                "blocked\n",
                encoding="utf-8",
            )
            mission = self.fixture.mission(
                persistence_mode="push",
                platform_push_approved=True,
                platform_main_write_acknowledged=False,
                target_branch="main",
            )
            # Explicit false must win over fixture auto-ack for approved pushes.
            mission["approval"]["platform_main_write_acknowledged"] = False
            self.assertEqual(
                require_persistence_push_target(mission),
                PLATFORM_MAIN_WRITE_ACK_REQUIRED,
            )
            with patch("mission_control.workspace._run_git") as mock_git:
                result = persist_workspace_changes(
                    "run-main-no-ack",
                    mission,
                    workspace_path,
                )
            self.assertFalse(result.ok)
            self.assertFalse(result.pushed)
            self.assertEqual(result.target_branch, "main")
            self.assertTrue(
                (result.error or "").startswith("PLATFORM_MAIN_WRITE_ACK_REQUIRED"),
                result.error,
            )
            mock_git.assert_not_called()
        finally:
            cleanup_workspace(workspace_path)

    def test_protected_default_branch_recognizes_exact_qualified_and_case(
        self,
    ) -> None:
        protected = (
            "main",
            "master",
            "Main",
            "MASTER",
            "Master",
            "refs/heads/main",
            "refs/heads/master",
            "refs/heads/Main",
            "refs/HEADS/MASTER",
            "  main  ",
            " refs/heads/master ",
        )
        for name in protected:
            self.assertTrue(is_protected_default_branch(name), name)

        unaffected = (
            "mission/safe-target",
            "feature",
            "maintenance",
            "mainline",
            "mastermind",
            "refs/heads/mission/safe-target",
            "heads/main",
            "refs/main",
        )
        for name in unaffected:
            self.assertFalse(is_protected_default_branch(name), name)

    def test_protected_default_bypass_spellings_require_main_write_ack(
        self,
    ) -> None:
        bypass_spellings = (
            "refs/heads/main",
            "refs/heads/master",
            "Main",
            "MASTER",
            "refs/heads/Main",
        )
        for target in bypass_spellings:
            mission = self.fixture.mission(
                persistence_mode="push",
                platform_push_approved=True,
                platform_main_write_acknowledged=False,
                target_branch=target,
            )
            mission["approval"]["platform_main_write_acknowledged"] = False
            self.assertEqual(
                resolve_persistence_target_branch(mission),
                target,
            )
            self.assertEqual(
                require_persistence_push_target(mission),
                PLATFORM_MAIN_WRITE_ACK_REQUIRED,
                target,
            )

    def test_protected_default_bypass_spellings_accepted_with_ack(self) -> None:
        bypass_spellings = (
            "refs/heads/main",
            "refs/heads/master",
            "Main",
            "MASTER",
            "refs/heads/Main",
        )
        for target in bypass_spellings:
            mission = self.fixture.mission(
                persistence_mode="push",
                platform_push_approved=True,
                platform_main_write_acknowledged=True,
                target_branch=target,
            )
            self.assertEqual(
                resolve_persistence_target_branch(mission),
                target,
            )
            self.assertIsNone(
                require_persistence_push_target(mission),
                target,
            )
            # Normalization must not rewrite the push destination spelling.
            resolved = resolve_persistence_target_branch(mission)
            self.assertEqual(resolved, target)
            self.assertEqual(f"HEAD:{resolved}", f"HEAD:{target}")
            self.assertNotEqual(f"HEAD:{resolved}", "HEAD:main")

    def test_qualified_target_push_refspec_preserves_original_spelling(
        self,
    ) -> None:
        target = "refs/heads/main"
        # Seed remote tip under the qualified ref so pre-push reconciliation
        # sees an unchanged destination for the approved spelling.
        _run_git(
            [
                "-C",
                str(self.fixture.source_repo),
                "push",
                "origin",
                f"{self.fixture.base_branch}:{target}",
            ]
        )
        workspace_path = self._prepare()
        try:
            (Path(workspace_path) / "qualified.txt").write_text(
                "qualified-main\n",
                encoding="utf-8",
            )
            mission = self.fixture.mission(
                persistence_mode="push",
                platform_push_approved=True,
                platform_main_write_acknowledged=True,
                target_branch=target,
            )
            self.assertTrue(is_protected_default_branch(target))
            self.assertEqual(resolve_persistence_target_branch(mission), target)
            recorded: list[list[str]] = []
            real_run_git = persist_workspace_changes.__globals__["_run_git"]

            def _tracking_run_git(args, **kwargs):
                recorded.append(list(args))
                return real_run_git(args, **kwargs)

            with patch(
                "mission_control.workspace._github_push_environment",
                return_value=(os.environ.copy(), None),
            ), patch(
                "mission_control.workspace._run_git",
                side_effect=_tracking_run_git,
            ):
                result = persist_workspace_changes(
                    "run-qualified-main-refspec",
                    mission,
                    workspace_path,
                )
            self.assertTrue(result.ok, result.error)
            self.assertTrue(result.pushed)
            self.assertEqual(result.target_branch, target)
            push_args = [args for args in recorded if "push" in args]
            self.assertTrue(push_args)
            self.assertIn(f"HEAD:{target}", push_args[0])
            self.assertNotIn("HEAD:main", push_args[0])
        finally:
            cleanup_workspace(workspace_path)

    def test_post_push_reconciliation_mismatch_keeps_pushed_false(self) -> None:
        workspace_path = self._prepare()
        try:
            (Path(workspace_path) / "mismatch.txt").write_text(
                "push then lie\n",
                encoding="utf-8",
            )
            mission = self.fixture.mission(
                persistence_mode="push",
                platform_push_approved=True,
            )
            real_fetch = persist_workspace_changes.__globals__[
                "_fetch_remote_branch_sha"
            ]
            fetch_calls = {"n": 0}

            def _fetch_with_mismatch(workspace, branch, *, env):
                fetch_calls["n"] += 1
                sha, error = real_fetch(workspace, branch, env=env)
                # After the platform push, lie about the remote tip.
                if fetch_calls["n"] >= 2 and error is None and sha:
                    return ("0" * 40, None)
                return sha, error

            with patch(
                "mission_control.workspace._github_push_environment",
                return_value=(os.environ.copy(), None),
            ), patch(
                "mission_control.workspace._fetch_remote_branch_sha",
                side_effect=_fetch_with_mismatch,
            ):
                result = persist_workspace_changes(
                    "run-post-push-mismatch",
                    mission,
                    workspace_path,
                )
            self.assertFalse(result.ok)
            self.assertFalse(result.pushed)
            self.assertEqual(result.target_branch, self.fixture.base_branch)
            self.assertEqual(result.remote_tip_sha, "0" * 40)
            self.assertEqual(
                result.failure_stage,
                POST_PUSH_RECONCILIATION_FAILURE_STAGE,
            )
            self.assertFalse(bool(result.reconciled))
            self.assertIn("Post-push reconciliation failed", result.error or "")
            self.assertIn("target_branch=", result.error or "")
        finally:
            cleanup_workspace(workspace_path)

    def test_approved_target_push_success_records_tip_evidence(self) -> None:
        workspace_path = self._prepare()
        try:
            (Path(workspace_path) / "ok.txt").write_text(
                "safe push\n",
                encoding="utf-8",
            )
            mission = self.fixture.mission(
                persistence_mode="push",
                platform_push_approved=True,
            )
            with patch(
                "mission_control.workspace._github_push_environment",
                return_value=(os.environ.copy(), None),
            ):
                result = persist_workspace_changes(
                    "run-safe-push-success",
                    mission,
                    workspace_path,
                )
            self.assertTrue(result.ok, result.error)
            self.assertTrue(result.pushed)
            self.assertEqual(result.target_branch, self.fixture.base_branch)
            self.assertEqual(result.remote_tip_sha, result.commit_sha)
            self.assertTrue(result.reconciled)
            self.assertIsNone(result.failure_stage)
            remote_tip = _run_git(
                [
                    "-C",
                    str(self.fixture.bare_remote),
                    "rev-parse",
                    self.fixture.base_branch,
                ]
            ).stdout.strip()
            self.assertEqual(remote_tip, result.remote_tip_sha)
        finally:
            cleanup_workspace(workspace_path)


if __name__ == "__main__":
    unittest.main()
