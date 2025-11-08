"""Cached market data helpers backed by SQLite."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import akshare as ak
import pandas as pd

from src.database import AkshareSQLiteCache
from src.network.proxy_manager import proxy_manager
from src.tools.baostock_client import query_history_k_data_plus
from src.utils.logging_config import setup_logger

# Column name constants (use unicode escapes to avoid encoding glitches)
COL_CODE = "\u4ee3\u7801"
COL_NAME = "\u540d\u79f0"
COL_DATE = "\u65e5\u671f"
COL_REPORT_DATE = "\u62a5\u544a\u65e5"
COL_REPORT_TYPE = "\u62a5\u8868\u7c7b\u578b"
COL_KEYWORD = "\u5173\u952e\u8bcd"
COL_PUBLISH_TIME = "\u53d1\u5e03\u65f6\u95f4"
COL_HEADLINE = "\u65b0\u95fb\u6807\u9898"
COL_ADJUST_TYPE = "\u590d\u6743\u7c7b\u578b"
COL_TRADE_DATE = "trade_date"

BASE_DIR = Path(__file__).resolve().parents[2]
CACHE_PATH = BASE_DIR / "data" / "market_data_cache.db"
HISTORY_TABLE = "baostock_history_k"

logger = setup_logger("akshare_cache")
cache = AkshareSQLiteCache(CACHE_PATH)


def _log_cache_hit(label: str, symbol: str, rows: int) -> None:
    logger.info("[cache] %s 命中，标的=%s，行数=%d", label, symbol, rows)


def _log_cache_upsert(label: str, symbol: str, rows: int, extra: str = "") -> None:
    suffix = f"，{extra}" if extra else ""
    logger.info("[cache] %s 写入完成，标的=%s，新增/更新行数=%d%s", label, symbol, rows, suffix)


def _call_with_retry(func, label: str):
    try:
        return proxy_manager.run(func, label)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"AkShare {label} error: {exc}")
        return None


def _drop_cache_columns(df: pd.DataFrame) -> pd.DataFrame:
    return df.drop(columns=["缓存时间"], errors="ignore")


def _records_to_df(records: List[Dict]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    return _drop_cache_columns(df)


def _resolve_exchange_symbol(symbol: str) -> str:
    cleaned = symbol.strip()
    lowered = cleaned.lower()
    if lowered.startswith(("sh", "sz")):
        return lowered
    if cleaned.startswith(("6", "9")):
        return f"sh{cleaned}"
    return f"sz{cleaned}"


def get_stock_spot_row(symbol: str, ttl_seconds: int = 600) -> Optional[pd.Series]:
    cached = cache.fetch_records(
        table="stock_zh_a_spot_em",
        filters={COL_CODE: symbol},
        ttl_seconds=ttl_seconds,
        order_by='"缓存时间" DESC',
        limit=1,
    )
    if cached:
        _log_cache_hit("stock_zh_a_spot_em", symbol, len(cached))
        row = cached[0].copy()
        row.pop("缓存时间", None)
        return pd.Series(row)

    df = _call_with_retry(lambda: ak.stock_zh_a_spot_em(), "stock_zh_a_spot_em")
    if df is None:
        return None

    if df is None or df.empty or COL_CODE not in df.columns:
        return None

    filtered = df[df[COL_CODE] == symbol]
    if filtered.empty:
        return None

    cache.upsert_records(
        "stock_zh_a_spot_em",
        filtered.to_dict("records"),
        key_columns=[COL_CODE],
    )
    _log_cache_upsert("stock_zh_a_spot_em", symbol, len(filtered))
    return filtered.iloc[0]


def get_financial_indicators(
    symbol: str, start_year: str, ttl_seconds: int = 24 * 3600
) -> pd.DataFrame:
    cached = cache.fetch_records(
        table="stock_financial_analysis_indicator",
        filters={COL_CODE: symbol},
        ttl_seconds=ttl_seconds,
        order_by=f'"{COL_DATE}" DESC',
    )
    if cached:
        _log_cache_hit("stock_financial_analysis_indicator", symbol, len(cached))
        return _records_to_df(cached)

    df = _call_with_retry(
        lambda: ak.stock_financial_analysis_indicator(symbol=symbol, start_year=start_year),
        "stock_financial_analysis_indicator",
    )
    if df is None:
        return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

    df[COL_CODE] = symbol
    cache.upsert_records(
        "stock_financial_analysis_indicator",
        df.to_dict("records"),
        key_columns=[COL_CODE, COL_DATE],
    )
    _log_cache_upsert("stock_financial_analysis_indicator", symbol, len(df))
    return df


def get_financial_report(
    symbol: str, report_type: str, ttl_seconds: int = 7 * 24 * 3600
) -> pd.DataFrame:
    cached = cache.fetch_records(
        table="stock_financial_report_sina",
        filters={COL_CODE: symbol, COL_REPORT_TYPE: report_type},
        ttl_seconds=ttl_seconds,
    )
    if cached:
        _log_cache_hit(f"stock_financial_report_sina[{report_type}]", symbol, len(cached))
        return _records_to_df(cached)

    exchange_symbol = _resolve_exchange_symbol(symbol)
    df = _call_with_retry(
        lambda: ak.stock_financial_report_sina(stock=exchange_symbol, symbol=report_type),
        "stock_financial_report_sina",
    )
    if df is None:
        return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

    if COL_REPORT_DATE in df.columns:
        df[COL_REPORT_DATE] = pd.to_datetime(df[COL_REPORT_DATE]).dt.strftime("%Y-%m-%d")
    df[COL_CODE] = symbol
    df[COL_REPORT_TYPE] = report_type
    cache.upsert_records(
        "stock_financial_report_sina",
        df.to_dict("records"),
        key_columns=[COL_CODE, COL_REPORT_TYPE, COL_REPORT_DATE],
    )
    _log_cache_upsert(
        f"stock_financial_report_sina[{report_type}]", symbol, len(df)
    )
    return df


def get_price_history_df(
    symbol: str,
    start_date: datetime,
    end_date: datetime,
    adjust: str = "qfq",
    ttl_seconds: Optional[int] = 24 * 3600,
) -> pd.DataFrame:
    ttl = ttl_seconds if ttl_seconds is not None else 24 * 3600
    filters = {"symbol": symbol, "adjust_flag": adjust or ""}
    cached = cache.fetch_records(
        table=HISTORY_TABLE,
        filters=filters,
        ttl_seconds=ttl,
        order_by='"date" ASC',
    )

    if cached:
        df_cached = _records_to_df(cached)
        if not df_cached.empty:
            df_cached["date"] = pd.to_datetime(df_cached["date"])
            mask = (df_cached["date"] >= start_date) & (df_cached["date"] <= end_date)
            subset = df_cached.loc[mask].copy()
            if not subset.empty:
                subset.sort_values("date", inplace=True)
                return subset

    fetched = query_history_k_data_plus(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        adjust=adjust,
    )
    if fetched.empty:
        return pd.DataFrame()

    numeric_cols = ["open", "high", "low", "close", "preclose", "volume", "amount"]
    for col in numeric_cols:
        fetched[col] = pd.to_numeric(fetched[col], errors="coerce")
    fetched["pct_change"] = pd.to_numeric(fetched["pctChg"], errors="coerce") / 100.0
    fetched["turnover"] = pd.to_numeric(fetched["turn"], errors="coerce") / 100.0
    fetched["change_amount"] = fetched["close"] - fetched["preclose"]
    base = fetched["preclose"].replace(0, pd.NA)
    fetched["amplitude"] = ((fetched["high"] - fetched["low"]) / base) * 100
    fetched["amplitude"] = fetched["amplitude"].fillna(0)
    fetched["date"] = pd.to_datetime(fetched["date"])
    fetched["symbol"] = symbol
    fetched["adjust_flag"] = adjust or ""

    columns = [
        "symbol",
        "adjust_flag",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "amplitude",
        "pct_change",
        "change_amount",
        "turnover",
    ]
    prepared = fetched[columns]
    cache.upsert_records(
        HISTORY_TABLE,
        prepared.to_dict("records"),
        key_columns=["symbol", "adjust_flag", "date"],
    )
    _log_cache_upsert(HISTORY_TABLE, symbol, len(prepared))
    return prepared

def get_stock_news(symbol: str, ttl_seconds: int = 2 * 3600) -> pd.DataFrame:
    cached = cache.fetch_records(
        table="stock_news_em",
        filters={COL_KEYWORD: symbol},
        ttl_seconds=ttl_seconds,
        order_by=f'"{COL_PUBLISH_TIME}" DESC',
    )
    if cached:
        _log_cache_hit("stock_news_em", symbol, len(cached))
        return _records_to_df(cached)

    df = _call_with_retry(lambda: ak.stock_news_em(symbol=symbol), "stock_news_em")
    if df is None:
        return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

    df[COL_KEYWORD] = symbol
    cache.upsert_records(
        "stock_news_em",
        df.to_dict("records"),
        key_columns=[COL_KEYWORD, COL_PUBLISH_TIME, COL_HEADLINE],
    )
    _log_cache_upsert("stock_news_em", symbol, len(df))
    return df
