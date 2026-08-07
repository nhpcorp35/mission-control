"""Backblaze B2 S3-compatible corpus storage adapter.

Configuration is read only from:
  B2_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET, B2_ENDPOINT, B2_REGION

Secret values are never logged or printed.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

import boto3
from botocore.client import BaseClient

SMOKE_TEST_KEY = "_mc_connection_test.txt"
SMOKE_TEST_TEXT = "Mission Control B2 smoke test"

_REQUIRED_ENV = (
    "B2_KEY_ID",
    "B2_APPLICATION_KEY",
    "B2_BUCKET",
    "B2_ENDPOINT",
    "B2_REGION",
)


@dataclass(frozen=True)
class B2Config:
    """Non-secret B2 connection settings plus credentials for the S3 client."""

    key_id: str
    application_key: str
    bucket: str
    endpoint: str
    region: str

    def __repr__(self) -> str:
        return (
            "B2Config("
            f"key_id='***', "
            f"application_key='***', "
            f"bucket={self.bucket!r}, "
            f"endpoint={self.endpoint!r}, "
            f"region={self.region!r})"
        )

    @classmethod
    def from_env(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "B2Config":
        env = os.environ if environ is None else environ
        missing = [name for name in _REQUIRED_ENV if not str(env.get(name, "")).strip()]
        if missing:
            raise RuntimeError(
                "Missing required B2 environment variables: " + ", ".join(missing)
            )
        return cls(
            key_id=str(env["B2_KEY_ID"]).strip(),
            application_key=str(env["B2_APPLICATION_KEY"]).strip(),
            bucket=str(env["B2_BUCKET"]).strip(),
            endpoint=str(env["B2_ENDPOINT"]).strip(),
            region=str(env["B2_REGION"]).strip(),
        )


def create_s3_client(config: B2Config) -> BaseClient:
    """Build a boto3 S3 client pointed at the B2 S3-compatible endpoint."""
    return boto3.client(
        "s3",
        endpoint_url=config.endpoint,
        aws_access_key_id=config.key_id,
        aws_secret_access_key=config.application_key,
        region_name=config.region,
    )


class B2Storage:
    """Small corpus storage adapter over Backblaze B2 (S3 API)."""

    def __init__(
        self,
        config: Optional[B2Config] = None,
        *,
        client: Optional[BaseClient] = None,
    ) -> None:
        self._config = config if config is not None else B2Config.from_env()
        self._client = client if client is not None else create_s3_client(self._config)
        self._bucket = self._config.bucket

    @property
    def bucket(self) -> str:
        return self._bucket

    def put_text(self, key: str, text: str) -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=text.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )

    def get_text(self, key: str) -> str:
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        body = response["Body"].read()
        if isinstance(body, bytes):
            return body.decode("utf-8")
        return str(body)

    def list_keys(self, prefix: str = "") -> list[str]:
        keys: list[str] = []
        continuation_token: Optional[str] = None
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": self._bucket,
                "Prefix": prefix,
            }
            if continuation_token is not None:
                kwargs["ContinuationToken"] = continuation_token
            response = self._client.list_objects_v2(**kwargs)
            for item in response.get("Contents") or ():
                key = item.get("Key")
                if key is not None:
                    keys.append(str(key))
            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")
            if not continuation_token:
                break
        return keys

    def delete_key(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)


def _report(step: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"{step}: {status}{suffix}")


def run_smoke_test(storage: Optional[B2Storage] = None) -> int:
    """Create-read-list-delete smoke test against ``_mc_connection_test.txt``."""
    steps: list[tuple[str, bool, str]] = []
    overall_ok = True

    try:
        store = storage if storage is not None else B2Storage()
    except Exception as exc:  # noqa: BLE001 — surface config/client errors as FAIL
        _report("configure", False, type(exc).__name__)
        return 1

    # write
    try:
        store.put_text(SMOKE_TEST_KEY, SMOKE_TEST_TEXT)
        steps.append(("write", True, ""))
    except Exception as exc:  # noqa: BLE001
        steps.append(("write", False, type(exc).__name__))
        overall_ok = False

    # read + verify
    if overall_ok:
        try:
            content = store.get_text(SMOKE_TEST_KEY)
            if content == SMOKE_TEST_TEXT:
                steps.append(("read", True, ""))
            else:
                steps.append(("read", False, "content mismatch"))
                overall_ok = False
        except Exception as exc:  # noqa: BLE001
            steps.append(("read", False, type(exc).__name__))
            overall_ok = False
    else:
        steps.append(("read", False, "skipped"))

    # list + confirm
    if overall_ok:
        try:
            keys = store.list_keys(prefix=SMOKE_TEST_KEY)
            if SMOKE_TEST_KEY in keys:
                steps.append(("list", True, ""))
            else:
                steps.append(("list", False, "key not found"))
                overall_ok = False
        except Exception as exc:  # noqa: BLE001
            steps.append(("list", False, type(exc).__name__))
            overall_ok = False
    else:
        steps.append(("list", False, "skipped"))

    # delete (always attempt cleanup if write succeeded)
    write_ok = any(name == "write" and ok for name, ok, _ in steps)
    if write_ok:
        try:
            store.delete_key(SMOKE_TEST_KEY)
            steps.append(("delete", True, ""))
        except Exception as exc:  # noqa: BLE001
            steps.append(("delete", False, type(exc).__name__))
            overall_ok = False
    else:
        steps.append(("delete", False, "skipped"))

    for name, ok, detail in steps:
        _report(name, ok, detail)
        if not ok:
            overall_ok = False

    return 0 if overall_ok else 1


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backblaze B2 corpus storage smoke test (Mission Control)",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="smoke-test",
        choices=("smoke-test",),
        help="Command to run (default: smoke-test)",
    )
    parser.parse_args(list(argv) if argv is not None else None)
    return run_smoke_test()


if __name__ == "__main__":
    sys.exit(main())
