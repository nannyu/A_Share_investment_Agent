from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CACHE_REFRESH: Dict[str, Dict[str, bool]] = {
    "market_data_agent": {
        "price_history": False,
        "financial_indicators": False,
        "financial_reports": False,
        "market_snapshot": False,
    },
    "market_snapshot": {
        "news": True,
        "snapshot": False,
    },
    "sentiment_agent": {
        "news": True,
    },
    "macro_analyst_agent": {
        "news": True,
    },
    "macro_news_agent": {
        "news": False,
        "summary": False,
    },
}

DEFAULT_NEWS_LIMITS: Dict[str, int] = {
    "news_max_news": 100,
    "tavily_max_news": 20,
}


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for key, value in base.items():
        if isinstance(value, dict):
            merged[key] = value.copy()
        else:
            merged[key] = value
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def get_cache_refresh_config() -> Dict[str, Dict[str, bool]]:
    config = load_config()
    user_cfg = config.get("cache_refresh", {})
    if not isinstance(user_cfg, dict):
        user_cfg = {}
    merged = _deep_merge(DEFAULT_CACHE_REFRESH, user_cfg)
    return merged


def get_cache_refresh_flag(agent_name: str, cache_key: str) -> bool:
    merged = get_cache_refresh_config()
    agent_cfg = merged.get(agent_name, {})
    if isinstance(agent_cfg, dict):
        return bool(agent_cfg.get(cache_key, False))
    return bool(agent_cfg)


def get_news_limits() -> Dict[str, int]:
    config = load_config()
    user_cfg = config.get("news_limits", {})
    if not isinstance(user_cfg, dict):
        user_cfg = {}
    merged = _deep_merge(DEFAULT_NEWS_LIMITS, user_cfg)
    return merged
