请分析以下新闻，评估当前宏观经济环境及其对相关A股上市公司的影响：

{news_content}

请严格按以下JSON格式返回结果（只输出JSON，不要添加```代码块或其他文字）：
{{
  "macro_environment": "<bullish/bearish/neutral>",
  "impact_on_stock": "<bullish/bearish/neutral>",
  "key_factors": ["<因素1>", "<因素2>", "<因素3>"],
  "reasoning": "<详细的中文分析推理>",
  "signal": "<bullish/bearish/neutral>",
  "confidence": "<根据分析确定性，0到1之间>"
}}

字段说明：

- macro_environment: 宏观环境评估，bullish表示积极，bearish表示消极，neutral表示中性
- impact_on_stock: 对目标股票的影响，与macro_environment使用相同的值域
- signal: 综合信号，应与impact_on_stock保持一致
- confidence: 0到1之间的浮点数，表示判断置信度
- key_factors: 3-5个关键影响因素
- reasoning: 完整的中文分析推理
