from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

from src.tools.openrouter_config import get_chat_completion
from src.tools.stock_basic import enrich_symbol, get_stock_name
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
        base = (
            "帮我搜索最近一周沪深300指数相关的大事件、重要政策、"
            "宏观数据发布、市场情绪变化及权重股动向"
        )
    elif agent_name == "macro_analyst_agent":
        base = (
            f"帮我搜索最近一周与 {symbol} 相关的行业与宏观事件、"
            "政策变化、市场环境影响及券商研报摘要"
        )
    elif agent_name == "sentiment_agent":
        base = (
            f"帮我搜索最近一周 {symbol} 的公司新闻、公告、业绩、"
            "订单进展、监管处罚与重大事项"
        )
    elif agent_name == "market_snapshot":
        base = (
            f"帮我搜索最近一周 {symbol} 的市场快照信息，包括资金流、"
            "成交活跃度、龙虎榜、机构观点与重要新闻"
        )
    else:
        base = (
            f"帮我搜索最近一周 {symbol} 的股票新闻与财经快讯，"
            "包含公告、研报与监管信息"
        )

    base = _append_date_window(base, date)

    news_sites = [
        "site:sina.com.cn",
        "site:163.com",
        "site:eastmoney.com",
        "site:cnstock.com",
        "site:hexun.com",
        "site:10jqka.com.cn",
        "site:stcn.com",
        "site:yicai.com",
        "site:cs.com.cn",
        "site:jrj.com.cn",
        "site:money.163.com",
        "site:finance.sina.com.cn",
        "site:finance.eastmoney.com",
        "site:stockstar.com",
        "site:caixin.com",
        "site:guosen.com.cn",
        "site:htsec.com",
        "site:gf.com.cn",
        "site:citics.com",
        "site:china.com.cn",
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
        result = log_llm_interaction(trace_state)(get_chat_completion)(messages)
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
    query_symbol = symbol
    if agent_name != "macro_news_agent" and symbol not in {"000300", "沪深300", "CSI300"}:
        company_name = get_stock_name(symbol)
        query_symbol = enrich_symbol(symbol, company_name)
    if mode == "llm":
        query = _llm_query(query_symbol, date, agent_name, trace_state)
        if query:
            return query
    return _rule_based_query(query_symbol, date, agent_name)
