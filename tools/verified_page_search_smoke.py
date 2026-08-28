#!/usr/bin/env python3
"""Run the fixed, read-only Szymczyk verified-page search smoke test.

The caller supplies the Bridge service token through its secret store.  This
module never writes data and never prints the token.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen


CASE_ID = "NY-NewYork-158068-2018-Szymczyk-v-Hudson-36-37"
SOURCE_SHA256 = "ff8a0773d740358d56e43055f518e42b6124a4bc4fb00a39abaf85c5393568dc"
DEFAULT_BRIDGE_URL = "https://hal-github-actions-bridge-production.up.railway.app"


def search_verified_pages(
    query: str,
    token: str,
    bridge_url: str = DEFAULT_BRIDGE_URL,
    opener: Callable[..., object] = urlopen,
) -> tuple[int, dict[str, object]]:
    """Call the production Bridge's fixed verified-page search surface."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("BRIDGE_SERVICE_TOKEN is required")
    if not re.fullmatch(r"https://[^/]+(?:/[^/]+)?", bridge_url.rstrip("/")):
        raise ValueError("BRIDGE_URL must be an HTTPS origin")

    payload = json.dumps(
        {
            "case_id": CASE_ID,
            "source_sha256": SOURCE_SHA256,
            "query": query.strip(),
            "limit": 20,
        }
    ).encode("utf-8")
    request = Request(
        bridge_url.rstrip("/") + "/cases/verified/search",
        data=payload,
        headers={
            "Authorization": "Bearer " + token.strip().removeprefix("Bearer ").strip(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with opener(request, timeout=30) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def main() -> int:
    try:
        status, result = search_verified_pages(
            query=os.environ.get("SEARCH_QUERY", "riparian law"),
            token=os.environ.get("BRIDGE_SERVICE_TOKEN", ""),
            bridge_url=os.environ.get("BRIDGE_URL", DEFAULT_BRIDGE_URL),
        )
    except (ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    print(json.dumps({"http_status": status, "response": result}, sort_keys=True))
    return 0 if 200 <= status < 300 and result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
