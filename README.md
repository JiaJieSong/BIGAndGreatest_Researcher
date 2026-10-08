# Researcher

Research Agent
版本：
research_v1 : 用户问题 → LLM → Search API → 得到 5 个 URL → Extract → 得到网页正文 → LLM → 答案
research_v2：在v1基础上加Evidence Extraction。Extract/Clean Article → Evidence Extraction → Evidence List →  LLM → 答案
research_v3：在v2基础上加Planner + Synthesis。用户问题 → Planner(拆子问题) → 每个子问题独立 Research(Search → Extract → Evidence → Summary) → Synthesis(综合各子问题结果回答原问题)
research_v4          
## 配置

`AI_BASE_URL`
`AI_API_KEY`
`AI_MODEL`

` RESEARCH_BASE_URL`
` RESEARCH_API_KEY`

## 文件

- `research_v3.py` — V3 全部代码（Planner、子问题 Research、Synthesis、LLM 调用、搜索）
- `research_v2.py` / `research_v1.py` — 旧版本
- `.env.example` — 配置模板
- `_test_v3_planner.py` — Planner 单元测试（mock LLM，含 fallback 不变式扫描 + 折线图）
- `_test_v3_dryrun.py` — 端到端离线 dry-run（mock LLM/搜索/抓取，不联网）

## V3 内部流程

```
                Original Question
                        ↓
                     Planner
                        ↓
          ┌─────────────┼─────────────┐
          ↓             ↓             ↓
       Q1 Research   Q2 Research   Q3 Research
          ↓             ↓             ↓
       Result 1      Result 2      Result 3
          └─────────────┼─────────────┘
                        ↓
                    Synthesis
                        ↓
                  Final Answer
```

- Planner 把原问题拆成 3~7 个可独立搜索的子问题（问题过简时 1~3 个）
- Planner 输出异常时有兜底：子问题数始终 >= `SUB_QUESTION_MIN`，id 保证唯一
- 每个子问题独立跑 v2 管线：Search → Filter → Extract → Evidence → Summary
- 单个子问题失败不会拖垮整个任务，Synthesis 会如实说明该方向暂无结果
- 当前为串行实现，保证正确性优先

### 引用（可追溯性）

- 子问题证据的 ID 带前缀（`EV-q1-001`），跨子问题汇总时不撞号
- Synthesis 拿到「各子问题小结 + 全部证据」，答案是带引用的，末尾附引用列表
- 同一来源只占一个编号（按 URL 合并），多个 ID 写在同一括号里也能正确展开
- 无法解析的内部 ID 会被抹掉并计数，不会泄漏到用户面前

### 源质量过滤

- `SEARCH_DEPTH = "advanced"`，并在入库前过滤：黑名单域名（SERP/跳转页）、
  空壳页（正文过短）、低相关度（`score` 过低）
- 过滤后若一条不剩会退回未拉黑结果，绝不清空
- 过滤条数按子问题累加，在 `[3/4]` 末尾汇总成一行

## 运行

```bash
python research_v3.py
```

终端默认只输出每条研究链的一行进度；想看到每个网页的抓取/证据明细，
把 `VERBOSE` 改成 `True`。

## 测试

```bash
python _test_v3_planner.py    # Planner 单测（mock LLM，不联网）
python _test_v3_dryrun.py     # 端到端 dry-run（mock 全部外部调用，不联网）
```



