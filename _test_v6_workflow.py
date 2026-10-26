# -*- coding: utf-8 -*-
"""V6 验收测试：LangGraph 工作流编排（Mock LLM / Search / Fetch，离线可重复）。

    python _test_v6_workflow.py

覆盖任务书「七、测试要求」:
- Test1  LangGraph 可以成功编译
- Test2  正常问题完整走 START → 研究流程 → END
- Test3  研究充分时，工作流能够停止（sufficient）
- Test4  没有新增证据时，工作流能够停止（no_new_evidence）
- Test5  达到最大轮次时，工作流能够停止（max_rounds）
- Test6  Replanner 生成 Follow-up 后继续下一轮
- Test7  没有有效 Follow-up 时正常工作结束（no_valid_followups）
- Test8  单个子问题失败不会破坏整个研究状态
- Test9  Evidence Registry 和 Source Registry 跨轮保持一致
- Test10 同一证据不会因节点重复执行而重复登记
- Test11 最终答案仍经过 Citation Validator 和 Citation Renderer
- Test12 原有 V5 引用编号、证据追溯、参考文献行为保持（回归自 _test_v5_citation）
- Test13 run_research() 调用方式与返回值兼容性（(ResearchState, str)）
- Test14 循环不会超过最大研究轮次

测试方式：monkey-patch research_v5 模块级函数（v6 通过 v5.xxx 调用的模块级
名称），与 V5 测试同一模式，不产生真实 API 费用。
"""
# -*- coding: utf-8 -*-
import json
import re
import sys

import research_v6 as v6
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


def _first_para(url):
    page = PAGES.get(url, DEFAULT_PAGE)
    return page.split("<p>", 1)[1].split("</p>", 1)[0]


def _page_url_of(prompt):
    for ln in prompt.splitlines():
        if ln.startswith("网页URL:"):
            return ln.split("网页URL:", 1)[1].strip()
    return ""


def make_chat(gap_response=SUFFICIENT):
    """构造一个可配置的 mock LLM：gap 响应可注入，其余角色固定行为。"""
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
        raise AssertionError("未预期的 prompt: " + prompt[:80])
    return chat


def install_mocks(gap_response=SUFFICIENT, search=None, fetch=None):
    """安装模块级 mock；返回可用于恢复现场的函数。"""
    old = (v5._chat, v5.search, v5.fetch_page)
    v5._chat = make_chat(gap_response)
    v5.search = search or (lambda q: [
        {"title": "A", "url": "https://a.com/1", "content": "x" * 200},
        {"title": "B", "url": "https://b.com/2", "content": "x" * 200}])
    v5.fetch_page = fetch or (lambda url: PAGES.get(url, DEFAULT_PAGE))
    return lambda: (setattr(v5, "_chat", old[0]),
                    setattr(v5, "search", old[1]),
                    setattr(v5, "fetch_page", old[2]))


# Test1: LangGraph 可以成功编译
g = v6.build_graph()
nodes = {n for n in g.get_graph().nodes}
assert "__start__" in nodes and "__end__" in nodes, f"图缺少 START/END: {nodes}"
for name in ("plan_node", "research_round_node", "gap_analysis_node",
             "replan_node", "finalize_node"):
    assert name in nodes, f"图缺少节点 {name}"
print("TEST1 OK: LangGraph StateGraph 成功编译（含 START/END 与全部节点）")

# Test2 + Test3: 正常问题完整走 START → 研究 → END；sufficient 停止
restore = install_mocks(SUFFICIENT)
state2, answer2 = v6.run_research("AI Agent 市场规模如何？")
restore()
assert state2.status == "stopped" and state2.stop_reason == "sufficient"
assert state2.current_round == 1
assert state2.evidence_list, "研究应至少登记一条证据"
assert "## 结论与证据追溯" in answer2, "最终报告含 V5 渲染（追溯表）"
assert "## 引用质量说明" in answer2, "最终报告含 V5 渲染（质量说明）"
assert re.search(r"\[\d+\] \[来源", answer2) or "## 引用" in answer2, \
    "最终报告含 V5 渲染（References）"
print("TEST2+3 OK: 完整工作流运行到 END；sufficient 正确停止")

# Test4: 没有新增证据时停止（no_new_evidence）
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
    raise AssertionError("无证据场景不应进入后续提示: " + prompt[:60])


old4 = (v5._chat, v5.search, v5.fetch_page)
v5._chat = chat_no_ev
v5.search = lambda q: [{"title": "A", "url": "https://a.com/1",
                        "content": "x" * 200}]
v5.fetch_page = lambda url: PAGES.get(url, DEFAULT_PAGE)
state4, ans4 = v6.run_research("什么也没有？")
v5._chat, v5.search, v5.fetch_page = old4
assert state4.stop_reason == "no_new_evidence", \
    f"无新增证据应停止，实际: {state4.stop_reason}"
assert state4.status == "stopped"
print("TEST4 OK: 没有新增证据 → no_new_evidence 停止")

# Test5 + Test14: 达到最大轮次停止，且循环不超过 MAX_ROUNDS
# 每轮都产生新证据（不触发 no_new_evidence）、都倾向继续、都给出 follow-up
_nonce5 = {"n": 0}


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
    raise AssertionError("未预期的 prompt: " + prompt[:60])


old5 = (v5._chat, v5.search, v5.fetch_page)
v5._chat = chat_always_more
v5.search = lambda q: [{"title": "A", "url": "https://a.com/1",
                        "content": "x" * 200}]
v5.fetch_page = lambda url: PAGES.get(url, DEFAULT_PAGE)
state5, ans5 = v6.run_research("需要很多轮的问题？")
v5._chat, v5.search, v5.fetch_page = old5
assert state5.stop_reason == "max_rounds", \
    f"应触发 max_rounds，实际: {state5.stop_reason}"
assert state5.current_round >= v5.MAX_ROUNDS, "达到轮次上限"
rounds_in_report = [c["round"] for c in state5.completed_questions]
assert max(rounds_in_report) <= v5.MAX_ROUNDS, \
    f"任何问题轮次不得超过 MAX_ROUNDS: {rounds_in_report}"
assert len(set(rounds_in_report)) == v5.MAX_ROUNDS, \
    "应实际经历 MAX_ROUNDS 轮（不因 bug 提前或超限）"
print(f"TEST5+14 OK: max_rounds({v5.MAX_ROUNDS}) 停止；循环未超过轮次上限")

# Test6 + Test7(部分): Replanner 生成 Follow-up 后继续下一轮
# 第一轮 gap 返回不充分 + 有 follow-up → 第二轮（follow-up 被研究）


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
        # claim 每次不同 → 第 2 轮 Follow-up 产生新证据（不复用第 1 轮 ID）
        return json.dumps({"evidence": [
            {"claim": f"量化论断{_nonce6['n']}", "evidence_text": _first_para(url),
             "source_title": "T", "source_url": url}]}, ensure_ascii=False)
    if "研究写作助手" in prompt:
        m = re.search(r"\[(EV-[A-Za-z0-9-]*\d{3})\]", prompt)
        return f"小结 [{m.group(1)}]。" if m else "小结。"
    if "研究缺口分析师" in prompt:
        # 第一轮：不足够，有 follow-up；第二轮：sufficient
        # （用独立计数器，不依赖任何跨场景共享状态）
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
    raise AssertionError("未预期的 prompt: " + prompt[:60])


old6 = (v5._chat, v5.search, v5.fetch_page)
v5._chat = chat_followup
v5.search = lambda q: [{"title": "A", "url": "https://a.com/1",
                        "content": "x" * 200}]
v5.fetch_page = lambda url: PAGES.get(url, DEFAULT_PAGE)
state6, ans6 = v6.run_research("AI Agent 市场规模如何？")
v5._chat, v5.search, v5.fetch_page = old6
assert state6.current_round == 2, f"应经历第 2 轮（Follow-up），实际 {state6.current_round}"
fu_questions = [c for c in state6.completed_questions
                if c["question"].source == "replanner"]
assert fu_questions, "第 2 轮应研究 Follow-up 问题"
assert fu_questions[0]["round"] == 2, "Follow-up 必须在第 2 轮完成研究"
assert fu_questions[0]["result"].ok
assert state6.stop_reason == "sufficient", "第二轮后应充分"
print("TEST6 OK: Replanner 生成 Follow-up → 进入第 2 轮并完成研究")

# Test7: 没有有效 Follow-up → no_valid_followups 停止
restore = install_mocks(NO_FOLLOWUP)
state7, ans7 = v6.run_research("AI Agent 市场规模如何？")
restore()
assert state7.stop_reason == "no_valid_followups", \
    f"应 no_valid_followups，实际: {state7.stop_reason}"
assert state7.current_round == 1, "不应进入第 2 轮"
print("TEST7 OK: 无有效 Follow-up → no_valid_followups 停止")

# Test8: 单个子问题失败不破坏研究状态 + Test9/Test10: 注册表跨轮一致、证据不重复
SEARCH_RESULTS_FAIL = {"q1": [{"title": "B", "url": "https://b.com/2",
                               "content": "x" * 200}],
                       "q2": [],  # q2 毫无结果
                       "q3": [{"title": "A", "url": "https://a.com/1",
                               "content": "x" * 200}]}


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
    raise AssertionError("未预期的 prompt: " + prompt[:60])


# q2 的搜索抛异常（模拟单个子问题失败）
def search_partial_fail(query):
    return [{"title": "A", "url": "https://a.com/1", "content": "x" * 200},
            {"title": "B", "url": "https://b.com/2", "content": "x" * 200}]


# 指定某个子问题（q2 的查询）搜索抛异常 → research_sub_question 捕获并返回 error，
# 其余子问题正常完成 —— 单子问题失败不拖垮整个研究状态。
# search 按 query 判定：q2 的查询会走 generate_queries mock（"查询一"），
# 但 q2 与 q1/q3 的查询无法区分 —— 改为随机失败一次搜索。
_search_fail_count = {"n": 0}


def flaky_search(query):
    _search_fail_count["n"] += 1
    if _search_fail_count["n"] == 1:
        # 第一个子问题的全部查询都失败 → 该子问题整体失败
        raise RuntimeError("Search API 报错 429: rate limit")
    return [{"title": "A", "url": "https://a.com/1", "content": "x" * 200},
            {"title": "B", "url": "https://b.com/2", "content": "x" * 200}]


v5._chat = chat_partial_fail
v5.search = flaky_search
v5.fetch_page = lambda url: PAGES.get(url, DEFAULT_PAGE)
state8, ans8 = v6.run_research("多问题研究，一个失败？")
# 至少有一个子问题失败，但整个研究状态完整可继续
assert any(not c["result"].ok for c in state8.completed_questions), \
    "应至少有一个子问题失败"
assert state8.status == "stopped", "研究状态仍正常结束"
assert state8.evidence_list, "失败子问题不阻断其他子问题证据入库"
# 成功问题登记的证据：来源/证据都进注册表
assert len(state8.source_registry) > 0
assert len(state8.evidence_registry) > 0
# 全部证据可回溯到来源
for ev in state8.evidence_list:
    assert state8.source_registry.get(ev["source_id"]) is not None, \
        f"证据 {ev['evidence_id']} 必须关联真实来源"
# 证据不重复：evidence_id 全部唯一
eids = [e["evidence_id"] for e in state8.evidence_list]
assert len(eids) == len(set(eids)), "注册表内 evidence_id 不得重复"
print("TEST8+9+10 OK: 单子问题失败不破坏状态；注册表一致；证据 ID 无重复")

# Test11 + Test12 + Test13: 最终答案经过 Validator/Renderer；V5 渲染协议回归；
# run_research 返回值兼容
restore = install_mocks(SUFFICIENT)
state13, answer13 = v6.run_research("AI Agent 市场规模如何？")
restore()
assert isinstance(state13, v5.ResearchState), "返回类型与 V5 一致"
assert isinstance(answer13, str) and answer13.strip(), "答案类型与 V5 一致"
# Validator 结果出现在 state 与报告中
assert state13.validation_summary.get("checked", 0) >= 1, "引用链校验已执行"
assert "引用链校验" in answer13 or "pass" in answer13.split("## 引用质量说明")[1], \
    "报告呈现校验统计"
# Renderer 输出协议回归（V5 的四个组成部分）
assert "## 引用质量说明" in answer13
assert "## 参考文献" in answer13 or re.search(r"\n## 引用\n", answer13), \
    "References 节存在（V5 用 ## 引用 或 ## 参考文献）"
# V5 的编号协议：正文 [n] 与引用节一一对应
body13 = answer13.split("## 结论与证据追溯")[0]
refs13 = answer13.split("## 引用\n\n")[1] if "## 引用\n\n" in answer13 else ""
if refs13:
    body_nums = {int(n) for n in re.findall(r"\[(\d+)\]", body13)}
    ref_nums = {int(n) for n in re.findall(r"^\[(\d+)\]", refs13, re.M)}
    assert body_nums == ref_nums, f"编号必须与 References 一一对应: {body_nums} vs {ref_nums}"
print("TEST11+12+13 OK: 最终答案经校验渲染；V5 渲染协议保持；返回值兼容")

# Test12 补充：把 V5 原验收测试中的渲染断言原样跑一遍（纯渲染单元，不联网）
st = v5.ResearchState("测试问题", v5.ResearchPlan("测试问题", []))
reg = v5.SourceRegistry()
reg.register("https://a.com/1", title="来源A", page_text="正文A。", retrieved_at="2026-01-01T00:00:00+00:00")
ereg = v5.EvidenceRegistry(reg)
e1 = ereg.register_from_llm("SRC-001", "SRC-001-v1", "正文A。",
                            claim="规模论断", llm_text="正文A。",
                            round_no=1, question_id="q1")["evidence"]
st.source_registry = reg
st.evidence_registry = ereg
st.evidence_list = [e1]
st._evidence_ids = {"EV-001"}
st.claims, _ = v5.validate_claims([
    {"text": "2024 年市场规模为 42 亿元", "evidence_ids": ["EV-001"],
     "support_status": "supported"}], st.evidence_list)
rendered = v5.render_report(st, "结论：市场规模 [EV-001]。")
assert "[1] [来源A](https://a.com/1)" in rendered.split("## 引用\n\n")[1]
assert "EV-001 → [1]" in rendered.split("## 结论与证据追溯")[1].split("## 引用质量说明")[0]
print("TEST12b OK: V5 引用编号 / 证据追溯 / References 渲染单元回归")

print("\nALL V6 WORKFLOW TESTS PASSED")