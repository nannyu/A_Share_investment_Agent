# -*- coding: utf-8 -*-
"""
新闻搜索模块

提供统一的新闻搜索抽象层，支持多种搜索引擎实现。
当前实现：
- Tavily Search API
"""

from .base import NewsSearchResult, NewsSearchProvider, SearchQuery, convert_to_news_format
from .tavily_impl import TavilyNewsSearch

__all__ = [
    "NewsSearchResult",
    "NewsSearchProvider",
    "SearchQuery",
    "TavilyNewsSearch",
    "convert_to_news_format",
]
