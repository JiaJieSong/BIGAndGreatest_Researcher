# Researcher

简单的 Research Agent

## 使用

```bash
# 1. 填 key
cp .env.example .env   # 填入 AI_KEY 和  RESEARCH_API_KEY

# 2. 运行
python research.py
```

交互式输入问题，输入 `exit` 退出。

## 配置

`AI_BASE_URL`
`AI_API_KEY`
`AI_MODEL`

` RESEARCH_BASE_URL`
` RESEARCH_API_KEY`

## 文件

- `research.py` — 全部代码（LLM 调用、搜索、主流程）
- `.env.example` — 配置模板



