import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage

from src.agents.state import AgentState, show_agent_reasoning, show_workflow_status
from src.tools.news_crawler import get_stock_news
from src.tools.openrouter_config import get_chat_completion
from src.utils.api_utils import agent_endpoint, log_llm_interaction
from src.utils.prompt_loader import load_prompt
from src.utils.logging_config import setup_logger

MACRO_INDEX_NAME = "沪深300指数"
DEFAULT_SUMMARY_TEXT = "宏观新闻分析不可用或尚未生成。"
ERROR_SUMMARY_TEXT = "宏观新闻分析过程中发生错误"
MACRO_SUMMARY_PATH = os.path.join("src", "data", "macro_summary.json")

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
    output_file_path = MACRO_SUMMARY_PATH

    summary_failed = False
    from_cache = False
    retrieved_news_count = 0
    analysis_payload: Optional[Dict[str, Any]] = None
    all_summaries: Dict[str, Any] = {}

    if os.path.exists(output_file_path):
        logger.info("📁 尝试加载宏观新闻缓存文件: %s", output_file_path)
        try:
            with open(output_file_path, "r", encoding="utf-8") as f:
                all_summaries = json.load(f)
            cached_entry = all_summaries.get(today_str)
            if cached_entry and cached_entry.get("summary_content"):
                cached_status = cached_entry.get("status", "ok")
                cached_analysis = cached_entry.get("analysis")
                invalid_cache = cached_status == "error" or not isinstance(cached_analysis, dict)
                if invalid_cache:
                    logger.warning("⚠️ 检测到无效宏观缓存（status=%s），准备重新生成", cached_status)
                    show_workflow_status(f"{agent_name}: 缓存中的 {today_str} 宏观摘要无效，尝试重新生成。")
                else:
                    analysis_payload = {
                        **cached_analysis,
                        "news_count": cached_analysis.get("news_count", cached_entry.get("retrieved_news_count", 0)),
                        "from_cache": True,
                        "generated_on": cached_analysis.get("generated_on", today_str),
                    }
                    retrieved_news_count = analysis_payload.get("news_count", 0)
                    from_cache = True
                    logger.info(
                        "📦 宏观新闻使用缓存: %s (news=%d)",
                        output_file_path,
                        retrieved_news_count,
                    )
                    show_workflow_status(f"{agent_name}: 从缓存加载 {today_str} 的宏观新闻总结。")
                    show_agent_reasoning(
                        analysis_payload,
                        "Macro News Agent (cached)",
                    )
        except json.JSONDecodeError:
            all_summaries = {}
            logger.error("❌ 宏观缓存 JSON 解码失败，忽略缓存文件。")
            show_agent_reasoning(
                {"error": "macro_summary.json decode error"},
                "Macro News Agent Cache",
            )
        except Exception as exc:
            all_summaries = {}
            logger.exception("❌ 读取宏观缓存文件异常: %s", exc)
            show_agent_reasoning(
                {"error": f"Failed to load macro summary cache: {exc}"},
                "Macro News Agent Cache",
            )

    news_items: List[Dict[str, str]] = []

    if not from_cache:
        try:
            logger.info("📰 正在抓取指数新闻: %s", symbol)
            today_str = datetime.now().strftime("%Y-%m-%d")
            news_raw = get_stock_news(
                symbol,
                max_news=100,
                date=today_str,
                agent_name="macro_news_agent",
                trace_state=state,
            )
            if not news_raw:
                summary_failed = True
                logger.warning("?? 未获取到任何宏观新闻，使用默认摘要。")
                analysis_payload = _default_macro_payload("未获取到相关宏观新闻数据。", 0, False, today_str)
            else:
                if isinstance(news_raw, list):
                    retrieved_news_count = len(news_raw)
                    logger.info("? 成功获取宏观新闻 %d 条", retrieved_news_count)
                    show_workflow_status(f"{agent_name}: 成功获取 {retrieved_news_count} 条新闻")
                    for item in news_raw:
                        news_items.append({
                            "title": str(item.get("title") or "").strip(),
                            "content": str(item.get("content") or item.get("title") or "").strip(),
                            "publish_time": str(item.get("publish_time") or item.get("search_time") or "").strip(),
                        })
                else:
                    news_df = news_raw
                    if news_df is None or getattr(news_df, "empty", False):
                        summary_failed = True
                        logger.warning("?? 未获取到任何宏观新闻，使用默认摘要。")
                        analysis_payload = _default_macro_payload("未获取到相关宏观新闻数据。", 0, False, today_str)
                    else:
                        retrieved_news_count = len(news_df)
                        logger.info("? 成功获取宏观新闻 %d 条", retrieved_news_count)
                        show_workflow_status(
                            f"{agent_name}: 成功获取 {retrieved_news_count} 条新闻")
                        for _, row in news_df.iterrows():
                            row_dict = row.to_dict()
                            news_items.append({
                                "title": str(row_dict.get("新闻标题") or row_dict.get("title") or "").strip(),
                                "content": str(row_dict.get("新闻内容") or row_dict.get("content") or "").strip(),
                                "publish_time": str(row_dict.get("发布时间") or row_dict.get("publish_time") or "").strip(),
                            })
                news_json = _format_prompt_payload(news_items)
                prompt_text = LLM_PROMPT_MACRO_ANALYSIS.replace("<<NEWS_JSON>>", news_json)
                logger.info("🤖 调用 LLM 生成宏观摘要 (news=%d)", retrieved_news_count)
                llm_response = log_llm_interaction(state)(
                    lambda: get_chat_completion(
                        [{"role": "user", "content": prompt_text}]
                    )
                )()
                if not llm_response:
                    raise ValueError("LLM returned empty response for macro news analysis.")
                analysis_payload = _payload_from_llm(llm_response, retrieved_news_count, False, today_str)
                logger.info("✅ LLM 宏观摘要生成完成 (news=%d)", retrieved_news_count)
                show_agent_reasoning(analysis_payload, "Macro News Agent (LLM)")
        except Exception as exc:
            summary_failed = True
            analysis_payload = _default_macro_payload(f"{ERROR_SUMMARY_TEXT}: {exc}", retrieved_news_count, False, today_str)
            logger.exception("Macro news agent failed during LLM workflow.")
            show_agent_reasoning({"error": str(exc)}, "Macro News Agent Error")
            logger.warning("⚠️ 宏观新闻降级为默认摘要: %s", exc)

    if analysis_payload is None:
        analysis_payload = _default_macro_payload(DEFAULT_SUMMARY_TEXT, retrieved_news_count, from_cache, today_str)
    else:
        analysis_payload["from_cache"] = from_cache
        analysis_payload["news_count"] = analysis_payload.get("news_count", retrieved_news_count)
        analysis_payload.setdefault("generated_on", today_str)

    summary_text = analysis_payload.get("summary", DEFAULT_SUMMARY_TEXT)

    if not from_cache:
        entry = {
            "summary_content": summary_text,
            "analysis": analysis_payload,
            "retrieved_news_count": analysis_payload.get("news_count", 0),
            "last_updated": datetime.now().isoformat(),
            "status": "error" if summary_failed else "ok",
        }
        all_summaries[today_str] = entry
        os.makedirs(os.path.dirname(output_file_path), exist_ok=True)
        with open(output_file_path, 'w', encoding='utf-8') as f:
            json.dump(all_summaries, f, ensure_ascii=False, indent=4)
        show_workflow_status(f"{agent_name}: 已将宏观总结保存至 {output_file_path}")
        logger.info(
            "💾 宏观摘要写入 %s（news=%d，status=%s）",
            output_file_path,
            entry["retrieved_news_count"],
            entry["status"],
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
