import json
import unittest

from tools.verified_page_search_smoke import CASE_ID, SOURCE_SHA256, search_verified_pages


class _Response:
    status = 200

    def read(self):
        return b'{"ok": true, "results": [{"page": 7}]}'

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class VerifiedPageSearchSmokeTests(unittest.TestCase):
    def test_uses_fixed_case_and_service_authorization(self):
        seen = {}

        def opener(request, timeout):
            seen["request"] = request
            seen["timeout"] = timeout
            return _Response()

        status, result = search_verified_pages("riparian law", "Bearer secret-value", opener=opener)

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(seen["timeout"], 30)
        self.assertEqual(seen["request"].full_url, "https://hal-github-actions-bridge-production.up.railway.app/cases/verified/search")
        self.assertEqual(seen["request"].get_header("Authorization"), "Bearer secret-value")
        self.assertNotIn("secret-value", json.dumps(result))
        self.assertEqual(json.loads(seen["request"].data), {
            "case_id": CASE_ID,
            "source_sha256": SOURCE_SHA256,
            "query": "riparian law",
            "limit": 20,
        })

    def test_rejects_missing_token(self):
        with self.assertRaisesRegex(ValueError, "BRIDGE_SERVICE_TOKEN"):
            search_verified_pages("riparian law", "")


if __name__ == "__main__":
    unittest.main()
