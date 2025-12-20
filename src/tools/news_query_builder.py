from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

from src.tools.openrouter_config import get_chat_completion
from src.utils.api_utils import log_llm_interaction
from src.utils.prompt_loader import load_prompt, format_prompt


def _append_date_window(query: str, date: Optional[str]) -> str:
    if not date:
        return query
    try:
        end_date = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return query
    start_date = end_date - timedelta(days=7)
    return f"{query} after:{start_date.strftime('%Y-%m-%d')} before:{date}"


def _rule_based_query(symbol: str, date: Optional[str], agent_name: Optional[str]) -> str:
    if agent_name == "macro_news_agent" or symbol in {"000300", "沪深300", "CSI300"}:
        base = "沪深300 指数 中国 A股 宏观 政策 央行 经济 数据 流动性 市场情绪"
    elif agent_name == "macro_analyst_agent":
        base = f"{symbol} A股 宏观 政策 央行 经济 数据 行业 供需"
    elif agent_name == "sentiment_agent":
        base = f"{symbol} 公司 新闻 公告 业绩 订单 监管 风险"
    elif agent_name == "market_snapshot":
        base = f"{symbol} 市值 成交量 换手 资金 研报 新闻 A股"
    else:
        base = f"{symbol} 股票 新闻 财经"

    base = _append_date_window(base, date)

    news_sites = [
        "site:sina.com.cn",
        "site:163.com",
        "site:eastmoney.com",
        "site:cnstock.com",
        "site:hexun.com",
    ]
    return f"{base} ({' OR '.join(news_sites)})"


def _llm_query(
    symbol: str,
    date: Optional[str],
    agent_name: Optional[str],
    trace_state: Optional[dict],
) -> Optional[str]:
    system_prompt = load_prompt("prompts/news_query_builder/system.md")
    user_prompt = format_prompt(
        "prompts/news_query_builder/user.md",
        symbol=symbol,
        agent_name=agent_name or "unknown",
        date=date or "today",
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    if trace_state:
        result = log_llm_interaction(trace_state)(lambda: get_chat_completion(messages))()
    else:
        result = get_chat_completion(messages)

    if not result:
        return None
    cleaned = str(result).strip()
    if not cleaned:
        return None
    return _append_date_window(cleaned, date)


def build_news_query(
    symbol: str,
    *,
    date: Optional[str] = None,
    agent_name: Optional[str] = None,
    trace_state: Optional[dict] = None,
) -> str:
    mode = (os.getenv("NEWS_QUERY_MODE", "rule") or "rule").lower()
    if mode == "llm":
        query = _llm_query(symbol, date, agent_name, trace_state)
        if query:
            return query
    return _rule_based_query(symbol, date, agent_name)
