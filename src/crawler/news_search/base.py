# -*- coding: utf-8 -*-
"""
新闻搜索抽象层

定义新闻搜索的统一接口，支持多种搜索引擎实现。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class SearchTopic(str, Enum):
    """搜索主题类型"""
    NEWS = "news"
    GENERAL = "general"
    FINANCE = "finance"


@dataclass
class SearchQuery:
    """搜索查询参数"""
    query: str
    max_results: int = 10
    topic: SearchTopic = SearchTopic.NEWS
    include_domains: Optional[List[str]] = None
    exclude_domains: Optional[List[str]] = None
    days_back: int = 7  # 搜索最近几天的新闻
    start_date: Optional[str] = None  # 起始日期 YYYY-MM-DD
    end_date: Optional[str] = None  # 结束日期 YYYY-MM-DD


@dataclass
class NewsSearchResult:
    """统一的新闻搜索结果"""
    title: str
    url: str
    content: str
    source: str = ""
    published_date: Optional[str] = None
    score: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "title": self.title,
            "url": self.url,
            "content": self.content,
            "source": self.source,
            "published_date": self.published_date,
            "score": self.score,
            "metadata": self.metadata,
        }


@dataclass
class NewsSearchQuery:
    """新闻搜索查询"""
    # 核心查询
    keyword: str  # 主要关键词（股票名/代码/主题）

    # 上下文
    agent_name: Optional[str] = None  # 调用的 agent
    symbol: Optional[str] = None  # 股票代码

    # 时间范围
    date: Optional[str] = None  # 目标日期 YYYY-MM-DD
    days_back: int = 7  # 回溯天数

    # 搜索配置
    max_results: int = 10
    topic: SearchTopic = SearchTopic.NEWS

    # 域名过滤
    include_domains: Optional[List[str]] = None
    exclude_domains: Optional[List[str]] = None

    # 额外参数
    extra: Dict[str, Any] = field(default_factory=dict)


class NewsSearchProvider(ABC):
    """新闻搜索提供者抽象类"""

    name: str = "base"

    @abstractmethod
    def search(self, query: NewsSearchQuery) -> List[NewsSearchResult]:
        """
        执行搜索

        Args:
            query: 搜索查询对象

        Returns:
            搜索结果列表
        """
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """检查搜索提供者是否可用"""
        pass

    def build_query_string(self, query: NewsSearchQuery) -> str:
        """
        构建搜索查询字符串

        子类可以覆盖此方法以实现特定的查询格式
        """
        return query.keyword


def convert_to_news_format(results: List[NewsSearchResult], symbol: str) -> List[Dict]:
    """
    将搜索结果转换为系统内部新闻格式

    Args:
        results: 搜索结果列表
        symbol: 股票代码

    Returns:
        标准新闻格式列表
    """
    news_list = []
    for r in results:
        if not r.title or not r.url:
            continue

        news_item = {
            "title": r.title,
            "content": r.content or r.title,
            "source": r.source or extract_domain(r.url),
            "url": r.url,
            "keyword": symbol,
            "search_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        if r.published_date:
            news_item["publish_time"] = r.published_date
        news_list.append(news_item)

    return news_list


def extract_domain(url: str) -> str:
    """从 URL 提取域名"""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc
    except Exception:
        return ""
