# -*- coding: utf-8 -*-
"""Research Agent V7 —— Multi-Agent 版。

V7 在 V6（LangGraph 编排）之上引入多 Agent 角色：
Planner → Chief Researcher →（并行）Researcher ×N → Analyst
→（研究循环）→ Reviewer → Writer → Publisher。

文件组织:
- state.py     V7State + reducer + new_state() + 新配置常量
- agents.py    7 个角色节点 + route_review
- graph.py     build_graph / get_compiled_graph / run_research
- main.py      CLI REPL（python -m research_v7）

运行:  python -m research_v7
"""

from .graph import build_graph, get_compiled_graph, run_research
from .state import (
    MAX_PARALLEL_RESEARCHERS,
    MAX_REVIEW_CYCLES,
    OUTPUT_DIR,
    V7State,
    new_state,
)

__all__ = [
    "build_graph",
    "get_compiled_graph",
    "run_research",
    "V7State",
    "new_state",
    "MAX_PARALLEL_RESEARCHERS",
    "MAX_REVIEW_CYCLES",
    "OUTPUT_DIR",
]
