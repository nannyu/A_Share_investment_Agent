from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from src.database import AkshareSQLiteCache
from src.tools.news_crawler import get_stock_news
from src.tools.openrouter_config import get_chat_completion
from src.utils.logging_config import setup_logger
from src.utils.api_utils import log_llm_interaction
from src.utils.prompt_loader import load_prompt, format_prompt
from src.utils.config_loader import get_cache_refresh_flag, get_news_limits

BASE_DIR = Path(__file__).resolve().parents[2]
CACHE_PATH = BASE_DIR / "data" / "market_data_cache.db"
SNAPSHOT_TABLE = "market_snapshot_cache"
SNAPSHOT_TTL_SECONDS = 24 * 3600

logger = setup_logger("market_snapshot")
cache = AkshareSQLiteCache(CACHE_PATH)
_SNAPSHOT_SCHEMA_CHECKED = False


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
    user_message = format_prompt(
        "prompts/market_snapshot/user.md",
        symbol=symbol,
        news_block=news_block,
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


def _ensure_snapshot_table_schema() -> None:
    """确保 market_snapshot_cache 主键为 (symbol, cache_date)。"""
    global _SNAPSHOT_SCHEMA_CHECKED
    if _SNAPSHOT_SCHEMA_CHECKED:
        return

    conn = sqlite3.connect(CACHE_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (SNAPSHOT_TABLE,),
        )
        if not cur.fetchone():
            _SNAPSHOT_SCHEMA_CHECKED = True
            return

        cur.execute(f'PRAGMA table_info("{SNAPSHOT_TABLE}")')
        cols_info = cur.fetchall()  # cid, name, type, notnull, dflt_value, pk
        if not cols_info:
            _SNAPSHOT_SCHEMA_CHECKED = True
            return

        pk_cols = [row[1] for row in sorted(cols_info, key=lambda x: x[5]) if row[5] > 0]
        if pk_cols == ["symbol", "cache_date"]:
            _SNAPSHOT_SCHEMA_CHECKED = True
            return

        col_names = [row[1] for row in cols_info]
        if "cache_date" not in col_names:
            col_names.append("cache_date")

        tmp_table = f"{SNAPSHOT_TABLE}_migrating"
        cur.execute(f'DROP TABLE IF EXISTS "{tmp_table}"')

        def _col_type(name: str) -> str:
            for c in cols_info:
                if c[1] == name and c[2]:
                    return c[2]
            return "TEXT"

        col_defs = [f'"{name}" {_col_type(name)}' for name in col_names]
        create_sql = (
            f'CREATE TABLE "{tmp_table}" ('
            + ", ".join(col_defs)
            + ', PRIMARY KEY ("symbol", "cache_date"))'
        )
        cur.execute(create_sql)

        insert_cols = ', '.join(f'"{c}"' for c in col_names)
        select_parts = []
        has_cache_date = any(c[1] == "cache_date" for c in cols_info)
        has_generated_on = any(c[1] == "generated_on" for c in cols_info)

        for c in col_names:
            if c == "cache_date":
                if has_cache_date:
                    expr = 'COALESCE(NULLIF("cache_date",""), '
                    expr += 'substr("generated_on",1,10), date("now"))'
                    if not has_generated_on:
                        expr = 'COALESCE(NULLIF("cache_date",""), date("now"))'
                else:
                    expr = 'COALESCE(substr("generated_on",1,10), date("now"))'
                    if not has_generated_on:
                        expr = 'date("now")'
                select_parts.append(f'{expr} AS "cache_date"')
            else:
                select_parts.append(f'"{c}"')
        select_sql = ", ".join(select_parts)

        cur.execute(
            f'INSERT INTO "{tmp_table}" ({insert_cols}) '
            f'SELECT {select_sql} FROM "{SNAPSHOT_TABLE}"'
        )
        cur.execute(f'DROP TABLE "{SNAPSHOT_TABLE}"')
        cur.execute(f'ALTER TABLE "{tmp_table}" RENAME TO "{SNAPSHOT_TABLE}"')
        conn.commit()
        logger.info("✅ 已完成 market_snapshot_cache 表结构迁移为主键(symbol, cache_date)")
        _SNAPSHOT_SCHEMA_CHECKED = True
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ market_snapshot_cache 表结构迁移失败: %s", exc)
        conn.rollback()
    finally:
        conn.close()

def _generate_snapshot(symbol: str, trace_state: dict | None = None, as_of_date: str | None = None) -> Dict[str, Any]:
    limits = get_news_limits()
    try:
        news_limit = max(1, int(limits.get("news_max_news", 10)))
    except (TypeError, ValueError):
        news_limit = 10

    news_items = get_stock_news(
        symbol,
        max_news=news_limit,
        date=as_of_date,
        agent_name="market_snapshot",
        trace_state=trace_state,
    )
    logger.info("🗞️ Snapshot news pool for %s: %d 条", symbol, len(news_items))
    if not news_items:
        logger.warning("⚠️ Snapshot prompt缺少新闻，将使用空模板: %s", symbol)
    messages = _build_prompt(symbol, news_items)
    logger.info("🤖 正在调用 LLM 生成 %s 的市场快照...", symbol)
    if trace_state:
        llm_response = log_llm_interaction(trace_state)(get_chat_completion)(messages)
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
    agent_name: str | None = None,
    as_of_date: str | None = None,
) -> Dict[str, Any]:
    _ensure_snapshot_table_schema()

    refresh_snapshot = get_cache_refresh_flag(agent_name or "market_snapshot", "snapshot")
    if refresh_snapshot:
        logger.info("🔄 强制刷新市场快照缓存: %s", symbol)

    cache_date = (as_of_date or datetime.now().strftime("%Y-%m-%d"))

    if not refresh_snapshot:
        cached = cache.fetch_records(
            table=SNAPSHOT_TABLE,
            filters={"symbol": symbol, "cache_date": cache_date},
            ttl_seconds=ttl_seconds,
            order_by='"缓存时间" DESC',
            limit=1,
        )
        if cached:
            record = _records_to_dict(cached[0])
            logger.info("?? Market snapshot cache hit for %s (%s)", symbol, cache_date)
            return record

    snapshot = _generate_snapshot(symbol, trace_state=trace_state, as_of_date=cache_date)
    record = {"symbol": symbol, "cache_date": cache_date, **snapshot}
    cache.upsert_records(SNAPSHOT_TABLE, [record], key_columns=["symbol", "cache_date"])
    logger.info("⚙️ Market snapshot refreshed for %s", symbol)
    return record


def _records_to_dict(record: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = dict(record)
    cleaned.pop("缓存时间", None)
    return cleaned
