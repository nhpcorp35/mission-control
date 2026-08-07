"""Regression tests for Railway-native / git commit provenance gates."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _load_cli():
    path = (
        Path(__file__).resolve().parent
        / "scripts"
        / "generate_attorney_feedback_candidate.py"
    )
    spec = importlib.util.spec_from_file_location(
        "generate_attorney_feedback_candidate_commit", path
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


CLI = _load_cli()

SHA = "95407c73201ca375b7f824d8cbcbe06ed598405c"
OTHER = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class CommitVerificationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self._env_backup = {
            key: os.environ.get(key) for key in CLI.RAILWAY_PROVENANCE_ENV_VARS
        }
        for key in CLI.RAILWAY_PROVENANCE_ENV_VARS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmpdir.cleanup()

    def _write_loose_checkout(self, commit: str) -> None:
        git = self.root / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "refs" / "remotes" / "origin").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "refs" / "heads" / "main").write_text(commit + "\n", encoding="utf-8")
        (git / "refs" / "remotes" / "origin" / "main").write_text(
            commit + "\n", encoding="utf-8"
        )

    def _write_packed_checkout(self, commit: str) -> None:
        git = self.root / ".git"
        git.mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "packed-refs").write_text(
            "# pack-refs with: peeled fully-peeled sorted\n"
            f"{commit} refs/heads/main\n"
            f"{commit} refs/remotes/origin/main\n",
            encoding="utf-8",
        )

    def test_normal_checkout_loose_refs(self):
        self._write_loose_checkout(SHA)
        info = CLI.assert_commits_match(self.root, SHA)
        self.assertEqual(info["checkout_commit"], SHA)
        self.assertEqual(info["origin_main_commit"], SHA)
        self.assertEqual(info["provenance_source"], "git_metadata")

    def test_packed_refs_checkout(self):
        self._write_packed_checkout(SHA)
        info = CLI.assert_commits_match(self.root, SHA)
        self.assertEqual(info["checkout_commit"], SHA)
        self.assertEqual(info["origin_main_commit"], SHA)
        self.assertEqual(info["provenance_source"], "git_metadata")

    def test_loose_origin_main_uses_refs_path_not_git_root(self):
        """Regression: remotes/origin/main must resolve under .git/refs/."""
        self._write_loose_checkout(SHA)
        # Poison the incorrect historical path; correct path must still win.
        wrong = self.root / ".git" / "remotes" / "origin"
        wrong.mkdir(parents=True)
        (wrong / "main").write_text(OTHER + "\n", encoding="utf-8")
        info = CLI.assert_commits_match(self.root, SHA)
        self.assertEqual(info["origin_main_commit"], SHA)

    def test_railway_runtime_success(self):
        os.environ[CLI.RAILWAY_GIT_COMMIT_SHA] = SHA
        os.environ[CLI.RAILWAY_GIT_REPO_OWNER] = "nhpcorp35"
        os.environ[CLI.RAILWAY_GIT_REPO_NAME] = "legal-ai"
        os.environ[CLI.RAILWAY_GIT_BRANCH] = "main"
        info = CLI.assert_commits_match(self.root, SHA)
        self.assertEqual(info["checkout_commit"], SHA)
        self.assertEqual(info["provenance_source"], "railway_deployment_metadata")
        self.assertEqual(info["railway_repo_name"], "legal-ai")

    def test_railway_mismatched_sha(self):
        os.environ[CLI.RAILWAY_GIT_COMMIT_SHA] = OTHER
        os.environ[CLI.RAILWAY_GIT_REPO_OWNER] = "nhpcorp35"
        os.environ[CLI.RAILWAY_GIT_REPO_NAME] = "legal-ai"
        os.environ[CLI.RAILWAY_GIT_BRANCH] = "main"
        with self.assertRaises(CLI.GenerationError) as ctx:
            CLI.assert_commits_match(self.root, SHA)
        self.assertIn("does not match required commit", ctx.exception.blocker)

    def test_railway_wrong_repo(self):
        os.environ[CLI.RAILWAY_GIT_COMMIT_SHA] = SHA
        os.environ[CLI.RAILWAY_GIT_REPO_OWNER] = "nhpcorp35"
        os.environ[CLI.RAILWAY_GIT_REPO_NAME] = "mission-control"
        os.environ[CLI.RAILWAY_GIT_BRANCH] = "main"
        with self.assertRaises(CLI.GenerationError) as ctx:
            CLI.assert_commits_match(self.root, SHA)
        self.assertIn("repository name mismatch", ctx.exception.blocker.lower())

    def test_missing_provenance_fail_closed(self):
        with self.assertRaises(CLI.GenerationError) as ctx:
            CLI.assert_commits_match(self.root, SHA)
        self.assertIn("provenance missing", ctx.exception.blocker.lower())

    def test_git_mismatch_fail_closed(self):
        self._write_loose_checkout(OTHER)
        with self.assertRaises(CLI.GenerationError) as ctx:
            CLI.assert_commits_match(self.root, SHA)
        self.assertIn("not exactly the required commit", ctx.exception.blocker)

    def test_partial_railway_metadata_fail_closed(self):
        os.environ[CLI.RAILWAY_GIT_COMMIT_SHA] = SHA
        # owner/name/branch intentionally omitted
        with self.assertRaises(CLI.GenerationError) as ctx:
            CLI.assert_commits_match(self.root, SHA)
        self.assertIn("incomplete", ctx.exception.blocker.lower())

    def test_git_metadata_preferred_over_railway_env(self):
        self._write_loose_checkout(SHA)
        os.environ[CLI.RAILWAY_GIT_COMMIT_SHA] = OTHER
        os.environ[CLI.RAILWAY_GIT_REPO_OWNER] = "nhpcorp35"
        os.environ[CLI.RAILWAY_GIT_REPO_NAME] = "legal-ai"
        os.environ[CLI.RAILWAY_GIT_BRANCH] = "main"
        info = CLI.assert_commits_match(self.root, SHA)
        self.assertEqual(info["provenance_source"], "git_metadata")
        self.assertEqual(info["checkout_commit"], SHA)


if __name__ == "__main__":
    unittest.main()
