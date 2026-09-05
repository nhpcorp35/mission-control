import ast
import pathlib
import unittest


class VerifiedCaseSourceMapLimitTests(unittest.TestCase):
    def test_source_map_limit_covers_complete_verified_matters(self):
        source = pathlib.Path(__file__).with_name("server.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        limit = next(
            node.value.value
            for node in module.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "MAX_SOURCE_MAP_DOCUMENTS"
                for target in node.targets
            )
        )
        self.assertGreaterEqual(limit, 415)

    def test_source_map_streams_page_records_instead_of_loading_whole_index(self):
        source = pathlib.Path(__file__).with_name("server.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        calls = [node for node in ast.walk(module) if isinstance(node, ast.Call)]
        self.assertTrue(
            any(
                isinstance(call.func, ast.Attribute) and call.func.attr == "iter_lines"
                for call in calls
            )
        )
        self.assertNotIn("MAX_SOURCE_MAP_INDEX_BYTES", source)


if __name__ == "__main__":
    unittest.main()
