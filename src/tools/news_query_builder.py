"""
新闻查询构建器

为不同 agent 构建优化的搜索查询，适配 Tavily API 格式。
"""

from __future__ import annotations

import os
from typing import Optional

from src.tools.openrouter_config import get_chat_completion
from src.tools.stock_basic import get_stock_name
from src.utils.api_utils import log_llm_interaction
from src.utils.prompt_loader import load_prompt, format_prompt


def _is_chinese(text: str) -> bool:
    """检测文本是否包含中文"""
    for char in text:
        if '\u4e00' <= char <= '\u9fff':
            return True
    return False


def _build_tavily_query(
    symbol: str,
    agent_name: Optional[str],
    company_name: Optional[str] = None,
) -> str:
    """
    构建 Tavily 优化的搜索查询

    Tavily 查询特点：
    - 简短精准的关键词优于长句子
    - 中英文结合效果更好
    - 不支持 after:/before: 操作符
    """
    # 获取公司名称
    if not company_name:
        company_name = get_stock_name(symbol)

    # 根据不同 agent 构建查询
    if agent_name == "macro_news_agent" or symbol in {"000300", "沪深300", "CSI300"}:
        # 宏观新闻：关注沪深300和市场政策
        return (
            "帮我搜索最近一周沪深300与A股市场宏观新闻，重点关注央行政策、证监会监管、"
            "流动性、北向资金、风险偏好、指数波动；同时关注海外最重要宏观事件，"
            "包括美联储利率、美国通胀与非农、地缘政治、原油与大宗商品波动对A股的影响"
        )

    if agent_name == "macro_analyst_agent":
        # 宏观分析：关注行业和政策
        if company_name:
            return f"帮我搜索最近一周{company_name}（{symbol}）相关的行业动态、政策变化与宏观研报"
        return f"帮我搜索最近一周{symbol}相关的行业动态、政策变化与宏观研报"

    if agent_name == "sentiment_agent":
        # 情感分析：关注公司具体新闻
        if company_name:
            return f"帮我搜索最近一周{company_name}（{symbol}）的新闻、公告、业绩和订单信息"
        return f"帮我搜索最近一周{symbol}的新闻、公告、业绩和订单信息"

    if agent_name == "market_snapshot":
        # 市场快照：关注资金和交易数据
        if company_name:
            return f"帮我搜索最近一周{company_name}（{symbol}）的资金流、龙虎榜、机构动向和成交信息"
        return f"帮我搜索最近一周{symbol}的资金流、龙虎榜、机构动向和成交信息"

    # 默认查询
    if company_name:
        return f"帮我搜索最近一周{company_name}（{symbol}）的股票财经新闻"
    return f"帮我搜索最近一周{symbol}的股票财经新闻"


def _build_enhanced_query(
    symbol: str,
    agent_name: Optional[str],
    company_name: Optional[str] = None,
) -> str:
    """
    构建增强版查询（中英结合）

    对于有英文名称的公司，添加英文关键词以提高国际新闻覆盖率。
    """
    base_query = _build_tavily_query(symbol, agent_name, company_name)

    # 已知的英文名称映射
    english_names = {
        "比亚迪": "BYD",
        "宁德时代": "CATL",
        "贵州茅台": "Moutai",
        "中国平安": "Ping An",
        "招商银行": "CMB China Merchants Bank",
        "美的集团": "Midea",
        "格力电器": "Gree",
        "海尔智家": "Haier",
        "隆基绿能": "LONGi",
        "通威股份": "Tongwei",
        "阳光电源": "Sungrow",
        "中芯国际": "SMIC",
        "腾讯控股": "Tencent",
        "阿里巴巴": "Alibaba",
        "京东": "JD.com",
        "拼多多": "PDD",
        "美团": "Meituan",
        "字节跳动": "ByteDance",
        "小米": "Xiaomi",
        "华为": "Huawei",
        "蔚来": "NIO",
        "小鹏": "XPeng",
        "理想": "Li Auto",
    }

    if company_name and company_name in english_names:
        eng_name = english_names[company_name]
        # 中英结合查询
        return f"{base_query} {eng_name}"

    return base_query


def _llm_query(
    symbol: str,
    agent_name: Optional[str],
    trace_state: Optional[dict],
) -> Optional[str]:
    """
    使用 LLM 生成搜索查询

    注意：LLM 生成的查询也应该遵循 Tavily 格式，不应包含高级操作符。
    """
    system_prompt = load_prompt("prompts/news_query_builder/system.md")

    # 根据语言选择用户提示
    if _is_chinese(symbol):
        user_prompt = format_prompt(
            "prompts/news_query_builder/user.md",
            symbol=symbol,
            agent_name=agent_name or "unknown",
            date="today",
        )
    else:
        user_prompt = f"Generate a concise news search query for stock {symbol}, agent: {agent_name or 'unknown'}. Return only keywords, no operators like after: or before:."

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        if trace_state:
            result = log_llm_interaction(trace_state)(get_chat_completion)(messages)
        else:
            result = get_chat_completion(messages)

        if not result:
            return None

        cleaned = str(result).strip()
        # 移除可能的高级操作符
        cleaned = _remove_advanced_operators(cleaned)
        return cleaned if cleaned else None

    except Exception:
        return None


def _remove_advanced_operators(query: str) -> str:
    """移除 Google 高级搜索操作符（Tavily 不支持）"""
    # 移除 after: 和 before: 操作符
    query = query.replace("after:", "")
    query = query.replace("before:", "")
    # 移除 site: 操作符（Tavily 用 include_domains 参数代替）
    query = query.replace("site:", "")
    # 清理多余空格
    query = " ".join(query.split())
    return query.strip()


def build_news_query(
    symbol: str,
    *,
    date: Optional[str] = None,  # 保留参数兼容性，但不再用于 after:/before:
    agent_name: Optional[str] = None,
    trace_state: Optional[dict] = None,
    search_engine: str = "tavily",  # 默认使用 tavily
) -> str:
    """
    构建新闻搜索查询

    Args:
        symbol: 股票代码
        date: 日期（保留兼容性，但不再用于日期过滤操作符）
        agent_name: Agent 名称，用于定制查询类型
        trace_state: 追踪状态
        search_engine: 搜索引擎类型（tavily/google），当前仅支持 tavily

    Returns:
        优化后的搜索查询字符串
    """
    # 获取公司名称
    company_name = None
    if agent_name != "macro_news_agent" and symbol not in {"000300", "沪深300", "CSI300"}:
        company_name = get_stock_name(symbol)

    # 查询模式
    mode = (os.getenv("NEWS_QUERY_MODE", "rule") or "rule").lower()

    if mode == "llm":
        query = _llm_query(symbol, agent_name, trace_state)
        if query:
            return query

    # 默认使用增强版规则查询
    return _build_enhanced_query(symbol, agent_name, company_name)


# 兼容旧接口
def build_search_query(symbol: str, date: str = None) -> str:
    """兼容旧入口"""
    return build_news_query(symbol, date=date)
