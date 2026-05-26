"""
新闻爬取模块

使用 Tavily API 进行新闻搜索，支持中文新闻源。

优先级：
1. SQLite 缓存
2. Tavily 搜索
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from src.database import AkshareSQLiteCache
from src.tools.news_query_builder import build_news_query
from src.utils.config_loader import get_cache_refresh_flag, get_news_limits

# 统一控制台编码
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 导入新闻搜索模块
try:
    from src.crawler.news_search import TavilyNewsSearch
    from src.crawler.news_search.base import SearchQuery, convert_to_news_format
except ImportError:
    TavilyNewsSearch = None
    SearchQuery = None
    convert_to_news_format = None

BASE_DIR = Path(__file__).resolve().parents[2]
NEWS_CACHE_DB_PATH = BASE_DIR / "data" / "market_data_cache.db"
NEWS_CACHE_TABLE = "stock_news_daily_cache"
MAX_NEWS_CAP = 50

cache = AkshareSQLiteCache(NEWS_CACHE_DB_PATH)


def _normalize_date_str(value: str | None) -> str:
    """规范化日期字符串"""
    text = (value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return datetime.now().strftime("%Y-%m-%d")


def _sort_news_items(items: list) -> list:
    """按时间排序新闻"""
    def _key(x):
        return x.get("publish_time") or x.get("search_time") or ""
    return sorted(items, key=_key, reverse=True)


def _build_date_window(end_date: str, days_back: int = 7) -> tuple[str, str]:
    """构建 [start_date, end_date] 时间窗口（含边界）"""
    try:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    except Exception:
        end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=max(1, int(days_back)))
    return start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")


def get_stock_news(
    symbol: str,
    max_news: int = 10,
    date: str = None,
    *,
    agent_name: str | None = None,
    trace_state: dict | None = None,
) -> list:
    """
    获取股票新闻

    优先级：
    1. SQLite 缓存
    2. Tavily 搜索

    Args:
        symbol: 股票代码
        max_news: 最大新闻条数
        date: 截止日期
        agent_name: Agent 名称（用于查询优化）
        trace_state: 追踪状态

    Returns:
        新闻列表
    """
    # 读取配置
    limits = get_news_limits()
    try:
        config_news_max = int(limits.get("news_max_news", 100))
    except (TypeError, ValueError):
        config_news_max = 100
    try:
        config_tavily_max = int(limits.get("tavily_max_news", 20))
    except (TypeError, ValueError):
        config_tavily_max = 20

    env_news_max = max(1, min(config_news_max, MAX_NEWS_CAP))
    tavily_max_news = max(1, min(config_tavily_max, 20))
    max_news = min(max_news, env_news_max)

    cache_date = _normalize_date_str(date)

    # 1. 检查强制刷新标志
    refresh_news = False
    if agent_name:
        refresh_news = get_cache_refresh_flag(agent_name, "news")
    if refresh_news:
        print(f"  [刷新] 强制刷新: {agent_name} {symbol} {cache_date}")

    # 2. 检查缓存
    cached_news = []
    if not refresh_news:
        cached_records = cache.fetch_records(
            NEWS_CACHE_TABLE,
            filters={"symbol": symbol, "cache_date": cache_date},
            limit=1,
        )
        if cached_records:
            record = dict(cached_records[0])
            record.pop("缓存时间", None)
            news_json = record.get("news_json")
            if news_json:
                try:
                    cached_news = json.loads(news_json)
                except Exception:
                    cached_news = []

    if len(cached_news) >= max_news:
        print(f"  [缓存] DB 缓存命中: {symbol} {cache_date}（{len(cached_news)} 条）")
        return cached_news[:max_news]

    print(f"  [搜索] DB 缓存不足: {symbol} {cache_date}（已有 {len(cached_news)} 条，需 {max_news} 条）")

    need_more_news = max_news - len(cached_news)
    fetch_count = min(max(need_more_news, max_news), MAX_NEWS_CAP)
    start_date, end_date = _build_date_window(cache_date, days_back=7)

    # 3. 构建搜索查询
    search_query = build_news_query(
        symbol,
        date=date,
        agent_name=agent_name,
        trace_state=trace_state,
    )

    new_news_list = []
    fetch_method = None

    # 4. Tavily 搜索（包含沪深300宏观新闻）
    if TavilyNewsSearch is None or SearchQuery is None or convert_to_news_format is None:
        print("  [Tavily] 模块不可用，返回现有缓存结果")
        return cached_news[:max_news]

    tavily_search = TavilyNewsSearch()
    if not tavily_search.is_available():
        print("  [Tavily] 未配置可用 API Key，返回现有缓存结果")
        return cached_news[:max_news]

    try:
        print(f"  [Tavily] 搜索查询: {search_query}")
        query = SearchQuery(
            query=search_query,
            max_results=min(fetch_count, tavily_max_news),
            days_back=7,
            start_date=start_date,
            end_date=end_date,
        )
        results = tavily_search.search(query)
        if results:
            new_news_list = convert_to_news_format(results, symbol)
            if new_news_list:
                fetch_method = "tavily"
                print(f"  [Tavily] 获取 {len(new_news_list)} 条新闻")
    except Exception as e:
        print(f"  [Tavily] 搜索出错: {e}")

    # 5. 合并缓存和新获取的新闻（去重）
    combined_news = cached_news[:]
    existing_titles = {news.get("title", "") for news in combined_news}

    for item in new_news_list:
        title = item.get("title", "")
        if not title or title in existing_titles:
            continue
        combined_news.append(item)
        existing_titles.add(title)

    combined_news = _sort_news_items(combined_news)

    # 6. 写入缓存
    if len(combined_news) > len(cached_news):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = {
            "symbol": symbol,
            "cache_date": cache_date,
            "news_json": json.dumps(combined_news, ensure_ascii=False),
            "news_count": len(combined_news),
            "method": fetch_method or "",
            "search_time": now_str,
        }
        cache.upsert_records(
            NEWS_CACHE_TABLE,
            [row],
            key_columns=["symbol", "cache_date"],
        )
        print(f"  [存储] DB 写入: {symbol} {cache_date}（{len(combined_news)} 条，来源={fetch_method}）")

    return combined_news[:max_news]


# 兼容旧入口
def build_search_query(symbol: str, date: str = None) -> str:
    """兼容旧入口"""
    return build_news_query(symbol, date=date)


def get_news_sentiment(
    news_list: list,
    num_of_news: int = 5,
    *,
    symbol: str | None = None,
    cache_date: str | None = None,
    trace_state: dict | None = None,
    agent_name: str | None = None,
) -> dict:
    """
    分析新闻情感得分

    Args:
        news_list: 新闻列表
        num_of_news: 用于分析的新闻数量

    Returns:
        包含 score, signal, confidence, reasoning 的字典
    """
    import re
    from src.tools.openrouter_config import get_chat_completion
    from src.utils.api_utils import log_llm_interaction
    from src.utils.prompt_loader import load_prompt, format_prompt

    default_result = {"score": 0.0, "signal": "neutral", "confidence": 0.5, "reasoning": ""}

    if not news_list:
        return default_result

    system_message = {
        "role": "system",
        "content": load_prompt("prompts/sentiment/system.md"),
    }

    news_content = "\n\n".join([
        f"标题：{news.get('title', '未知')}\n"
        f"来源：{news.get('source', '未知')}\n"
        f"时间：{news.get('publish_time', '未知')}\n"
        f"内容：{news.get('content', '')}"
        for news in news_list[:num_of_news]
    ])

    user_message = {
        "role": "user",
        "content": format_prompt(
            "prompts/sentiment/user.md",
            news_content=news_content,
        ),
    }

    try:
        if trace_state:
            result = log_llm_interaction(trace_state)(get_chat_completion)(
                [system_message, user_message]
            )
        else:
            result = get_chat_completion([system_message, user_message])

        if result is None:
            print("  [sentiment] LLM 返回 None")
            return default_result

        preview = str(result)
        print(f"  [sentiment] LLM 原始响应: {preview[:200]}")

        # 尝试解析 JSON
        try:
            json_start = preview.find('{')
            json_end = preview.rfind('}') + 1
            if json_start >= 0 and json_end > json_start:
                json_str = preview[json_start:json_end]
                parsed = json.loads(json_str)

                score = float(parsed.get("score", 0))
                score = max(-1.0, min(1.0, score))

                return {
                    "score": score,
                    "signal": parsed.get("signal", "neutral"),
                    "confidence": float(parsed.get("confidence", 0.5)),
                    "reasoning": parsed.get("reasoning", ""),
                }
        except (json.JSONDecodeError, ValueError):
            pass

        # 回退到数字解析
        match = re.search(r"-?\d+(\.\d+)?", preview.replace("%", " "))
        if match:
            score = max(-1.0, min(1.0, float(match.group())))
            return {
                "score": score,
                "signal": "bullish" if score >= 0.3 else ("bearish" if score <= -0.3 else "neutral"),
                "confidence": abs(score),
                "reasoning": "",
            }

        return default_result

    except Exception as e:
        print(f"  [sentiment] 分析出错: {e}")
        return default_result
