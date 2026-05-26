# -*- coding: utf-8 -*-
"""
Tavily 新闻搜索实现

使用 Tavily API 进行新闻搜索。
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

import requests

from .base import (
    NewsSearchProvider,
    NewsSearchResult,
    SearchQuery,
    extract_domain,
)


class TavilyNewsSearch(NewsSearchProvider):
    """
    Tavily 新闻搜索实现

    特点：
    - 支持中英文查询
    - 自动处理 API 密钥轮换
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_keys: Optional[List[str]] = None,
        timeout_seconds: int = 30,
    ):
        """
        初始化 Tavily 搜索

        Args:
            api_key: 单个 API 密钥
            api_keys: 多个 API 密钥列表（用于轮换）
            timeout_seconds: 请求超时时间
        """
        self._keys: List[str] = []

        # 收集 API 密钥
        if api_key:
            self._keys.append(api_key)
        if api_keys:
            self._keys.extend(api_keys)

        # 从环境变量获取
        env_key = os.getenv("TAVILY_API_KEY", "").strip()
        if env_key:
            self._keys.append(env_key)

        env_keys = os.getenv("TAVILY_API_KEYS", "").strip()
        if env_keys:
            keys_from_env = [k for k in re.split(r"[;,\s]+", env_keys) if k]
            self._keys.extend(keys_from_env)

        # 去重
        self._keys = list(dict.fromkeys(k.strip() for k in self._keys if k and k.strip()))
        self._timeout = timeout_seconds

    @property
    def name(self) -> str:
        return "tavily"

    def is_available(self) -> bool:
        """检查 Tavily 是否可用（有 API 密钥）"""
        return len(self._keys) > 0

    def search(
        self,
        query: SearchQuery,
    ) -> List[NewsSearchResult]:
        """
        执行 Tavily 搜索

        Args:
            query: 搜索查询对象

        Returns:
            搜索结果列表
        """
        if not self._keys:
            return []

        # 构建查询参数
        payload = self._build_payload(query)

        # 尝试使用不同的 API 密钥
        last_error: Optional[Exception] = None
        data: Dict[str, Any] = {}

        for key in self._keys:
            try:
                resp = requests.post(
                    "https://api.tavily.com/search",
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                data = resp.json() or {}
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                continue

        if last_error is not None and not data:
            return []

        # 解析结果
        return self._parse_results(data.get("results") or [])

    def _build_payload(self, query: SearchQuery) -> Dict[str, Any]:
        """构建 Tavily API 请求参数"""
        payload: Dict[str, Any] = {
            "query": query.query,
            "max_results": min(query.max_results, 10),  # Tavily 单次最多 10 条
            "topic": "news",  # 始终使用新闻主题
            "search_depth": "basic",
            "include_answer": False,
            "include_images": False,
            "include_raw_content": False,
        }

        # 时间过滤（回测对齐）
        # 优先使用明确日期区间；否则回退为 days 参数
        if query.start_date and query.end_date:
            payload["start_date"] = query.start_date
            payload["end_date"] = query.end_date
        elif query.days_back and int(query.days_back) > 0:
            payload["days"] = int(query.days_back)

        # 仅当调用方显式指定时才使用域名过滤
        if query.include_domains:
            payload["include_domains"] = list(query.include_domains)

        # 处理排除域名
        if query.exclude_domains:
            payload["exclude_domains"] = query.exclude_domains

        return payload

    def _parse_results(self, raw_results: List[Any]) -> List[NewsSearchResult]:
        """解析 Tavily API 返回的结果"""
        results: List[NewsSearchResult] = []

        for item in raw_results:
            if not isinstance(item, dict):
                continue

            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            content = str(item.get("content") or "").strip()

            if not title or not url:
                continue

            results.append(
                NewsSearchResult(
                    title=title,
                    url=url,
                    content=content or title,
                    source=extract_domain(url),
                    published_date=str(item.get("published_date") or "").strip() or None,
                    score=item.get("score"),
                )
            )

        return results
