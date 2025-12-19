from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests


@dataclass
class TavilyResult:
    title: str
    url: str
    content: str
    score: Optional[float] = None
    published_date: Optional[str] = None


def tavily_search(
    query: str,
    *,
    api_key: Optional[str] = None,
    max_results: int = 10,
    topic: str = "news",
    search_depth: str = "basic",
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    timeout_seconds: int = 30,
) -> List[TavilyResult]:
    """
    Tavily Search API wrapper.

    Notes:
    - Uses `TAVILY_API_KEY` env var by default.
    - Returns structured results only; callers decide how to map/clean fields.
    """
    api_key = api_key or os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        return []

    payload: Dict[str, Any] = {
        "query": query,
        "max_results": int(max_results),
        "topic": topic,
        "search_depth": search_depth,
        "include_answer": False,
        "include_images": False,
        "include_raw_content": False,
    }
    if include_domains:
        payload["include_domains"] = include_domains
    if exclude_domains:
        payload["exclude_domains"] = exclude_domains

    resp = requests.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=timeout_seconds,
    )
    resp.raise_for_status()
    data = resp.json() or {}
    raw_results = data.get("results") or []

    results: List[TavilyResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        results.append(
            TavilyResult(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("url") or "").strip(),
                content=str(item.get("content") or "").strip(),
                score=item.get("score"),
                published_date=str(item.get("published_date") or "").strip() or None,
            )
        )
    return results

