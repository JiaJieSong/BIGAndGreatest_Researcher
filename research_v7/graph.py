# -*- coding: utf-8 -*-
"""Research Agent V7 —— LangGraph StateGraph 构图与公共入口。

图结构（与 v7_DESIGN.md 一致）：

    START → planner_node → chief_researcher_node
                │  (Command.goto=[Send×k])  · 并行派发
                ▼
            researcher_node ×k ──(正常边 barrier)──► analyst_node
                                              │
                                              ▼   （研究循环）
                                        chief_researcher_node
                │ 研究结束
                ▼
        reviewer_node ──route_review──┬─ pass ─────────────► writer_node
                                      ├─ fail & cycles≤上限 ─► chief_researcher_node
                                      └─ fail & cycles>上限 ─► writer_node
                                                 │
                                                 ▼
                                           publisher_node ──► END

关键 LangGraph 语义（实测 1.2.17）:
- 派发: 节点返回 Command(goto=[Send("node", payload)×k], update={...})
- 被 Send 节点: 只写 reducer 字段（Annotated[list, operator.add]），
  重放形态返回空 reducer 列表 —— 绝不写非 reducer 键
- 汇合: reducer 自动合并并行结果；消费后须 Overwrite([]) 重建
- 停止: Command(goto=END) 直接收尾
"""

from __future__ import annotations

from typing import Any

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

import research_v5 as v5

from .agents import (
    analyst_node,
    chief_researcher_node,
    planner_node,
    publisher_node,
    researcher_node,
    reviewer_node,
    route_review,
    writer_node,
)
from .state import V7State, new_state

# ------------------------- 图级安全网 -------------------------

# LangGraph - Invoke 时通过 config 传入的安全上限。V5 的 MAX_ROUNDS 是业务上限，
# 这里设置一个宽松得多的图级安全网：即使路由逻辑出现 bug，也不会无限循环。
_RECURSION_LIMIT = 500


# ------------------------- 构图 -------------------------


def build_graph() -> Any:
    """构建并编译 V7 的 LangGraph StateGraph。"""
    graph = StateGraph(V7State)

    graph.add_node("planner_node", planner_node)
    graph.add_node("chief_researcher_node", chief_researcher_node)
    graph.add_node("researcher_node", researcher_node)
    graph.add_node("analyst_node", analyst_node)
    graph.add_node("reviewer_node", reviewer_node)
    graph.add_node("writer_node", writer_node)
    graph.add_node("publisher_node", publisher_node)

    graph.add_edge(START, "planner_node")
    graph.add_edge("planner_node", "chief_researcher_node")
    # researcher 由 Chief 的 Command.goto=[Send...] 派发；正常边是汇合点
    # （LangGraph 保证全部并行 researcher 完成后才进 analyst = super-step barrier）
    graph.add_edge("researcher_node", "analyst_node")
    graph.add_edge("analyst_node", "chief_researcher_node")

    # Reviewer 的条件路由（route_review 返回 writer / chief）
    graph.add_conditional_edges(
        "reviewer_node",
        route_review,
        path_map={"writer": "writer_node", "chief": "chief_researcher_node"},
    )
    graph.add_edge("writer_node", "publisher_node")
    graph.add_edge("publisher_node", END)

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
    """V7 公共入口：通过已编译的 LangGraph 图执行完整研究管线。

    与 V5/V6 的返回值约定保持一致: (ResearchState, final_answer)。
    """
    print("=" * 50)
    print("Research Agent V7（Multi-Agent）")
    print("=" * 50)

    graph = get_compiled_graph()
    initial = new_state(question)
    try:
        result = graph.invoke(initial, {"recursion_limit": _RECURSION_LIMIT})
    except GraphRecursionError as e:
        raise RuntimeError(
            "LangGraph 触发 recursion limit（图级安全网）: "
            "研究/评审循环疑似失去边界控制"
        ) from e

    final_state: V7State = result
    st = final_state["state"]
    answer = final_state.get("final_answer") or "（本次没有生成正文。）"
    if final_state.get("error"):
        raise RuntimeError(final_state["error"])
    return st, answer