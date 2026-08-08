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
    ALLOWED_CASE00_B2_Q1_SCRIPT,
    ALLOWED_REBUILD_SCRIPT,
    ALLOWED_SCRIPT,
    AUTHORIZATION_ACK,
    AUTHORIZATION_CONFIRMED_FLAG,
    AUTHORIZATION_FLAG,
    CANDIDATE_B2_PREFIX_FLAG,
    CommandRunnerError,
    GENERATION_ONLY_FLAG,
    LEGALAI_DEFAULT_CANDIDATE_B2_PREFIX,
    REDACTED,
    REPOSITORY_URL_MAP_ENV,
    RepositoryCommandSpec,
    build_command_env,
    redact_argv,
    run_repository_command,
    validate_and_build_argv,
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
            f'''#!/usr/bin/env python3
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
    "{AUTHORIZATION_FLAG}",
    required=True,
)
parser.add_argument("{GENERATION_ONLY_FLAG}", action="store_true", required=True)
args = parser.parse_args()

# Test-only probe: sleep so the command runner timeout path can be exercised
# without adding non-allowlisted CLI flags.
if args.question_id == "__TIMEOUT_PROBE__":
    time.sleep(2.0)

out = Path(args.candidate_output_root)
out.mkdir(parents=True, exist_ok=True)
artifact = out / "candidate.json"
payload = {{
    "question_id": args.question_id,
    "required_commit": args.required_commit,
    "marker_env": os.environ.get("PYTHONUNBUFFERED"),
    "secret_env_present": "OPENAI_API_KEY" in os.environ,
}}
artifact.write_text(json.dumps(payload), encoding="utf-8")
print(f"wrote:{{artifact}}")
raise SystemExit(0)
''',
            encoding="utf-8",
        )
        rebuild_path = script_dir / "rebuild_case00_derived.py"
        rebuild_path.write_text(
            '''#!/usr/bin/env python3
import argparse
import json
import os
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--case-root", required=True)
parser.add_argument("--source-dir", default=None)
parser.add_argument("--b2-prefix", nargs="?", const="DEFAULT", default=None)
parser.add_argument("--validate-only", action="store_true")
args = parser.parse_args()
if args.validate_only:
    mode = "validate-only"
elif args.b2_prefix is not None:
    mode = "b2"
else:
    mode = "local"
payload = {
    "ok": True,
    "mode": mode,
    "case_root": args.case_root,
    "source_dir": args.source_dir,
    "b2_env_present": "B2_KEY_ID" in os.environ,
}
print(json.dumps(payload))
raise SystemExit(0)
''',
            encoding="utf-8",
        )
        case00_b2_q1_path = script_dir / "run_case00_b2_q1.py"
        case00_b2_q1_path.write_text(
            f'''#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--case-root", type=Path, required=True)
parser.add_argument("--question-id", required=True)
parser.add_argument("--required-commit", required=True)
parser.add_argument("--candidate-output-root", type=Path, required=True)
parser.add_argument(
    "{CANDIDATE_B2_PREFIX_FLAG}",
    default="{LEGALAI_DEFAULT_CANDIDATE_B2_PREFIX}",
)
parser.add_argument(
    "{AUTHORIZATION_CONFIRMED_FLAG}",
    action="store_true",
    required=True,
)
parser.add_argument("{GENERATION_ONLY_FLAG}", action="store_true", required=True)
args = parser.parse_args()

out = Path(args.candidate_output_root)
out.mkdir(parents=True, exist_ok=True)
artifact = out / "case00_b2_q1.json"
payload = {{
    "ok": True,
    "question_id": args.question_id,
    "required_commit": args.required_commit,
    "case_root": str(args.case_root),
    "candidate_b2_prefix": args.candidate_b2_prefix,
    "b2_env_present": "B2_KEY_ID" in os.environ,
    "openai_env_present": "OPENAI_API_KEY" in os.environ,
    "openai_model": os.environ.get("OPENAI_MODEL"),
    # Fixture echoes presence only — never credential values.
    "durable_artifacts": {{
        "prefix": args.candidate_b2_prefix,
        "object_keys": [
            args.candidate_b2_prefix.rstrip("/")
            + "/q1-candidate-fixture/Q1_candidate_answer.json"
        ],
    }},
}}
artifact.write_text(json.dumps(payload), encoding="utf-8")
print(json.dumps(payload))
raise SystemExit(0)
''',
            encoding="utf-8",
        )
        (self.source_repo / "README.md").write_text("legal-ai fixture\n", encoding="utf-8")
        case_root = self.source_repo / "data" / "case"
        case_root.mkdir(parents=True)
        (case_root / "placeholder.txt").write_text("case\n", encoding="utf-8")
        case00_root = self.source_repo / "data" / "case-00-triborough"
        case00_root.mkdir(parents=True)
        (case00_root / "placeholder.txt").write_text("case00\n", encoding="utf-8")
        source_dir = case00_root / "source-pdfs"
        source_dir.mkdir(parents=True)
        (source_dir / "doc.pdf").write_text("%PDF-1.4\n", encoding="utf-8")

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
        auth: str = AUTHORIZATION_ACK,
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
            AUTHORIZATION_FLAG,
            auth,
            GENERATION_ONLY_FLAG,
        ]

    def rebuild_argv(
        self,
        *extra: str,
        case_root: str = "data/case-00-triborough",
    ) -> list[str]:
        return [
            "python3",
            ALLOWED_REBUILD_SCRIPT,
            "--case-root",
            case_root,
            *extra,
        ]

    def case00_b2_q1_argv(
        self,
        *,
        case_root: str = "data/case-00-triborough",
        candidate_output_root: str = "out/case00-b2-q1",
        question_id: str = "Q1",
        include_generation_only: bool = True,
        include_authorization_confirmed: bool = True,
        required_commit: str | None = None,
        candidate_b2_prefix: str | None = None,
    ) -> list[str]:
        argv = [
            "python3",
            ALLOWED_CASE00_B2_Q1_SCRIPT,
            "--case-root",
            case_root,
            "--question-id",
            question_id,
            "--required-commit",
            required_commit or self.commit_sha,
            "--candidate-output-root",
            candidate_output_root,
        ]
        if candidate_b2_prefix is not None:
            argv.extend([CANDIDATE_B2_PREFIX_FLAG, candidate_b2_prefix])
        if include_authorization_confirmed:
            argv.append(AUTHORIZATION_CONFIRMED_FLAG)
        if include_generation_only:
            argv.append(GENERATION_ONLY_FLAG)
        return argv


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
            GENERATION_ONLY_FLAG,
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

    def test_authorization_and_generation_only_flags_accepted(self) -> None:
        """Approved LegalAI safety/generation flags pass argv validation."""
        argv = self.fixture.allowlisted_argv()
        self.assertIn(AUTHORIZATION_FLAG, argv)
        self.assertIn(GENERATION_ONLY_FLAG, argv)
        workspace = self.fixture.source_repo
        resolved, _cwd, _out = validate_and_build_argv(
            argv,
            workspace=workspace,
            working_directory=".",
            mounted=[self.fixture.mount_root],
        )
        self.assertIn(AUTHORIZATION_FLAG, resolved)
        self.assertIn(GENERATION_ONLY_FLAG, resolved)

    def test_unknown_flag_rejected(self) -> None:
        """Flags outside the generator allowlist remain rejected."""
        argv = self.fixture.allowlisted_argv()
        argv.append("--not-an-allowlisted-flag")
        with self.assertRaises(CommandRunnerError) as ctx:
            validate_and_build_argv(
                argv,
                workspace=self.fixture.source_repo,
                working_directory=".",
                mounted=[self.fixture.mount_root],
            )
        self.assertEqual(ctx.exception.code, "FLAG_NOT_ALLOWLISTED")

        # End-to-end runner path also rejects unknown flags.
        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=self.fixture.commit_sha,
                argv=argv,
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "FLAG_NOT_ALLOWLISTED")

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
        secret = AUTHORIZATION_ACK
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

    def test_rebuild_b2_invocation_accepted(self) -> None:
        """Case-00 B2 rebuild argv + approved B2 env names are accepted."""
        argv = self.fixture.rebuild_argv("--b2-prefix")
        resolved, _cwd, _out = validate_and_build_argv(
            argv,
            workspace=self.fixture.source_repo,
            working_directory=".",
            mounted=[self.fixture.mount_root],
        )
        self.assertEqual(resolved[0], "python3")
        self.assertTrue(resolved[1].endswith(ALLOWED_REBUILD_SCRIPT))
        self.assertIn("--b2-prefix", resolved)
        self.assertNotIn("--source-dir", resolved)

        b2_names = [
            "B2_KEY_ID",
            "B2_APPLICATION_KEY",
            "B2_BUCKET",
            "B2_ENDPOINT",
            "B2_REGION",
        ]
        with patch.dict(
            os.environ,
            {name: f"test-{name}" for name in b2_names},
            clear=False,
        ):
            env = build_command_env(b2_names, script=ALLOWED_REBUILD_SCRIPT)
            self.assertEqual(env.get("B2_KEY_ID"), "test-B2_KEY_ID")
            result = run_repository_command(
                RepositoryCommandSpec(
                    repository="nhpcorp35/legal-ai",
                    ref=self.fixture.commit_sha,
                    argv=argv,
                    allowed_env_names=b2_names,
                )
            )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.exit_code, 0)
        self.assertIn(ALLOWED_REBUILD_SCRIPT, result.argv)
        self.assertIn('"mode": "b2"', result.stdout)

    def test_rebuild_local_source_invocation_accepted(self) -> None:
        """Local --source-dir rebuild stays workspace-local and executes."""
        argv = self.fixture.rebuild_argv(
            "--source-dir",
            "data/case-00-triborough/source-pdfs",
        )
        resolved, _cwd, _out = validate_and_build_argv(
            argv,
            workspace=self.fixture.source_repo,
            working_directory=".",
            mounted=[self.fixture.mount_root],
        )
        self.assertIn("--source-dir", resolved)
        self.assertNotIn("--b2-prefix", resolved)

        result = run_repository_command(
            RepositoryCommandSpec(
                repository="nhpcorp35/legal-ai",
                ref=self.fixture.commit_sha,
                argv=argv,
            )
        )
        self.assertTrue(result.ok, result.error)
        self.assertIn('"mode": "local"', result.stdout)

    def test_rebuild_validate_only_accepted(self) -> None:
        """--validate-only with workspace-local --case-root is accepted."""
        argv = self.fixture.rebuild_argv("--validate-only")
        resolved, _cwd, _out = validate_and_build_argv(
            argv,
            workspace=self.fixture.source_repo,
            working_directory=".",
            mounted=[self.fixture.mount_root],
        )
        self.assertIn("--validate-only", resolved)

        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=self.fixture.commit_sha,
                argv=argv,
            )
        )
        self.assertTrue(result.ok, result.error)
        self.assertIn('"mode": "validate-only"', result.stdout)

    def test_rebuild_unknown_flag_rejected(self) -> None:
        """Rebuild allowlist rejects flags outside the Case-00 safe set."""
        argv = self.fixture.rebuild_argv(
            "--validate-only",
            "--inventory-path",
            "data/case-00-triborough/nyscef_filing_inventory.json",
        )
        with self.assertRaises(CommandRunnerError) as ctx:
            validate_and_build_argv(
                argv,
                workspace=self.fixture.source_repo,
                working_directory=".",
                mounted=[self.fixture.mount_root],
            )
        self.assertEqual(ctx.exception.code, "FLAG_NOT_ALLOWLISTED")

        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=self.fixture.commit_sha,
                argv=argv,
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "FLAG_NOT_ALLOWLISTED")

    def test_rebuild_path_escape_and_absolute_rejected(self) -> None:
        """Rebuild path args reject absolute paths and workspace escapes."""
        absolute = self.fixture.rebuild_argv(
            "--source-dir",
            str(self.fixture.mount_root / "outside"),
        )
        with self.assertRaises(CommandRunnerError) as abs_ctx:
            validate_and_build_argv(
                absolute,
                workspace=self.fixture.source_repo,
                working_directory=".",
                mounted=[self.fixture.mount_root],
            )
        self.assertEqual(abs_ctx.exception.code, "PATH_OUTSIDE_ALLOWED_ROOTS")

        escape = [
            "python3",
            ALLOWED_REBUILD_SCRIPT,
            "--case-root",
            "../outside-case",
            "--validate-only",
        ]
        with self.assertRaises(CommandRunnerError) as esc_ctx:
            validate_and_build_argv(
                escape,
                workspace=self.fixture.source_repo,
                working_directory=".",
                mounted=[self.fixture.mount_root],
            )
        self.assertEqual(esc_ctx.exception.code, "PATH_TRAVERSAL")

        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=self.fixture.commit_sha,
                argv=absolute,
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PATH_OUTSIDE_ALLOWED_ROOTS")

    def test_rebuild_unapproved_env_names_rejected(self) -> None:
        """Rebuild script accepts only B2 env names, not generation secrets."""
        with self.assertRaises(CommandRunnerError) as ctx:
            build_command_env(
                ["OPENAI_API_KEY"],
                script=ALLOWED_REBUILD_SCRIPT,
            )
        self.assertEqual(ctx.exception.code, "ENV_NAME_NOT_ALLOWLISTED")
        self.assertIn("not allowlisted", str(ctx.exception))

        with self.assertRaises(CommandRunnerError) as ctx:
            build_command_env(
                ["NOT_A_REAL_ENV_NAME"],
                script=ALLOWED_REBUILD_SCRIPT,
            )
        self.assertEqual(ctx.exception.code, "ENV_NAME_NOT_ALLOWLISTED")

        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=self.fixture.commit_sha,
                argv=self.fixture.rebuild_argv("--b2-prefix"),
                allowed_env_names=["OPENAI_API_KEY"],
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "ENV_NAME_NOT_ALLOWLISTED")

    def test_case00_b2_q1_single_shot_accepted(self) -> None:
        """Case-00 B2 Q1 single-shot argv + composed env names are accepted."""
        argv = self.fixture.case00_b2_q1_argv()
        resolved, _cwd, out = validate_and_build_argv(
            argv,
            workspace=self.fixture.source_repo,
            working_directory=".",
            mounted=[self.fixture.mount_root],
        )
        self.assertEqual(resolved[0], "python3")
        self.assertTrue(resolved[1].endswith(ALLOWED_CASE00_B2_Q1_SCRIPT))
        self.assertIn(GENERATION_ONLY_FLAG, resolved)
        self.assertIn(AUTHORIZATION_CONFIRMED_FLAG, resolved)
        self.assertNotIn(AUTHORIZATION_FLAG, resolved)
        self.assertIsNotNone(out)

        env_names = [
            "B2_KEY_ID",
            "B2_APPLICATION_KEY",
            "B2_BUCKET",
            "B2_ENDPOINT",
            "B2_REGION",
            "OPENAI_API_KEY",
            "OPENAI_MODEL",
            "OPENAI_TIMEOUT_SECONDS",
        ]
        with patch.dict(
            os.environ,
            {
                **{
                    name: f"test-{name}"
                    for name in env_names
                    if name not in {"OPENAI_MODEL", "OPENAI_TIMEOUT_SECONDS"}
                },
                "OPENAI_MODEL": "gpt-test",
                "OPENAI_TIMEOUT_SECONDS": "120",
            },
            clear=False,
        ):
            env = build_command_env(env_names, script=ALLOWED_CASE00_B2_Q1_SCRIPT)
            self.assertEqual(env.get("B2_KEY_ID"), "test-B2_KEY_ID")
            self.assertEqual(env.get("OPENAI_MODEL"), "gpt-test")
            self.assertEqual(env.get("OPENAI_TIMEOUT_SECONDS"), "120")
            result = run_repository_command(
                RepositoryCommandSpec(
                    repository="nhpcorp35/legal-ai",
                    ref=self.fixture.commit_sha,
                    argv=argv,
                    allowed_env_names=env_names,
                )
            )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.exit_code, 0)
        self.assertIn(ALLOWED_CASE00_B2_Q1_SCRIPT, result.argv)
        self.assertIn(AUTHORIZATION_CONFIRMED_FLAG, result.argv)
        self.assertNotIn(AUTHORIZATION_FLAG, result.argv)
        self.assertNotIn(AUTHORIZATION_ACK, result.argv)
        self.assertIn('"question_id": "Q1"', result.stdout)

    def test_case00_b2_q1_missing_generation_only_rejected(self) -> None:
        """Single-shot allowlist requires --generation-only."""
        argv = self.fixture.case00_b2_q1_argv(include_generation_only=False)
        with self.assertRaises(CommandRunnerError) as ctx:
            validate_and_build_argv(
                argv,
                workspace=self.fixture.source_repo,
                working_directory=".",
                mounted=[self.fixture.mount_root],
            )
        self.assertEqual(ctx.exception.code, "INVALID_ARGV")
        self.assertIn(GENERATION_ONLY_FLAG, str(ctx.exception))

        result = run_repository_command(
            RepositoryCommandSpec(
                repository="nhpcorp35/legal-ai",
                ref=self.fixture.commit_sha,
                argv=argv,
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_ARGV")

    def test_case00_b2_q1_missing_authorization_confirmed_rejected(self) -> None:
        """Single-shot allowlist requires --authorization-confirmed."""
        argv = self.fixture.case00_b2_q1_argv(include_authorization_confirmed=False)
        with self.assertRaises(CommandRunnerError) as ctx:
            validate_and_build_argv(
                argv,
                workspace=self.fixture.source_repo,
                working_directory=".",
                mounted=[self.fixture.mount_root],
            )
        self.assertEqual(ctx.exception.code, "INVALID_ARGV")
        self.assertIn(AUTHORIZATION_CONFIRMED_FLAG, str(ctx.exception))

        result = run_repository_command(
            RepositoryCommandSpec(
                repository="nhpcorp35/legal-ai",
                ref=self.fixture.commit_sha,
                argv=argv,
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_ARGV")

    def test_case00_b2_q1_old_authorization_token_flag_rejected(self) -> None:
        """Wrapper policy rejects the generator's token-bearing authorization flag."""
        argv = self.fixture.case00_b2_q1_argv()
        argv.extend([AUTHORIZATION_FLAG, AUTHORIZATION_ACK])
        with self.assertRaises(CommandRunnerError) as ctx:
            validate_and_build_argv(
                argv,
                workspace=self.fixture.source_repo,
                working_directory=".",
                mounted=[self.fixture.mount_root],
            )
        self.assertEqual(ctx.exception.code, "FLAG_NOT_ALLOWLISTED")
        self.assertIn(AUTHORIZATION_FLAG, str(ctx.exception))

        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=self.fixture.commit_sha,
                argv=argv,
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "FLAG_NOT_ALLOWLISTED")
        self.assertNotIn(AUTHORIZATION_ACK, result.argv)

    def test_case00_b2_q1_unknown_flag_rejected(self) -> None:
        """Flags outside the single-shot allowlist remain rejected."""
        argv = self.fixture.case00_b2_q1_argv()
        argv.extend(["--b2-prefix", "case-00"])
        with self.assertRaises(CommandRunnerError) as ctx:
            validate_and_build_argv(
                argv,
                workspace=self.fixture.source_repo,
                working_directory=".",
                mounted=[self.fixture.mount_root],
            )
        self.assertEqual(ctx.exception.code, "FLAG_NOT_ALLOWLISTED")

        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=self.fixture.commit_sha,
                argv=argv,
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "FLAG_NOT_ALLOWLISTED")

    def test_case00_b2_q1_path_escape_and_absolute_rejected(self) -> None:
        """Single-shot path args reject absolute paths and workspace escapes."""
        absolute = self.fixture.case00_b2_q1_argv(
            candidate_output_root=str(self.fixture.mount_root / "outside"),
        )
        with self.assertRaises(CommandRunnerError) as abs_ctx:
            validate_and_build_argv(
                absolute,
                workspace=self.fixture.source_repo,
                working_directory=".",
                mounted=[self.fixture.mount_root],
            )
        self.assertEqual(abs_ctx.exception.code, "PATH_OUTSIDE_ALLOWED_ROOTS")

        escape = self.fixture.case00_b2_q1_argv(case_root="../outside-case")
        with self.assertRaises(CommandRunnerError) as esc_ctx:
            validate_and_build_argv(
                escape,
                workspace=self.fixture.source_repo,
                working_directory=".",
                mounted=[self.fixture.mount_root],
            )
        self.assertEqual(esc_ctx.exception.code, "PATH_TRAVERSAL")

        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=self.fixture.commit_sha,
                argv=absolute,
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PATH_OUTSIDE_ALLOWED_ROOTS")

    def test_case00_b2_q1_unapproved_env_names_rejected(self) -> None:
        """Single-shot accepts timeout env names; unrelated names stay rejected."""
        with patch.dict(
            os.environ,
            {
                "OPENAI_TIMEOUT_SECONDS": "90",
                "LEGALAI_MODEL_TIMEOUT_SECONDS": "45",
            },
            clear=False,
        ):
            env = build_command_env(
                ["OPENAI_TIMEOUT_SECONDS"],
                script=ALLOWED_CASE00_B2_Q1_SCRIPT,
            )
            self.assertEqual(env.get("OPENAI_TIMEOUT_SECONDS"), "90")

            legalai_env = build_command_env(
                ["LEGALAI_MODEL_TIMEOUT_SECONDS"],
                script=ALLOWED_CASE00_B2_Q1_SCRIPT,
            )
        self.assertEqual(
            legalai_env.get("LEGALAI_MODEL_TIMEOUT_SECONDS"), "45"
        )

        with self.assertRaises(CommandRunnerError) as ctx:
            build_command_env(
                ["ANTHROPIC_API_KEY"],
                script=ALLOWED_CASE00_B2_Q1_SCRIPT,
            )
        self.assertEqual(ctx.exception.code, "ENV_NAME_NOT_ALLOWLISTED")
        self.assertIn("not allowlisted", str(ctx.exception))

        with self.assertRaises(CommandRunnerError) as ctx:
            build_command_env(
                ["NOT_A_REAL_ENV_NAME"],
                script=ALLOWED_CASE00_B2_Q1_SCRIPT,
            )
        self.assertEqual(ctx.exception.code, "ENV_NAME_NOT_ALLOWLISTED")

        result = run_repository_command(
            RepositoryCommandSpec(
                repository="legal-ai",
                ref=self.fixture.commit_sha,
                argv=self.fixture.case00_b2_q1_argv(),
                allowed_env_names=["ANTHROPIC_API_KEY"],
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "ENV_NAME_NOT_ALLOWLISTED")

    def test_case00_b2_q1_candidate_b2_prefix_accepted(self) -> None:
        """Exact LegalAI Q1 argv with --candidate-b2-prefix is allowlisted."""
        argv = self.fixture.case00_b2_q1_argv(
            candidate_b2_prefix=LEGALAI_DEFAULT_CANDIDATE_B2_PREFIX,
        )
        self.assertIn(CANDIDATE_B2_PREFIX_FLAG, argv)
        self.assertIn(LEGALAI_DEFAULT_CANDIDATE_B2_PREFIX, argv)

        resolved, _cwd, out = validate_and_build_argv(
            argv,
            workspace=self.fixture.source_repo,
            working_directory=".",
            mounted=[self.fixture.mount_root],
        )
        self.assertIn(CANDIDATE_B2_PREFIX_FLAG, resolved)
        flag_idx = resolved.index(CANDIDATE_B2_PREFIX_FLAG)
        self.assertEqual(resolved[flag_idx + 1], LEGALAI_DEFAULT_CANDIDATE_B2_PREFIX)
        # Prefix is not treated as a local path (value unchanged / unresolved).
        self.assertEqual(resolved[flag_idx + 1], argv[argv.index(CANDIDATE_B2_PREFIX_FLAG) + 1])
        self.assertIsNotNone(out)

        env_names = [
            "B2_KEY_ID",
            "B2_APPLICATION_KEY",
            "B2_BUCKET",
            "B2_ENDPOINT",
            "B2_REGION",
            "OPENAI_API_KEY",
            "OPENAI_MODEL",
        ]
        secret_values = {
            "B2_KEY_ID": "b2-key-id-secret-value",
            "B2_APPLICATION_KEY": "b2-app-key-secret-value",
            "B2_BUCKET": "legalai-corpus",
            "B2_ENDPOINT": "https://s3.us-east-005.backblazeb2.com",
            "B2_REGION": "us-east-005",
            "OPENAI_API_KEY": "sk-openai-secret-value",
            "OPENAI_MODEL": "gpt-test",
        }
        with patch.dict(os.environ, secret_values, clear=False):
            result = run_repository_command(
                RepositoryCommandSpec(
                    repository="nhpcorp35/legal-ai",
                    ref=self.fixture.commit_sha,
                    argv=argv,
                    allowed_env_names=env_names,
                )
            )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.exit_code, 0)
        self.assertIn(CANDIDATE_B2_PREFIX_FLAG, result.argv)
        self.assertIn(LEGALAI_DEFAULT_CANDIDATE_B2_PREFIX, result.argv)
        self.assertIn(LEGALAI_DEFAULT_CANDIDATE_B2_PREFIX, result.stdout)
        self.assertEqual(result.persistence["mode"], "none")
        self.assertFalse(result.persistence["attempted"])
        # B2 / model secrets must never appear in returned evidence.
        for secret in (
            "b2-key-id-secret-value",
            "b2-app-key-secret-value",
            "sk-openai-secret-value",
        ):
            self.assertNotIn(secret, result.argv)
            self.assertNotIn(secret, result.stdout)
            self.assertNotIn(secret, result.stderr)
            self.assertNotIn(secret, result.error or "")
        # Local artifact_paths are ephemeral evidence only — not durable B2 proof.
        self.assertTrue(result.artifact_paths)
        self.assertTrue(
            any(path.endswith("case00_b2_q1.json") for path in result.artifact_paths)
        )
        self.assertIn("durable_artifacts", result.stdout)

    def test_case00_b2_q1_candidate_b2_prefix_omitted_uses_wrapper_default(
        self,
    ) -> None:
        """Omitting --candidate-b2-prefix keeps LegalAI's canonical default."""
        argv = self.fixture.case00_b2_q1_argv()
        self.assertNotIn(CANDIDATE_B2_PREFIX_FLAG, argv)

        resolved, _cwd, _out = validate_and_build_argv(
            argv,
            workspace=self.fixture.source_repo,
            working_directory=".",
            mounted=[self.fixture.mount_root],
        )
        self.assertNotIn(CANDIDATE_B2_PREFIX_FLAG, resolved)

        result = run_repository_command(
            RepositoryCommandSpec(
                repository="nhpcorp35/legal-ai",
                ref=self.fixture.commit_sha,
                argv=argv,
            )
        )
        self.assertTrue(result.ok, result.error)
        self.assertNotIn(CANDIDATE_B2_PREFIX_FLAG, result.argv)
        self.assertIn(
            f'"candidate_b2_prefix": "{LEGALAI_DEFAULT_CANDIDATE_B2_PREFIX}"',
            result.stdout,
        )
        self.assertEqual(result.persistence["mode"], "none")
        self.assertFalse(result.persistence["attempted"])

    def test_case00_b2_q1_unsafe_candidate_b2_prefix_rejected(self) -> None:
        """Unsafe B2 object-prefix values are rejected before execution."""
        unsafe = [
            ("../escape/", "INVALID_ARGV"),
            ("Benchmarks/../other/", "INVALID_ARGV"),
            ("/tmp/case00-runs", "INVALID_ARGV"),
            ("~/Benchmarks/", "INVALID_ARGV"),
            ("prefix|rm", "SHELL_METACHARACTERS"),
            ("a/./b/", "INVALID_ARGV"),
            ("Benchmarks//gap/", "INVALID_ARGV"),
            (r"Benchmarks\Case-00", "SHELL_METACHARACTERS"),
        ]
        for bad, expected_code in unsafe:
            with self.subTest(bad=bad):
                argv = self.fixture.case00_b2_q1_argv(candidate_b2_prefix=bad)
                with self.assertRaises(CommandRunnerError) as ctx:
                    validate_and_build_argv(
                        argv,
                        workspace=self.fixture.source_repo,
                        working_directory=".",
                        mounted=[self.fixture.mount_root],
                    )
                self.assertEqual(ctx.exception.code, expected_code)

                result = run_repository_command(
                    RepositoryCommandSpec(
                        repository="legal-ai",
                        ref=self.fixture.commit_sha,
                        argv=argv,
                    )
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, expected_code)
                self.assertEqual(result.persistence["mode"], "none")

        # Empty prefix token is rejected as an empty argv entry.
        empty_argv = self.fixture.case00_b2_q1_argv(candidate_b2_prefix="x")
        idx = empty_argv.index(CANDIDATE_B2_PREFIX_FLAG)
        empty_argv[idx + 1] = ""
        with self.assertRaises(CommandRunnerError) as empty_ctx:
            validate_and_build_argv(
                empty_argv,
                workspace=self.fixture.source_repo,
                working_directory=".",
                mounted=[self.fixture.mount_root],
            )
        self.assertIn(empty_ctx.exception.code, {"INVALID_ARGV", "SHELL_METACHARACTERS"})

    def test_case00_b2_q1_prefix_does_not_weaken_auth_or_ref_gates(self) -> None:
        """Prefix support does not bypass authorization or checkout/ref gates."""
        missing_auth = self.fixture.case00_b2_q1_argv(
            candidate_b2_prefix=LEGALAI_DEFAULT_CANDIDATE_B2_PREFIX,
            include_authorization_confirmed=False,
        )
        with self.assertRaises(CommandRunnerError) as auth_ctx:
            validate_and_build_argv(
                missing_auth,
                workspace=self.fixture.source_repo,
                working_directory=".",
                mounted=[self.fixture.mount_root],
            )
        self.assertEqual(auth_ctx.exception.code, "INVALID_ARGV")
        self.assertIn(AUTHORIZATION_CONFIRMED_FLAG, str(auth_ctx.exception))

        wrong_sha = "a" * 40
        self.assertNotEqual(wrong_sha, self.fixture.commit_sha)
        result = run_repository_command(
            RepositoryCommandSpec(
                repository="nhpcorp35/legal-ai",
                ref=wrong_sha,
                argv=self.fixture.case00_b2_q1_argv(
                    candidate_b2_prefix=LEGALAI_DEFAULT_CANDIDATE_B2_PREFIX,
                    required_commit=wrong_sha,
                ),
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CHECKOUT_FAILED")


class TestRepositoryCommandApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._previous_api_key = os.environ.get("MISSION_CONTROL_API_KEY")
        os.environ["MISSION_CONTROL_API_KEY"] = "test-command-runner-key"

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._previous_api_key is None:
            os.environ.pop("MISSION_CONTROL_API_KEY", None)
        else:
            os.environ["MISSION_CONTROL_API_KEY"] = cls._previous_api_key

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

    def test_api_case00_b2_q1_candidate_b2_prefix(self) -> None:
        """API accepts durable prefix argv; secrets stay out of the response."""
        secret_env = {
            "B2_KEY_ID": "api-b2-key-id-secret",
            "B2_APPLICATION_KEY": "api-b2-app-key-secret",
            "B2_BUCKET": "legalai-corpus",
            "B2_ENDPOINT": "https://s3.example.test",
            "B2_REGION": "us-east-005",
            "OPENAI_API_KEY": "api-openai-secret",
            "OPENAI_MODEL": "gpt-test",
        }
        argv = self.fixture.case00_b2_q1_argv(
            candidate_b2_prefix=LEGALAI_DEFAULT_CANDIDATE_B2_PREFIX,
        )
        with patch.dict(os.environ, secret_env, clear=False):
            response = self.client.post(
                "/repository-commands",
                headers={"Authorization": "Bearer test-command-runner-key"},
                json={
                    "repository": "nhpcorp35/legal-ai",
                    "ref": self.fixture.commit_sha,
                    "argv": argv,
                    "working_directory": ".",
                    "timeout_seconds": 30,
                    "allowed_env_names": list(secret_env.keys()),
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["persistence"]["mode"], "none")
        self.assertFalse(body["persistence"]["attempted"])
        self.assertIn(CANDIDATE_B2_PREFIX_FLAG, body["argv"])
        self.assertIn(LEGALAI_DEFAULT_CANDIDATE_B2_PREFIX, body["argv"])
        for secret in (
            "api-b2-key-id-secret",
            "api-b2-app-key-secret",
            "api-openai-secret",
        ):
            self.assertNotIn(secret, json.dumps(body))
        # Ephemeral local paths are reported; they are not durable B2 proof.
        self.assertTrue(body["artifact_paths"])

    def test_api_rejects_unauthenticated_repository_commands(self) -> None:
        response = self.client.post(
            "/repository-commands",
            json={
                "repository": "nhpcorp35/legal-ai",
                "ref": self.fixture.commit_sha,
                "argv": self.fixture.case00_b2_q1_argv(
                    candidate_b2_prefix=LEGALAI_DEFAULT_CANDIDATE_B2_PREFIX,
                ),
            },
        )
        self.assertIn(response.status_code, {401, 403})


if __name__ == "__main__":
    unittest.main()
