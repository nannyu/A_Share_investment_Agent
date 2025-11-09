from langchain_core.messages import HumanMessage
from src.agents.state import AgentState, show_agent_reasoning, show_workflow_status
from src.utils.api_utils import agent_endpoint, log_llm_interaction
from src.utils.logging_config import setup_logger
import json
import ast


logger = setup_logger("researcher_bull_agent")


def _load_agent_signals(state: AgentState, agent_name: str) -> dict:
    """Fetch agent output if available, otherwise return neutral placeholder."""
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
                        "Failed to parse %s payload (%s), using neutral placeholder",
                        agent_name,
                        exc,
                    )
                    break
    logger.warning("Missing %s output, falling back to neutral signal", agent_name)
    return {"signal": "neutral", "confidence": "0%"}


@agent_endpoint("researcher_bull", "多方研究员，从看多角度分析市场数据并提出投资论点")
def researcher_bull_agent(state: AgentState):
    """Analyzes signals from a bullish perspective and generates optimistic investment thesis."""
    show_workflow_status("Bullish Researcher")
    show_reasoning = state["metadata"]["show_reasoning"]

    technical_signals = _load_agent_signals(state, "technical_analyst_agent")
    fundamental_signals = _load_agent_signals(state, "fundamentals_agent")
    sentiment_signals = _load_agent_signals(state, "sentiment_agent")
    valuation_signals = _load_agent_signals(state, "valuation_agent")

    # Analyze from bullish perspective
    bullish_points = []
    confidence_scores = []

    # Technical Analysis
    if technical_signals["signal"] == "bullish":
        bullish_points.append(
            f"Technical indicators show bullish momentum with {technical_signals['confidence']} confidence")
        confidence_scores.append(
            float(str(technical_signals["confidence"]).replace("%", "")) / 100)
    else:
        bullish_points.append(
            "Technical indicators may be conservative, presenting buying opportunities")
        confidence_scores.append(0.3)

    # Fundamental Analysis
    if fundamental_signals["signal"] == "bullish":
        bullish_points.append(
            f"Strong fundamentals with {fundamental_signals['confidence']} confidence")
        confidence_scores.append(
            float(str(fundamental_signals["confidence"]).replace("%", "")) / 100)
    else:
        bullish_points.append(
            "Company fundamentals show potential for improvement")
        confidence_scores.append(0.3)

    # Sentiment Analysis
    if sentiment_signals["signal"] == "bullish":
        bullish_points.append(
            f"Positive market sentiment with {sentiment_signals['confidence']} confidence")
        confidence_scores.append(
            float(str(sentiment_signals["confidence"]).replace("%", "")) / 100)
    else:
        bullish_points.append(
            "Market sentiment may be overly pessimistic, creating value opportunities")
        confidence_scores.append(0.3)

    # Valuation Analysis
    if valuation_signals["signal"] == "bullish":
        bullish_points.append(
            f"Stock appears undervalued with {valuation_signals['confidence']} confidence")
        confidence_scores.append(
            float(str(valuation_signals["confidence"]).replace("%", "")) / 100)
    else:
        bullish_points.append(
            "Current valuation may not fully reflect growth potential")
        confidence_scores.append(0.3)

    # Calculate overall bullish confidence
    avg_confidence = sum(confidence_scores) / len(confidence_scores)

    message_content = {
        "perspective": "bullish",
        "confidence": avg_confidence,
        "thesis_points": bullish_points,
        "reasoning": "Bullish thesis based on comprehensive analysis of technical, fundamental, sentiment, and valuation factors"
    }

    message = HumanMessage(
        content=json.dumps(message_content),
        name="researcher_bull_agent",
    )

    if show_reasoning:
        show_agent_reasoning(message_content, "Bullish Researcher")
        # 保存推理信息到metadata供API使用
        state["metadata"]["agent_reasoning"] = message_content

    show_workflow_status("Bullish Researcher", "completed")
    return {
        "messages": state["messages"] + [message],
        "data": state["data"],
        "metadata": state["metadata"],
    }
