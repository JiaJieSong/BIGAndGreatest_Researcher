"""Research Agent V6 —— 在 V5 基础上引入 LangGraph StateGraph 工作流编排。

V6 的核心目标不是重写 Research Agent，而是保留 V5 全部研究能力与引用系统，
将 V5 的手动 Research Loop 重构为真正由 LangGraph StateGraph 驱动的工作流。

本文件是 V6 入口层：导入 research_v5 的所有业务函数（Planner / Research Loop /
Gap Analyzer / Replanner / Synthesis / Citation Validator / Citation Renderer /
两个注册表），不复制一套实现。V6 只新增:
- Graph State 定义（TypedDict）
- 五个 Graph 节点（plan / research_round / gap_analysis / replan / finalize）
  + 一个条件路由节点（route_research），用 add_conditional_edges 实现
  研究循环的「继续 / 停止」分支
- run_research() 公共入口：编译 StateGraph 后 invoke 执行，返回与 V5 一致
  的 (ResearchState, final_answer)

Graph 结构:
    START → plan_node → research_round_node → gap_analysis_node
          → route_research（条件路由）
                 ├─ "continue" → replan_node → research_round_node（循环）
                 └─ "stop"     → finalize_node → END

状态模型:
- Graph State 只有一个 mutable 字段 `state: ResearchState`（V5 的统一研究状态），
  两个注册表（SourceRegistry / EvidenceRegistry）是它的成员，全图共享同一实例，
  不复制、不重建 —— 因此 source_id / evidence_id 跨轮持续分配，证据只增不减。
- `runtime` 字段保存 V5 运行级统计（filter_stats / 每轮新增证据数等），
  不参与研究状态本身。
- 节点一律「原地修改 graph_state 后再返回 graph_state」，绝不返回局部字典
  （LangGraph 1.x 中节点返回 dict 是全量替换而非合并，返回整份图状态才能
  保证字段跨节点累积）。
"""

from __future__ import annotations

import sys
from typing import Any, Optional, TypedDict

# V6 唯一新增的第三方依赖: LangGraph（不含 LangChain）
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

import research_v5 as v5

# ------------------------- 配置（沿用 V5）-------------------------

# LangGraph - Invoke 时通过 config 传入的安全上限。V5 的 MAX_ROUNDS 是业务上限，
# 这里设置一个宽松得多的图级安全网：即使路由逻辑出现 bug，也不会无限循环。
_RECURSION_LIMIT = 500

# ------------------------- Graph State (TypedDict) -------------------------


class GraphState(TypedDict):
    """LangGraph 工作流的图状态。

    - original_question: 原始用户问题（从 plan_node 开始携带，供最终报告回查）
    - state: V5 的 ResearchState 实例 —— 全图唯一，含两个注册表、证据/来源/
      搜索历史/缺口/冲突/轮次等全部研究状态
    - runtime: V6 运行级数据，与 V5 的 filter_stats 等保持一致，T 研究状态正交
      - filter_stats: 源质量过滤统计（跨轮累加）
      - round_evidence_before: 本轮开始前已累计证据数（用于计算本轮新增）
      - last_round_new_evidence: 上一轮新增证据数（供路由判断 no_new_evidence）
    - pending_rq: 本轮正在研究的问题（本轮串行循环期间缓存，供节点打印与断言）
    - final_answer: 最终渲染完成的报告；研究结束后非空
    - error: 致命错误的诊断信息；非空时工作流应停止（不掩盖异常）
    """

    original_question: str
    state: Optional["v5.ResearchState"]
    runtime: dict[str, Any]
    pending_rq: Optional[Any]          # 本轮剩余待研究问题（ResearchQuestion | None）
    final_answer: Optional[str]
    error: Optional[str]


def new_graph_state(question: str) -> GraphState:
    """构造初始图状态：研究状态尚未建立，由 plan_node 创建。"""
    return {
        "original_question": question,
        "state": None,                 # plan_node 建立
        "runtime": {"filter_stats": {}},
        "pending_rq": None,
        "final_answer": None,
        "error": None,
    }


# ------------------------- 节点辅助 -------------------------


def _runtime(g: GraphState) -> dict:
    """取 runtime 字段（保险：缺失时补默认）。"""
    rt = g.setdefault("runtime", {})
    rt.setdefault("filter_stats", {})
    return rt


def _state(g: GraphState) -> "v5.ResearchState":
    """取研究状态；未建立时抛清晰异常（不应发生 —— plan_node 保证）。"""
    st = g.get("state")
    if st is None:
        raise RuntimeError("graph state 中缺少 ResearchState（plan_node 未执行？）")
    return st


def _pass(state: Any) -> Any:
    """透传节点：路由专用节点需要可调用 action；原地不改动、返回完整状态。"""
    return state


# ------------------------- 节点 1: plan_node -------------------------


def plan_node(g: GraphState) -> GraphState:
    """[1/X] 初始规划节点：调用 V5 的 plan_research() 并创建 ResearchState。

    - 接收原始问题，调用现有 Planner（不重写）
    - 创建现有 ResearchState（内部自动创建两个注册表）
    - 初始化运行级统计（filter_stats 等）
    - 原地修改后返回完整图状态
    """
    rt = _runtime(g)
    question = g["original_question"]
    print("\n[1/3] Understanding Question")
    print(f"      User: {question}")
    print("\n[2/3] Research Loop")
    plan = v5.plan_research(question)
    print(f"      Generated {len(plan.sub_questions)} sub-questions\n")
    g["state"] = v5.ResearchState(question, plan, max_rounds=v5.MAX_ROUNDS)
    rt["filter_stats"] = {}
    print("      [node] plan_node ✓")
    return g


# ------------------------- 节点 2: research_round_node -------------------------


def research_round_node(g: GraphState) -> GraphState:
    """[2/X] 研究单轮节点：处理本轮 pending_questions，逐题串行研究。

    - 读取当前 ResearchState，处理本轮 pending（调用 V5 research_sub_question）
    - 保留 V5 的搜索/抓取/证据提取/来源登记全流程（函数未改动）
    - 更新搜索历史、已完成问题、证据集合、轮次、未解决缺口
    - 保留单子问题失败不拖垮的既有容错（research_sub_question 内部 try/except）
    - 记录本轮新增证据数（供路由与日志）
    返回的图状态无局部重建 —— state 仍是同一个 ResearchState 实例。
    """
    st = _state(g)
    rt = _runtime(g)
    rnd = st.current_round + 1
    st.start_round(rnd)
    print(f"      [node] research_round_node —— 第 {rnd}/{st.max_rounds} 轮")

    evidence_before = len(st.evidence_list)
    rt["round_evidence_before"] = evidence_before

    # 本轮剩余的待研究问题：循环中逐题弹出一个处理（串行，与 V5 一致）
    while st.pending_questions:
        rq = st.pending_questions[0]
        g["pending_rq"] = rq
        print(f"      [{rq.id.upper()}] Search → Fetch → Extract → Summary",
              end="", flush=True)
        query_log: list[str] = []
        result = v5.research_sub_question(
            v5.SubQuestion(rq.id, rq.question), stats=rt["filter_stats"],
            fixed_queries=rq.suggested_queries or None,
            query_log=query_log, state=st, round_no=rnd,
        )
        print(" ✓" if result.ok else " ✗ (failed)")
        if not result.ok:
            print(f"            原因: {result.error}")
        st.record_searches(query_log)
        st.complete_question(rq, result, rnd)
        st.add_evidence(result.evidence_list)
        if result.ok:
            st.resolve_gap(rq.target_gap)

    new_evidence = len(st.evidence_list) - evidence_before
    rt["last_round_new_evidence"] = new_evidence
    g["pending_rq"] = None
    print(f"      [round] 本轮新增证据 {new_evidence} 条，"
          f"累积 {len(st.evidence_list)} 条")
    return g


# ------------------------- 节点 3: gap_analysis_node -------------------------


def gap_analysis_node(g: GraphState) -> GraphState:
    """[3/X] 缺口分析节点：调用 V5 的 analyze_gaps() 并更新研究缺口 / 冲突。

    - 保留 V5 的错误兜底（analyze_gaps 内部已处理，不会因分析失败崩溃）
    - 更新 state.identified_gaps / identified_conflicts / open_gaps
    - 复制当前 GapReport 到 runtime 供路由节点判断（保持一份权威 GAP 数据）
    - 报告充分性判断（runtime["gap_sufficient"]），方便节点日志
    不重新实现 Gap Analyzer。
    """
    st = _state(g)
    rt = _runtime(g)
    print("      [node] gap_analysis_node")
    print("      [gap] 分析研究缺口...", end="", flush=True)
    report = v5.analyze_gaps(st)
    print(" ✓" if report.ok else " ✗ (已兜底)")
    st.identified_gaps = report.gaps
    st.identified_conflicts = report.conflicts
    for gap in report.gaps:
        if gap not in st.open_gaps:
            st.open_gaps.append(gap)
    if report.ok and report.reasoning_summary:
        v5._log(f"      [gap] {report.reasoning_summary}")
    # 把 GapReport 放入 runtime —— 路由节点只读它，不触碰研究状态
    rt["last_gap_report"] = report
    rt["gap_sufficient"] = bool(report.ok and report.is_sufficient)
    print(f"      [gap] 充分性判定: {'sufficient' if rt['gap_sufficient'] else 'not sufficient'}")
    return g


# ------------------------- 路由: route_research -------------------------


def route_research(g: GraphState) -> str:
    """研究路由：决定继续研究还是停止。

    停止规则与 V5 `_stop_reason()` 完全一致（不维护第二套判定）:
    1. sufficient       —— Gap Analyzer 判定证据充分
    2. no_new_evidence  —— 本轮没有任何新证据
    3. max_rounds       —— 已达研究轮次上限（兜底防无限循环）

    注意: **不能**在 replan 之前判断 pending 为空 —— 每轮研究完全部题目后
    pending 本来就清空，Follow-up 要等 replan 之后才入队。V5 语义是
    「replan 无产出才停」（规则 4，由 replan_node + route_after_replan 负责）。
    """
    st = _state(g)
    rt = _runtime(g)
    report: Optional["v5.GapReport"] = rt.get("last_gap_report")
    new_evidence = int(rt.get("last_round_new_evidence", 0))

    reason = v5._stop_reason(st, report, new_evidence) if report is not None else ""
    if reason:
        st.stop(reason)
        print(f"      [loop] 触发停止条件: {reason}")
        return "stop"
    print("      [loop] 证据不足，继续研究")
    return "continue_research"


def route_after_replan(g: GraphState) -> str:
    """重规划后的第二次路由：replan_node 是否已触发停止。

    - replan() 无有效 Follow-up 时，replan_node 已调用 stop("no_valid_followups")；
      此时再空转一轮毫无意义，直接进入 finalize。
    - 否则进入下一轮 research_round_node。
    这与 V5「replan 后 added 为空直接 break 到 Synthesis」语义一致。
    """
    st = _state(g)
    if st.status == "stopped":
        print(f"      [loop] 已无继续研究的依据（{st.stop_reason}），停止研究")
        return "stop"
    return "continue_research"


# ------------------------- 节点 4: replan_node -------------------------


def replan_node(g: GraphState) -> GraphState:
    """[4/X] 重规划节点：调用 V5 的 replan() 生成下一轮 Follow-up 问题。

    - replan() 已直接修改 ResearchState（把有效 Follow-up 加入 pending），
      并返回实际添加的列表 —— 不重复添加（V5 语义）
    - 无有效 Follow-up 时显式停止（no_valid_followups，与 V5 run_research 一致）
    - 本轮结束时累积输出 filter 统计行（与 V5 轮次循环末尾一致）
    返回后路由将再次进入 research_round_node 开启下一轮。
    """
    st = _state(g)
    rt = _runtime(g)
    print("      [node] replan_node")
    report: Optional["v5.GapReport"] = rt.get("last_gap_report")
    if report is None or report.error:
        # 缺口分析失败时无 Follow-up 可用 —— 与 V5 的 replan([]) 等价（不会新增）
        added: list = []
    else:
        added = v5.replan(st, report)
    print(f"      [replan] 新增 {len(added)} 个 Follow-up 问题")
    for rq in added:
        extra = f"  ← {rq.target_gap}" if rq.target_gap else ""
        print(f"         [{rq.id.upper()}] {rq.question}{extra}")
    if not added:
        st.stop("no_valid_followups")
        print("      [loop] 没有可执行的新研究问题，停止研究")
    if rt["filter_stats"]:
        fs = rt["filter_stats"]
        print(f"      [filter] 共过滤 {sum(fs.values())} 条低质结果"
              f"（黑名单 {fs.get('blocked', 0)} / 空壳 {fs.get('too_short', 0)}"
              f" / 低分 {fs.get('low_score', 0)}"
              f" / 兜底回退 {fs.get('fallback', 0)} 次）")
    return g


# ------------------------- 节点 5: finalize_node -------------------------


def finalize_node(g: GraphState) -> GraphState:
    """[5/X] 收尾节点：Synthesis → Citation Validator → Citation Renderer。

    - 调用 V5 synthesize_from_state()（含 Claim–Evidence Mapping）
    - 调用 V5 validate_citation_chain()（五级校验 + 关键结论二次核验）
    - 调用 V5 render_report()（引用编号 / 结论追溯 / References / 质量说明）
    - 最终报告写入 g["final_answer"]；V5 的 Run Summary 照常打印
    不跳过校验、不用替代渲染。
    """
    st = _state(g)
    rt = _runtime(g)
    print("\n[3/3] Synthesizing Final Answer")
    print("      [node] finalize_node")
    final_answer, _claims = v5.synthesize_from_state(st)
    print("      [validate] 校验引用链（ID / URL / Quote / 语义 / 冲突）...")
    _reports, _vstats = v5.validate_citation_chain(st)
    final_answer = v5.render_report(st, final_answer)
    g["final_answer"] = final_answer
    print("\n" + "=" * 50)
    print("Final Answer")
    print("=" * 50)
    print("\n--- Run Summary ---")
    v5._print_run_summary(st, rt["filter_stats"])
    return g


# ------------------------- 工作流构建 -------------------------


def build_graph() -> Any:
    """构建并编译 LangGraph StateGraph。

    节点划分与边:
    START → plan_node → research_round_node → gap_analysis_node
          → route_research（条件边）
                 ├─ "continue_research" → replan_node → route_after_replan
                 │     ├─ "continue_research" → research_round_node（循环）
                 │     └─ "stop" → finalize_node
                 └─ "stop" → finalize_node → END

    状态传递: 每个节点接收上一节点的完整图状态 dict，原地修改关键字段后
    整份返回（LangGraph 1.x 节点返回 dict 是全量替换；返回整份状态才能保证
    字段跨节点累积）。唯一的共享可变对象是 GraphState["state"]
    （ResearchState 实例），全图自始至终是同一个对象。
    """
    graph = StateGraph(GraphState)

    graph.add_node("plan_node", plan_node)
    graph.add_node("research_round_node", research_round_node)
    graph.add_node("gap_analysis_node", gap_analysis_node)
    graph.add_node("replan_node", replan_node)
    graph.add_node("finalize_node", finalize_node)

    graph.add_edge(START, "plan_node")
    graph.add_edge("plan_node", "research_round_node")
    graph.add_edge("research_round_node", "gap_analysis_node")
    graph.add_conditional_edges(
        "gap_analysis_node",
        route_research,
        path_map={
            "continue_research": "replan_node",
            "stop": "finalize_node",
        },
    )
    graph.add_conditional_edges(
        "replan_node",
        route_after_replan,
        path_map={
            "continue_research": "research_round_node",   # 循环回研究节点
            "stop": "finalize_node",
        },
    )
    graph.add_edge("finalize_node", END)

    return graph.compile()


# ------------------------- 公共入口 -------------------------

_COMPILED: Any = None


def get_compiled_graph() -> Any:
    """返回（缓存）已编译的 LangGraph 图。"""
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = build_graph()
    return _COMPILED


def run_research(question: str) -> tuple["v5.ResearchState", str]:
    """V6 公共入口：通过已编译的 LangGraph 图执行完整研究管线。

    与 V5 的返回值约定保持一致: (ResearchState, final_answer)。
    - StateGraph 的 invoke 返回最终图状态；最终答案在 g["final_answer"]。
    - 结构上可达的最终状态只有 finalize_node（END 前），因此结束即完成。
    - 致命错误：单个子问题失败/research 内部兜底由 V5 既有机制处理，不阻断；
      只有编排层异常（如图的 recursion limit 被触发）才向上抛出，附诊断信息。
    """
    print("=" * 50)
    print("Research Agent V6")
    print("=" * 50)

    graph = get_compiled_graph()
    initial = new_graph_state(question)
    try:
        result = graph.invoke(initial, {"recursion_limit": _RECURSION_LIMIT})
    except GraphRecursionError as e:
        # 理论上被 MAX_ROUNDS 挡住；这里作为最后安全网，给出可诊断的信息
        raise RuntimeError(
            "LangGraph 触发 recursion limit（图级安全网）: "
            "研究循环疑似失去边界控制"
        ) from e

    final_state: GraphState = result
    st = _state(final_state)
    answer = final_state.get("final_answer") or "（本次没有生成正文。）"
    if final_state.get("error"):
        raise RuntimeError(final_state["error"])
    return st, answer


# ------------------------- 交互入口 -------------------------


def main() -> int:
    v5.load_dotenv()
    v5.init_config()
    if not v5.AI_API_KEY or not v5.RESEARCH_API_KEY:
        print(
            "缺少 API key。请先创建 .env 文件，内容参照 .env.example：\n"
            "  AI_API_KEY=sk-...\n"
            "  RESEARCH_API_KEY=tvly-..."
        )
        return 1

    print("Research Agent V6 就绪（输入 exit 退出）\n")
    while True:
        try:
            question = input("Q> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue
        if question.lower() in ("exit", "quit"):
            break

        try:
            _, final_answer = run_research(question)
            print(final_answer)
        except (RuntimeError, ValueError) as e:
            print(f"[错误] {e}")
        print("-" * 60)

       
if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    sys.exit(main())