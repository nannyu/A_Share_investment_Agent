请分析以下A股上市公司相关新闻的情感倾向：

{news_content}

请严格按以下JSON格式返回结果（只输出JSON，不要添加```代码块或其他文字）：
{{
  "score": "<根据新闻分析，-1到1之间>",
  "signal": "<bullish/bearish/neutral>",
  "confidence": "<根据信息充分程度，0到1之间>",
  "reasoning": "<简短的中文分析说明>"
}}

字段说明：

- score: -1到1之间的浮点数，-1表示极其消极，1表示极其积极，0表示中性
- signal: "bullish"(看多，score>0.3)、"bearish"(看空，score<-0.3)、"neutral"(中性)
- confidence: 0到1之间的浮点数，表示判断的置信度
- reasoning: 基于新闻内容的简短中文分析（1-2句话）
