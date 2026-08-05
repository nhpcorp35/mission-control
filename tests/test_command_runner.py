"""Focused tests for allowlisted repository command runner."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mission_control.command_runner import (
    ALLOWED_SCRIPT,
    REDACTED,
    REPOSITORY_URL_MAP_ENV,
    RepositoryCommandSpec,
    build_command_env,
    redact_argv,
    run_repository_command,
)
from mission_control.workspace import prepare_ephemeral_checkout


def _run_git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=check,
        shell=False,
    )


class CommandRepoFixture:
    """Bare remote + source repo containing the allowlisted generation script."""

    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.bare_remote = root / "remote.git"
        self.source_repo = root / "source"
        self.mount_root = root / "mount"
        self.mount_root.mkdir()
        self.artifacts_root = self.mount_root / "artifacts"
        self.artifacts_root.mkdir()

        _run_git(["init", "--bare", str(self.bare_remote)])
        self.source_repo.mkdir()
        _run_git(["init", str(self.source_repo)])
        _run_git(
            ["-C", str(self.source_repo), "config", "user.email", "test@example.com"]
        )
        _run_git(["-C", str(self.source_repo), "config", "user.name", "Test User"])

        script_dir = self.source_repo / "scripts"
        script_dir.mkdir()
        script_path = script_dir / "generate_attorney_feedback_candidate.py"
        script_path.write_text(
            '''#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--case-root", type=Path, required=True)
parser.add_argument("--question-id", required=True)
parser.add_argument("--required-commit", required=True)
parser.add_argument("--candidate-output-root", type=Path, required=True)
parser.add_argument(
    "--authorize-private-evidence-transmission",
    required=True,
)
parser.add_argument("--generation-only", action="store_true", required=True)
args = parser.parse_args()

# Test-only probe: sleep so the command runner timeout path can be exercised
# without adding non-allowlisted CLI flags.
if args.question_id == "__TIMEOUT_PROBE__":
    time.sleep(2.0)

out = Path(args.candidate_output_root)
out.mkdir(parents=True, exist_ok=True)
artifact = out / "candidate.json"
payload = {
    "question_id": args.question_id,
    "required_commit": args.required_commit,
    "marker_env": os.environ.get("PYTHONUNBUFFERED"),
    "secret_env_present": "OPENAI_API_KEY" in os.environ,
}
artifact.write_text(json.dumps(payload), encoding="utf-8")
print(f"wrote:{artifact}")
raise SystemExit(0)
''',
            encoding="utf-8",
        )
        (self.source_repo / "README.md").write_text("legal-ai fixture\n", encoding="utf-8")
        case_root = self.source_repo / "data" / "case"
        case_root.mkdir(parents=True)
        (case_root / "placeholder.txt").write_text("case\n", encoding="utf-8")

        _run_git(["-C", str(self.source_repo), "add", "-A"])
        _run_git(["-C", str(self.source_repo), "commit", "-m", "init"])
        _run_git(["-C", str(self.source_repo), "branch", "-M", "main"])
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
        _run_git(["-C", str(self.source_repo), "push", "-u", "origin", "main"])
        head = _run_git(["-C", str(self.source_repo), "rev-parse", "HEAD"])
        self.commit_sha = head.stdout.strip()

        self._previous_map = os.environ.get(REPOSITORY_URL_MAP_ENV)
        os.environ[REPOSITORY_URL_MAP_ENV] = json.dumps(
            {
                "nhpcorp35/legal-ai": str(self.bare_remote),
                "legal-ai": str(self.bare_remote),
            }
        )
        self._previous_mounts = os.environ.get("MISSION_CONTROL_MOUNTED_PATHS")
        os.environ["MISSION_CONTROL_MOUNTED_PATHS"] = str(self.mount_root)

    def close(self) -> None:
        if self._previous_map is None:
            os.environ.pop(REPOSITORY_URL_MAP_ENV, None)
        else:
            os.environ[REPOSITORY_URL_MAP_ENV] = self._previous_map
        if self._previous_mounts is None:
            os.environ.pop("MISSION_CONTROL_MOUNTED_PATHS", None)
        else:
            os.environ["MISSION_CONTROL_MOUNTED_PATHS"] = self._previous_mounts
        self.temp.cleanup()

    def allowlisted_argv(
        self,
        *,
        candidate_output_root: str | None = None,
        auth: str = "I_AUTHORIZE_PRIVATE_EVIDENCE_TRANSMISSION_TO_MODEL_PROVIDER",
        question_id: str = "Q1",
    ) -> list[str]:
        out = candidate_output_root or str(self.artifacts_root / "out")
        return [
            "python3",
            ALLOWED_SCRIPT,
            "--case-root",
            "data/case",
            "--question-id",
            question_id,
            "--required-commit",
            self.commit_sha,
            "--candidate-output-root",
            out,
            "--authorize-private-evidence-transmission",
            auth,
            "--generation-only",
        ]


class TestCommandRunner(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CommandRepoFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_successful_allowlisted_execution(self) -> None:
        result = run_repository_command(
            RepositoryCommandSpec(
                repository="nhpcorp35/legal-ai",
                ref=self.fixture.commit_sha,
                argv=self.fixture.allowlisted_argv(),
                allowed_env_names=["PYTHONUNBUFFERED"],
            )
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.checkout_commit, self.fixture.commit_sha)
        self.assertEqual(result.exit_code, 0)
        self.assertIsNotNone(result.run_id)
        self.assertIn("wrote:", result.stdout)
        self.assertEqual(result.persistence["mode"], "none")
        self.assertFalse(result.persistence["attempted"])
        self.assertFalse(result.persistence["pushed"])
        self.assertIsNone(result.persistence["commit_sha"])

    def test_wrong_commit_rejected(self) -> None:
        wrong = "0123456789abcdef0123456789abcdef01234567"
        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=wrong,
                argv=self.fixture.allowlisted_argv(),
            )
        )
        self.assertFalse(result.ok)
        self.assertIn(result.error_code, {"WRONG_COMMIT", "CHECKOUT_FAILED"})

    def test_non_allowlisted_executable_rejected(self) -> None:
        argv = self.fixture.allowlisted_argv()
        argv[0] = "bash"
        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=self.fixture.commit_sha,
                argv=argv,
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "EXECUTABLE_NOT_ALLOWLISTED")

    def test_shell_metacharacters_rejected(self) -> None:
        argv = self.fixture.allowlisted_argv()
        argv[5] = "Q1; rm -rf /"
        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=self.fixture.commit_sha,
                argv=argv,
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "SHELL_METACHARACTERS")

    def test_path_traversal_rejected(self) -> None:
        argv = [
            "python3",
            "../etc/passwd",
            "--generation-only",
        ]
        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=self.fixture.commit_sha,
                argv=argv,
            )
        )
        self.assertFalse(result.ok)
        self.assertIn(
            result.error_code,
            {"PATH_TRAVERSAL", "SCRIPT_NOT_ALLOWLISTED"},
        )

    def test_timeout(self) -> None:
        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=self.fixture.commit_sha,
                argv=self.fixture.allowlisted_argv(
                    question_id="__TIMEOUT_PROBE__"
                ),
                timeout_seconds=0.3,
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "TIMEOUT")
        self.assertIsNone(result.exit_code)
        self.assertEqual(result.persistence["mode"], "none")

    def test_environment_name_allowlist(self) -> None:
        with self.assertRaises(Exception) as ctx:
            build_command_env(["MISSION_CONTROL_API_KEY"])
        self.assertIn("not permitted", str(ctx.exception))

        with self.assertRaises(Exception) as ctx:
            build_command_env(["NOT_A_REAL_ENV_NAME"])
        self.assertIn("not allowlisted", str(ctx.exception))

        with patch.dict(
            os.environ,
            {
                "PYTHONUNBUFFERED": "1",
                "OPENAI_API_KEY": "sk-test-secret",
            },
            clear=False,
        ):
            env = build_command_env(["PYTHONUNBUFFERED"])
        self.assertEqual(env.get("PYTHONUNBUFFERED"), "1")
        self.assertNotIn("OPENAI_API_KEY", env)

        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=self.fixture.commit_sha,
                argv=self.fixture.allowlisted_argv(),
                allowed_env_names=["NOT_ALLOWED_NAME"],
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "ENV_NAME_NOT_ALLOWLISTED")

    def test_redaction(self) -> None:
        secret = "I_AUTHORIZE_PRIVATE_EVIDENCE_TRANSMISSION_TO_MODEL_PROVIDER"
        argv = self.fixture.allowlisted_argv(auth=secret)
        redacted = redact_argv(argv)
        self.assertIn(REDACTED, redacted)
        self.assertNotIn(secret, redacted)

        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=self.fixture.commit_sha,
                argv=argv,
            )
        )
        self.assertTrue(result.ok, result.error)
        self.assertIn(REDACTED, result.argv)
        self.assertNotIn(secret, result.argv)
        # Must not appear in returned stderr either as an echo of argv.
        self.assertNotIn(secret, result.stderr)

    def test_artifact_reporting(self) -> None:
        out = str(self.fixture.artifacts_root / "run-artifacts")
        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=self.fixture.commit_sha,
                argv=self.fixture.allowlisted_argv(candidate_output_root=out),
            )
        )
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.artifact_paths)
        self.assertTrue(
            any(path.endswith("candidate.json") for path in result.artifact_paths)
        )

    def test_persistence_none(self) -> None:
        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=self.fixture.commit_sha,
                argv=self.fixture.allowlisted_argv(),
            )
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(
            result.persistence,
            {
                "mode": "none",
                "attempted": False,
                "ok": True,
                "commit_sha": None,
                "pushed": False,
            },
        )

    def test_prepare_ephemeral_checkout_detached_ref(self) -> None:
        prep = prepare_ephemeral_checkout(
            repository_url=str(self.fixture.bare_remote),
            ref=self.fixture.commit_sha,
        )
        self.assertTrue(prep.ok, prep.error)
        assert prep.workspace_path is not None
        head = _run_git(["-C", prep.workspace_path, "rev-parse", "HEAD"])
        self.assertEqual(head.stdout.strip(), self.fixture.commit_sha)
        from mission_control.workspace import cleanup_workspace

        cleanup_workspace(prep.workspace_path)

    def test_pipeline_token_rejected(self) -> None:
        argv = self.fixture.allowlisted_argv()
        argv.append("|")
        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=self.fixture.commit_sha,
                argv=argv,
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "SHELL_METACHARACTERS")


class TestRepositoryCommandApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["MISSION_CONTROL_API_KEY"] = "test-command-runner-key"

    def setUp(self) -> None:
        self.fixture = CommandRepoFixture()
        import app.api as api_module
        from fastapi.testclient import TestClient
        from mission_control.run_registry import RunRegistry

        self.api_module = api_module
        self._previous_registry = api_module.run_registry
        api_module.run_registry = RunRegistry(
            db_path=str(Path(self.fixture.temp.name) / "runs.db")
        )
        self.client = TestClient(api_module.app)

    def tearDown(self) -> None:
        self.api_module.run_registry = self._previous_registry
        self.fixture.close()

    def test_api_allowlisted_execution(self) -> None:
        response = self.client.post(
            "/repository-commands",
            headers={"Authorization": "Bearer test-command-runner-key"},
            json={
                "repository": "nhpcorp35/legal-ai",
                "ref": self.fixture.commit_sha,
                "argv": self.fixture.allowlisted_argv(),
                "working_directory": ".",
                "timeout_seconds": 30,
                "allowed_env_names": ["PYTHONUNBUFFERED"],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["checkout_commit"], self.fixture.commit_sha)
        self.assertEqual(body["persistence"]["mode"], "none")
        self.assertTrue(body["run_id"])
        self.assertIn(REDACTED, body["argv"])


if __name__ == "__main__":
    unittest.main()
