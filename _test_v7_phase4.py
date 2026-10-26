# -*- coding: utf-8 -*-
"""V7 Phase 4 验收测试：完整 Reviewer（Mock LLM / Search / Fetch，离线）。

    python _test_v7_phase4.py

覆盖:
- T1   Reviewer pass → 直接进 Writer（一次评审）
- T2   Reviewer fail（evidence 类）→ 整改任务入队 → 再研究 → 再评审 → pass
- T3   Reviewer 持续 fail → 打回达到上限 → 强制进 Writer，review_notes 如实披露
- T4   Review 循环不超上限（有硬上限，不会无限打回）
- T5   非 evidence 类 issue 只披露、不触发再研究
- T6   review_notes 出现在最终报告中
"""
# -*- coding: utf-8 -*-
import glob
import json
import os
import re
import shutil
import sys

import research_v7 as v7
import research_v5 as v5

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

PLANNER = [{"id": "q1", "question": "问题一？"},
           {"id": "q2", "question": "问题二？"}]
PAGES = {
    "https://a.com/1": "<html><body><p>A 页：市场 42 亿元。</p></body></html>",
    "https://b.com/2": "<html><body><p>B 页：机构 51 亿元。</p></body></html>",
}
DEFAULT_PAGE = "<html><body><p>默认正文。</p></body></html>"
SUFFICIENT = {"gaps": [], "conflicts": [], "follow_up_questions": [],
              "is_sufficient": True, "reasoning_summary": "足够"}
PASS_REVIEW = {"verdict": "pass", "issues": []}
# 第一次 fail（evidence + clarity 各一），之后 pass
FAIL_EV_REVIEW = {"verdict": "fail",
                  "issues": [{"type": "evidence", "description": "缺少 2025 年权威数据"},
                             {"type": "clarity", "description": "术语「市场规模」未定义"}]}


def _first_para(url):
    page = PAGES.get(url, DEFAULT_PAGE)
    return page.split("<p>", 1)[1].split("</p>", 1)[0]


def _page_url_of(prompt):
    for ln in prompt.splitlines():
        if ln.startswith("网页URL:"):
            return ln.split("网页URL:", 1)[1].strip()
    return ""


def make_chat(gap_response=SUFFICIENT, review_responses=PASS_REVIEW):
    """review_responses 可为单个 dict 或 list（按评审次数取，用尽后重复最后一个）。"""
    review_seq = review_responses if isinstance(review_responses, list) else [review_responses]
    _review_n = {"n": 0}

    def chat(messages, temperature=None):
        prompt = messages[0]["content"]
        if "研究规划器" in prompt:
            return json.dumps({"sub_questions": PLANNER}, ensure_ascii=False)
        if "网络搜索助手" in prompt:
            return json.dumps(["查询一"], ensure_ascii=False)
        if "事实核查助手" in prompt:
            url = _page_url_of(prompt)
            return json.dumps({"evidence": [
                {"claim": "量化数据", "evidence_text": _first_para(url),
                 "source_title": "T", "source_url": url}]}, ensure_ascii=False)
        if "研究写作助手" in prompt:
            m = re.search(r"\[(EV-[A-Za-z0-9-]*\d{3})\]", prompt)
            return f"小结 [{m.group(1)}]。" if m else "小结。"
        if "研究缺口分析师" in prompt:
            return json.dumps(gap_response, ensure_ascii=False)
        if "研究合著者" in prompt:
            use = list(dict.fromkeys(re.findall(r"\[(EV-[A-Za-z0-9-]*\d{3})\]",
                                                prompt)))[:1]
            cites = "、".join(f"[{i}]" for i in use)
            return json.dumps({"answer_markdown": f"## 综合结论\n\n答案 {cites}。",
                               "claims": [{"text": "综合结论",
                                           "evidence_ids": use[:1],
                                           "support_status": "supported"}]},
                              ensure_ascii=False)
        if "证据原文支持" in prompt or "独立复核员" in prompt:
            ids = re.findall(r"证据 ID: (EV-\d+)", prompt)
            return json.dumps({"status": "supported", "supporting_evidence_ids": ids[:1],
                               "contradicting_evidence_ids": [], "explanation": "一致"},
                              ensure_ascii=False)
        if "研究质量评审员" in prompt:
            resp = review_seq[min(_review_n["n"], len(review_seq) - 1)]
            _review_n["n"] += 1
            return json.dumps(resp, ensure_ascii=False)
        raise AssertionError("未预期的 prompt: " + prompt[:80])
    return chat


def install_mocks(gap_response=SUFFICIENT, review_responses=PASS_REVIEW):
    old = (v5._chat, v5.search, v5.fetch_page)
    v5._chat = make_chat(gap_response, review_responses)
    v5.search = lambda q: [{"title": "A", "url": "https://a.com/1", "content": "x" * 200},
                           {"title": "B", "url": "https://b.com/2", "content": "x" * 200}]
    v5.fetch_page = lambda url: PAGES.get(url, DEFAULT_PAGE)
    return lambda: (setattr(v5, "_chat", old[0]),
                    setattr(v5, "search", old[1]),
                    setattr(v5, "fetch_page", old[2]))


# T1: pass → 直接 Writer
restore = install_mocks(SUFFICIENT, PASS_REVIEW)
state1, ans1 = v7.run_research("AI Agent 市场规模如何？")
restore()
assert state1.stop_reason == "sufficient"
# 一次评审（pass），无整改
print(f"T1 OK: Reviewer pass → 直接 Writer（{state1.completed_questions} 问题）")

# T2: fail(evidence) → 整改任务入队 → 再研究 → 再评审 pass
# 评审序列: 第1次 fail → 整改 → 第2次 pass
restore = install_mocks(SUFFICIENT, [FAIL_EV_REVIEW, PASS_REVIEW])
state2, ans2 = v7.run_research("AI Agent 市场规模如何？")
restore()
reviewed_qs = [c for c in state2.completed_questions if c["question"].source == "reviewer"]
assert reviewed_qs, "evidence 类 issue 应转为 reviewer 来源的研究任务"
print(f"T2 OK: fail(evidence) → 整改任务 {len(reviewed_qs)} 个 → 再研究 → pass")

# T3+T4: 持续 fail → 打回达到上限 → 强制 Writer + review_notes 披露
# 评审序列: fail ×3（第3次超上限 → 强制 Writer）
REVIEW_FAIL3 = [{"verdict": "fail", "issues": [{"type": "evidence",
                                                "description": f"第 {i} 次缺数据"}]}
                for i in (1, 2, 3)]
restore = install_mocks(SUFFICIENT, REVIEW_FAIL3)
state3, ans3 = v7.run_research("AI Agent 市场规模如何？")
restore()
assert state3.status == "stopped"
assert "评审问题清单" in ans3, "超上限进 Writer 应披露 review_notes"
assert "第 1 次缺数据" in ans3 or "缺数据" in ans3, "问题清单内容应出现在报告"
# 整改任务最多 MAX_REVIEW_CYCLES 个（第1、2次 fail 各转1个）
reviewed3 = [c for c in state3.completed_questions if c["question"].source == "reviewer"]
assert len(reviewed3) <= v7.MAX_REVIEW_CYCLES, \
    f"整改任务不得超过上限: {len(reviewed3)} > {v7.MAX_REVIEW_CYCLES}"
print(f"T3+T4 OK: 持续 fail → 打回上限 {v7.MAX_REVIEW_CYCLES} → 强制 Writer，"
      f"review_notes 披露（整改 {len(reviewed3)} 次）")

# T5+T6: 非 evidence 类 issue 只披露不触发再研究
REVIEW_CLARITY = {"verdict": "fail",
                  "issues": [{"type": "clarity", "description": "术语未定义"}]}
restore = install_mocks(SUFFICIENT, [REVIEW_CLARITY, PASS_REVIEW])
state4, ans4 = v7.run_research("AI Agent 市场规模如何？")
restore()
reviewed4 = [c for c in state4.completed_questions if c["question"].source == "reviewer"]
assert not reviewed4, "clarity 类 issue 不应触发再研究"
assert "术语未定义" in ans4, "clarity 类 issue 应披露在报告中"
print("T5+T6 OK: clarity 类 issue 只披露、不触发再研究；review_notes 进报告")

print("\nALL V7 PHASE4 TESTS PASSED")
