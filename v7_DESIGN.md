# V7 设计说明（Phase 1）

Multi-Agent 版 Research Agent：在 V5 业务核心 + V6 LangGraph 编排之上，引入并行 Researcher fan-out / 汇合、Reviewer 质检回环、Writer / Publisher 多格式输出。

> 本文件为设计稿，经确认后才开始写代码（Phase 2 起）。

---

## 1. 目标架构

```
START → planner ──► chief_researcher
                        │
              （Seve 并行 fan-out，每波 ≤ MAX_PARALLEL_RESEARCHERS=3）
        chief ──(有任务, Command(goto=[Send×k]))──► researcher[R1]…[Rk]
                        │ (reducer 汇合，正常边 = super-step barrier)
                        ▼
                    analyst（串行重放 + gap 分析 + replan）
                        │
                        ▼
                    chief（回环：再派发 / 停止判定）
                        │ (研究结束)
                        ▼
                    reviewer  ←───────┐ fail 且 review_cycles < MAX_REVIEW_CYCLES
                   pass │ ↑            │（evidence 类 issue → 转研究任务）
                        ▼ └────────────┘
                    writer ──► publisher ──► END
```

分两个回环，各自有硬上限：

| 回环 | 驱动 | 上限 | 语义 |
|---|---|---|---|
| 研究循环 | chief ↔ analyst（replan 产 follow-up） | v5.MAX_ROUNDS=3（业务）+ recursion_limit=500（图级安全网） | 与 V6 完全一致 |
| Review 循环 | reviewer ⇄ chief（整改转新任务） | MAX_REVIEW_CYCLES=2 | 新增质检打回上限 |

## 2. V7State 字段表（state.py）

| 字段 | 类型 | reducer | 写入者 | 说明 |
|---|---|---|---|---|
| `original_question` | str | —（覆盖） | planner | 贯穿全图 |
| `state` | `v5.ResearchState \| None` | — | planner 创建；analyst/writer 修改 | **全图唯一** ResearchState 实例（含两个注册表），不复制不重建 |
| `runtime` | dict[str, Any] | — | planner / chief / analyst | filter_stats（全局累积）、round_evidence_before、last_gap_report、gap_sufficient、batch/round 计数 |
| `researcher_reports` | `Annotated[list, operator.add]` | **reducer 合并** | researcher ×N（并行写） | 并行结果汇合；analyst 消费后清空 |
| `review_notes` | list[str] | —（覆盖，追加写入同列表对象） | reviewer | 跨 review cycle 累积的问题清单，最终交 writer 如实披露 |
| `review_cycles` | int | — | reviewer | 已打回次数（< MAX_REVIEW_CYCLES 时允许再次打回） |
| `review_verdict` | str \| None | — | reviewer | pass / fail（本次评审结论） |
| `final_answer` | str \| None | — | writer | 渲染完成的报告 markdown |
| `product_paths` | dict[str, str] | — | publisher | {"md":…, "pdf":…, "docx":…} |
| `error` | str \| None | — | 各节点异常路径 | 非空即中止 |

**关键约束**：`researcher_reports` 是**唯一**需要 reducer 的字段（并行累加）；其余字段靠 LangGraph 1.x 节点返回整份状态的全量替换语义累积。ResearchState 的 `pending_questions`/`completed_questions`/`evidence_list` 等列表**不进 graph reducer**——它们只被串行节点（chief / analyst）原地修改，并行期间由锁保护的注册表承担并发写入。

## 3. 七个角色节点（agents.py）

| # | 节点 | 输入（从哪来） | 行为 | 输出（写哪） | 复用的 v5 callsite |
|---|---|---|---|---|---|
| 1 | `planner_node` | original_question | 创建 ResearchState、初始化 runtime、打 `[1/3]` 头 | `g["state"]`, `g["runtime"]` | `v5.plan_research(question) -> ResearchPlan`；`v5.ResearchState(question, plan, max_rounds=v5.MAX_ROUNDS)` |
| 2 | `chief_researcher_node` | 全图 | **决策点**：(a) `pending_questions` 非空 → 弹 ≤3 个任务 → `Command(goto=[Send("researcher_node", payload)×k], update={…})`；(b) 空 → 停止判定 → `Command(goto="researcher/reviewer")` | 首波前 `v5.ResearchState.start_round(rnd)`；记录 round_evidence_before | `st.start_round(rnd)`（首波）；`st.stop(reason)`；`v5._stop_reason(st, report, new_evidence) -> str`（唯一决策来源） |
| 3 | `researcher_node` ×N | Send payload `{task, round_no}` | 独立本地 `stats = {}` 并行跑研究；**绝不**共享同一 stats dict | `researcher_reports: [report]`（reducer +） | `v5.research_sub_question(SubQuestion(rq.id, rq.question), stats=局部, fixed_queries=rq.suggested_queries或None, query_log=局部, state=st, round_no=rnd) -> ResearchResult` |
| 4 | `analyst_node`（串行） | researcher_reports（reducer 汇合后全量） | 按派发序重放：complete_question → add_evidence → record_searches → resolve_gap；合并各 researcher 局部 filter_stats 进全局；**本波处理完且 pending 已空时**做 gap + replan | 原地改 `g["state"]`；`runtime` | `st.complete_question(rq, result, rnd)`、`st.add_evidence(result.evidence_list)`、`st.record_searches(query_log)`、`st.resolve_gap(rq.target_gap)`；`v5.analyze_gaps(st) -> GapReport`；`v5.replan(st, report) -> list[ResearchQuestion]` |
| 5 | `reviewer_node` | 研究结束后的状态 | 新增 review prompt（评审材料 = 子问题完成度/证据统计/缺口/冲突，复用 v5 build_*_block 拼材料块）→ 解析 JSON `{verdict, issues:[{type, description}]}`；issues 追加 review_notes；cycles 累加 | `g["review_verdict"]`, `g["review_notes"]`, `g["review_cycles"]` | `v5.build_plan_block` / `v5.build_round_results_block` / `v5.build_evidence_block`（拼材料） |
| 6 | `writer_node` | reviewer pass / 或被强制进入 | 合成 + 校验 + 渲染完整报告 | `g["final_answer"]`, `g["state"]` | `v5.synthesize_from_state(st) -> (str, claims)`；`v5.validate_citation_chain(st) -> (reports, summary)`；`v5.render_report(st, answer) -> str`；`v5._print_run_summary(st, filter_stats)` |
| 7 | `publisher_node` | final_answer + state | 写 `output/<slug>-<yyyymmdd-hhmm>/`：report.md（render 产物原样）、report.pdf（reportlab，`pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))`，中文必须此字体）、report.docx（python-docx） | `g["product_paths"]` | `v5._print_run_summary` 后的产品路径打印 |

**Chief 的三种返回形态**（已验证可行，见实验注释）：
- 有任务：`return Command(goto=[Send("researcher_node", p) for p in payloads], update={...potential runtime 更新})`
- 无任务、停止判定后应进入 reviewer：`return Command(goto="reviewer_node", update={...})`
- （实验证明直接 `return [Send(...)]` 会抛 `InvalidUpdateError: Expected dict, got [Send(...)]`——这是本次设计必须采用 Command 包 Send 的事实基础）

**reviewer → chief 的整改回路**：fail 且 `review_cycles < 2` 时，chief 将 evidence 类 issue（`type == "evidence"`）转为 `v5.ResearchQuestion(qid=f"rq{cycle}", question=desc, source="reviewer", reason="reviewer 打回整改", target_gap=…)` 挂回 `pending_questions`，再正常派发。非 evidence 类 issue 留在 review_notes 透传给 writer。

**消息序 / 派发语义**：chief 每轮把全部 pending 弹出（pop & 清空队列，防重复派发），不足一批则本波发完；analyst 汇合点=正常边（非 Send 路径），LangGraph 保证所有已派发的 researcher 完成后才执行 analyst → 无需额外 barrier 节点；analyst 发现 pending 已空才跑 gap，`round_evidence_before` 在轮首记录，`new_evidence = len(st.evidence_list) - round_evidence_before` 只在轮末 analyst 计算一次 → `_stop_reason` 输入精确。

## 4. 边 / 条件路由表（graph.py）

| 起点 | 类型 | 去向/分支 | 判定 |
|---|---|---|---|
| START | edge | planner_node | — |
| planner_node | edge | chief_researcher_node | — |
| chief_researcher_node | **Command.goto** | `Send(researcher_node)×k`（k = min(len(pending), 3)）| pending 非空且研究未停止 |
| chief_researcher_node | Command.goto | analyst_node | pending 空（本波无任务）→ 进入分析 |
| chief_researcher_node | Command.goto | reviewer_node | 停止判定命中：`v5._stop_reason` 非空 或 pending 空且 replan 无产出（no_valid_followups） |
| researcher_node | edge | analyst_node | **super-step barrier**：全部并行 researcher 完成后才进 analyst |
| analyst_node | edge | chief_researcher_node | 回环：研究循环 / 波次调度 |
| chief_researcher_node | edge(停止后) | reviewer_node | — |
| reviewer_node | 条件边 route_review | pass → writer_node；fail 且 review_cycles < MAX_REVIEW_CYCLES → chief_researcher_node（转任务）；fail 且 cycles ≥ 上限 → writer_node（带 review_notes 如实呈现） | `verdict == "pass"`；`review_cycles` 计数 |
| writer_node | edge | publisher_node | — |
| publisher_node | edge | END | — |

图级安全网：`compile()` 后 `invoke(initial, {"recursion_limit": 500})`（沿用 v6 的 `_RECURSION_LIMIT`），`GraphRecursionError` → RuntimeError（与 v6 同构）。

## 5. 并发安全分析（本轮设计的核心）

并发点只有一处：并行 researcher 同时调用 `research_sub_question(state=st)` 时，函数内部会写 `st.source_registry` / `st.evidence_registry`（登记来源、验证并登记证据、分配全局唯一 ID）。其余研究状态字段**不会被并行写**：`pending_questions` 只被 chief（串行）pop、`completed_questions`/`evidence_list` 只被 analyst（串行）add_evidence/complete_question 追加。

对策（对 v5 的唯一改动，且为最小侵入，提交时单独说明）：

1. **锁**（任务书许可的 v5 改动）：`SourceRegistry.__init__` 与 `EvidenceRegistry.__init__` 各加 `self._lock = threading.Lock()`；`SourceRegistry.register` 与 `EvidenceRegistry.register` 方法体用 `with self._lock:` 包裹（覆盖 `_counter += 1` 与 dict 写入的读改写）。
   - **register_from_llm 不加锁**：它的全部写入 100% 经由 `self.register(...)`（见 v5:846-863），锁已覆盖；若两个方法各自 `with self._lock:` 会导致同一线程重入 `threading.Lock` 死锁（非重入锁）。这是与任务书字面「register/register_from_llm 都包」的差异点，理由见疑问清单 Q2。
   - 串行调用行为零变化（锁在单线程下无副作用）；V6 测试应继续全绿。
2. **局部 stats**：每个 researcher 用自己局部 `stats = {}`（filter_stats），结果随 report 返回，由 analyst 串行合并进全局 runtime["filter_stats"]——**并行节点绝不共享同一个 stats dict**（任务书硬约束）。
3. **ID 唯一性压力测试**（Phase 3）：100 并发 researcher 各跑一次 import（不联网，mock registry），断言全部 source_id / evidence_id 全局唯一、page_versions 不重复。

## 6. 配置 / 输出 / 依赖（main.py / 新配置）

- 新配置（state 或 config 常量）：`MAX_PARALLEL_RESEARCHERS = 3`、`MAX_REVIEW_CYCLES = 2`、`OUTPUT_DIR = "output"`。
- 输出目录：`output/<slug>-<yyyymmdd-hhmm>/`（slug = 问题前 8 个非空白字符的 URL 安全缩写；时间戳 `%Y%m%d-%H%M`），内含 `report.md` / `report.pdf` / `report.docx`。
- 依赖：仅追加 `reportlab`、`python-docx`；**禁止** langchain / langchain-core / openai SDK（零框架，仅 LangGraph 1.2.x + reportlab + python-docx）。
- CLI：`python -m research_v7`（REPL，与 v6 同构，`exit` 退出）；`run_research()` 保持 `(ResearchState, str)` 返回契约。
- 日志前缀：并行 researcher 输出 `[R1]`…`[Rk]`（交错可见）；保留 V5 Run Summary，并追加 `[batch]` / `[review cycle]` / `[product]` 行。

## 7. 文件布局（research_v7/ 包）

```
research_v7/
  __init__.py   # run_research() 公共入口（内部用 graph.invoke）
  state.py      # V7State + reducer + new_state()（含新配置常量）
  agents.py     # 7 个角色节点 + route_review 路由
  graph.py      # build_graph() / get_compiled_graph() / _RECURSION_LIMIT
  main.py       # python -m research_v7 CLI REPL
requirements-v7.txt   # langgraph>=1.2,<2.0 + reportlab + python-docx
```

## 8. 实施阶段

- **Phase 2（串行骨架）**：全部节点串行连通（Chief 单任务派发退化为一条 Send 路径），行为与 v6 逐项等价；产出读为同一批 mock 断言。
- **Phase 3（并行化）**：Send fan-out + reducer + v5 注册表锁 + 局部 filter_stats；100 并发唯一 ID 压力测试；注册表锁改动单独 commit 说明。
- **Phase 4（完整 Reviewer）**：review prompt / cycles / review_notes 透传 writer。
- **Phase 5（mock 验收）**：REPL 可用、`[R1][R2]` 交错、3 个输出文件（PDF 中文可看 / DOCX 完整）、引用系统零回归、信息稀缺场景走 follow-up + reviewer 打回、v5 除锁外零改动且 v6 零改动、更多 mock 测试全绿。

## 9. 疑问清单（请确认）

- **Q1 Reviewer 评审对象**：本设计按架构顺序 `chief → reviewer → writer`，reviewer 评审的是**研究材料**（子问题完成度 / 证据统计 / 缺口 / 冲突，复用 v5 build_*_block 拼材料），而非成文草稿。若希望 reviewer 评审草稿，需要把 writer 的合成提前一步（与架构图的顺序冲突）——**按当前设计（材料评审）是否接受？**
- **Q2 register_from_llm 锁**：为规避非重入锁自死锁，只在 `register` 加锁（其写入覆盖 register_from_llm 全部写路径）。与任务书字面「两个方法都包」不同——**接受此差异？**（若必须两处都写，需改用 `threading.RLock`，与任务书指定的 `threading.Lock()` 不符。）
- **Q3 波次语义**：一轮内 pending 超过 3 个时，同一轮采用**多波派发**（chief 回环继续派发至 pending 清空，才做 gap）——保证 v5「一轮 = 处理完全部 pending」语义不漂移。**接受此设计？**
- **Q4** `review_notes` 在超限强制进 writer 时，报告中以「未解决的不确定性 / 评审问题清单」小节如实披露（沿用 v5 现有 disclosure 风格）。**接受？**
- **Q5** slug 取问题前 8 个非空白字符做 URL 安全缩写；与已有 `output/` 下旧版本产物并存不冲突。**接受？**