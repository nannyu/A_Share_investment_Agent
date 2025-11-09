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
            f"Technical indicators show bearish momentum with {technical_signals['confidence']} confidence")
        confidence_scores.append(
            float(str(technical_signals["confidence"]).replace("%", "")) / 100)
    else:
        bearish_points.append(
            "Technical rally may be temporary, suggesting potential reversal")
        confidence_scores.append(0.3)

    # Fundamental Analysis
    if fundamental_signals["signal"] == "bearish":
        bearish_points.append(
            f"Concerning fundamentals with {fundamental_signals['confidence']} confidence")
        confidence_scores.append(
            float(str(fundamental_signals["confidence"]).replace("%", "")) / 100)
    else:
        bearish_points.append(
            "Current fundamental strength may not be sustainable")
        confidence_scores.append(0.3)

    # Sentiment Analysis
    if sentiment_signals["signal"] == "bearish":
        bearish_points.append(
            f"Negative market sentiment with {sentiment_signals['confidence']} confidence")
        confidence_scores.append(
            float(str(sentiment_signals["confidence"]).replace("%", "")) / 100)
    else:
        bearish_points.append(
            "Market sentiment may be overly optimistic, indicating potential risks")
        confidence_scores.append(0.3)

    # Valuation Analysis
    if valuation_signals["signal"] == "bearish":
        bearish_points.append(
            f"Stock appears overvalued with {valuation_signals['confidence']} confidence")
        confidence_scores.append(
            float(str(valuation_signals["confidence"]).replace("%", "")) / 100)
    else:
        bearish_points.append(
            "Current valuation may not fully reflect downside risks")
        confidence_scores.append(0.3)

    # Calculate overall bearish confidence
    avg_confidence = sum(confidence_scores) / len(confidence_scores)

    message_content = {
        "perspective": "bearish",
        "confidence": avg_confidence,
        "thesis_points": bearish_points,
        "reasoning": "Bearish thesis based on comprehensive analysis of technical, fundamental, sentiment, and valuation factors"
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
