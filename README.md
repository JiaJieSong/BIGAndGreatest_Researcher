# Researcher

Research Agent

文档按版本组织：每个版本一个独立章节，按 v1 → v5 的演进顺序排列；
以后新增 v6 时，按文末「如何新增一个版本」的三步操作即可。

## 版本一览

research_v1 : 用户问题 → LLM → Search API → 得到 5 个 URL → Extract → 得到网页正文 → LLM → 答案
research_v2：在v1基础上加Evidence Extraction。Extract/Clean Article → Evidence Extraction → Evidence List →  LLM → 答案
research_v3：在v2基础上加Planner + Synthesis。用户问题 → Planner(拆子问题) → 每个子问题独立 Research(Search → Extract → Evidence → Summary) → Synthesis(综合各子问题结果回答原问题)
research_v4：在v3基础上加 Research State + Gap Analyzer + Replanner，从「一次性执行研究计划」进化为「根据研究结果动态调整计划」：Planner 初始计划 → Research Loop(更新 Research State) → Gap Analysis(发现证据缺口/矛盾) → 继续则生成 Follow-up Questions 再研究，否则 Synthesis → Citation Validation → Final Report（已实现）
research_v5：在v4基础上加 Citation System（引用追踪链）。Research Loop 中页面正文注册进 Source Registry、证据经 Evidence Registry 登记（quote 对正文验证 + 关联 source_id）→ Synthesis 输出结构化结论（claims JSON，程序校验）→ Citation Validator 五级检查（ID/URL/Quote/语义/冲突）→ Citation Renderer 程序渲染最终报告，Claim → Evidence → Source → 原文全链可追溯（已实现）
research_v6：在v5基础上加 LangGraph 工作流编排。保留 V5 全部研究能力与引用系统，把 V4 的手动 Research Loop 重构为 LangGraph StateGraph 驱动的工作流：START → plan_node → research_round_node → gap_analysis_node →（条件路由）→ 继续则 replan_node 回环 / 停止则 finalize_node → END（已实现）
research_v7：在v6基础上加 Multi-Agent。V5 业务核心 + V6 LangGraph 编排升级为多角色协作：Planner → Chief Researcher（并行派发 Researcher ×N）→ Analyst（reducer 汇合）→（研究循环）→ Reviewer（质检打回）→ Writer → Publisher（md/pdf/docx 三产物）（已实现）

## 配置

`AI_BASE_URL`
`AI_API_KEY`
`AI_MODEL`

` RESEARCH_BASE_URL`
` RESEARCH_API_KEY`

各版本共用同一套配置，写在一个 `.env` 里即可（模板见 `.env.example`）。

## 文件

- `research_v1.py` / `research_v2.py` / `research_v3.py` / `research_v4.py` / `research_v5.py` — 各版本全部代码，互相独立、单文件即可运行
- `.env.example` — 配置模板
- `_test_v3_planner.py` — Planner 单元测试（mock LLM，含 fallback 不变式扫描 + 折线图）
- `_test_v3_dryrun.py` — V3 端到端离线 dry-run（mock LLM/搜索/抓取，不联网）
- `_test_v4_state.py` — V4 单元测试：ResearchState / GapAnalyzer / Replanner（mock LLM，不联网）
- `_test_v4_dryrun.py` — V4 端到端离线 dry-run（mock LLM/搜索/抓取，不联网）
- `_test_v5_citation.py` — V5 Phase G 验收测试：Citation System 12 项覆盖（mock LLM/搜索/抓取，离线可重复）
- `research_v6.py` — V6：导入 V5 全部业务函数，LangGraph StateGraph 工作流编排（依赖 `langgraph`，见 `requirements-v6.txt`）
- `_test_v6_workflow.py` — V6 验收测试：工作流编译/完整运行/全部停止条件/跨轮注册表一致性/节点重复执行不重复登记/渲染回归（mock 外部调用，离线可重复）
- `research_v7/` — V7 Multi-Agent 包：`state.py`（V7State + reducer + 新配置）/ `agents.py`（7 角色节点）/ `graph.py`（构图 + run_research）/ `main.py`（CLI REPL，`python -m research_v7`）；依赖见 `requirements-v7.txt`
- `_test_v7_phase2.py` / `_test_v7_phase4.py` / `_test_v7_phase5.py` — V7 分阶段验收测试（mock 外部调用，离线可重复）
- `v7_DESIGN.md` — V7 Phase 1 设计说明（状态表 / 节点 callsite / 边路由 / 并发安全 / 疑问清单）

## v1 基础管线

### 流程

```
用户问题 → LLM 生成搜索关键词 → Search API 搜索
        → URL 去重、取前 5 条 → Extract 逐个抓网页正文 → LLM 生成答案
```

### 要点

- 交互式命令行：`Q>` 后输入问题，`exit` 退出
- 搜索、抓取、LLM 调用全部用标准库 `urllib` 实现，无第三方依赖
- Extract 有兜底：URL 含中文/特殊字符时先百分号编码再重试，
  仍失败则跳过该页，LLM 退回用搜索摘要作答
- 运行：`python research_v1.py`

## v2 加 Evidence Extraction

在 v1 基础上，把「整页正文直接塞给 LLM」改为「先抽取证据，再只基于证据作答」。

### 流程

```
User Question → Search → Fetch Web Pages → Extract/Clean Article
             → Evidence Extraction → Evidence List
             → LLM Final Answer（带 [n] 引用）
```

### 要点

- 每个网页单独抽取证据，统一编号（`EV-001` …），并记录来源标题与 URL
- 证据条数与长度受限，防止 prompt 过长：单页正文截断 `EXTRACT_MAX_CHARS=6000` 字符、
  单页最多 `EVIDENCE_MAX_PER_PAGE=5` 条、单条最多 `EVIDENCE_MAX_TEXT=400` 字符
- 终端会打印 Evidence 清单；最终答案的 `[n]` 引用可追溯到来源
- 运行：`python research_v2.py`

## v3 加 Planner + Synthesis

### 内部流程

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



## v4 加 Research State + Gap Analyzer + Replanner

核心变化一句话：V3 是「一次性执行研究计划」；V4 除了预先规划，还能在研究过程中
发现证据缺口、生成新的研究问题、继续搜索，并判断是否需要再研究一轮 ——
也就是说，V4 要让系统从「一次性执行研究计划」进化为「根据研究结果动态调整计划」。

`research_v4.py` 已实现：从 v3 复制改造，每个子问题的研究管线（Search →
Extract → Evidence → Summary）直接复用 V3，未复制一套 Search/Extract/Evidence 代码。

### 推荐流程

```
                Original Question
                        ↓
         保存研究目标 / 范围 / 限制条件
                        ↓
                     Planner
            （拆子问题，生成初始研究计划）
                        ↓
              Research Loop · 第 N 轮
     选择待研究问题 → Search → Extract
           → Evidence Extraction → Summary
                        ↓
              Update Research State
     （合并证据、记录来源、更新问题状态）
                        ↓
                Gap Analysis
   证据是否充分？是否存在矛盾？还有什么未知？
                        ↓
          Decision · 是否继续研究？
        ├── 继续 → 生成 Follow-up Questions
        │           回到 Research Loop（第 N+1 轮）
        └── 结束 → Synthesis
                    → Citation Validation
                    → Final Report
```

- 与 V3 的区别：V3 中 Planner → Research → Synthesis 只走一遍；
  V4 在 Research 与 Synthesis 之间插入了「更新状态 → 缺口分析 → 是否继续」的循环，
  「继续」分支生成的 Follow-up Questions 会回到 Research Loop 顶部进入下一轮。

### 三个新增核心模块

刻意只加这三个模块，避免把项目做得太复杂。

1. **Research State（研究状态管理）** — 记录整个研究过程，而不是每一轮都从头开始：

   - 原始问题与初始计划
   - 已完成和待研究的子问题
   - 全部 Evidence 与来源
   - 已执行的搜索查询
   - 当前发现的证据缺口与矛盾
   - 当前研究轮次

2. **Gap Analyzer（研究缺口分析器）** — 每一轮结束后，根据当前证据判断：

   - 哪个子问题还没有得到回答？
   - 哪个结论只有单一来源支持？
   - 不同来源是否存在冲突？
   - 哪些重要结论仍然缺少直接证据？
   - 下一轮最值得研究什么？

3. **Replanner（动态再规划器）** — 根据 Gap Analyzer 的结果生成新的研究任务，
   或调整原有任务：

   - 发现某个结论缺少数据 → 追加「寻找原始统计数据」
   - 发现来源互相矛盾 → 追加「核实两个来源的数据口径」

### 设计约束

- 每个子问题的研究管线（Search → Extract → Evidence → Summary）复用 V3，
  V4 只在外层加循环，不重写单链逻辑
- Research Loop 有轮次上限（计划 `MAX_ROUNDS`），达到上限强制进入 Synthesis，
  防止无限循环
- 每轮新增的 Follow-up 数量有限制；缺口较多时按「对回答主问题的影响」排优先级
- Synthesis 拿到的是跨轮次合并后的 Research State（各子问题小结 + 全部证据），
  引用与来源追溯规则沿用 V3

### 实现要点

- `ResearchState`：统一研究状态（原始问题、计划、pending/completed 问题、
  证据、来源、搜索历史、缺口、冲突、轮次、上限、状态/停止原因），
  `add_evidence` 按 (source_url, claim) 全局去重、只增不减
- `analyze_gaps`：输出 gaps / conflicts / follow_up_questions / is_sufficient /
  reasoning_summary；解析失败重试一次，仍失败按「无新缺口」兜底，不拖垮研究
- `replan`：纯逻辑不调 LLM —— 问题查重（规范化文本比对）、查询查重
  （suggested_queries 全部与搜索历史重复则丢弃）、每轮上限
  `MAX_FOLLOWUPS_PER_ROUND`、按 priority 排序、ID 形如 `r1f1`
  （证据编号 `EV-r1f1-001`，跨轮不撞号）
- 停止条件：`sufficient` / `no_new_evidence` / `max_rounds` / `no_valid_followups`；
  停止时若仍有未解决缺口，最终答案以「未解决的不确定性」小节如实披露
- 引用机制沿用 V3（`[EV-xxx]` → `[n]`、同源同号、非法 ID 抹除），
  新增 `validate_citations` 在转编号前输出引用质量指标


### 测试

```bash
python _test_v4_state.py     # 单元测试：ResearchState / GapAnalyzer / Replanner
python _test_v4_dryrun.py    # 端到端 dry-run（mock 全部外部调用，不联网）
```

## v5 加 Citation System（引用追踪链）

核心变化一句话：V4 的引用（`[EV-xxx]` → `[n]`）只保证「编号能对上证据」；
V5 在 V4 基础上构建完整的 Citation System，让最终报告中的每条结论都能沿
**Claim → Evidence → Source → 原文 / URL** 逐级追溯 —— 全部 ID 由程序分配，
不信任 LLM 自己编的编号、URL 和原文片段。

`research_v5.py` 已实现：从 v4 复制改造，Planner / Research Loop /
Gap Analyzer / Replanner / 源质量过滤全部沿用 V4；子问题研究管线在传入
ResearchState 时走 V5 登记路径（不传 state 仍保持 V4 兼容行为），不复制一套
Search/Extract/Evidence 代码。

### 推荐流程

```text
                Original Question
                        ↓
                [1/3] Understanding Question
                        ↓
                      Planner
             （拆子问题，生成初始研究计划）
                        ↓
               Research Loop · 第 N 轮
      选择待研究问题 → Search → Extract
            → Evidence Extraction → Summary
                        ↓
        Source Registry 登记页面正文（SRC-xxx）
        
      （canonical_url 去重 / 保存正文版本 page_versions）
                        ↓
      Evidence Registry 登记证据（EV-xxx，全局唯一）
      （quote 对保存正文自动验证 + locator 段落定位
        + 强制关联 source_id；同源同论断去重复用 ID）
                        ↓
               Update Research State
      （证据跨轮累积，只增不减）
                        ↓
                 Gap Analysis
    证据是否充分？是否存在矛盾？还有什么未知？
                        ↓
           Decision · 是否继续研究？
         ├── 继续 → 生成 Follow-up Questions
         │           回到 Research Loop（第 N+1 轮）
         └── 结束 → Synthesis
                     （answer_markdown + claims JSON，
                       validate_claims 程序校验结论）
                     → Citation Validator 五级检查
                     → Citation Renderer 程序渲染
                     → Final Report
```

结束阶段的三个步骤：

1. **Synthesis 结构化结论（Phase D）**：LLM 输出最终答案 markdown + claims
   JSON（claim_id 程序分配）；`validate_claims` 程序校验 —— evidence_ids 只认
   真实存在的证据、小结不充当证据、无证据的数字/因果结论强制 unsupported
2. **Citation Validator 五级校验（Phase E）**：每条结论依次检查
   ① ID（证据/来源/版本存在性）② URL（可解析，可达性可选）
   ③ Quote（对注册表保存的正文复验 + content_hash 一致）
   ④ Semantic（LLM 结构化判定，程序过滤 ID 与合并裁决）
   ⑤ Conflict（与 GapAnalyzer 发现的冲突取交集）；
   关键结论（含数字/因果要素，最多 `SECOND_PASS_MAX_CLAIMS` 条）独立二次核验，
   两次不一致标 `needs_review` 交人工复核
3. **Citation Renderer 程序渲染（Phase F）**：`[EV-xxx]` → `[n]` 同源同号、
   References 每来源一条且只列被引来源（元数据只取注册表，Markdown 可点击链接）、
   结论与证据追溯表、引用质量说明显式呈现；无法映射到来源的引用被抹掉并计数，
   不会泄漏到用户面前

### 五个新增核心模块

对应任务书 Phase B~F，刻意只加这五个模块，外层研究循环不动。

1. **Source Registry（来源注册表，Phase B）** — 来源的唯一权威记录：

   - `source_id` 程序分配（`SRC-001` …），canonical_url 去重（删 `utm_` 跟踪参数）
   - 页面正文按版本保存（`page_versions` + content_hash），供 quote 复验使用
   - 注册表元数据是 References 的唯一数据源，LLM 无法篡改来源标题/URL

2. **Evidence Registry（证据注册表，Phase C）** — 证据的全局登记处：

   - `evidence_id` 全局分配（`EV-001` …，不再按子问题加前缀）
   - quote 对保存的页面正文自动验证，未命中不入库（locator 记录段落位置）
   - 强制关联 `source_id`：没有来源的证据一律不收
   - 跨轮累积、同源同论断去重复用同一 ID；不同来源的相同论断各自保留

3. **Claim–Evidence Mapping（结论映射，Phase D）** — Synthesis 不再只给一段文字：

   - 输出 `answer_markdown + claims JSON`，`claim_id`（`CLM-001` …）程序分配
   - `validate_claims` 程序校验：evidence_ids 必须真实存在；小结不充当证据；
     无证据支撑的数字/因果结论强制标 `unsupported`，不强行生成结论

4. **Citation Validator（引用校验器，Phase E）** — 五级检查见上「推荐流程」第 2 步；
   输出 pass / warn / fail / needs_review 统计，失败项在报告与 Run Summary 中
   显式呈现，不静默忽略

5. **Citation Renderer（引用渲染器，Phase F）** — 最终报告由程序统一渲染，
   见上「推荐流程」第 3 步；LLM 只负责生成结论文本，编号、References、
   追溯表全部由程序拼装

### 设计约束

- 外层 Research Loop（轮次上限、Follow-up 限制、停止条件、缺口披露）沿用 V4，
  V5 只把「证据进状态」的路径换成两个注册表，不改循环逻辑
- 所有 ID 由程序分配；LLM 输出的 evidence_id / source_id / claim_id 一律不采信，
  只用程序登记时返回的 ID
- quote 验证只信注册表里保存的正文（content_hash 防正文被换），
  URL 可达性检查默认关闭（`URL_REACHABILITY_CHECK = False`，离线优先）
- 语义校验走 LLM（`SEMANTIC_CHECK_ENABLED`），关闭时一律标 `unverified`，
  不冒充已验证
- 校验失败不阻断研究：渲染时抹掉无法追溯的引用并计数，结论如实降级
  （unsupported / needs_review），最终在质量说明中披露


### 测试

```bash
python _test_v5_citation.py   # Phase G 验收测试：12 项（mock 全部外部调用，离线可重复）
```

12 项覆盖：合法链生成引用 / 不存在的 Evidence ID、Source ID 被拒 / 虚构原文被标记 /
语义不符被标记 / 首轮证据进最终报告 / 多结论共享来源 / 重复来源不重复列 /
跨来源矛盾不被覆盖 / 编号全部映射 References / 校验失败不静默 / 搜索抓取失败不虚构引用。

## v6 加 LangGraph StateGraph 工作流编排

核心变化一句话：V5 的 Research Loop 是手写 `while` 循环；V6 把「研究 → 缺口分析 →
重规划 → 继续/停止」的循环控制交给 LangGraph StateGraph，节点与边显式表达工作流，
循环由条件路由驱动，停止条件仍是 V5 那套规则 —— 不重写任何研究/引用功能。

`research_v6.py` 已实现：导入 `research_v5` 的全部业务函数（Planner / Research /
Gap Analyzer / Replanner / Synthesis / Citation Validator / Citation Renderer /
两个注册表），不复制一套实现。

### 推荐流程

```text
                Original Question
                        ↓
              [1/3] Understanding Question
                        ↓
              START → plan_node（Planner + 创建 ResearchState）
                        ↓
              research_round_node（单轮串行研究 pending 问题）
              （Search → Extract → Evidence → Source Registry
                → Evidence Registry → Update Research State）
                        ↓
              gap_analysis_node（analyze_gaps → 缺口 / 冲突 / 充分性）
                        ↓
              route_research（条件路由，add_conditional_edges）
              ├─ 继续 → replan_node（生成 Follow-up 入队）
              │        → route_after_replan
              │           ├─ 有 Follow-up → 回到 research_round_node（第 N+1 轮）
              │           └─ 无 Follow-up（no_valid_followups）→ finalize_node
              └─ 停止（sufficient / no_new_evidence / max_rounds）
                        → finalize_node → END
```

### 五个 Graph 节点

1. **plan_node**：接收原始问题，调用 V5 `plan_research()`，创建 `ResearchState`（含
   两个注册表），初始化运行级统计（filter_stats 等）
2. **research_round_node**：读取 `ResearchState`，串行研究本轮 pending 问题（调用
   V5 `research_sub_question`），更新搜索历史 / 已完成问题 / 证据 / 轮次 / 缺口；
   记录本轮新增证据数；单子问题失败沿用 V5 容错
3. **gap_analysis_node**：调用 V5 `analyze_gaps()`，更新缺口/冲突/充分性；GapReport
   存入 graph runtime 供路由只读
4. **replan_node**：调用 V5 `replan()`（已直接入队 Follow-up，不重复添加）；无有效
   产出时 `stop("no_valid_followups")`
5. **finalize_node**：V5 Synthesis（含 Claim–Evidence Mapping）→ Citation Validator
   （五级校验 + 二次核验）→ Citation Renderer（编号/追溯表/References/质量说明）

### 停止条件（唯一来源 = V5 规则）

`route_research` 调用 V5 `_stop_reason()`（sufficient / no_new_evidence / max_rounds）；
`replan_node` 无有效 Follow-up 时停止（no_valid_followups）。LangGraph 另有
`recursion_limit=500` 作为图级安全网（V5 的 `MAX_ROUNDS=3` 才是业务上限）。

### 状态模型要点

- Graph State（TypedDict）含 `original_question` / `state`（ResearchState 实例）/
  `runtime`（filter_stats、每轮新增证据等运行级数据）/ `final_answer` / `error`
- 全图只有**一个** `ResearchState` 实例（含 SourceRegistry / EvidenceRegistry），
  节点原地修改后返回完整图状态 —— 不复制注册表、不在节点里重建状态，所以
  source_id / evidence_id 跨轮连续分配、证据只增不减
- 节点返回整份 graph state（LangGraph 1.x 中节点返回 dict 是**全量替换**而非合并，
  返回整份状态才能保证字段跨节点累积）
- 当前图状态**不可直接序列化**（`state` 是带注册表/正文版本的 ResearchState 对象，
  runtime 含 GapReport）。本阶段是内存工作流；未来引入 Checkpointer 持久化时，
  需要为 ResearchState 设计序列化方案（正文版本可存盘、LLM 输出缓存等），
  **目前未实现任何持久化能力**。


### 测试

```bash
python _test_v5_citation.py   # V5 回归（应全部通过）
python _test_v6_workflow.py   # V6 工作流：编译/完整流程/5 种停止条件/注册表一致性/渲染回归
```

## v7 加 Multi-Agent（并行 Researcher + Reviewer 质检）

核心变化一句话：V6 的 LangGraph 编排是**单线程**（研究循环内逐题串行）；
V7 引入多 Agent 角色协作 —— Chief Researcher 并行派发多个 Researcher，
Analyst 汇合结果，Reviewer 质检打回，Writer / Publisher 产出多格式报告。
V5 业务核心（研究 / 引用 / 渲染）零重写，只在两个注册表上加锁支持并发。

### 推荐流程

```text
                Original Question
                        ↓
              START → planner_node（Planner + 创建 ResearchState）
                        ↓
              chief_researcher_node（唯一决策点）
              ├─ 有任务 → Command(goto=[Send×k]) 并行派发
              │        → researcher_node ×k（[R1]..[Rk]，各带局部 stats）
              │        →（正常边 = super-step barrier）→ analyst_node
              │          （reducer 汇合，串行重放 complete/add_evidence/
              │           record_searches/resolve_gap → gap 分析 + replan）
              │        → 回 chief（下一波 / 停止判定）
              └─ 研究结束 → reviewer_node
                          ├─ pass → writer_node
                          ├─ fail 且打回 ≤ MAX_REVIEW_CYCLES → chief（整改回环）
                          └─ fail 且超上限 → writer（问题清单如实披露）
                                    ↓
                     writer_node（Synthesis → 五级校验 → Render）
                                    ↓
                     publisher_node（output/<slug>-<ts>/: md / pdf / docx）
                                    ↓
                                  END
```

### 七个角色节点

1. **planner_node**：V5 `plan_research()` → 创建唯一 ResearchState（含注册表）
2. **chief_researcher_node**：唯一决策点 —— V5 `_stop_reason()` 语义；
   弹出 ≤`MAX_PARALLEL_RESEARCHERS` 个任务用 `Command(goto=[Send×k])` 并行派发；
   Reviewer 打回的 evidence 类 issue 转 `source="reviewer"` 任务重新研究
3. **researcher_node ×N（并行）**：V5 `research_sub_question()` 原样调用，
   只写 reducer 字段 `researcher_reports`；日志前缀 `[R1]..[Rk]` 交错可见
4. **analyst_node（串行）**：reducer 汇合后重放 `complete_question` /
   `add_evidence` / `record_searches` / `resolve_gap` → gap 分析 + replan
5. **reviewer_node**：新增评审 prompt（材料 = 子问题完成度/证据/缺口/冲突），
   输出 JSON `{verdict: pass|fail, issues: [{type, description}]}`；
   evidence 类 issue 触发整改回环，clarity/conflict 类只披露
6. **writer_node**：V5 `synthesize_from_state()` → `validate_citation_chain()` →
   `render_report()`；review_notes 追加为「评审问题清单」
7. **publisher_node**：写 `output/<slug>-<yyyymmdd-hhmm>/` 下
   `report.md` / `report.pdf` / `report.docx`（PDF 用 reportlab
   `UnicodeCIDFont("STSong-Light")`，DOCX 用 python-docx）

### 并发安全

- 并行 Researcher 各自局部 `stats`，绝不同享 filter_stats dict
- 两个注册表加 `threading.Lock()`（v5 唯一改动）：`SourceRegistry.register` 与
  `EvidenceRegistry.register` 用 `with self._lock:` 包裹整个注册（counter 自增 +
  dict 读改写原子化）；`register_from_llm` 不经锁（其全部写路径走 register，
  避免非重入锁自死锁）—— 串行调用行为零变化
- `researcher_reports` 用 `Annotated[list, operator.add]` reducer 合并；
  消费后以 `Overwrite([])` 重建（`[]` 直接赋值会被 operator.add 当累加项）
- 并行汇合由正常边 `researcher_node → analyst_node` 保证（super-step barrier），
  无需额外同步节点

### 新配置

`MAX_PARALLEL_RESEARCHERS=3`（每波并行 Researcher 上限）、
`MAX_REVIEW_CYCLES=2`（Reviewer 打回上限，超过强制进 Writer 如实披露）、
`OUTPUT_DIR="output"`（输出根目录）。

### 图级安全网

`recursion_limit=500`（沿用 V6）；研究循环上限 `v5.MAX_ROUNDS=3`；
Review 打回上限 `MAX_REVIEW_CYCLES=2` —— 三保险防无限循环。

### 测试

```bash
python _test_v5_citation.py     # V5 回归（应全部通过）
python _test_v6_workflow.py     # V6 回归（应全部通过）
python _test_v7_phase2.py       # V7 串行骨架：等价 V6 + 三产物
python _test_v7_phase4.py       # V7 Reviewer：pass/整改/超上限披露/非evidence只披露
python _test_v7_phase5.py       # V7 端到端验收：REPL/并行日志/三产物/引用回归/配置
```

## 如何新增一个版本

1. 「版本一览」加一行：在 v(X-1) 基础上加什么、流程链是什么、状态（已实现 / 规划中）
2. 新增 `## vX` 章节：定位一句话 → 流程图 → 新增点（要点列表）→ 运行 / 测试
3. 「文件」清单加上对应文件（如 `research_v5.py` 及配套 `_test_v5_*.py`）



