"""Deterministic ranked search over immutable verified-case page records."""
from __future__ import annotations

import re
import json
from collections import Counter
from typing import Any, Iterable

_WORD = re.compile(r"[a-z0-9]{2,}")


def query_terms(query: str) -> list[str]:
    terms = _WORD.findall(query.lower())
    if not 1 <= len(terms) <= 12:
        raise ValueError("query must contain 1 to 12 searchable terms")
    return list(dict.fromkeys(terms))


def search_page_records(records: Iterable[dict[str, Any]], query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return exact file/page citations using simple term-frequency ranking.

    ``records`` are immutable page-text records produced from a verified source;
    this function makes no legal inference and never opens an unverified file.
    """
    terms = query_terms(query)
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    results: list[dict[str, Any]] = []
    for record in records:
        text = str(record.get("text", ""))
        filename = str(record.get("filename", ""))
        page_number = record.get("page_number")
        if not filename or not isinstance(page_number, int) or page_number < 1:
            continue
        words = Counter(_WORD.findall(text.lower()))
        score = sum(words[term] for term in terms)
        if not score:
            continue
        snippet = re.sub(r"\s+", " ", text).strip()[:320]
        results.append({"filename": filename, "page_number": page_number, "score": score, "snippet": snippet})
    return sorted(results, key=lambda item: (-item["score"], item["filename"], item["page_number"]))[:limit]


def search_index_jsonl(raw: bytes, query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search an immutable UTF-8 JSONL page index without invoking an LLM."""
    try:
        records = (json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip())
        return search_page_records(records, query, limit)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("verified page index is invalid") from exc
