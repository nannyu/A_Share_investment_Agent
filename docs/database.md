# AkShare SQLite 缓存说明

## 概览
- **数据库文件**：`data/akshare_cache.db`，采用 SQLite + WAL 模式，写入由 `src/database/sqlite_cache.py` 统一管理。
- **列结构**：所有表保留 AkShare 原始列，并额外加入 `缓存时间` 字段用于 TTL 判定。
- **写入策略**：所有 upsert 均使用 `ON CONFLICT (...) DO UPDATE`，保证重复拉取只刷新数据而不会生成重复行。

## 数据表结构

| 表名 | 主键 / 索引 | 主要字段示例 | TTL / 刷新策略 | 说明 |
| --- | --- | --- | --- | --- |
| `stock_zh_a_spot_em` | `代码` | `名称`、`最新价`、`涨跌幅`、`成交量`、`市盈率-动态`、`总市值`、`流通市值`、`52周最高/最低`、`缓存时间` | 默认 10 分钟 | 实时行情，供 `market_data_agent`、`valuation_agent`、`risk_management_agent` 等读取；若 AkShare 多次失败则回退到零值 |
| `stock_financial_analysis_indicator` | `代码` + `日期` | ROE、净利率、营收/利润增长率、EPS、现金流指标等全部列 | 默认 24 小时 | 财务指标快照，供基本面 / 估值模块使用 |
| `stock_financial_report_sina` | `代码` + `报表类型` + `报告日` | 资产负债表、利润表、现金流量表的全部项目 | 默认 7 天 | `报表类型` ∈ {`资产负债表`, `利润表`, `现金流量表`}，每次 miss 都会刷新整张报表 |
| `stock_zh_a_hist` | `代码` + `复权类型` + `日期` | `收盘`、`开盘`、`最高`、`最低`、`成交量`、`成交额`、`振幅`、`涨跌幅`、`涨跌额`、`换手率`、`缓存时间` | 无 TTL，按需补数 | 技术面 / 回测模块的日线数据：系统会对 `[start_date, end_date]`（CLI 默认为“昨天”向前 1 年）先检查缓存，仅对缺失的连续日期段调用 AkShare，再写回 SQLite，实现真正的“按日增量” |
| `stock_news_em` | `关键词` + `发布时间` + `新闻标题` | `新闻内容`、`新闻来源`、`新闻链接`、`缓存时间` | 默认 2 小时 | 新闻兜底数据；Google 搜索失败或数量不足时回退到该表 |

## 数据流（Dataflow）
1. **时间区间确定**  
   - `src/main.py` 未传 `--start-date/--end-date` 时，自动设定 `end_date=昨天`、`start_date=end_date-365 天`；支持用户自定义区间。
2. **缓存优先**  
   - `src/tools/api.py` 中的每个 `get_*` 函数首先查询 SQLite（含 TTL）；命中即返回，未命中则进入网络请求。
3. **代理轮询与重试**  
   - `src/network/proxy_manager.py` 会根据 `.env` 中 `AKSHARE_PROXY_LIST`（支持逗号/分号分隔，`direct` 表示直连）、`AKSHARE_PROXY_MAX_ATTEMPTS`、`AKSHARE_PROXY_BASE_DELAY` 等参数，进行代理池轮询、指数退避和随机抖动，多次尝试 AkShare 请求。
4. **缓存回写**  
   - 请求成功后立即 upsert；其中 `stock_zh_a_hist` 会对缺失区间分段拉取并写入，只抓尚未存在的日期，避免整年重复下载。
5. **Agent 消费**  
   - 技术面、基本面、估值、情绪、宏观等 Agent 统一通过缓存读取数据，再交由 LLM 模块生成信号并汇总为最终决策。

## 维护建议
- **刷新数据**：可删除某只股票在 SQLite 中的行或直接删除 `akshare_cache.db`，下次运行会自动重建并重新拉取。
- **列变更**：如 AkShare 新增列，`AkshareSQLiteCache` 会自动 `ALTER TABLE ADD COLUMN`，无需手动迁移。
- **监控**：关注 `缓存时间` 可判断 TTL 是否合理；必要时在 `.env` 或 `akshare_cache` 中调整 TTL 配置。
- **默认区间与增量**：CLI 默认区间为“昨天向前一年”，结合 `stock_zh_a_hist` 的增量逻辑意味着缓存会随每日运行自动补齐缺失的日线数据；而财务指标 / 报表则依赖 TTL 周期性刷新。
