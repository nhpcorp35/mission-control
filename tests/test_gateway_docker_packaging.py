"""Regression: gateway Docker packaging must include production imports.

The Railway healthcheck failure on deployment d1558bbd-b92c-4367-92ec-12a5389bb65c
was ModuleNotFoundError for hal_legalai_gateway.readonly_plan_normalization
because Dockerfile used an explicit per-file COPY list that omitted the module
introduced at merge 76b75533cbcc36eff26e873618787fd05bff1342.
"""

from __future__ import annotations

import ast
import fnmatch
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY_DIR = REPO_ROOT / "hal_legalai_gateway"
DOCKERFILE = GATEWAY_DIR / "Dockerfile"
DOCKERIGNORE = GATEWAY_DIR / ".dockerignore"
PACKAGE_NAME = "hal_legalai_gateway"

# Historical explicit COPY sources that omitted readonly_plan_normalization.py
# (the packaging contract at the failing merge).
LEGACY_EXPLICIT_COPY_MODULES = frozenset(
    {
        "__init__",
        "config",
        "health",
        "registry",
        "request_context",
        "server",
        "auth",
        "forwarding",
        "mcp_server",
    }
)

FORBIDDEN_CONTEXT_NAMES = frozenset(
    {
        ".env",
        ".venv",
        "venv",
        "tests",
        "secrets",
        "__pycache__",
    }
)


def _parse_dockerignore_patterns(text: str) -> list[str]:
    patterns: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def _ignored_by_dockerignore(rel_path: str, patterns: list[str]) -> bool:
    name = Path(rel_path).name
    for pattern in patterns:
        if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(name, pattern):
            return True
        # Directory-style patterns ("tests") should match that path prefix.
        if "/" not in pattern.rstrip("/") and rel_path.split("/", 1)[0] == pattern.rstrip(
            "/"
        ):
            return True
    return False


def _dockerfile_uses_package_directory_copy(dockerfile_text: str) -> bool:
    for raw in dockerfile_text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if line.upper().startswith("COPY ") and " ./hal_legalai_gateway/" in line:
            sources = line.split()[1:-1]
            if any(src == "." for src in sources):
                return True
    return False


def _explicit_copied_py_modules(dockerfile_text: str) -> set[str]:
    """Parse brittle per-file COPY lists into module stem names."""
    modules: set[str] = set()
    for raw in dockerfile_text.splitlines():
        line = raw.strip()
        if line.startswith("#") or not line.upper().startswith("COPY "):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        dest = parts[-1]
        if "hal_legalai_gateway" not in dest:
            continue
        for src in parts[1:-1]:
            if src.endswith(".py"):
                modules.add(Path(src).stem)
    return modules


def _packaged_py_modules(
    dockerfile_text: str,
    *,
    ignore_patterns: list[str],
) -> set[str]:
    if _dockerfile_uses_package_directory_copy(dockerfile_text):
        modules: set[str] = set()
        for path in GATEWAY_DIR.glob("*.py"):
            rel = path.name
            if _ignored_by_dockerignore(rel, ignore_patterns):
                continue
            modules.add(path.stem)
        return modules
    return _explicit_copied_py_modules(dockerfile_text)


def _production_imported_package_modules() -> set[str]:
    """Modules under hal_legalai_gateway referenced by production package imports."""
    imported: set[str] = set()
    for path in sorted(GATEWAY_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == PACKAGE_NAME:
                    for alias in node.names:
                        imported.add(alias.name)
                elif node.module.startswith(PACKAGE_NAME + "."):
                    imported.add(node.module.split(".", 1)[1].split(".", 1)[0])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == PACKAGE_NAME:
                        continue
                    if alias.name.startswith(PACKAGE_NAME + "."):
                        imported.add(alias.name.split(".", 1)[1].split(".", 1)[0])
    # Only keep imports that resolve to local package modules (not attributes).
    local = {p.stem for p in GATEWAY_DIR.glob("*.py")}
    return imported & local


def _simulate_packaged_tree(packaged_modules: set[str]) -> Path:
    """Build a temp tree mirroring image layout for import smoke tests."""
    root = Path(tempfile.mkdtemp(prefix="gateway-docker-packaging-"))
    pkg = root / PACKAGE_NAME
    pkg.mkdir(parents=True)
    for stem in packaged_modules:
        src = GATEWAY_DIR / f"{stem}.py"
        if src.is_file():
            shutil.copy2(src, pkg / src.name)
    registry = GATEWAY_DIR / "registry.json"
    if registry.is_file():
        shutil.copy2(registry, pkg / registry.name)
    return root


class GatewayDockerPackagingTests(unittest.TestCase):
    def test_dockerfile_uses_safe_package_directory_copy(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertTrue(
            _dockerfile_uses_package_directory_copy(text),
            "Dockerfile should COPY the package directory (.) into "
            "./hal_legalai_gateway/ so new production modules are included",
        )
        self.assertTrue(
            DOCKERIGNORE.is_file(),
            ".dockerignore is required to keep secrets/tests/local state out",
        )
        patterns = _parse_dockerignore_patterns(
            DOCKERIGNORE.read_text(encoding="utf-8")
        )
        for required in (".env", ".env.*", "__pycache__", "tests", "*.pem"):
            self.assertIn(required, patterns)

    def test_packaged_modules_cover_production_imports(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        patterns = _parse_dockerignore_patterns(
            DOCKERIGNORE.read_text(encoding="utf-8")
        )
        packaged = _packaged_py_modules(dockerfile, ignore_patterns=patterns)
        required = _production_imported_package_modules()
        missing = sorted(required - packaged)
        self.assertFalse(
            missing,
            f"Dockerfile packaging omits imported modules: {missing}",
        )
        self.assertIn(
            "readonly_plan_normalization",
            packaged,
            "readonly_plan_normalization.py must be packaged (Railway healthcheck)",
        )

    def test_legacy_explicit_copy_at_failing_merge_would_break_contract(self) -> None:
        """Prove the pre-fix per-file manifest fails today's import contract."""
        required = _production_imported_package_modules()
        missing = sorted(required - LEGACY_EXPLICIT_COPY_MODULES)
        self.assertIn(
            "readonly_plan_normalization",
            missing,
            "Expected legacy COPY list to omit readonly_plan_normalization "
            "(regression anchor for merge 76b75533)",
        )
        self.assertTrue(
            missing,
            "Legacy explicit COPY list should fail the packaging contract",
        )

    def test_build_context_has_no_forbidden_entries(self) -> None:
        """Package-directory COPY is only safe if context stays clean."""
        patterns = _parse_dockerignore_patterns(
            DOCKERIGNORE.read_text(encoding="utf-8")
        )
        offenders: list[str] = []
        for path in GATEWAY_DIR.rglob("*"):
            rel = str(path.relative_to(GATEWAY_DIR))
            name = path.name
            if name in FORBIDDEN_CONTEXT_NAMES or rel.split("/", 1)[0] in FORBIDDEN_CONTEXT_NAMES:
                if not _ignored_by_dockerignore(rel, patterns):
                    offenders.append(rel)
        self.assertEqual(
            offenders,
            [],
            f"Forbidden context paths not covered by .dockerignore: {offenders}",
        )

    def test_packaging_simulation_import_smoke(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        patterns = _parse_dockerignore_patterns(
            DOCKERIGNORE.read_text(encoding="utf-8")
        )
        packaged = _packaged_py_modules(dockerfile, ignore_patterns=patterns)
        self.assertIn("readonly_plan_normalization", packaged)
        self.assertIn("mcp_server", packaged)

        simulated_root = _simulate_packaged_tree(packaged)
        try:
            # Subprocess keeps sys.modules in this runner pristine for sibling
            # gateway tests (in-process reload breaks mock.patch targets).
            probe = textwrap.dedent(
                f"""
                import importlib
                mod = importlib.import_module(
                    "{PACKAGE_NAME}.readonly_plan_normalization"
                )
                assert hasattr(mod, "normalize_readonly_plan_mission_yaml")
                mcp = importlib.import_module("{PACKAGE_NAME}.mcp_server")
                assert hasattr(mcp, "create_mcp_server")
                print("packaging-import-smoke-ok")
                """
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(simulated_root)
            result = subprocess.run(
                [sys.executable, "-c", probe],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=(
                    "packaged-tree import smoke failed\n"
                    f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                ),
            )
            self.assertIn("packaging-import-smoke-ok", result.stdout)
        finally:
            shutil.rmtree(simulated_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
