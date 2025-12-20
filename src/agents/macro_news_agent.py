import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage

from src.agents.state import AgentState, show_agent_reasoning, show_workflow_status
from src.tools.news_crawler import get_stock_news
from src.tools.openrouter_config import get_chat_completion
from src.utils.api_utils import agent_endpoint, log_llm_interaction
from src.utils.prompt_loader import load_prompt
from src.utils.config_loader import get_cache_refresh_flag
from src.database import AkshareSQLiteCache
from src.tools.akshare_cache import CACHE_PATH
from src.utils.logging_config import setup_logger

MACRO_INDEX_NAME = "沪深300指数"
DEFAULT_SUMMARY_TEXT = "宏观新闻分析不可用或尚未生成。"
ERROR_SUMMARY_TEXT = "宏观新闻分析过程中发生错误"
LLM_PROMPT_MACRO_ANALYSIS = load_prompt("prompts/macro_news_agent/prompt.md")




logger = setup_logger("macro_news_agent")


def _sanitize_signal(value: Any) -> str:
    text = str(value).strip().lower()
    return text if text in {"bullish", "bearish", "neutral"} else "neutral"


def _sanitize_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _sanitize_score(value: Any) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return 50


def _sanitize_list(value: Any) -> List[str]:
    if isinstance(value, list) and value:
        cleaned = [str(item).strip() or "暂无" for item in value]
        return cleaned or ["暂无"]
    return ["暂无"]


def _default_macro_payload(summary: str, news_count: int, from_cache: bool, generated_on: str) -> Dict[str, Any]:
    return {
        "index": MACRO_INDEX_NAME,
        "signal": "neutral",
        "confidence": 0.5,
        "score": 50,
        "summary": summary or DEFAULT_SUMMARY_TEXT,
        "key_drivers": ["暂无"],
        "key_risks": ["暂无"],
        "actionable_insight": "暂无",
        "news_count": news_count,
        "from_cache": from_cache,
        "generated_on": generated_on,
    }


def _normalize_llm_json_text(raw: str) -> str:
    text = raw.strip().lstrip("\ufeff")
    if not text:
        return text
    if text[0] in {"'", '"'} and text[-1] == text[0]:
        text = text[1:-1].strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and start < end:
        return text[start:end + 1]
    cleaned = text.strip().strip("'").strip()
    if cleaned.startswith("json"):
        cleaned = cleaned[4:].lstrip()
    cleaned = cleaned.strip()
    if cleaned.startswith("{"):
        candidate = cleaned
    else:
        candidate = "{\n" + cleaned
    if not candidate.rstrip().endswith("}"):
        candidate = candidate.rstrip().rstrip(",") + "\n}"
    return candidate


def _coerce_to_dict(text: str) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    json_match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if json_match:
        try:
            return json.loads(json_match.group(1).strip())
        except json.JSONDecodeError:
            pass
    brace_match = re.search(r"(\{.*\})", text, re.DOTALL)
    if brace_match:
        candidate = brace_match.group(1)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        try:
            import ast

            value = ast.literal_eval(candidate)
            if isinstance(value, dict):
                return value
        except Exception:
            pass
    try:
        import ast

        value = ast.literal_eval(text)
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    raise ValueError("Unable to parse structured JSON.")


def _escape_summary_inner_quotes(text: str) -> str:
    marker = "\"summary\":"
    idx = text.find(marker)
    if idx == -1:
        return text
    open_quote = text.find("\"", idx + len(marker))
    if open_quote == -1:
        return text
    closing = None
    i = open_quote + 1
    while i < len(text):
        if text[i] == "\"" and text[i - 1] != "\\":
            closing = i
            break
        i += 1
    if closing is None:
        return text
    inner = text[open_quote + 1:closing]
    escaped_inner_chars = []
    prev = ""
    for ch in inner:
        if ch == "\"" and prev != "\\":
            escaped_inner_chars.append('\\"')
        else:
            escaped_inner_chars.append(ch)
        prev = ch
    escaped_inner = "".join(escaped_inner_chars)
    return text[: open_quote + 1] + escaped_inner + text[closing:]


def _sanitize_llm_text(text: str) -> str:
    normalized = _normalize_llm_json_text(text)
    replacements = {
        "“": '"',
        "”": '"',
        "’": "'",
        "‘": "'",
        "，": ',',
    }
    for src, dst in replacements.items():
        normalized = normalized.replace(src, dst)
    normalized = _escape_summary_inner_quotes(normalized)
    return normalized
def _payload_from_llm(response_text: str, news_count: int, from_cache: bool, generated_on: str) -> Dict[str, Any]:
    candidate_text = _sanitize_llm_text(response_text)
    try:
        parsed = _coerce_to_dict(candidate_text)
    except ValueError as exc:
        logger.error("Macro news LLM raw response: %s", response_text)
        logger.error("Macro news LLM normalized candidate: %s", candidate_text)
        raise ValueError(
            f"Unable to parse macro news JSON: {exc}; "
            f"normalized={candidate_text[:200]!r}"
        ) from exc

    summary_text = str(parsed.get("summary", "")).strip() or DEFAULT_SUMMARY_TEXT
    payload = {
        "index": parsed.get("index", MACRO_INDEX_NAME),
        "signal": _sanitize_signal(parsed.get("signal")),
        "confidence": _sanitize_confidence(parsed.get("confidence")),
        "score": _sanitize_score(parsed.get("score")),
        "summary": summary_text,
        "key_drivers": _sanitize_list(parsed.get("key_drivers")),
        "key_risks": _sanitize_list(parsed.get("key_risks")),
        "actionable_insight": str(parsed.get("actionable_insight", "暂无")).strip() or "暂无",
        "news_count": news_count,
        "from_cache": from_cache,
        "generated_on": generated_on,
    }
    return payload


def _format_prompt_payload(news_items: List[Dict[str, str]]) -> str:
    return json.dumps(news_items, ensure_ascii=False, indent=2)


@agent_endpoint("macro_news_agent", "获取沪深300全量新闻并进行宏观分析，为投资决策提供市场层面的宏观环境评估")
def macro_news_agent(state: AgentState) -> Dict[str, Any]:
    agent_name = "macro_news_agent"
    show_workflow_status(f"{agent_name}: --- Executing Macro News Agent ---")
    symbol = "000300"
    today_str = datetime.now().strftime("%Y-%m-%d")

    summary_failed = False
    from_cache = False
    retrieved_news_count = 0
    analysis_payload: Optional[Dict[str, Any]] = None

    refresh_summary = get_cache_refresh_flag("macro_news_agent", "summary")
    if refresh_summary:
        logger.info("🔄 强制刷新宏观摘要缓存: %s", today_str)

    cache_key = f"macro_news_summary|{symbol}|{today_str}"
    cache = AkshareSQLiteCache(CACHE_PATH)
    if not refresh_summary:
        cached_rows = cache.fetch_records(
            table="llm_result_cache",
            filters={"cache_key": cache_key},
            limit=1,
        )
        if cached_rows:
            cached_val = cached_rows[0].get("result")
            if cached_val:
                try:
                    analysis_payload = json.loads(cached_val)
                    analysis_payload = {
                        **analysis_payload,
                        "from_cache": True,
                        "generated_on": analysis_payload.get("generated_on", today_str),
                    }
                    retrieved_news_count = analysis_payload.get("news_count", 0)
                    from_cache = True
                    logger.info(
                        "📦 宏观新闻使用缓存: cache_key=%s (news=%d)",
                        cache_key,
                        retrieved_news_count,
                    )
                    show_workflow_status(f"{agent_name}: 从缓存加载 {today_str} 的宏观新闻总结。")
                    show_agent_reasoning(
                        analysis_payload,
                        "Macro News Agent (cached)",
                    )
                except Exception as exc:
                    logger.warning("⚠️ 宏观摘要缓存解析失败，准备重新生成: %s", exc)
                    analysis_payload = None

    news_items: List[Dict[str, str]] = []
    if not from_cache:
        cache.upsert_records(
            table="llm_result_cache",
            records=[
                {
                    "cache_key": cache_key,
                    "cache_type": "macro_news_summary",
                    "result": json.dumps(analysis_payload, ensure_ascii=False),
                }
            ],
            key_columns=["cache_key"],
        )
        show_workflow_status(f"{agent_name}: 已将宏观总结保存至 SQLite 缓存")
        logger.info(
            "📦 宏观摘要写入 SQLite（cache_key=%s，news=%d，status=%s）",
            cache_key,
            analysis_payload.get("news_count", 0),
            "error" if summary_failed else "ok",
        )
    else:
        logger.info(
            "📦 宏观摘要直接复用缓存结果（signal=%s, confidence=%s）",
            analysis_payload.get("signal"),
            analysis_payload.get("confidence"),
        )

    new_message = HumanMessage(
        content=json.dumps(analysis_payload, ensure_ascii=False),
        name=agent_name,
    )

    metadata_details = {
        "summary_generated_on": today_str,
        "news_count_for_summary": analysis_payload.get("news_count", 0),
        "signal": analysis_payload.get("signal"),
        "confidence": analysis_payload.get("confidence"),
        "score": analysis_payload.get("score"),
        "loaded_from_cache": from_cache,
    }

    updated_data = {
        **state["data"],
        "macro_news_analysis_result": analysis_payload,
        "macro_news_summary_text": summary_text,
    }

    logger.info(
        "📊 宏观摘要完成: signal=%s score=%s news=%d cache=%s",
        analysis_payload.get("signal"),
        analysis_payload.get("score"),
        analysis_payload.get("news_count"),
        from_cache,
    )
    show_workflow_status(f"{agent_name}: Execution finished.")

    return {
        "messages": list(state["messages"]) + [new_message],
        "data": updated_data,
        "metadata": {
            **state["metadata"],
            f"{agent_name}_details": metadata_details,
        },
    }
