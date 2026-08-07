"""Unit tests for Backblaze B2 corpus storage adapter (mocked S3 client)."""

from __future__ import annotations

import io
import unittest
from unittest.mock import MagicMock, patch

from mission_control.b2_storage import (
    SMOKE_TEST_KEY,
    SMOKE_TEST_TEXT,
    B2Config,
    B2Storage,
    create_s3_client,
    run_smoke_test,
)


def _env() -> dict[str, str]:
    return {
        "B2_KEY_ID": "key-id-secret",
        "B2_APPLICATION_KEY": "app-key-secret",
        "B2_BUCKET": "legalai-corpus",
        "B2_ENDPOINT": "https://s3.us-west-004.backblazeb2.com",
        "B2_REGION": "us-west-004",
    }


class TestB2Config(unittest.TestCase):
    def test_from_env_loads_required_vars(self) -> None:
        config = B2Config.from_env(_env())
        self.assertEqual(config.bucket, "legalai-corpus")
        self.assertEqual(config.endpoint, "https://s3.us-west-004.backblazeb2.com")
        self.assertEqual(config.region, "us-west-004")
        self.assertEqual(config.key_id, "key-id-secret")
        self.assertEqual(config.application_key, "app-key-secret")

    def test_from_env_requires_all_vars(self) -> None:
        env = _env()
        del env["B2_APPLICATION_KEY"]
        with self.assertRaises(RuntimeError) as ctx:
            B2Config.from_env(env)
        message = str(ctx.exception)
        self.assertIn("B2_APPLICATION_KEY", message)
        self.assertNotIn("app-key-secret", message)

    def test_repr_does_not_expose_secrets(self) -> None:
        config = B2Config.from_env(_env())
        rendered = repr(config)
        self.assertNotIn("key-id-secret", rendered)
        self.assertNotIn("app-key-secret", rendered)
        self.assertIn("legalai-corpus", rendered)


class TestB2StorageOperations(unittest.TestCase):
    def setUp(self) -> None:
        self.config = B2Config.from_env(_env())
        self.client = MagicMock()
        self.storage = B2Storage(self.config, client=self.client)

    def test_put_text_encodes_utf8(self) -> None:
        self.storage.put_text("docs/a.txt", "hello café")
        self.client.put_object.assert_called_once_with(
            Bucket="legalai-corpus",
            Key="docs/a.txt",
            Body="hello café".encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )

    def test_get_text_decodes_body(self) -> None:
        self.client.get_object.return_value = {
            "Body": io.BytesIO("payload".encode("utf-8")),
        }
        text = self.storage.get_text("docs/a.txt")
        self.assertEqual(text, "payload")
        self.client.get_object.assert_called_once_with(
            Bucket="legalai-corpus",
            Key="docs/a.txt",
        )

    def test_list_keys_paginates(self) -> None:
        self.client.list_objects_v2.side_effect = [
            {
                "Contents": [{"Key": "p/a"}, {"Key": "p/b"}],
                "IsTruncated": True,
                "NextContinuationToken": "tok-2",
            },
            {
                "Contents": [{"Key": "p/c"}],
                "IsTruncated": False,
            },
        ]
        keys = self.storage.list_keys("p/")
        self.assertEqual(keys, ["p/a", "p/b", "p/c"])
        self.assertEqual(self.client.list_objects_v2.call_count, 2)
        second_kwargs = self.client.list_objects_v2.call_args_list[1].kwargs
        self.assertEqual(second_kwargs["ContinuationToken"], "tok-2")

    def test_list_keys_empty_prefix(self) -> None:
        self.client.list_objects_v2.return_value = {"IsTruncated": False}
        self.assertEqual(self.storage.list_keys(), [])

    def test_delete_key(self) -> None:
        self.storage.delete_key("docs/a.txt")
        self.client.delete_object.assert_called_once_with(
            Bucket="legalai-corpus",
            Key="docs/a.txt",
        )


class TestCreateS3Client(unittest.TestCase):
    @patch("mission_control.b2_storage.boto3.client")
    def test_create_s3_client_uses_b2_endpoint(self, mock_client: MagicMock) -> None:
        config = B2Config.from_env(_env())
        create_s3_client(config)
        mock_client.assert_called_once_with(
            "s3",
            endpoint_url=config.endpoint,
            aws_access_key_id=config.key_id,
            aws_secret_access_key=config.application_key,
            region_name=config.region,
        )


class TestSmokeTest(unittest.TestCase):
    def test_smoke_test_pass_path(self) -> None:
        client = MagicMock()
        client.get_object.return_value = {
            "Body": io.BytesIO(SMOKE_TEST_TEXT.encode("utf-8")),
        }
        client.list_objects_v2.return_value = {
            "Contents": [{"Key": SMOKE_TEST_KEY}],
            "IsTruncated": False,
        }
        storage = B2Storage(B2Config.from_env(_env()), client=client)

        with patch("builtins.print") as mock_print:
            code = run_smoke_test(storage)

        self.assertEqual(code, 0)
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        self.assertIn("write: PASS", printed)
        self.assertIn("read: PASS", printed)
        self.assertIn("list: PASS", printed)
        self.assertIn("delete: PASS", printed)
        self.assertNotIn("key-id-secret", printed)
        self.assertNotIn("app-key-secret", printed)
        client.put_object.assert_called_once()
        client.delete_object.assert_called_once_with(
            Bucket="legalai-corpus",
            Key=SMOKE_TEST_KEY,
        )

    def test_smoke_test_fails_on_content_mismatch(self) -> None:
        client = MagicMock()
        client.get_object.return_value = {
            "Body": io.BytesIO(b"wrong"),
        }
        storage = B2Storage(B2Config.from_env(_env()), client=client)

        with patch("builtins.print") as mock_print:
            code = run_smoke_test(storage)

        self.assertEqual(code, 1)
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        self.assertIn("write: PASS", printed)
        self.assertIn("read: FAIL", printed)
        self.assertNotIn("app-key-secret", printed)

    def test_smoke_test_configure_failure_does_not_leak_secrets(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with patch("builtins.print") as mock_print:
                code = run_smoke_test()
        self.assertEqual(code, 1)
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        self.assertIn("configure: FAIL", printed)
        self.assertNotIn("secret", printed.lower())


if __name__ == "__main__":
    unittest.main()
