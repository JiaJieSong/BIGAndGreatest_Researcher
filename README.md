# Researcher

最简单的 Research Agent，零第三方依赖（纯 Python 标准库）。

```
用户问题
  ↓ LLM 生成搜索关键词
  ↓ Tavily Search API
  ↓ LLM 根据搜索结果总结答案
```

无 LangGraph、无 RAG、无数据库、无前端、无 Multi-Agent。

## 使用

```bash
# 1. 填 key
cp .env.example .env   # 填入 DEEPSEEK_API_KEY 和 TAVILY_API_KEY

# 2. 运行
python research.py
```

交互式输入问题，输入 `exit` 退出。

## 配置

`AI_BASE_URL`
`AI_API_KEY`
`AI_MODEL`

`TAVILY_BASE_URL`
`TAVILY_API_KEY`

## 文件

- `research.py` — 全部代码（LLM 调用、搜索、主流程）
- `.env.example` — 配置模板

## 运作方式

1. **生成关键词**：问题交给 LLM，返回 3 个 JSON 数组形式的搜索关键词。
2. **搜索**：每个关键词调一次 Tavily `/search`，按 URL 去重。
3. **总结**：把搜索结果（标题 + URL + 内容片段）拼进提示词，LLM 输出带来源标注的答案。