import os
import os
import os
import sys
import json
import re
from datetime import datetime, timedelta
import time
import pandas as pd
from urllib.parse import urlparse
from src.tools.openrouter_config import get_chat_completion, logger as api_logger
from src.tools.akshare_cache import get_stock_news as get_stock_news_akshare_cached
from src.tools.akshare_cache import CACHE_PATH
from pathlib import Path
from src.database import AkshareSQLiteCache

# 导入新的搜索模块
try:
    from src.crawler.search import google_search_sync, SearchOptions
except ImportError:
    print("⚠️ 警告: 无法导入新的搜索模块，将回退到 akshare")
    google_search_sync = None
    SearchOptions = None

# Tavily Search（可选，推荐用于替代直接爬 Google）
try:
    from src.crawler.tavily_search import tavily_search
except ImportError:
    tavily_search = None

BASE_DIR = Path(__file__).resolve().parents[2]
NEWS_CACHE_DB_PATH = BASE_DIR / "data" / "market_data_cache.db"
NEWS_CACHE_TABLE = "stock_news_cache"

def build_search_query(symbol: str, date: str = None) -> str:
    """
    构建针对股票新闻的 Google 搜索查询

    Args:
        symbol: 股票代码，如 "300059"
        date: 截止日期，格式 "YYYY-MM-DD"

    Returns:
        构建好的搜索查询字符串
    """
    # 基础查询：股票代码 + 新闻关键词
    base_query = f"{symbol} 股票 新闻 财经"

    # 添加时间限制（搜索指定日期之前的新闻）
    if date:
        try:
            # 解析日期并计算一周前的日期作为开始时间
            end_date = datetime.strptime(date, "%Y-%m-%d")
            start_date = end_date - timedelta(days=7)  # 搜索过去一周的新闻

            # Google 搜索时间语法：after:YYYY-MM-DD before:YYYY-MM-DD
            base_query += f" after:{start_date.strftime('%Y-%m-%d')} before:{date}"
        except ValueError:
            print(f"⚠️ 日期格式错误: {date}，忽略时间限制")

    # 限制新闻网站 - 只选择主要的财经网站
    news_sites = [
        "site:sina.com.cn",
        "site:163.com",
        "site:eastmoney.com",
        "site:cnstock.com",
        "site:hexun.com"
    ]

    # 添加网站限制
    query = f"{base_query} ({' OR '.join(news_sites)})"

    return query


def extract_domain(url: str) -> str:
    """从 URL 提取域名作为新闻来源"""
    try:
        parsed = urlparse(url)
        return parsed.netloc
    except:
        return "未知来源"


def convert_search_results_to_news_format(search_results, symbol: str) -> list:
    """
    将搜索结果转换为现有新闻格式

    Args:
        search_results: Google 搜索结果
        symbol: 股票代码

    Returns:
        符合现有格式的新闻列表
    """
    news_list = []

    for result in search_results:
        # 过滤掉明显不相关的结果
        if any(keyword in result.title.lower() for keyword in ['招聘', '求职', '广告', '登录', '注册']):
            continue

        # 尝试从snippet中提取时间信息
        publish_time = None
        if result.snippet:
            # 查找常见的时间模式
            import re
            time_patterns = [
                r'(\d{1,2}天前)',
                r'(\d{1,2}小时前)',
                r'(\d{4}-\d{2}-\d{2})',
                r'(\d{4}年\d{1,2}月\d{1,2}日)',
                r'(\d{2}-\d{2})'
            ]

            for pattern in time_patterns:
                match = re.search(pattern, result.snippet)
                if match:
                    time_str = match.group(1)
                    try:
                        # 处理相对时间
                        if '天前' in time_str:
                            days = int(time_str.replace('天前', ''))
                            publish_date = datetime.now() - timedelta(days=days)
                            publish_time = publish_date.strftime(
                                '%Y-%m-%d %H:%M:%S')
                        elif '小时前' in time_str:
                            hours = int(time_str.replace('小时前', ''))
                            publish_date = datetime.now() - timedelta(hours=hours)
                            publish_time = publish_date.strftime(
                                '%Y-%m-%d %H:%M:%S')
                        # YYYY-MM-DD格式
                        elif '-' in time_str and len(time_str) == 10:
                            publish_time = f"{time_str} 00:00:00"
                        break
                    except:
                        continue

        news_item = {
            "title": result.title,
            "content": result.snippet or result.title,
            "source": extract_domain(result.link),
            "url": result.link,
            "keyword": symbol,
            "search_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')  # 搜索时间
        }

        # 只有当能提取到发布时间时才添加，否则不包含这个字段
        if publish_time:
            news_item["publish_time"] = publish_time

        news_list.append(news_item)

    return news_list


def convert_tavily_results_to_news_format(results, symbol: str) -> list:
    """将 Tavily 搜索结果转换为新闻格式（兼容后续 dataflow）。"""
    news_list = []
    for r in results or []:
        title = getattr(r, "title", "") or ""
        url = getattr(r, "url", "") or ""
        content = getattr(r, "content", "") or title
        published_date = getattr(r, "published_date", None)

        if not title or not url:
            continue

        news_item = {
            "title": title,
            "content": content,
            "source": extract_domain(url),
            "url": url,
            "keyword": symbol,
            "search_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        if published_date:
            news_item["publish_time"] = published_date
        news_list.append(news_item)

    return news_list


def _normalize_date_str(value: str | None) -> str:
    text = (value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return datetime.now().strftime("%Y-%m-%d")


def _sort_news_items(items: list) -> list:
    def _key(x):
        return x.get("publish_time") or x.get("search_time") or ""

    return sorted(items, key=_key, reverse=True)


cache = AkshareSQLiteCache(NEWS_CACHE_DB_PATH)


def get_stock_news_via_akshare(symbol: str, max_news: int = 10, *, cache_date: str | None = None) -> list:
    """使用缓存增强的 AkShare 新闻接口"""
    try:
        news_df = get_stock_news_akshare_cached(symbol, date=cache_date)
        if news_df is None or news_df.empty:
            print(f"⚠️ 未获取到 {symbol} 的新闻数据")
            return []

        available_news_count = len(news_df)
        if available_news_count < max_news:
            print(f"ℹ️ 提示: 实际可获取的新闻数量({available_news_count})少于目标({max_news})")
            max_news = available_news_count

        news_list = []
        for _, row in news_df.head(int(max_news * 1.5)).iterrows():
            try:
                content = str(row.get("新闻内容", "") or row.get("新闻标题", "")).strip()
                if len(content) < 10:
                    continue

                news_item = {
                    "title": str(row.get("新闻标题", "")).strip(),
                    "content": content,
                    "publish_time": str(row.get("发布时间", "")),
                    "source": str(row.get("新闻来源", "")).strip(),
                    "url": str(row.get("新闻链接", "")).strip(),
                    "keyword": str(row.get("关键词", "")).strip()
                }
                news_list.append(news_item)
            except Exception as err:
                print(f"⚠️ 转换新闻记录时出错: {err}")
                continue

        news_list.sort(key=lambda x: x.get("publish_time", ""), reverse=True)
        return news_list[:max_news]

    except Exception as e:
        print(f"⚠️ akshare 获取新闻数据时出错: {e}")
        return []


def get_stock_news(symbol: str, max_news: int = 10, date: str = None) -> list:
    """获取并处理个股新闻

    Args:
        symbol (str): 股票代码，如 "300059"
        max_news (int, optional): 获取的新闻条数，默认为10条。最大支持100条。
        date (str, optional): 截止日期，格式 "YYYY-MM-DD"，用于限制获取新闻的时间范围，
                             获取该日期及之前的新闻。如果不指定，则使用当前日期。

    Returns:
        list: 新闻列表，每条新闻包含标题、内容、发布时间等信息。
              新闻来源通过智能搜索引擎获取，包含各大财经网站的相关报道。
    """

    # 环境变量控制（全局上限 & Tavily 单次上限）
    # - NEWS_MAX_NEWS: get_stock_news 的全局上限（默认 100）
    # - TAVILY_MAX_NEWS: Tavily 每次最多拉取数量（默认 20）
    try:
        env_news_max = int(os.getenv("NEWS_MAX_NEWS", "100") or "100")
    except ValueError:
        env_news_max = 100
    try:
        tavily_max_news = int(os.getenv("TAVILY_MAX_NEWS", "20") or "20")
    except ValueError:
        tavily_max_news = 20

    env_news_max = max(1, min(env_news_max, 100))
    tavily_max_news = max(1, min(tavily_max_news, 20))

    # 限制最大新闻条数
    max_news = min(max_news, env_news_max)

    cache_date = _normalize_date_str(date)

    # 先查 SQLite 缓存：key=股票+日期（避免跨标的/跨日期误命中）
    cached_records = cache.fetch_records(
        NEWS_CACHE_TABLE,
        filters={"symbol": symbol, "cache_date": cache_date},
        order_by='"publish_time" DESC, "search_time" DESC',
        limit=max_news,
    )
    cached_news = []
    for r in cached_records:
        r = dict(r)
        r.pop("缓存时间", None)
        cached_news.append(
            {
                "title": r.get("title", ""),
                "content": r.get("content", ""),
                "source": r.get("source", ""),
                "url": r.get("url", ""),
                "keyword": symbol,
                "publish_time": r.get("publish_time") or "",
                "search_time": r.get("search_time") or "",
            }
        )

    if len(cached_news) >= max_news:
        print(f"📦 DB 缓存命中: {symbol} {cache_date}（{len(cached_news)} 条）")
        return cached_news[:max_news]

    print(f"🚀 DB 缓存不足: {symbol} {cache_date}（已有 {len(cached_news)} 条，需 {max_news} 条）")

    # 计算需要获取的新闻数量
    need_more_news = max_news - len(cached_news)
    fetch_count = max(need_more_news, max_news)  # 至少获取请求的数量

    # 构建搜索查询（Tavily/Google 共用）
    search_query = build_search_query(symbol, date)

    # 优先：Tavily（需要配置 TAVILY_API_KEY；比直接爬 Google 更稳定）
    new_news_list = []
    fetch_method = None
    if tavily_search:
        try:
            print("🧭 使用 Tavily 搜索获取新闻...")
            print(f"🔍 搜索查询: {search_query}")
            tavily_results = tavily_search(search_query, max_results=min(fetch_count, tavily_max_news))
            new_news_list = convert_tavily_results_to_news_format(tavily_results, symbol)
            if new_news_list:
                fetch_method = "tavily"
                print(f"✅ Tavily 获取 {len(new_news_list)} 条新闻")
            else:
                print("⚠️ Tavily 返回 0 条结果，回退到 Google/akshare")
        except Exception as e:
            print(f"⚠️ Tavily 搜索出错({e})，回退到 Google/akshare")

    # 次优先：Google（Playwright，可能被 /sorry 拦截）
    if not new_news_list and google_search_sync and SearchOptions:
        try:
            print("🌐 使用 Google 搜索获取新闻...")
            print(f"🔍 搜索查询: {search_query}")

            # 执行搜索
            search_options = SearchOptions(
                limit=fetch_count * 2,  # 获取更多结果以便过滤
                timeout=30000,
                locale="zh-CN"
            )

            search_response = google_search_sync(search_query, search_options)

            if search_response.results:
                # 转换搜索结果为新闻格式
                new_news_list = convert_search_results_to_news_format(
                    search_response.results, symbol)

                fetch_method = "google"
                print(f"✅ Google 搜索获取 {len(new_news_list)} 条新闻")
            else:
                print("⚠️ Google 搜索未返回有效结果，回退到 akshare")

        except Exception as e:
            print(f"⚠️ Google 搜索出错({e})，回退到 akshare")

    # 如果 Google 搜索失败，回退到 akshare
    if not new_news_list:
        print("🀄 使用 akshare 获取新闻...")
        new_news_list = get_stock_news_via_akshare(symbol, fetch_count, cache_date=cache_date)
        fetch_method = "akshare"
    # 如果 Tavily 返回不足（例如 max_news>20），允许 AkShare 补齐缺口
    elif fetch_method == "tavily" and len(new_news_list) < need_more_news:
        print(f"🧩 Tavily 仅返回 {len(new_news_list)} 条，尝试用 akshare 补齐缺口…")
        try:
            more = get_stock_news_via_akshare(
                symbol,
                max(need_more_news - len(new_news_list), 0),
                cache_date=cache_date,
            )
            if more:
                existing_titles = {news.get("title", "") for news in new_news_list}
                for item in more:
                    title = item.get("title", "")
                    if title and title not in existing_titles:
                        new_news_list.append(item)
                        existing_titles.add(title)
            fetch_method = "tavily+akshare"
        except Exception as e:
            print(f"⚠️ akshare 补齐失败: {e}")

    # 合并缓存和新获取的新闻，去重（title 维度）
    combined_news = cached_news[:]
    existing_titles = {news.get("title", "") for news in combined_news}
    unique_new_news = []
    for item in new_news_list:
        title = item.get("title", "")
        if not title or title in existing_titles:
            continue
        unique_new_news.append(item)
        existing_titles.add(title)
    combined_news.extend(unique_new_news)
    combined_news = _sort_news_items(combined_news)

    # 写入 DB（只写新增部分即可）
    if unique_new_news:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for n in unique_new_news:
            rows.append(
                {
                    "symbol": symbol,
                    "cache_date": cache_date,
                    "title": n.get("title", ""),
                    "content": n.get("content", ""),
                    "source": n.get("source", ""),
                    "url": n.get("url", ""),
                    "publish_time": n.get("publish_time") or "",
                    "search_time": n.get("search_time") or now_str,
                    "method": fetch_method or "",
                }
            )
        cache.upsert_records(
            NEWS_CACHE_TABLE,
            rows,
            key_columns=["symbol", "cache_date", "title"],
        )
        print(f"💾 DB 写入新闻: {symbol} {cache_date}（新增 {len(rows)} 条，来源={fetch_method}）")

    return combined_news[:max_news]


def _parse_sentiment_score(raw_text: str) -> float:
    """
    Try multiple strategies to extract a numeric score from an LLM response.
    """
    if raw_text is None:
        raise ValueError("LLM returned None when computing sentiment score")

    text = str(raw_text).strip()
    if not text:
        raise ValueError("LLM returned empty response")

    # Direct float string
    try:
        return float(text)
    except ValueError:
        pass

    # JSON payload
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            for key in ("score", "sentiment", "sentiment_score", "value"):
                if key in parsed:
                    return float(parsed[key])
        if isinstance(parsed, list) and parsed:
            return float(parsed[0])
        if isinstance(parsed, (int, float)):
            return float(parsed)
    except Exception:
        # Ignore and fall back to regex parsing
        pass

    match = re.search(r"-?\d+(\.\d+)?", text.replace("%", " "))
    if match:
        return float(match.group())

    raise ValueError(f"Unable to parse numeric sentiment score from response: {text[:160]}")


def get_news_sentiment(
    news_list: list,
    num_of_news: int = 5,
    *,
    symbol: str | None = None,
    cache_date: str | None = None,
) -> float:
    """分析新闻情感得分

    Args:
        news_list (list): 新闻列表
        num_of_news (int): 用于分析的新闻数量，默认为5条

    Returns:
        float: 情感得分，范围[-1, 1]，-1最消极，1最积极
    """
    if not news_list:
        return 0.0

    # # 获取项目根目录
    # project_root = os.path.dirname(os.path.dirname(
    #     os.path.dirname(os.path.abspath(__file__))))

    # 缓存 Key 规则（按“股票 + 日期 + 数量”）：避免用新闻内容做 key
    if not cache_date:
        cache_date = datetime.now().strftime("%Y-%m-%d")
    if symbol:
        cache_key = f"sentiment|{symbol}|{cache_date}|n={int(num_of_news)}"
    else:
        cache_key = f"sentiment|{cache_date}|n={int(num_of_news)}"

    cache = AkshareSQLiteCache(CACHE_PATH)
    cached_rows = cache.fetch_records(
        table="llm_result_cache",
        filters={"cache_key": cache_key},
        limit=1,
    )
    if cached_rows:
        cached_val = cached_rows[0].get("result")
        try:
            return float(cached_val)
        except Exception:
            pass

    # 准备系统消息
    system_message = {
        "role": "system",
        "content": """你是一个专业的A股市场分析师，擅长解读新闻对股票走势的影响。你需要分析一组新闻的情感倾向，并给出一个介于-1到1之间的分数：
        - 1表示极其积极（例如：重大利好消息、超预期业绩、行业政策支持）
        - 0.5到0.9表示积极（例如：业绩增长、新项目落地、获得订单）
        - 0.1到0.4表示轻微积极（例如：小额合同签订、日常经营正常）
        - 0表示中性（例如：日常公告、人事变动、无重大影响的新闻）
        - -0.1到-0.4表示轻微消极（例如：小额诉讼、非核心业务亏损）
        - -0.5到-0.9表示消极（例如：业绩下滑、重要客户流失、行业政策收紧）
        - -1表示极其消极（例如：重大违规、核心业务严重亏损、被监管处罚）

        分析时重点关注：
        1. 业绩相关：财报、业绩预告、营收利润等
        2. 政策影响：行业政策、监管政策、地方政策等
        3. 市场表现：市场份额、竞争态势、商业模式等
        4. 资本运作：并购重组、股权激励、定增配股等
        5. 风险事件：诉讼仲裁、处罚、债务等
        6. 行业地位：技术创新、专利、市占率等
        7. 舆论环境：媒体评价、社会影响等

        请确保分析：
        1. 新闻的真实性和可靠性
        2. 新闻的时效性和影响范围
        3. 对公司基本面的实际影响
        4. A股市场的特殊反应规律"""
    }

    # 准备新闻内容
    news_content = "\n\n".join([
        f"标题：{news.get('title', '未知')}\n"
        f"来源：{news.get('source', '未知')}\n"
        f"时间：{news.get('publish_time', '未知')}\n"
        f"内容：{news.get('content', '')}"
        for news in news_list[:num_of_news]  # 使用指定数量的新闻
    ])

    user_message = {
        "role": "user",
        "content": f"请分析以下A股上市公司相关新闻的情感倾向：\n\n{news_content}\n\n请直接返回一个数字，范围是-1到1，无需解释。"
    }

    try:

        # 获取LLM响应

        result = get_chat_completion([system_message, user_message])

        if result is None:

            print("❌ Error: LLM 返回 None，无法生成情感分数")

            return 0.0



        preview = str(result)

        print(f"🗒️ [sentiment] LLM 原始响应: {preview[:200]}")

        try:

            sentiment_score = _parse_sentiment_score(preview)

        except ValueError as e:

            print(f"⚠️ 解析情感分数失败: {e}")

            return 0.0



        print(f"✅ [sentiment] 解析得分: {sentiment_score}")



        # 确保在-1到1之间

        sentiment_score = max(-1.0, min(1.0, sentiment_score))



        cache.upsert_records(
            table="llm_result_cache",
            records=[
                {
                    "cache_key": cache_key,
                    "cache_type": "sentiment",
                    "result": float(sentiment_score),
                }
            ],
            key_columns=["cache_key"],
        )



        return sentiment_score



    except Exception as e:

        print(f"❌ 情感分析过程中出错: {e}")

        return 0.0  # 发生异常时退回中性评分


