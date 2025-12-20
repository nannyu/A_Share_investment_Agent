from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from src.database import AkshareSQLiteCache
from src.tools.news_crawler import get_stock_news
from src.tools.openrouter_config import get_chat_completion
from src.utils.logging_config import setup_logger
from src.utils.api_utils import log_llm_interaction
from src.utils.prompt_loader import load_prompt

BASE_DIR = Path(__file__).resolve().parents[2]
CACHE_PATH = BASE_DIR / "data" / "market_data_cache.db"
SNAPSHOT_TABLE = "market_snapshot_cache"
SNAPSHOT_TTL_SECONDS = 24 * 3600

logger = setup_logger("market_snapshot")
cache = AkshareSQLiteCache(CACHE_PATH)


def _build_prompt(symbol: str, news_items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    if not news_items:
        news_block = "暂无最新新闻，请给出一个基于常识的高层次总结。"
    else:
        lines = []
        for item in news_items[:10]:
            lines.append(
                f"标题：{item.get('title', '未知')}\n"
                f"来源：{item.get('source', '未知')}\n"
                f"时间：{item.get('publish_time', '')}\n"
                f"内容：{item.get('content', '')}"
            )
        news_block = "\n\n".join(lines)

    system_message = load_prompt("prompts/market_snapshot/system.md")
    user_message = (
        f"股票代码：{symbol}\n"
        f"新闻要点：\n{news_block}\n\n"
        "请严格返回一个JSON字符串，不要包含额外文本。"
    )
    return [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_message},
    ]


def _parse_snapshot_response(raw: str) -> Dict[str, Any]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            start = raw.index("{")
            end = raw.rindex("}") + 1
            return json.loads(raw[start:end])
        except Exception as exc:  # noqa: BLE001
            logger.error("⚠️ Failed to parse snapshot JSON: %s", exc)
            return {}




def _parse_numeric(value: Any, default_multiplier: float | None = None) -> float:
    """Parse snapshot numeric fields supporting 中文单位 (亿/万等)."""
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        if default_multiplier and number < default_multiplier:
            number *= default_multiplier
        return number

    text = str(value).strip()
    if not text:
        return 0.0

    unit_tokens = [
        ('万亿', 1e12),
        ('亿股', 1e8),
        ('亿手', 1e8),
        ('亿', 1e8),
        ('万股', 1e4),
        ('万手', 1e4),
        ('万', 1e4),
    ]
    multiplier = 1.0
    unit_applied = False
    for token, factor in unit_tokens:
        if token in text:
            text = text.replace(token, "")
            multiplier = factor
            unit_applied = True
            break

    cleaned = text.replace(",", "").replace(" ", "")
    match = re.search(r"-?\d+(\.\d+)?", cleaned)
    if not match:
        return 0.0

    number = float(match.group())
    if not unit_applied and default_multiplier:
        number *= default_multiplier
    return number * multiplier


def _sanitize_snapshot(data: Dict[str, Any]) -> Dict[str, float]:
    market_cap = _parse_numeric(data.get("market_cap"), default_multiplier=1e8)
    volume = _parse_numeric(data.get("volume"), default_multiplier=1e8)
    average_volume = _parse_numeric(data.get("average_volume"), default_multiplier=1e8)
    high = _parse_numeric(data.get("fifty_two_week_high"))
    low = _parse_numeric(data.get("fifty_two_week_low"))

    try:
        confidence = float(data.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return {
        "market_cap": market_cap,
        "volume": volume,
        "average_volume": average_volume,
        "fifty_two_week_high": high,
        "fifty_two_week_low": low,
        "confidence": confidence,
        "summary": str(data.get("summary", "")).strip(),
    }

def _generate_snapshot(symbol: str, trace_state: dict | None = None) -> Dict[str, Any]:
    news_items = get_stock_news(
        symbol,
        max_news=20,
        agent_name="market_snapshot",
        trace_state=trace_state,
    )
    logger.info("🗞️ Snapshot news pool for %s: %d 条", symbol, len(news_items))
    if not news_items:
        logger.warning("⚠️ Snapshot prompt缺少新闻，将使用空模板: %s", symbol)
    messages = _build_prompt(symbol, news_items)
    logger.info("🤖 正在调用 LLM 生成 %s 的市场快照...", symbol)
    if trace_state:
        llm_response = log_llm_interaction(trace_state)(
            lambda: get_chat_completion(messages)
        )()
    else:
        llm_response = get_chat_completion(messages)
    snapshot_raw = _parse_snapshot_response(llm_response or "{}")
    sanitized = _sanitize_snapshot(snapshot_raw)
    sanitized.setdefault("summary", "")
    if not sanitized["summary"]:
        logger.warning("⚠️ LLM 返回内容缺少 summary，已使用空字符串: %s", symbol)
    sanitized["news_count"] = len(news_items)
    sanitized["generated_on"] = datetime.utcnow().isoformat()
    return sanitized


def get_market_snapshot(
    symbol: str,
    ttl_seconds: int = SNAPSHOT_TTL_SECONDS,
    *,
    trace_state: dict | None = None,
) -> Dict[str, Any]:
    cached = cache.fetch_records(
        table=SNAPSHOT_TABLE,
        filters={"symbol": symbol},
        ttl_seconds=ttl_seconds,
        order_by='"缓存时间" DESC',
        limit=1,
    )
    if cached:
        record = _records_to_dict(cached[0])
        logger.info("📦 Market snapshot cache hit for %s", symbol)
        return record

    snapshot = _generate_snapshot(symbol, trace_state=trace_state)
    record = {"symbol": symbol, **snapshot}
    cache.upsert_records(SNAPSHOT_TABLE, [record], key_columns=["symbol"])
    logger.info("⚙️ Market snapshot refreshed for %s", symbol)
    return record


def _records_to_dict(record: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = dict(record)
    cleaned.pop("缓存时间", None)
    return cleaned
