from datetime import datetime, timedelta
import json

from langchain_core.messages import HumanMessage

from src.agents.state import AgentState, show_agent_reasoning, show_workflow_status
from src.tools.news_crawler import get_stock_news, get_news_sentiment
from src.utils.logging_config import setup_logger
from src.utils.api_utils import agent_endpoint, log_llm_interaction

logger = setup_logger("sentiment_agent")


@agent_endpoint("sentiment", "情绪分析师，分析市场情绪和媒体信号")
def sentiment_agent(state: AgentState):
    """Responsible for sentiment analysis."""
    show_workflow_status("Sentiment Analyst")
    show_reasoning = state["metadata"]["show_reasoning"]
    data = state["data"]
    symbol = data["ticker"]
    logger.info(f"正在分析股票: {symbol}")

    num_of_news = data.get("num_of_news", 20)
    end_date = data.get("end_date")

    news_list = get_stock_news(symbol, max_news=num_of_news, date=end_date)
    logger.debug("Fetched %d news items (max=%d)", len(news_list), num_of_news)

    cutoff_date = datetime.now() - timedelta(days=7)
    recent_news = []
    for news in news_list:
        publish_time = news.get("publish_time")
        if publish_time:
            try:
                news_date = datetime.strptime(publish_time, "%Y-%m-%d %H:%M:%S")
                if news_date > cutoff_date:
                    recent_news.append(news)
            except ValueError:
                recent_news.append(news)
        else:
            recent_news.append(news)

    sentiment_score = None
    sentiment_error = None
    try:
        sentiment_score = get_news_sentiment(recent_news, num_of_news=num_of_news)
        logger.debug(
            "Sentiment score for %s based on %d filtered news: %.4f",
            symbol,
            len(recent_news),
            sentiment_score,
        )
    except Exception as exc:  # noqa: BLE001
        sentiment_error = str(exc)
        logger.exception("Failed to compute news sentiment for %s: %s", symbol, exc)

    if sentiment_score is None:
        signal = "error"
        confidence = "0%"
        reasoning_text = (
            f"Failed to compute sentiment score (news={len(recent_news)}). "
            f"Error: {sentiment_error or 'unknown'}"
        )
        sentiment_value = "error"
    elif sentiment_score >= 0.5:
        signal = "bullish"
        confidence = f"{round(abs(sentiment_score) * 100)}%"
        reasoning_text = (
            f"Based on {len(recent_news)} recent news articles, sentiment score: "
            f"{sentiment_score:.2f}"
        )
        sentiment_value = sentiment_score
    elif sentiment_score <= -0.5:
        signal = "bearish"
        confidence = f"{round(abs(sentiment_score) * 100)}%"
        reasoning_text = (
            f"Based on {len(recent_news)} recent news articles, sentiment score: "
            f"{sentiment_score:.2f}"
        )
        sentiment_value = sentiment_score
    else:
        signal = "neutral"
        confidence = f"{round((1 - abs(sentiment_score)) * 100)}%"
        reasoning_text = (
            f"Based on {len(recent_news)} recent news articles, sentiment score: "
            f"{sentiment_score:.2f}"
        )
        sentiment_value = sentiment_score

    message_content = {
        "signal": signal,
        "confidence": confidence,
        "reasoning": reasoning_text,
    }
    logger.debug("Sentiment agent output: %s", message_content)

    if show_reasoning:
        show_agent_reasoning(message_content, "Sentiment Analysis Agent")
        state["metadata"]["agent_reasoning"] = message_content

    message = HumanMessage(
        content=json.dumps(message_content),
        name="sentiment_agent",
    )

    show_workflow_status("Sentiment Analyst", "completed")
    return {
        "messages": [message],
        "data": {
            **data,
            "sentiment_analysis": sentiment_value,
        },
        "metadata": state["metadata"],
    }
