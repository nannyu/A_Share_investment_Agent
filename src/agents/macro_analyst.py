from langchain_core.messages import HumanMessage
from src.agents.state import AgentState, show_agent_reasoning, show_workflow_status
from src.tools.news_crawler import get_stock_news
from src.utils.logging_config import setup_logger
from src.utils.api_utils import agent_endpoint, log_llm_interaction
import json
from datetime import datetime, timedelta
from src.tools.openrouter_config import get_chat_completion
from src.utils.prompt_loader import load_prompt, format_prompt
from src.database import AkshareSQLiteCache
from src.tools.akshare_cache import CACHE_PATH

# 设置日志记录
logger = setup_logger('macro_analyst_agent')


@agent_endpoint("macro_analyst", "宏观分析师，分析宏观经济环境对目标股票的影响")
def macro_analyst_agent(state: AgentState):
    """Responsible for macro analysis"""
    show_workflow_status("Macro Analyst")
    show_reasoning = state["metadata"]["show_reasoning"]
    data = state["data"]
    symbol = data["ticker"]
    logger.info(f"🧠 正在进行宏观分析: {symbol}")

    # 获取 end_date 并传递给 get_stock_news
    end_date = data.get("end_date")  # 从 run_hedge_fund 传递来的 end_date

    # 获取大量新闻数据（最多100条），传递正确的日期参数
    news_list = get_stock_news(
        symbol,
        max_news=100,
        date=end_date,
        agent_name="macro_analyst_agent",
        trace_state=state,
    )

    # 过滤七天前的新闻（只对有publish_time字段的新闻进行过滤）
    cutoff_date = datetime.now() - timedelta(days=7)
    recent_news = []
    for news in news_list:
        if 'publish_time' in news:
            try:
                news_date = datetime.strptime(
                    news['publish_time'], '%Y-%m-%d %H:%M:%S')
                if news_date > cutoff_date:
                    recent_news.append(news)
            except ValueError:
                # 如果时间格式无法解析，默认包含这条新闻
                recent_news.append(news)
        else:
            # 如果没有publish_time字段，默认包含这条新闻
            recent_news.append(news)

    logger.info(f"📰 获取到 {len(recent_news)} 条七天内的新闻")

    # 如果没有获取到新闻，返回默认结果
    if not recent_news:
        logger.warning(f"⚠️ 未获取到 {symbol} 的最近新闻，无法进行宏观分析")
        message_content = {
            "macro_environment": "neutral",
            "impact_on_stock": "neutral",
            "key_factors": [],
            "reasoning": "未获取到最近新闻，无法进行宏观分析"
        }
    else:
        # 获取宏观分析结果
        macro_analysis = get_macro_news_analysis(
            recent_news,
            symbol=symbol,
            cache_date=end_date,
            trace_state=state,
        )
        message_content = macro_analysis
        logger.info(
            "📊 宏观分析完成: env=%s impact=%s (news=%d)",
            macro_analysis.get("macro_environment"),
            macro_analysis.get("impact_on_stock"),
            len(recent_news),
        )

    # 如果需要显示推理过程
    if show_reasoning:
        show_agent_reasoning(message_content, "Macro Analysis Agent")
        # 保存推理信息到metadata供API使用
        state["metadata"]["agent_reasoning"] = message_content

    # 创建消息
    message = HumanMessage(
        content=json.dumps(message_content),
        name="macro_analyst_agent",
    )

    show_workflow_status("Macro Analyst", "completed")
    # logger.info(f"--- DEBUG: macro_analyst_agent COMPLETED ---")
    # logger.info(
    # f"--- DEBUG: macro_analyst_agent RETURN messages: {[msg.name for msg in (state['messages'] + [message])]} ---")
    return {
        "messages": state["messages"] + [message],
        "data": {
            **data,
            "macro_analysis": message_content
        },
        "metadata": state["metadata"],
    }


def get_macro_news_analysis(
    news_list: list,
    *,
    symbol: str | None = None,
    cache_date: str | None = None,
    trace_state: dict | None = None,
) -> dict:
    """分析宏观经济新闻对股票的影响

    Args:
        news_list (list): 新闻列表

    Returns:
        dict: 宏观分析结果，包含环境评估、对股票的影响、关键因素和详细推理
    """
    if not news_list:
        return {
            "macro_environment": "neutral",
            "impact_on_stock": "neutral",
            "key_factors": [],
            "reasoning": "没有足够的新闻数据进行宏观分析"
        }

    # 缓存 Key 规则（按“股票 + 日期 + v2”）：不要用新闻内容作为 key
    if not cache_date:
        cache_date = datetime.now().strftime("%Y-%m-%d")
    if symbol:
        cache_key = f"macro_analysis|{symbol}|{cache_date}"
    else:
        cache_key = f"macro_analysis|{cache_date}"

    cache = AkshareSQLiteCache(CACHE_PATH)
    cached_rows = cache.fetch_records(
        table="llm_result_cache",
        filters={"cache_key": cache_key},
        limit=1,
    )
    if cached_rows:
        cached_val = cached_rows[0].get("result")
        if cached_val:
            try:
                logger.info("📦 使用缓存的宏观分析结果")
                return json.loads(cached_val)
            except Exception:
                pass

    # 准备系统消息
    system_message = {
        "role": "system",
        "content": load_prompt("prompts/macro_analyst/system.md"),
    }

    # 准备新闻内容
    # 准备用户输入内容，最多取前50条新闻构造上下文
    news_content_blocks = []
    for news in news_list[:50]:
        news_content_blocks.append(
            f"标题：{news.get('title', '未知')}\n"
            f"来源：{news.get('source', '未知')}\n"
            f"时间：{news.get('publish_time', news.get('search_time', '未知'))}\n"
            f"内容：{news.get('content', '')}"
        )
    news_content = "\n\n".join(news_content_blocks)

    user_message = {
        "role": "user",
        "content": format_prompt(
            "prompts/macro_analyst/user.md",
            news_content=news_content,
        ),
    }

    try:
        # 获取LLM分析结果
        logger.info("🤖 正在调用LLM进行宏观分析...")
        if trace_state:
            result = log_llm_interaction(trace_state)(
                lambda: get_chat_completion([system_message, user_message])
            )()
        else:
            result = get_chat_completion([system_message, user_message])
        if result is None:
            logger.error("❌ LLM分析失败，无法获取宏观分析结果")
            return {
                "macro_environment": "neutral",
                "impact_on_stock": "neutral",
                "key_factors": [],
                "reasoning": "LLM分析失败，无法获取宏观分析结果"
            }

        # 解析JSON结果
        try:
            # 尝试直接解析
            analysis_result = json.loads(result.strip())
            logger.info("✅ 成功解析LLM返回的JSON结果")
        except json.JSONDecodeError:
            # 如果直接解析失败，尝试提取JSON部分
            import re
            json_match = re.search(r'```json\s*(.*?)\s*```', result, re.DOTALL)
            if json_match:
                try:
                    analysis_result = json.loads(json_match.group(1).strip())
                    logger.info("📤 成功从代码块中提取并解析JSON结果")
                except:
                    # 如果仍然失败，返回默认结果
                    logger.error("⚠️ 无法解析代码块中的JSON结果")
                    return {
                        "macro_environment": "neutral",
                        "impact_on_stock": "neutral",
                        "key_factors": [],
                        "reasoning": "无法解析LLM返回的JSON结果"
                    }
            else:
                # 如果没有找到JSON，返回默认结果
                logger.error("⚠️ LLM未返回有效的JSON格式结果")
                return {
                    "macro_environment": "neutral",
                    "impact_on_stock": "neutral",
                    "key_factors": [],
                    "reasoning": "LLM未返回有效的JSON格式结果"
                }

        # 缓存结果
        cache.upsert_records(
            table="llm_result_cache",
            records=[
                {
                    "cache_key": cache_key,
                    "cache_type": "macro_analysis",
                    "result": json.dumps(analysis_result, ensure_ascii=False),
                }
            ],
            key_columns=["cache_key"],
        )
        logger.info("💾 宏观分析结果已缓存")

        return analysis_result

    except Exception as e:
        logger.error(f"❌ 宏观分析出错: {e}")
        return {
            "macro_environment": "neutral",
            "impact_on_stock": "neutral",
            "key_factors": [],
            "reasoning": f"分析过程中出错: {str(e)}"
        }
