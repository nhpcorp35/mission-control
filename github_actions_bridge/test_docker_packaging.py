"""Regression guard for Bridge Docker packaging."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path


BRIDGE_DIR = Path(__file__).resolve().parent
DOCKERFILE = BRIDGE_DIR / "Dockerfile"


def _copied_modules() -> set[str]:
    copied: set[str] = set()
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        parts = raw.strip().split()
        if len(parts) >= 3 and parts[0].upper() == "COPY":
            copied.update(Path(part).stem for part in parts[1:-1] if part.endswith(".py"))
    return copied


def _local_imports(path: Path, local: set[str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".", 1)[0])
    return imported & local


def _required_modules() -> set[str]:
    local = {path.stem for path in BRIDGE_DIR.glob("*.py")}
    required, pending = {"server"}, ["server"]
    while pending:
        module = pending.pop()
        for dependency in _local_imports(BRIDGE_DIR / f"{module}.py", local):
            if dependency not in required:
                required.add(dependency)
                pending.append(dependency)
    return required


class BridgeDockerPackagingTests(unittest.TestCase):
    def test_dockerfile_packages_all_local_runtime_imports(self) -> None:
        missing = sorted(_required_modules() - _copied_modules())
        self.assertFalse(missing, f"Bridge Dockerfile omits runtime modules: {missing}")

    def test_framework_helpers_are_packaged_regression_anchor(self) -> None:
        copied = _copied_modules()
        self.assertTrue({"framework_evidence", "framework_conflicts"} <= copied)


if __name__ == "__main__":
    unittest.main()
