from langchain_core.messages import HumanMessage
from src.agents.state import AgentState, show_agent_reasoning, show_workflow_status
from src.utils.api_utils import agent_endpoint, log_llm_interaction
from src.utils.logging_config import setup_logger
import json
import ast


logger = setup_logger("researcher_bear_agent")


def _load_agent_signals(state: AgentState, agent_name: str) -> dict:
    for message in reversed(state["messages"]):
        if message.name == agent_name:
            payload = message.content
            try:
                return json.loads(payload)
            except Exception:
                try:
                    return ast.literal_eval(payload)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Failed to parse %s payload (%s), fallback to neutral", agent_name, exc
                    )
                    break
    logger.warning("Missing %s output, fallback to neutral", agent_name)
    return {"signal": "neutral", "confidence": "0%"}


@agent_endpoint("researcher_bear", "空方研究员，从看空角度分析市场数据并提出风险警示")
def researcher_bear_agent(state: AgentState):
    """Analyzes signals from a bearish perspective and generates cautionary investment thesis."""
    show_workflow_status("Bearish Researcher")
    show_reasoning = state["metadata"]["show_reasoning"]

    technical_signals = _load_agent_signals(state, "technical_analyst_agent")
    fundamental_signals = _load_agent_signals(state, "fundamentals_agent")
    sentiment_signals = _load_agent_signals(state, "sentiment_agent")
    valuation_signals = _load_agent_signals(state, "valuation_agent")

    # Analyze from bearish perspective
    bearish_points = []
    confidence_scores = []

    # Technical Analysis
    if technical_signals["signal"] == "bearish":
        bearish_points.append(
            f"技术指标偏空，置信度 {technical_signals['confidence']}")
        confidence_scores.append(
            float(str(technical_signals["confidence"]).replace("%", "")) / 100)
    else:
        bearish_points.append(
            "近期反弹或属技术性修复，存在再度回落的风险")
        confidence_scores.append(0.3)

    # Fundamental Analysis
    if fundamental_signals["signal"] == "bearish":
        bearish_points.append(
            f"基本面压力未消化，置信度 {fundamental_signals['confidence']}")
        confidence_scores.append(
            float(str(fundamental_signals["confidence"]).replace("%", "")) / 100)
    else:
        bearish_points.append(
            "当前基本面优势或难长期维持")
        confidence_scores.append(0.3)

    # Sentiment Analysis
    if sentiment_signals["signal"] == "bearish":
        bearish_points.append(
            f"市场情绪偏空，置信度 {sentiment_signals['confidence']}")
        confidence_scores.append(
            float(str(sentiment_signals["confidence"]).replace("%", "")) / 100)
    else:
        bearish_points.append(
            "市场情绪可能过度乐观，需警惕回撤")
        confidence_scores.append(0.3)

    # Valuation Analysis
    if valuation_signals["signal"] == "bearish":
        bearish_points.append(
            f"估值偏高，置信度 {valuation_signals['confidence']}")
        confidence_scores.append(
            float(str(valuation_signals["confidence"]).replace("%", "")) / 100)
    else:
        bearish_points.append(
            "估值尚未充分计入下行风险")
        confidence_scores.append(0.3)

    # Calculate overall bearish confidence
    avg_confidence = sum(confidence_scores) / len(confidence_scores)

    message_content = {
        "perspective": "bearish",
        "confidence": avg_confidence,
        "thesis_points": bearish_points,
        "reasoning": "综合技术、基本面、情绪与估值因素，整体偏空，建议保持谨慎并关注潜在下行风险"
    }

    message = HumanMessage(
        content=json.dumps(message_content),
        name="researcher_bear_agent",
    )

    if show_reasoning:
        show_agent_reasoning(message_content, "Bearish Researcher")
        # 保存推理信息到metadata供API使用
        state["metadata"]["agent_reasoning"] = message_content

    show_workflow_status("Bearish Researcher", "completed")
    return {
        "messages": state["messages"] + [message],
        "data": state["data"],
        "metadata": state["metadata"],
    }
