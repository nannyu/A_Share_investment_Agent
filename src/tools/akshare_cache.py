"""Cached AkShare helpers backed by SQLite."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import akshare as ak
import pandas as pd

from src.database import AkshareSQLiteCache
from src.network.proxy_manager import proxy_manager
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

BASE_DIR = Path(__file__).resolve().parents[2]
CACHE_PATH = BASE_DIR / "data" / "akshare_cache.db"

logger = setup_logger("akshare_cache")
cache = AkshareSQLiteCache(CACHE_PATH)


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


def get_stock_spot_row(symbol: str, ttl_seconds: int = 600) -> Optional[pd.Series]:
    cached = cache.fetch_records(
        table="stock_zh_a_spot_em",
        filters={COL_CODE: symbol},
        ttl_seconds=ttl_seconds,
        order_by='"缓存时间" DESC',
        limit=1,
    )
    if cached:
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
        return _records_to_df(cached)

    df = _call_with_retry(
        lambda: ak.stock_financial_report_sina(stock=f"sh{symbol}", symbol=report_type),
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
    return df


def get_price_history_df(
    symbol: str,
    start_date: datetime,
    end_date: datetime,
    adjust: str = "qfq",
    ttl_seconds: Optional[int] = None,
) -> pd.DataFrame:
    def _fetch_segment(seg_start: datetime, seg_end: datetime) -> pd.DataFrame:
        data = _call_with_retry(
            lambda: ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=seg_start.strftime("%Y%m%d"),
                end_date=seg_end.strftime("%Y%m%d"),
                adjust=adjust,
            ),
            "stock_zh_a_hist",
        )
        if data is None or data.empty:
            return pd.DataFrame()
        data = data.copy()
        data[COL_DATE] = pd.to_datetime(data["����"]).dt.strftime("%Y-%m-%d")
        data[COL_CODE] = symbol
        data[COL_ADJUST_TYPE] = adjust
        cache.upsert_records(
            "stock_zh_a_hist",
            data.to_dict("records"),
            key_columns=[COL_CODE, COL_ADJUST_TYPE, COL_DATE],
        )
        data[COL_DATE] = pd.to_datetime(data[COL_DATE])
        return data

    filters = {COL_CODE: symbol, COL_ADJUST_TYPE: adjust}
    cached = cache.fetch_records(
        table="stock_zh_a_hist",
        filters=filters,
        ttl_seconds=ttl_seconds,
    )

    cached_frames: List[pd.DataFrame] = []
    cached_dates: Sequence[pd.Timestamp] = []
    if cached:
        df_cached = _records_to_df(cached)
        if COL_DATE in df_cached.columns:
            df_cached[COL_DATE] = pd.to_datetime(df_cached[COL_DATE])
            cached_dates = list(df_cached[COL_DATE].dt.normalize())
            cached_frames.append(df_cached)

    def _normalize(ts: datetime) -> pd.Timestamp:
        return pd.Timestamp(ts).normalize()

    def _missing_segments(
        req_start: datetime, req_end: datetime, existing: Sequence[pd.Timestamp]
    ) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
        if req_start > req_end:
            return []
        required = pd.date_range(_normalize(req_start), _normalize(req_end), freq="D")
        existing_set = set(existing)
        segments: List[Tuple[pd.Timestamp, pd.Timestamp]] = []
        current_start: Optional[pd.Timestamp] = None
        prev_day: Optional[pd.Timestamp] = None
        for day in required:
            if day in existing_set:
                if current_start is not None:
                    segments.append((current_start, prev_day))
                    current_start = None
            else:
                if current_start is None:
                    current_start = day
                prev_day = day
        if current_start is not None:
            segments.append((current_start, prev_day))
        return segments

    missing_segments = _missing_segments(start_date, end_date, cached_dates)
    for seg_start, seg_end in missing_segments:
        fetched = _fetch_segment(seg_start, seg_end)
        if not fetched.empty:
            cached_frames.append(fetched)

    if not cached_frames:
        return pd.DataFrame()

    combined = pd.concat(cached_frames, ignore_index=True)
    combined[COL_DATE] = pd.to_datetime(combined[COL_DATE])
    combined = combined.drop_duplicates(
        subset=[COL_CODE, COL_ADJUST_TYPE, COL_DATE], keep="last"
    )
    mask = (combined[COL_DATE] >= start_date) & (combined[COL_DATE] <= end_date)
    result = combined.loc[mask].copy()
    result.sort_values(COL_DATE, inplace=True)
    return result

def get_stock_news(symbol: str, ttl_seconds: int = 2 * 3600) -> pd.DataFrame:
    cached = cache.fetch_records(
        table="stock_news_em",
        filters={COL_KEYWORD: symbol},
        ttl_seconds=ttl_seconds,
        order_by=f'"{COL_PUBLISH_TIME}" DESC',
    )
    if cached:
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
    return df
