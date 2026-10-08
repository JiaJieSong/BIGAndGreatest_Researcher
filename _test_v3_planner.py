# -*- coding: utf-8 -*-
"""Planner 逻辑测试（mock _chat，不走真实 API）。

- Test1: 正常 Planner 输出（带围栏 / 多余文字 / 重复）→ 解析、去重正确
- Test2: Planner LLM 连续失败 → 重试后抛 ValueError，不静默吞错
- Test3: 参数扫描 Planner 返回 0~6 个子问题 → fallback 保证「始终成立」，
         并生成折线图 _test_v3_planner_chart.png 展示结果
- Test4: ResearchResult 失败标记 + Synthesis 文本块拼接
- Test5: 单子问题研究失败 → 返回 error 结果，不抛出
"""
import sys
import research_v3 as v3

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

# 中文字体：优先微软雅黑，找不到时退回 SimHei，避免方块字
for name in ("Microsoft YaHei", "SimHei", "SimSun"):
    if any(f.name == name for f in font_manager.fontManager.ttflist):
        plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
        break
plt.rcParams["axes.unicode_minus"] = False

# ---------- Test1: 正常输出（围栏、多余文字、重复子问题） ----------
def fake_chat(messages, temperature):
    return (
        "好的，以下是拆解结果：\n"
        "```json\n"
        '{"sub_questions": [\n'
        '  {"id": "q1", "question": "AI Agent 技术发展到什么程度？"},\n'
        '  {"id": "q2", "question": "AI Agent 应用在哪些领域？"},\n'
        '  {"id": "q3", "question": "AI Agent 有哪些技术瓶颈？"},\n'
        '  {"id": "q3", "question": "AI Agent 有哪些技术瓶颈？"}\n'
        "]}\n```\n以上"
    )
v3._chat = fake_chat
plan = v3.plan_research("AI Agent 未来前景")
print("TEST1 plan:", len(plan.sub_questions), "subs, original =", plan.original_question)
for sq in plan.sub_questions:
    print("   ", sq.id, sq.question)
assert len(plan.sub_questions) == 3, "去重后应为 3 个"

# ---------- Test2: Planner 连续失败 → ValueError ----------
def bad_chat(messages, temperature):
    raise RuntimeError("API down")
v3._chat = bad_chat
try:
    v3.plan_research("测试")
    print("TEST2 FAIL: no exception")
    sys.exit(1)
except ValueError as e:
    print("TEST2 OK raised:", e)

# ---------- Test3: fallback 不变式扫描 + 折线图 ----------
def make_stats_chat(valid_count):
    """构造一个恰好返回 valid_count 个有效子问题的 mock _chat。"""
    def chat(messages, temperature):
        items = [
            {"id": f"q{i}", "question": f"子问题 {i}"} for i in range(valid_count)
        ]
        return '{"sub_questions": %s}' % items.__str__().replace("'", '"')
    return chat

x_input = list(range(0, 7))          # Planner 返回的有效子问题数
y_final = []                                    # 最终计划子问题数
invar_ok = True
for n in x_input:
    v3._chat = make_stats_chat(n)
    plan = v3.plan_research("不变式测试问题")
    final_n = len(plan.sub_questions)
    y_final.append(final_n)

    # 不变式 1: 计划大小始终 >= SUB_QUESTION_MIN
    ok_size = final_n >= v3.SUB_QUESTION_MIN
    # 不变式 2: id 全部唯一
    ids = [sq.id for sq in plan.sub_questions]
    ok_ids = len(ids) == len(set(ids))
    # 不变式 3: 原问题保留在 plan 里（作为补位或 original_question）
    ok_orig = plan.original_question == "不变式测试问题"
    invar_ok = invar_ok and ok_size and ok_ids and ok_orig
    print(f"TEST3 n={n} -> final={final_n} (>= {v3.SUB_QUESTION_MIN}: {ok_size}, "
          f"ids唯一: {ok_ids})")
assert invar_ok, "fallback 不变式未始终成立！"

# 折线图：x = Planner 返回数, y = 最终计划数（含下限参考线）
plt.figure(figsize=(8, 5), facecolor="#fcfcfb")
ax = plt.gca()
ax.set_facecolor("#fcfcfb")
plt.plot(
    x_input, y_final,
    color="#2a78d6",                # slot 1 blue, linewidth=2, marker="o",
    markersize=8,                    # >=8px markers
    label="最终子问题数",
)
plt.axhline(
    v3.SUB_QUESTION_MIN,
    color="#52514e",                 # secondary ink（参考线，非系列色）
    linestyle="--", linewidth=1.5,
)
plt.text(
    x_input[-1] - 0.15, v3.SUB_QUESTION_MIN + 0.12,
    f"下限 {v3.SUB_QUESTION_MIN}", color="#52514e", fontsize=10, ha="right",
)
plt.xticks(x_input, [str(i) for i in x_input])
ax.yaxis.set_major_locator(plt.MultipleLocator(1))
plt.grid(axis="y", color="#e1e0d9", linewidth=0.8)   # recessive hairline grid
plt.title("Planner fallback 保证: 最终计划子问题数 ≥ SUB_QUESTION_MIN",
          color="#0b0b0b", fontsize=12)
plt.xlabel("Planner 返回的有效子问题数", color="#52514e")
plt.ylabel("最终计划子问题数", color="#52514e")
ax.tick_params(colors="#52514e")
plt.legend(frameon=False)
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
plt.tight_layout()
plt.savefig("_test_v3_planner_chart.png", dpi=150)
print("TEST3 chart saved -> _test_v3_planner_chart.png")

# ---------- Test4: ResearchResult 失败标记 + 文本块 ----------
r1 = v3.ResearchResult("q1", "Q1?", [{"id": "EV-001", "claim": "c"}], "Q1 小结")
r2 = v3.ResearchResult("q2", "Q2?", [], "", error="Search API 超时")
block = v3.build_results_block([r1, r2])
print("TEST4 r1.ok:", r1.ok, "| r2.ok:", r2.ok)
assert r1.ok and not r2.ok
assert "该子问题研究失败" in block and r1.summary in block

# ---------- Test5: 单子问题研究失败不抛出 ----------
sq = v3.SubQuestion("q1", "问题？")
def bad_summarize(q, id_prefix="", stats=None):
    raise RuntimeError("boom")
v3.summarize_sub_question = bad_summarize
res = v3.research_sub_question(sq)
print("TEST5 ok:", res.ok, "error:", res.error)
assert not res.ok and res.error

print("ALL TESTS PASSED")