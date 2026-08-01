"""Regression: Railway/Railpack OCR runtime for LegalAI PDF page extraction.

Verifies Railpack deploy apt packages (the builder actually used on Railway)
and Python OCR integrations are declared so the deployed executor can invoke
tesseract and import pytesseract/pdf2image from agent shells. Does not run
OCR (no PDF conversion or recognition).

Nixpacks aptPkgs alone are insufficient: Railpack ignores nixpacks.toml.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RAILPACK_JSON = REPO_ROOT / "railpack.json"
REQUIREMENTS = REPO_ROOT / "requirements.txt"

# Minimal OCR stack: system binary + PDF rasterizer + Python bindings.
REQUIRED_APT_OCR = ("tesseract-ocr", "tesseract-ocr-eng", "poppler-utils")
REQUIRED_PIP_OCR = ("pytesseract", "pdf2image")


class TestExecutorOcrRuntime(unittest.TestCase):
    def test_railpack_deploy_apt_packages_include_ocr_stack(self) -> None:
        self.assertTrue(
            RAILPACK_JSON.is_file(),
            msg=(
                "railpack.json is required: Railway builds with Railpack, "
                "which ignores nixpacks.toml aptPkgs"
            ),
        )
        config = json.loads(RAILPACK_JSON.read_text(encoding="utf-8"))
        apt_pkgs = config["deploy"]["aptPackages"]
        self.assertIn(
            "...",
            apt_pkgs,
            msg=(
                "deploy.aptPackages must include '...' so Railpack extends "
                "its default runtime apt set instead of replacing it"
            ),
        )
        for pkg in REQUIRED_APT_OCR:
            self.assertIn(
                pkg,
                apt_pkgs,
                msg=(
                    f"railpack.json deploy.aptPackages must include {pkg} "
                    "for LegalAI PDF-page OCR on the Railway executor "
                    "(nixpacks.toml alone is ignored by Railpack)"
                ),
            )

    def test_requirements_declare_ocr_packages(self) -> None:
        text = REQUIREMENTS.read_text(encoding="utf-8")
        for name in REQUIRED_PIP_OCR:
            pins = re.findall(rf"(?m)^{re.escape(name)}(?:==\S+)?$", text)
            self.assertEqual(
                pins,
                [name],
                msg=(
                    f"requirements.txt must declare unpinned {name} for "
                    "agent OCR imports via the venv /usr/local/bin wrappers"
                ),
            )
        # Existing PDF text path must remain declared.
        pypdf_pins = re.findall(r"(?m)^pypdf(?:==\S+)?$", text)
        self.assertEqual(pypdf_pins, ["pypdf"])

    def test_tesseract_resolves_when_installed(self) -> None:
        """On the deployed image, tesseract must be on PATH; skip locally if absent."""
        tesseract_path = shutil.which("tesseract")
        if tesseract_path is None:
            self.skipTest("tesseract not installed in this environment")
        version = subprocess.run(
            [tesseract_path, "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
        combined = f"{version.stdout}\n{version.stderr}".lower()
        self.assertIn("tesseract", combined)
        self.assertTrue(Path(tesseract_path).is_file())

    def test_pdftoppm_resolves_when_installed(self) -> None:
        """pdf2image needs poppler's pdftoppm; skip locally if absent."""
        pdftoppm_path = shutil.which("pdftoppm")
        if pdftoppm_path is None:
            self.skipTest("pdftoppm not installed in this environment")
        version = subprocess.run(
            [pdftoppm_path, "-v"],
            check=False,
            capture_output=True,
            text=True,
        )
        combined = f"{version.stdout}\n{version.stderr}".lower()
        self.assertIn("poppler", combined)
        self.assertTrue(Path(pdftoppm_path).is_file())

    def test_ocr_python_packages_importable_when_installed(self) -> None:
        """Import check only — does not invoke OCR or convert PDFs."""
        missing = [
            name
            for name in REQUIRED_PIP_OCR
            if importlib.util.find_spec(name) is None
        ]
        if missing:
            self.skipTest(
                "OCR Python packages not installed in this environment: "
                + ", ".join(missing)
            )
        import pdf2image  # noqa: F401
        import pytesseract  # noqa: F401

        self.assertTrue(callable(getattr(pytesseract, "get_tesseract_version", None)))
        self.assertTrue(callable(getattr(pdf2image, "convert_from_path", None)))


if __name__ == "__main__":
    unittest.main()
