"""Validate YAML examples embedded in docs/CANONICAL_MISSION_SCHEMA.md."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

from mission_control.validator import (
    load_mission_yaml,
    validate_mission_for_execute,
    validate_mission_for_run,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DOC = REPO_ROOT / "docs" / "CANONICAL_MISSION_SCHEMA.md"

# Fenced YAML blocks under "## 9. Minimal valid YAML examples"
_EXAMPLE_HEADINGS = (
    "### 9.1 Inspection / planning",
    "### 9.2 Execute with `persistence.mode: none`",
    "### 9.3 Execute with `persistence.mode: commit`",
    "### 9.4 Execute with `persistence.mode: push`",
)


def _extract_section_yaml(markdown: str, heading: str) -> str:
    pattern = re.compile(
        rf"^{re.escape(heading)}.*?```yaml\n(.*?)```",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(markdown)
    if match is None:
        raise AssertionError(f"Missing YAML example for heading: {heading}")
    return match.group(1)


def _with_repo_path(mission: dict) -> dict:
    repository = dict(mission["repository"])
    repository["path"] = str(REPO_ROOT)
    return {**mission, "repository": repository}


class TestCanonicalMissionSchemaExamples(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.markdown = SCHEMA_DOC.read_text(encoding="utf-8")

    def test_schema_doc_exists(self) -> None:
        self.assertTrue(SCHEMA_DOC.is_file())

    def test_plan_example_valid_for_run(self) -> None:
        raw = _extract_section_yaml(self.markdown, _EXAMPLE_HEADINGS[0])
        result, mission = load_mission_yaml(raw)
        self.assertTrue(result.ok, result.error)
        assert mission is not None
        run_result = validate_mission_for_run(_with_repo_path(mission))
        self.assertTrue(run_result.ok, run_result.error)
        self.assertEqual(mission["execution"]["mode"], "plan")

    def test_execute_examples_valid_for_execute(self) -> None:
        expected_modes = ("none", "commit", "push")
        for heading, mode in zip(_EXAMPLE_HEADINGS[1:], expected_modes):
            with self.subTest(mode=mode):
                raw = _extract_section_yaml(self.markdown, heading)
                result, mission = load_mission_yaml(raw)
                self.assertTrue(result.ok, result.error)
                assert mission is not None
                self.assertEqual(mission["persistence"]["mode"], mode)
                self.assertEqual(mission["execution"]["mode"], "execute")
                execute_result = validate_mission_for_execute(
                    _with_repo_path(mission)
                )
                self.assertTrue(execute_result.ok, execute_result.error)
                if mode == "push":
                    self.assertTrue(
                        mission["approval"].get("platform_push_approved")
                    )

    def test_examples_are_parseable_mappings(self) -> None:
        for heading in _EXAMPLE_HEADINGS:
            with self.subTest(heading=heading):
                raw = _extract_section_yaml(self.markdown, heading)
                data = yaml.safe_load(raw)
                self.assertIsInstance(data, dict)


if __name__ == "__main__":
    unittest.main()
