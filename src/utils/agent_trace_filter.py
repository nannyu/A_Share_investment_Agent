from __future__ import annotations

from typing import Any, Dict, Iterable


_BASE_KEYS = ("ticker", "start_date", "end_date", "num_of_news")

_AGENT_DATA_KEYS: Dict[str, Iterable[str]] = {
    "market_data_agent": (
        "prices",
        "price_history",
        "financial_metrics",
        "financial_line_items",
        "financial_statements",
        "market_data",
        "market_snapshot",
        "market_cap",
    ),
    "technical_analyst_agent": ("prices", "price_history", "technical_analysis"),
    "fundamentals_agent": ("financial_metrics", "financial_line_items", "fundamental_analysis"),
    "sentiment_agent": ("sentiment_analysis", "num_of_news"),
    "valuation_agent": ("financial_metrics", "financial_line_items", "market_cap", "valuation_analysis"),
    "macro_news_agent": ("macro_news_analysis_result", "macro_news_summary_text"),
    "macro_analyst_agent": ("macro_analysis",),
    "researcher_bull_agent": (
        "technical_analysis",
        "fundamental_analysis",
        "sentiment_analysis",
        "valuation_analysis",
        "macro_analysis",
        "macro_news_analysis_result",
    ),
    "researcher_bear_agent": (
        "technical_analysis",
        "fundamental_analysis",
        "sentiment_analysis",
        "valuation_analysis",
        "macro_analysis",
        "macro_news_analysis_result",
    ),
    "debate_room_agent": ("debate_analysis",),
    "portfolio_management_agent": (
        "technical_analysis",
        "fundamental_analysis",
        "sentiment_analysis",
        "valuation_analysis",
        "macro_analysis",
        "macro_news_analysis_result",
        "debate_analysis",
        "risk_analysis",
        "portfolio_decision",
        "portfolio_management_decision",
    ),
    "risk_management_agent": ("risk_analysis", "debate_analysis", "technical_analysis"),
}

_AGENT_ALIASES: Dict[str, str] = {
    # agent_endpoint names -> canonical keys above
    "market_data": "market_data_agent",
    "technical_analyst": "technical_analyst_agent",
    "fundamentals": "fundamentals_agent",
    "sentiment": "sentiment_agent",
    "valuation": "valuation_agent",
    "macro_news_agent": "macro_news_agent",
    "macro_analyst": "macro_analyst_agent",
    "researcher_bull": "researcher_bull_agent",
    "researcher_bear": "researcher_bear_agent",
    "debate_room": "debate_room_agent",
    "portfolio_management": "portfolio_management_agent",
    "risk_management": "risk_management_agent",
}


def _pick_keys(data: Dict[str, Any], keys: Iterable[str]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    return {key: data[key] for key in keys if key in data}


def build_agent_trace_payload(state: Dict[str, Any], agent_name: str, stage: str) -> Dict[str, Any]:
    """Return a minimized trace payload for a single agent."""
    if not isinstance(state, dict):
        return {"stage": stage, "agent_name": agent_name, "error": "state_not_dict"}

    metadata = state.get("metadata", {}) if isinstance(state.get("metadata"), dict) else {}
    data = state.get("data", {}) if isinstance(state.get("data"), dict) else {}

    canonical_name = _AGENT_ALIASES.get(agent_name, agent_name)
    selected = _pick_keys(data, _BASE_KEYS)
    selected.update(_pick_keys(data, _AGENT_DATA_KEYS.get(canonical_name, ())))

    filtered_metadata = {
        key: metadata.get(key)
        for key in ("run_id", "current_agent_name", "show_reasoning", "show_summary", "trace_dir")
        if key in metadata
    }

    return {
        "stage": stage,
        "agent_name": agent_name,
        "canonical_agent": canonical_name,
        "metadata": filtered_metadata,
        "data": selected,
    }
