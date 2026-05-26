from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from src.database import AkshareSQLiteCache
from src.tools.baostock_client import format_symbol, query_stock_basic
from src.utils.logging_config import setup_logger

BASE_DIR = Path(__file__).resolve().parents[2]
_default_cache_path = BASE_DIR / "data" / "market_data_cache.db"
CACHE_PATH = Path(os.getenv("MARKET_CACHE_DB_PATH", str(_default_cache_path)))

STOCK_BASIC_TABLE = "stock_basic"
logger = setup_logger("stock_basic")
cache = AkshareSQLiteCache(CACHE_PATH)


def get_stock_name(
    symbol: str,
    *,
    ttl_seconds: int = 30 * 24 * 3600,
    force_refresh: bool = False,
) -> Optional[str]:
    # 股票基础信息缓存改为“无 TTL 常驻”；ttl_seconds 参数仅保留兼容性
    if not symbol:
        return None

    bs_symbol = format_symbol(symbol)
    if force_refresh:
        logger.info("?? 强制刷新股票基础信息: %s", symbol)
    else:
        cached = cache.fetch_records(
            table=STOCK_BASIC_TABLE,
            filters={"code": bs_symbol},
            order_by='"缓存时间" DESC',
            limit=1,
        )
        if cached:
            return cached[0].get("code_name")

    df = query_stock_basic(bs_symbol)
    if df is None or df.empty:
        return None

    record = df.iloc[0].to_dict()
    record["code"] = bs_symbol
    cache.upsert_records(
        STOCK_BASIC_TABLE,
        [record],
        key_columns=["code"],
    )
    return record.get("code_name")


def enrich_symbol(symbol: str, company_name: Optional[str]) -> str:
    symbol = symbol.strip()
    name = (company_name or "").strip()
    if not name or name == symbol:
        return symbol
    return f"{symbol} {name}"
