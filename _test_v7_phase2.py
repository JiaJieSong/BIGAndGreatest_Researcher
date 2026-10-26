# -*- coding: utf-8 -*-
"""V7 Phase 2 验收测试：串行骨架等价于 V6（Mock LLM / Search / Fetch，离线）。

    python _test_v7_phase2.py

覆盖（Phase 2 目标 —— 串行行为与 v6 相等）:
- T1   LangGraph 成功编译，含全部 7 个节点 + START/END
- T2   正常问题完整走 START → 研究 → 评审 → 写作 → 发布 → END
- T3   研究充分时停止（sufficient）
- T4   没有新增证据时停止（no_new_evidence）
- T5   达到最大轮次时停止（max_rounds）
- T6   Replanner 生成 Follow-up 后继续下一轮
- T7   没有有效 Follow-up 时结束（no_valid_followups）
- T8   单个子问题失败不破坏研究状态
- T9   证据 / 来源注册表跨轮一致
- T10  最终答案经 Citation Validator + Renderer
- T11  输出三个产物文件（md/pdf/docx）且目录名带 slug+时间戳
- T12  run_research 返回 (ResearchState, str) 契约
"""
# -*- coding: utf-8 -*-
import glob
import json
import os
import re
import sys

import research_v7 as v7
import research_v5 as v5

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# ---------------------------------------------------------------- fixtures

PLANNER = [{"id": "q1", "question": "问题一？"},
           {"id": "q2", "question": "问题二？"},
           {"id": "q3", "question": "问题三？"}]
PAGES = {
    "https://a.com/1": "<html><body><p>A 页第一段：2024 年市场规模为 42 亿元。</p></body></html>",
    "https://b.com/2": "<html><body><p>B 页第一段：另有机构估计为 51 亿元。</p></body></html>",
}
DEFAULT_PAGE = "<html><body><p>默认正文。</p></body></html>"
SUFFICIENT = {"gaps": [], "conflicts": [], "follow_up_questions": [],
              "is_sufficient": True, "reasoning_summary": "足够"}
GAPPY = {"gaps": ["缺少 2025 年数据", "缺少权威机构口径"],
         "conflicts": [],
         "follow_up_questions": [
             {"question": "2025 年的市场规模是多少？", "reason": "补充最新年份数据",
              "priority": "high", "target_gap": "缺少 2025 年数据",
              "suggested_queries": ["2025 市场规模", "市场规模 2025"]},
             {"question": "权威机构怎么统计？", "reason": "核实口径",
              "priority": "medium", "target_gap": "缺少权威机构口径",
              "suggested_queries": ["权威机构口径"]},
         ],
         "is_sufficient": False, "reasoning_summary": "还缺最新数据"}
NO_FOLLOWUP = {"gaps": ["仍有缺口"], "conflicts": [],
               "follow_up_questions": [],
               "is_sufficient": False, "reasoning_summary": "仍缺数据"}
PASS_REVIEW = {"verdict": "pass", "issues": []}
FAIL_REVIEW = {"verdict": "fail",
               "issues": [{"type": "evidence", "description": "缺少 2025 年权威数据"},
                          {"type": "clarity", "description": "术语未定义"}]}


def _first_para(url):
    page = PAGES.get(url, DEFAULT_PAGE)
    return page.split("<p>", 1)[1].split("</p>", 1)[0]


def _page_url_of(prompt):
    for ln in prompt.splitlines():
        if ln.startswith("网页URL:"):
            return ln.split("网页URL:", 1)[1].strip()
    return ""


def make_chat(gap_response=SUFFICIENT, review_response=PASS_REVIEW):
    """构造 mock LLM：gap / review 响应可注入，其余角色固定行为。"""
    def chat(messages, temperature=None):
        prompt = messages[0]["content"]
        if "研究规划器" in prompt:
            return json.dumps({"sub_questions": PLANNER}, ensure_ascii=False)
        if "网络搜索助手" in prompt:
            return json.dumps(["查询一", "查询二", "查询三"], ensure_ascii=False)
        if "事实核查助手" in prompt:
            url = _page_url_of(prompt)
            return json.dumps({"evidence": [
                {"claim": "市场规模存在量化数据", "evidence_text": _first_para(url),
                 "source_title": "T", "source_url": url}]}, ensure_ascii=False)
        if "研究写作助手" in prompt:
            m = re.search(r"\[(EV-[A-Za-z0-9-]*\d{3})\]", prompt)
            return f"小结 [{m.group(1)}]。" if m else "小结。"
        if "研究缺口分析师" in prompt:
            return json.dumps(gap_response, ensure_ascii=False)
        if "研究合著者" in prompt:
            use = list(dict.fromkeys(re.findall(r"\[(EV-[A-Za-z0-9-]*\d{3})\]",
                                                prompt)))[:2]
            cites = "、".join(f"[{i}]" for i in use)
            return json.dumps({"answer_markdown": f"## 综合结论\n\n答案 {cites}。",
                               "claims": [
                                   {"text": "2024 年市场规模约为 42 亿元",
                                    "evidence_ids": use[:1],
                                    "support_status": "supported"},
                                   {"text": "竞争格局分散暂无量化证据",
                                    "evidence_ids": [],
                                    "support_status": "supported"}]},
                              ensure_ascii=False)
        if "证据原文支持" in prompt or "独立复核员" in prompt:
            ids = re.findall(r"证据 ID: (EV-\d+)", prompt)
            return json.dumps({"status": "supported",
                               "supporting_evidence_ids": ids[:1],
                               "contradicting_evidence_ids": [],
                               "explanation": "原文一致"}, ensure_ascii=False)
        if "研究质量评审员" in prompt:
            return json.dumps(review_response, ensure_ascii=False)
        raise AssertionError("未预期的 prompt: " + prompt[:80])
    return chat


def install_mocks(gap_response=SUFFICIENT, review_response=PASS_REVIEW,
                  search=None, fetch=None):
    """安装模块级 mock；返回恢复函数。"""
    old = (v5._chat, v5.search, v5.fetch_page)
    v5._chat = make_chat(gap_response, review_response)
    v5.search = search or (lambda q: [
        {"title": "A", "url": "https://a.com/1", "content": "x" * 200},
        {"title": "B", "url": "https://b.com/2", "content": "x" * 200}])
    v5.fetch_page = fetch or (lambda url: PAGES.get(url, DEFAULT_PAGE))
    return lambda: (setattr(v5, "_chat", old[0]),
                    setattr(v5, "search", old[1]),
                    setattr(v5, "fetch_page", old[2]))


# T1: 编译
g = v7.build_graph()
nodes = {n for n in g.get_graph().nodes}
assert "__start__" in nodes and "__end__" in nodes
for name in ("planner_node", "chief_researcher_node", "researcher_node",
             "analyst_node", "reviewer_node", "writer_node", "publisher_node"):
    assert name in nodes, f"图缺少节点 {name}"
print("T1 OK: V7 StateGraph 成功编译（7 节点 + START/END）")

# T2+T3+T10+T11+T12: 正常流程 + sufficient 停止 + 校验/渲染 + 三产物 + 返回契约
# 清理旧产物
for old in glob.glob(os.path.join(v7.OUTPUT_DIR, "ai-agent-*")):
    import shutil
    shutil.rmtree(old, ignore_errors=True)

restore = install_mocks(SUFFICIENT, PASS_REVIEW)
state2, answer2 = v7.run_research("AI Agent 市场规模如何？")
restore()
assert isinstance(state2, v5.ResearchState), "返回类型与 V5 一致"
assert isinstance(answer2, str) and answer2.strip(), "答案类型与 V5 一致"
assert state2.status == "stopped" and state2.stop_reason == "sufficient"
assert state2.current_round == 1
assert state2.evidence_list, "研究应至少登记一条证据"
assert "## 结论与证据追溯" in answer2, "最终报告含 V5 渲染（追溯表）"
assert "## 引用质量说明" in answer2, "最终报告含 V5 渲染（质量说明）"
assert state2.validation_summary.get("checked", 0) >= 1, "引用链校验已执行"
# 三产物
import glob as _glob
mds = _glob.glob(os.path.join(v7.OUTPUT_DIR, "ai-agent-*/report.md"))
pdfs = _glob.glob(os.path.join(v7.OUTPUT_DIR, "ai-agent-*/report.pdf"))
docxs = _glob.glob(os.path.join(v7.OUTPUT_DIR, "ai-agent-*/report.docx"))
assert len(mds) == 1 and len(pdfs) == 1 and len(docxs) == 1, \
    f"应产出 md/pdf/docx 各一份: {len(mds)}/{len(pdfs)}/{len(docxs)}"
assert os.path.getsize(pdfs[0]) > 0, "PDF 非空"
assert os.path.getsize(docxs[0]) > 0, "DOCX 非空"
with open(mds[0], encoding="utf-8") as f:
    md_content = f.read()
assert "AI Agent" in md_content or "市场" in md_content, "md 内容为报告正文"
print(f"T2+T3+T10+T11+T12 OK: 完整流程 → sufficient 停止；校验渲染回归；"
      f"三产物就绪（md={len(md_content)}B / pdf={os.path.getsize(pdfs[0])}B / "
      f"docx={os.path.getsize(docxs[0])}B）")

# T4: 没有新增证据 → no_new_evidence
def chat_no_ev(messages, temperature=None):
    prompt = messages[0]["content"]
    if "研究规划器" in prompt:
        return json.dumps({"sub_questions": PLANNER}, ensure_ascii=False)
    if "网络搜索助手" in prompt:
        return json.dumps(["查询一"], ensure_ascii=False)
    if "事实核查助手" in prompt:
        return json.dumps({"evidence": []}, ensure_ascii=False)
    if "研究写作助手" in prompt:
        return "小结，无引用。"
    if "研究缺口分析师" in prompt:
        return json.dumps({"gaps": ["有缺口"], "conflicts": [],
                           "follow_up_questions": [{
                               "question": "继续查？", "reason": "r", "priority": "low",
                               "target_gap": "有缺口", "suggested_queries": ["q"]}],
                           "is_sufficient": False,
                           "reasoning_summary": "证据不足"}, ensure_ascii=False)
    if "研究合著者" in prompt:
        return json.dumps({"answer_markdown": "无证据结论。",
                           "claims": []}, ensure_ascii=False)
    if "证据原文支持" in prompt or "独立复核员" in prompt:
        return json.dumps({"status": "supported", "supporting_evidence_ids": [],
                           "contradicting_evidence_ids": [], "explanation": "一致"},
                          ensure_ascii=False)
    if "研究质量评审员" in prompt:
        return json.dumps({"verdict": "pass", "issues": []}, ensure_ascii=False)
    raise AssertionError("无证据场景不应进入后续提示: " + prompt[:60])


old4 = (v5._chat, v5.search, v5.fetch_page)
v5._chat = chat_no_ev
v5.search = lambda q: [{"title": "A", "url": "https://a.com/1",
                        "content": "x" * 200}]
v5.fetch_page = lambda url: PAGES.get(url, DEFAULT_PAGE)
state4, ans4 = v7.run_research("什么也没有？")
v5._chat, v5.search, v5.fetch_page = old4
assert state4.stop_reason == "no_new_evidence", \
    f"无新增证据应停止，实际: {state4.stop_reason}"
assert state4.status == "stopped"
print("T4 OK: 没有新增证据 → no_new_evidence 停止")

# T5: 达到最大轮次停止（每轮新证据 + 倾向继续 + follow-up）
_nonce5 = {"n": 0}
_ev_seen = {"n": 0}


def chat_always_more(messages, temperature=None):
    prompt = messages[0]["content"]
    if "研究规划器" in prompt:
        return json.dumps({"sub_questions": PLANNER}, ensure_ascii=False)
    if "网络搜索助手" in prompt:
        return json.dumps(["查询一"], ensure_ascii=False)
    if "事实核查助手" in prompt:
        _nonce5["n"] += 1
        nonce = _nonce5["n"]
        url = _page_url_of(prompt)
        return json.dumps({"evidence": [
            {"claim": f"新增论断{nonce}",
             "evidence_text": f"第 {nonce} 条：{_first_para(url)}",
             "source_title": "T", "source_url": url}]}, ensure_ascii=False)
    if "研究写作助手" in prompt:
        m = re.search(r"\[(EV-[A-Za-z0-9-]*\d{3})\]", prompt)
        return f"小结 [{m.group(1)}]。" if m else "小结。"
    if "研究缺口分析师" in prompt:
        _nonce5["n"] += 1
        nonce = _nonce5["n"]
        return json.dumps({"gaps": ["总是有缺口"], "conflicts": [],
                           "follow_up_questions": [{
                               "question": f"第 {nonce} 轮追加问题？",
                               "reason": "每轮都需要补充",
                               "priority": "high",
                               "target_gap": "总是有缺口",
                               "suggested_queries": [f"追加查询{nonce}"]}],
                           "is_sufficient": False,
                           "reasoning_summary": "继续"}, ensure_ascii=False)
    if "研究合著者" in prompt:
        return json.dumps({"answer_markdown": "结论 [EV-001]。",
                           "claims": []}, ensure_ascii=False)
    if "证据原文支持" in prompt or "独立复核员" in prompt:
        ids = re.findall(r"证据 ID: (EV-\d+)", prompt)
        return json.dumps({"status": "supported", "supporting_evidence_ids": ids[:1],
                           "contradicting_evidence_ids": [], "explanation": "一致"},
                          ensure_ascii=False)
    if "研究质量评审员" in prompt:
        return json.dumps({"verdict": "pass", "issues": []}, ensure_ascii=False)
    raise AssertionError("未预期的 prompt: " + prompt[:60])


old5 = (v5._chat, v5.search, v5.fetch_page)
v5._chat = chat_always_more
v5.search = lambda q: [{"title": "A", "url": "https://a.com/1",
                        "content": "x" * 200}]
v5.fetch_page = lambda url: PAGES.get(url, DEFAULT_PAGE)
state5, ans5 = v7.run_research("需要很多轮的问题？")
v5._chat, v5.search, v5.fetch_page = old5
assert state5.stop_reason == "max_rounds", \
    f"应触发 max_rounds，实际: {state5.stop_reason}"
assert state5.current_round >= v5.MAX_ROUNDS, "达到轮次上限"
rounds_in_report = [c["round"] for c in state5.completed_questions]
assert max(rounds_in_report) <= v5.MAX_ROUNDS, "任何问题轮次不得超过 MAX_ROUNDS"
assert len(set(rounds_in_report)) == v5.MAX_ROUNDS, "应实际经历 MAX_ROUNDS 轮"
print(f"T5 OK: max_rounds({v5.MAX_ROUNDS}) 停止；循环未超过轮次上限")

# T6: Replanner 生成 Follow-up → 进入第 2 轮
_nonce6 = {"n": 0}
_gap6 = {"calls": 0}


def chat_followup(messages, temperature=None):
    prompt = messages[0]["content"]
    if "研究规划器" in prompt:
        return json.dumps({"sub_questions": PLANNER}, ensure_ascii=False)
    if "网络搜索助手" in prompt:
        return json.dumps(["查询一"], ensure_ascii=False)
    if "事实核查助手" in prompt:
        _nonce6["n"] += 1
        url = _page_url_of(prompt)
        return json.dumps({"evidence": [
            {"claim": f"量化论断{_nonce6['n']}", "evidence_text": _first_para(url),
             "source_title": "T", "source_url": url}]}, ensure_ascii=False)
    if "研究写作助手" in prompt:
        m = re.search(r"\[(EV-[A-Za-z0-9-]*\d{3})\]", prompt)
        return f"小结 [{m.group(1)}]。" if m else "小结。"
    if "研究缺口分析师" in prompt:
        _gap6["calls"] += 1
        return json.dumps(GAPPY if _gap6["calls"] == 1 else SUFFICIENT,
                          ensure_ascii=False)
    if "研究合著者" in prompt:
        use = list(dict.fromkeys(re.findall(r"\[(EV-[A-Za-z0-9-]*\d{3})\]",
                                            prompt)))[:2]
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
        return json.dumps({"verdict": "pass", "issues": []}, ensure_ascii=False)
    raise AssertionError("未预期的 prompt: " + prompt[:60])


old6 = (v5._chat, v5.search, v5.fetch_page)
v5._chat = chat_followup
v5.search = lambda q: [{"title": "A", "url": "https://a.com/1",
                        "content": "x" * 200}]
v5.fetch_page = lambda url: PAGES.get(url, DEFAULT_PAGE)
state6, ans6 = v7.run_research("AI Agent 市场规模如何？")
v5._chat, v5.search, v5.fetch_page = old6
assert state6.current_round == 2, f"应经历第 2 轮（Follow-up），实际 {state6.current_round}"
fu_questions = [c for c in state6.completed_questions
                if c["question"].source == "replanner"]
assert fu_questions, "第 2 轮应研究 Follow-up 问题"
assert fu_questions[0]["round"] == 2, "Follow-up 必须在第 2 轮完成研究"
assert fu_questions[0]["result"].ok
assert state6.stop_reason == "sufficient", "第二轮后应充分"
print("T6 OK: Replanner 生成 Follow-up → 进入第 2 轮并完成研究")

# T7: 无有效 Follow-up → no_valid_followups
restore = install_mocks(NO_FOLLOWUP, PASS_REVIEW)
state7, ans7 = v7.run_research("AI Agent 市场规模如何？")
restore()
assert state7.stop_reason == "no_valid_followups", \
    f"应 no_valid_followups，实际: {state7.stop_reason}"
assert state7.current_round == 1, "不应进入第 2 轮"
print("T7 OK: 无有效 Follow-up → no_valid_followups 停止")

# T8+T9: 单子问题失败不破坏状态；注册表跨轮一致
def chat_partial_fail(messages, temperature=None):
    prompt = messages[0]["content"]
    if "研究规划器" in prompt:
        return json.dumps({"sub_questions": PLANNER}, ensure_ascii=False)
    if "网络搜索助手" in prompt:
        return json.dumps(["查询一"], ensure_ascii=False)
    if "事实核查助手" in prompt:
        url = _page_url_of(prompt)
        return json.dumps({"evidence": [
            {"claim": "量化论断", "evidence_text": _first_para(url),
             "source_title": "T", "source_url": url}]}, ensure_ascii=False)
    if "研究写作助手" in prompt:
        m = re.search(r"\[(EV-[A-Za-z0-9-]*\d{3})\]", prompt)
        return f"小结 [{m.group(1)}]。" if m else "小结。"
    if "研究缺口分析师" in prompt:
        return json.dumps(SUFFICIENT, ensure_ascii=False)
    if "研究合著者" in prompt:
        use = list(dict.fromkeys(re.findall(r"\[(EV-[A-Za-z0-9-]*\d{3})\]",
                                            prompt)))[:2]
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
        return json.dumps({"verdict": "pass", "issues": []}, ensure_ascii=False)
    raise AssertionError("未预期的 prompt: " + prompt[:60])


_search_fail_count = {"n": 0}


def flaky_search(query):
    _search_fail_count["n"] += 1
    if _search_fail_count["n"] == 1:
        raise RuntimeError("Search API 报错 429: rate limit")
    return [{"title": "A", "url": "https://a.com/1", "content": "x" * 200},
            {"title": "B", "url": "https://b.com/2", "content": "x" * 200}]


v5._chat = chat_partial_fail
v5.search = flaky_search
v5.fetch_page = lambda url: PAGES.get(url, DEFAULT_PAGE)
state8, ans8 = v7.run_research("多问题研究，一个失败？")
assert any(not c["result"].ok for c in state8.completed_questions), \
    "应至少有一个子问题失败"
assert state8.status == "stopped", "研究状态仍正常结束"
assert state8.evidence_list, "失败子问题不阻断其他子问题证据入库"
assert len(state8.source_registry) > 0 and len(state8.evidence_registry) > 0
for ev in state8.evidence_list:
    assert state8.source_registry.get(ev["source_id"]) is not None, \
        f"证据 {ev['evidence_id']} 必须关联真实来源"
eids = [e["evidence_id"] for e in state8.evidence_list]
assert len(eids) == len(set(eids)), "注册表内 evidence_id 不得重复"
print("T8+T9 OK: 单子问题失败不破坏状态；注册表一致；证据 ID 无重复")

print("\nALL V7 PHASE2 TESTS PASSED")
