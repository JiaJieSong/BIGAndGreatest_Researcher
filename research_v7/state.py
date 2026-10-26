# -*- coding: utf-8 -*-
"""Research Agent V7 —— V7State（Multi-Agent 图状态）+ 配置常量。

V7 在 V6（LangGraph 编排）之上引入多 Agent：
- Chief Researcher 用 Command.goto=[Send(...)] 并行派发 Researcher ×N
- 并行结果经 reducer（Annotated[list, operator.add]）汇合
- Reviewer 质检回环；Writer / Publisher 多格式输出

本模块只定义状态与配置，不含任何角色逻辑（见 agents.py / graph.py）。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Optional, TypedDict

# ------------------------- 新配置（V7）-------------------------

MAX_PARALLEL_RESEARCHERS = 3   # 每波并行 Researcher 上限
MAX_REVIEW_CYCLES = 2          # Reviewer 打回上限（达到后强制进 Writer 如实呈现）
OUTPUT_DIR = "output"          # 输出根目录

# ------------------------- V7State（TypedDict）-------------------------


class V7State(TypedDict):
    """Multi-Agent 工作流状态。

    - original_question: 原始用户问题（planner 携带，供最终报告回查）
    - state: V5 的 ResearchState 实例 —— 全图唯一，含两个注册表、证据/来源/
      搜索历史/缺口/冲突/轮次等全部研究状态
    - runtime: 运行级数据，与 V6 同构（filter_stats / 每波计数 / GapReport）
    - researcher_reports: 并行 Researcher 的结果汇合（reducer 累加）。
      **唯一**需要 reducer 的字段；由 analyst 消费后以 Overwrite 重建。
    - review_notes: Reviewer 发现的整改问题清单（跨 cycle 累积，最终交 Writer）
    - review_cycles: 已执行的 Reviewer 打回次数
    - review_verdict: 最近一次评审结论（pass | fail）
    - final_answer: 最终渲染完成的报告 markdown
    - product_paths: 发布产物路径 {"md", "pdf", "docx"}
    - error: 致命错误诊断信息；非空即中止
    """

    original_question: str
    state: Optional[Any]                      # v5.ResearchState（避免循环导入）
    runtime: dict[str, Any]
    researcher_reports: Annotated[list, operator.add]
    review_notes: list[str]
    review_cycles: int
    review_verdict: Optional[str]
    final_answer: Optional[str]
    product_paths: dict[str, str]
    error: Optional[str]


def new_state(question: str) -> V7State:
    """构造初始 V7State：研究状态由 planner 建立，其余字段给默认值。"""
    return {
        "original_question": question,
        "state": None,
        "runtime": {"filter_stats": {}},
        "researcher_reports": [],
        "review_notes": [],
        "review_cycles": 0,
        "review_verdict": None,
        "final_answer": None,
        "product_paths": {},
        "error": None,
    }


# ------------------------- 状态访问辅助 -------------------------


def _runtime(g: V7State) -> dict:
    """取 runtime 字段（保险：缺失时补默认）。"""
    rt = g.setdefault("runtime", {})
    rt.setdefault("filter_stats", {})
    return rt


def _state(g: V7State):
    """取研究状态；未建立时抛清晰异常（planner 保证建立）。"""
    st = g.get("state")
    if st is None:
        raise RuntimeError("V7State 中缺少 ResearchState（planner 未执行？）")
    return st
