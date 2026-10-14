# Researcher

Research Agent

文档按版本组织：每个版本一个独立章节，按 v1 → v5 的演进顺序排列；
以后新增 v6 时，按文末「如何新增一个版本」的三步操作即可。

## 版本一览

research_v1 : 用户问题 → LLM → Search API → 得到 5 个 URL → Extract → 得到网页正文 → LLM → 答案
research_v2：在v1基础上加Evidence Extraction。Extract/Clean Article → Evidence Extraction → Evidence List →  LLM → 答案
research_v3：在v2基础上加Planner + Synthesis。用户问题 → Planner(拆子问题) → 每个子问题独立 Research(Search → Extract → Evidence → Summary) → Synthesis(综合各子问题结果回答原问题)
research_v4：在v3基础上加 Research State + Gap Analyzer + Replanner，从「一次性执行研究计划」进化为「根据研究结果动态调整计划」：Planner 初始计划 → Research Loop(更新 Research State) → Gap Analysis(发现证据缺口/矛盾) → 继续则生成 Follow-up Questions 再研究，否则 Synthesis → Citation Validation → Final Report（已实现）
research_v5：在v4基础上加 Citation System（引用追踪链）。核心不是让 LLM 在句尾写 [1]，而是建立完整数据链 Claim → Evidence → Source → 原文/URL：Source Registry（来源注册表与正文版本）→ Evidence Registry（quote 对保存正文自动验证 + locator 定位）→ Claim Extraction（Synthesis 结构化输出 claims）→ Claim–Evidence Mapping（程序校验 evidence_ids）→ Citation Validation（ID/URL/Quote/语义/冲突五级检查 + 关键结论二次核验）→ Report Renderer（程序生成 [n] 编号、结论追溯表、去重 References 与引用质量说明）（已实现）
research_v6：待规划

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
- `_test_v5_registry.py` — V5 单元测试：Source Registry（URL 规范化 / 正文版本 / 边界，不联网）
- `_test_v5_evidence.py` — V5 单元测试：Evidence Registry（quote 验证 / locator / 管线接线 / 端到端）
- `_test_v5_claims.py` — V5 单元测试：Claim–Evidence Mapping（validate_claims / Synthesis JSON）
- `_test_v5_validator.py` — V5 单元测试：Citation Validator（五级检查 / 语义判定 / 二次核验）
- `_test_v5_renderer.py` — V5 单元测试：Citation Renderer（编号 / References / 追溯表 / 质量说明）
- 后续版本的 `research_v6.py` 及配套测试，实现后再补进此清单

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

### 运行

```bash
python research_v3.py
```

终端默认只输出每条研究链的一行进度；想看到每个网页的抓取/证据明细，
把 `VERBOSE` 改成 `True`。

### 测试

```bash
python _test_v3_planner.py    # Planner 单测（mock LLM，不联网）
python _test_v3_dryrun.py     # 端到端 dry-run（mock 全部外部调用，不联网）
```

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

### 运行

```bash
python research_v4.py
```

### 测试

```bash
python _test_v4_state.py     # 单元测试：ResearchState / GapAnalyzer / Replanner
python _test_v4_dryrun.py    # 端到端 dry-run（mock 全部外部调用，不联网）
```

## v5 加 Citation System（引用追踪链）

核心变化一句话：V4 的引用是「LLM 在句尾写 [EV-xxx]，程序转成 [n]」；V5 把引用升级为
完整的数据链 **Claim → Evidence → Source → 原文/URL** —— 每一跳的 ID 由程序分配、
由程序校验，最终报告附结论追溯表与引用质量说明，LLM 不再参与参考文献生成。

`research_v5.py` 已实现：从 v4 复制改造，逐 Phase 增量交付（B→F），
Planner / Research Loop / GapAnalyzer / Replanner 与 V4 一致，多轮研究功能不受影响。

### 流程

```
V4 输出：全部 Evidence（多轮研究、来源、原文及摘要）

① Source Registry · 来源注册表
   每个来源分配稳定 source_id，保存 URL / canonical_url / 标题 / 域名 / 时间 / sha256，
   页面正文按版本保存（content_hash 变化 → 追加新版本，旧版本永不覆盖）
② Evidence Registry · 证据注册表
   每条证据分配 evidence_id，关联 source_id + 正文版本；
   quote 对保存正文自动验证（命中 → locator 段落定位；未命中 → 概括标记，不冒充原文）
③ Claim Extraction · 结论拆分
   Synthesis 输出 JSON（answer_markdown + claims），重要结论拆成独立 Claim
④ Claim–Evidence Mapping
   evidence_ids 只认真实存在的证据（小结不充当证据、编造 ID 剔除）；
   数字/因果结论无证据 → 强制 unsupported，不强行生成结论
⑤ Citation Validation · 五级校验
   ID → URL → Quote 复验 → Semantic（LLM 结构化判定，程序过滤与合并）→ Conflict；
   关键结论独立二次核验，不一致标 needs_review
⑥ Report Renderer
   程序生成 [1]/[2]…（同源同号）、结论追溯表（EV → [n]）、
   去重 References（Markdown 可点击链接、元数据只取注册表）与引用质量说明
```

### 要点

- 引用链每一跳都有程序校验：`evidence_id → source_id → version_id → 原文正文`；
  校验失败显式呈现（报告内「引用质量说明」小节），绝不静默省略
- 两个注册表是内存态（与项目单文件、零依赖一致）；来源 URL 永远保留原始形态，
  canonical_url 仅用于去重（小写 host / 去 www / 去 fragment / 只删 utm_* 跟踪参数）
- quote 验证只做子串级匹配（含空白折叠容忍）；命中 = 原文，未命中 = LLM 概括并打标
- Synthesis 解析失败重试一次，仍失败按 V4 兜底（整段当正文），引用链在解析层不断裂
- 校验开关：`SEMANTIC_CHECK_ENABLED` / `URL_REACHABILITY_CHECK`（默认不发网络请求）/
  `SECOND_PASS_MAX_CLAIMS`

### 数据结构（三个核心对象）

```
Source（来源注册表 SourceRegistry）
├─ source_id        SRC-001            程序分配，与来源绑定后永不改变
├─ url              原始形态，永不改写 | canonical_url 仅用于去重
├─ title / domain / published_at（可为空，不伪造） / retrieved_at（首见时间）
├─ content_hash     最新正文的 sha256
└─ page_versions[]  正文版本，append-only，旧版本永不覆盖
   └─ version_id SRC-001-v1 / content_hash / page_text / char_count / retrieved_at / url

Evidence（证据注册表 EvidenceRegistry）
├─ evidence_id      EV-001             全局唯一
├─ source_id + version_id              引用链：证据 → 来源 → 具体正文版本
├─ quote            原文片段          | context 前后文 | locator 段落序号 + 字符区间
├─ supporting_claim / extraction_method（llm_quote_verified | llm_paraphrase）
├─ quote_verified   quote 是否已对保存正文复验命中
└─ first_seen {round, question_id}     首见出处，跨轮去重时不覆盖

Claim（结构化结论，validate_claims 校验 + validate_citation_chain 合并裁决后写回）
├─ claim_id         CLM-001            程序分配，忽略 LLM 给的任何编号
├─ text / evidence_ids（只含真实存在的 ID） / invalid_evidence_ids（被剔除的编造引用）
├─ support_status   supported / partial / unsupported / contradicted / unverified
├─ source_ids       程序反查的真实 distinct 来源（衡量来源独立性，不按证据条数）
├─ needs_evidence   numbers / causality（数字/日期/比例/因果启发式）
└─ validation / needs_review           五级校验摘要与人工复核标记
```

### 最终报告结构（Renderer 输出）

最终报告由程序组装为四节，LLM 只负责正文内容，编号、追溯表、参考文献
与质量说明全部由 `render_report` 生成：

```
（正文：LLM 的 Markdown；[EV-xxx] 已由程序转成 [1]/[2]，同源同号，
 映射不到来源的引用整组抹掉并计数，内部 EV ID 不会泄漏）

## 结论与证据追溯                      ← 程序生成的表格，内部证据 ID 保留
| 结论 ID | 结论 | 最终判定 | 证据 → 引用编号 |
| CLM-001 | 2024 年市场规模约为 42 亿元 | supported | EV-001 → [1] |
| CLM-002 | 竞争格局分散暂无量化证据 | unsupported | —（无证据） |

## 引用质量说明                        ← 校验结果显式呈现，失败绝不静默
- 校验结论：pass 1 / warn 1 / fail 0，需人工复核 0 条。
- ⚠ CLM-002（unsupported）：需注意 —— 竞争格局分散暂无量化证据

## 引用                                ← 去重 References，每来源恰一条、Markdown 可点击
[1] [来源A](https://a.com/1) — a.com，抓取于 2026-01-01
[2] [来源B](https://b.com/2) — b.com，抓取于 2026-01-02
```

端到端 dry-run（`_test_v5_renderer.py` Test9）的 Run Summary 实际输出：

```
停止原因: sufficient
证据: 2 条（原文命中 2 / 未命中 0） | 来源: 2 个 | 搜索: 3 次
结论: 2 条（supported 1 / partial 0 / unsupported 1 / 其他 0）
引用链校验: pass 1 / warn 1 / fail 0（需人工复核 0）
```

### 已知限制

- URL 不跟随重定向、不合并 http/https；canonical 化只做小写 host、去 www、
  去 fragment、删 utm_* 跟踪参数 —— 宁可少合并，不可错合并（错合并会让证据失去出处）
- quote 验证为子串级（含空白折叠容忍）：跳词摘录、近义改写会归入 llm_paraphrase
  （概括，原样保存并打标，不冒充原文）
- 语义判定 = 单次 LLM 结构化输出 + 程序把关；关键结论（含数字/因果要素，
  最多 `SECOND_PASS_MAX_CLAIMS=3` 条）追加独立二次核验，两次不一致标
  needs_review 交人工，程序不自动裁决
- 冲突检测依赖 GapAnalyzer 在研究轮次中的 LLM 发现，程序只做结论证据与
  已知冲突集合的交集比对
- 两个注册表为内存态、不落盘（保持项目单文件、零依赖约定），进程结束即清空
- 依赖 LLM 遵守 Synthesis 的 JSON 输出格式；不合规时重试一次，仍失败则按
  V4 兜底（整段回复当正文、claims 为空），并在终端与报告质量说明中显式提示

### 运行

```bash
python research_v5.py
```

### 测试

```bash
python _test_v5_registry.py    # Source Registry（不联网）
python _test_v5_evidence.py    # Evidence Registry + 管线接线（mock，不联网）
python _test_v5_claims.py      # Claim–Evidence Mapping（mock，不联网）
python _test_v5_validator.py   # Citation Validator 五级检查（mock，不联网）
python _test_v5_renderer.py    # Citation Renderer（mock，不联网）
```

## v6（待规划）

预留章节，规划确定后在此补充：定位一句话 → 流程 → 新增点 → 运行 / 测试。

## 如何新增一个版本

1. 「版本一览」加一行：在 v(X-1) 基础上加什么、流程链是什么、状态（已实现 / 规划中）
2. 新增 `## vX` 章节：定位一句话 → 流程图 → 新增点（要点列表）→ 运行 / 测试
3. 「文件」清单加上对应文件（如 `research_v5.py` 及配套 `_test_v5_*.py`）



