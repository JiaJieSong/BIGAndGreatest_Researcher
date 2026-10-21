# -*- coding: utf-8 -*-
"""V5 Phase G 验收测试：Citation System 12 项覆盖（Mock LLM / Search / Fetch，离线可重复）。

    python _test_v5_citation.py

对应任务书「九、Phase G：测试」的 12 项要求:
- Test1  合法 Claim 和 Evidence 可以成功生成引用
- Test2  不存在的 Evidence ID 会被拒绝
- Test3  不存在的 Source ID 会被拒绝
- Test4  虚构原文片段会被标记
- Test5  Claim 与原文语义不符时会被标记
- Test6  第一轮 Evidence 能在最终报告中被引用
- Test7  多个 Claim 可以共享同一个 Source
- Test8  重复来源不会生成重复的参考文献条目
- Test9  不同来源之间的矛盾不会被简单覆盖
- Test10 最终报告中的所有编号都能映射到 References
- Test11 引用校验失败不会被静默忽略
- Test12 搜索或抓取失败不会导致虚构引用
"""
import json
import re
import sys

import research_v5 as v5

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

PAGE_A = ("第一段介绍背景。\n"
          "第二段包含关键数据：2024 年市场规模为 42 亿元，同比增长 18%。\n"
          "第三段是结论性描述。")
PAGE_B = "另一来源的独立正文。增长率为 18%，与前者一致。"
TIME = "2026-01-01T00:00:00+00:00"


def build_state():
    """SRC-001(A) 与 SRC-002(B)：EV-001/EV-002 同属 A，EV-003 属 B，均原文命中。"""
    reg = v5.SourceRegistry()
    reg.register("https://a.com/1", title="来源A", page_text=PAGE_A, retrieved_at=TIME)
    reg.register("https://b.com/1", title="来源B", page_text=PAGE_B, retrieved_at=TIME)
    ereg = v5.EvidenceRegistry(reg)
    e1 = ereg.register_from_llm("SRC-001", "SRC-001-v1", PAGE_A,
                                claim="规模论断", llm_text="2024 年市场规模为 42 亿元",
                                round_no=1, question_id="q1")["evidence"]
    e2 = ereg.register_from_llm("SRC-001", "SRC-001-v1", PAGE_A,
                                claim="增长论断A", llm_text="同比增长 18%",
                                round_no=1, question_id="q1")["evidence"]
    e3 = ereg.register_from_llm("SRC-002", "SRC-002-v1", PAGE_B,
                                claim="增长论断B", llm_text="增长率为 18%",
                                round_no=1, question_id="q1")["evidence"]
    plan = v5.ResearchPlan("测试问题", [v5.SubQuestion("q1", "问题一")])
    st = v5.ResearchState("测试问题", plan,
                          source_registry=reg, evidence_registry=ereg)
    st.evidence_list.extend([e1, e2, e3])
    st._evidence_ids.update(("EV-001", "EV-002", "EV-003"))
    return st


def semantic_supported(messages, temperature=None):
    """语义校验 mock：引用块里列出的第一条证据支持该结论。"""
    ids = re.findall(r"证据 ID: (EV-\d+)", messages[0]["content"])
    return json.dumps({"status": "supported",
                       "supporting_evidence_ids": ids[:1],
                       "contradicting_evidence_ids": [],
                       "explanation": "原文一致"}, ensure_ascii=False)


# ---------- Test1: 合法 Claim 和 Evidence 成功生成引用 ----------
st = build_state()
st.claims, _ = v5.validate_claims([
    {"text": "2024 年市场规模为 42 亿元", "evidence_ids": ["EV-001"],
     "support_status": "supported"}], st.evidence_list)
v5._chat = semantic_supported
v5.validate_citation_chain(st, second_pass=False)
rendered = v5.render_report(st, "结论：市场规模为 42 亿元 [EV-001]。")
body = rendered.split("## 结论与证据追溯")[0]
assert "[1]" in body, "合法链应生成正文编号"
refs = rendered.split("## 引用\n\n")[1]
assert "[1] [来源A](https://a.com/1)" in refs, "应生成 Reference 条目"
assert st.validation_summary["pass"] == 1
print("TEST1 OK: 合法 Claim+Evidence → 成功生成引用编号与参考文献")

# ---------- Test2: 不存在的 Evidence ID 被拒绝 ----------
st = build_state()
claims, stats = v5.validate_claims([
    {"text": "引用了不存在的证据", "evidence_ids": ["EV-404"],
     "support_status": "supported"}], st.evidence_list)
assert claims[0]["evidence_ids"] == []
assert claims[0]["invalid_evidence_ids"] == ["EV-404"]
assert claims[0]["support_status"] == "unsupported"
assert stats["invalid_evidence_refs"] == 1, "Phase D 记录无效引用"
# 绕过 Phase D 直接构造 → Phase E 拒绝
st.claims = [dict(claims[0], evidence_ids=["EV-404"], invalid_evidence_ids=[])]
r_id = v5.check_ids(st.claims[0], st.evidence_registry, st.source_registry)
assert r_id["status"] == "fail" and "EV-404" in r_id["missing_evidence_ids"]
# Phase F：正文引用不存在 ID → 抹掉、不产生编号与引用节
rendered = v5.render_report(st, "引用 [EV-404] 的断言。")
body = rendered.split("## 结论与证据追溯")[0]
assert "[EV-404]" not in body and "[1]" not in body
assert "\n\n## 引用\n\n" not in rendered, "无有效来源不得生成引用节"
print("TEST2 OK: 不存在的 Evidence ID 在校验/检查/渲染三层都被拒绝")

# ---------- Test3: 不存在的 Source ID 被拒绝 ----------
st = build_state()
ereg = st.evidence_registry
assert ereg.register("SRC-404", claim="x", quote="y") is None, "登记层拒绝"
assert ereg.register("", claim="x", quote="y") is None
ereg.evidence["EV-901"] = {"id": "EV-901", "evidence_id": "EV-901",
                           "source_id": "SRC-404", "version_id": "SRC-404-v1",
                           "quote": "x"}
ghost_claim = {"claim_id": "CLM-X", "text": "t", "evidence_ids": ["EV-901"],
               "invalid_evidence_ids": [], "support_status": "supported",
               "source_ids": ["SRC-404"]}
r1 = v5.check_ids(ghost_claim, ereg, st.source_registry)
assert r1["status"] == "fail" and "EV-901" in r1["missing_source_evidence_ids"]
r2 = v5.check_urls(ghost_claim, st.source_registry)
assert r2["status"] == "fail" and r2["issues"][0]["problem"] == "来源不存在"
print("TEST3 OK: 不存在的 Source ID 在登记 / ID 检查 / URL 检查都被拒绝")

# ---------- Test4: 虚构原文片段会被标记 ----------
st = build_state()
ereg = st.evidence_registry
fake = ereg.register_from_llm("SRC-001", "SRC-001-v1", PAGE_A,
                              claim="虚构论断", llm_text="这句话根本不在原文里",
                              round_no=1, question_id="q1")["evidence"]
assert fake["quote_verified"] is False, "虚构原文不得标为已验证"
assert fake["extraction_method"] == "llm_paraphrase"
assert fake["quote"] == "这句话根本不在原文里", "概括原样保存，不冒充原文"
# Phase E 复验：声称原文但复验未命中 → fail
claim = {"claim_id": "CLM-F", "text": "t", "evidence_ids": ["EV-001"],
         "invalid_evidence_ids": [], "support_status": "supported",
         "source_ids": ["SRC-001"]}
ev1 = ereg.get("EV-001")
orig_quote = ev1["quote"]
ev1["quote"] = "被篡改后不在正文里的片段"
rq = v5.check_quotes(claim, st.evidence_registry, st.source_registry)
assert rq["status"] == "fail" and "复验未命中" in rq["issues"][0]["problem"]
ev1["quote"] = orig_quote
# paraphrase 证据 → warn（可见但不判死）
rq2 = v5.check_quotes(dict(claim, evidence_ids=["EV-004"]),
                      st.evidence_registry, st.source_registry)
assert rq2["status"] == "warn" and "概括" in rq2["issues"][0]["problem"]
print("TEST4 OK: 虚构原文标记 paraphrase；复验未命中 fail、概括 warn")

# ---------- Test5: Claim 与原文语义不符会被标记 ----------
st = build_state()
st.claims, _ = v5.validate_claims([
    {"text": "2024 年市场规模为 42 亿元", "evidence_ids": ["EV-001"],
     "support_status": "supported"}], st.evidence_list)


def semantic_contradicted(messages, temperature=None):
    ids = re.findall(r"证据 ID: (EV-\d+)", messages[0]["content"])
    return json.dumps({"status": "contradicted",
                       "supporting_evidence_ids": [],
                       "contradicting_evidence_ids": ids[:1],
                       "explanation": "原文数据与结论直接矛盾"},
                      ensure_ascii=False)


v5._chat = semantic_contradicted
reports, summary = v5.validate_citation_chain(st, second_pass=False)
assert st.claims[0]["support_status"] == "contradicted", "语义不符应被标记降级"
assert summary["fail"] >= 1
rep = reports[0]
assert rep["semantic_check"]["status"] == "contradicted"
assert rep["semantic_check"]["contradicting_evidence_ids"] == ["EV-001"]
assert rep["semantic_check"]["explanation"], "必须带结构化 explanation"
rendered = v5.render_report(st, "结论 [EV-001]。")
assert "引用链校验未通过" in rendered, "fail 必须在报告中显式呈现"
print("TEST5 OK: 语义不符 → contradicted 标记 + 报告 fail 明细")

# ---------- Test6: 第一轮 Evidence 能在最终报告中被引用 ----------
PAGES_E2E = {
    "https://a.com/1": "<html><body><p>A 页第一段：2024 年市场规模为 42 亿元。</p>"
                       "<p>A 页第二段：增速平稳。</p></body></html>",
    "https://b.com/2": "<html><body><p>B 页第一段：另有机构估计为 51 亿元。</p>"
                       "<p>B 页第二段：口径不同。</p></body></html>",
}
DEFAULT_PAGE = "<html><body><p>默认正文。</p></body></html>"
PLANNER = [{"id": "q1", "question": "问题一？"},
           {"id": "q2", "question": "问题二？"},
           {"id": "q3", "question": "问题三？"}]


def first_para(url):
    page = PAGES_E2E.get(url, DEFAULT_PAGE)
    return page.split("<p>", 1)[1].split("</p>", 1)[0]


def _page_url_of(prompt):
    for ln in prompt.splitlines():
        if ln.startswith("网页URL:"):
            return ln.split("网页URL:", 1)[1].strip()
    return ""


def e2e_chat(messages, temperature=None):
    prompt = messages[0]["content"]
    if "研究规划器" in prompt:
        return json.dumps({"sub_questions": PLANNER}, ensure_ascii=False)
    if "网络搜索助手" in prompt:
        return json.dumps(["查询一", "查询二", "查询三"], ensure_ascii=False)
    if "事实核查助手" in prompt:
        url = _page_url_of(prompt)
        return json.dumps({"evidence": [
            {"claim": "市场规模存在量化数据", "evidence_text": first_para(url),
             "source_title": "T", "source_url": url}]}, ensure_ascii=False)
    if "研究写作助手" in prompt:
        m = re.search(r"\[(EV-[A-Za-z0-9-]*\d{3})\]", prompt)
        return f"小结 [{m.group(1)}]。" if m else "小结。"
    if "研究缺口分析师" in prompt:
        return json.dumps({"gaps": [], "conflicts": [], "follow_up_questions": [],
                           "is_sufficient": True, "reasoning_summary": "足够"},
                          ensure_ascii=False)
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
    if "证据原文支持" in prompt:
        ids = re.findall(r"证据 ID: (EV-\d+)", prompt)
        return json.dumps({"status": "supported",
                           "supporting_evidence_ids": ids[:1],
                           "contradicting_evidence_ids": [],
                           "explanation": "原文一致"}, ensure_ascii=False)
    if "独立复核员" in prompt:
        ids = re.findall(r"证据 ID: (EV-\d+)", prompt)
        return json.dumps({"status": "supported",
                           "supporting_evidence_ids": ids[:1],
                           "contradicting_evidence_ids": [],
                           "explanation": "复核一致"}, ensure_ascii=False)
    raise AssertionError("未预期的 prompt: " + prompt[:80])


v5._chat = e2e_chat
v5.search = lambda q: [{"title": "A", "url": "https://a.com/1", "content": "x" * 200},
                       {"title": "B", "url": "https://b.com/2", "content": "x" * 200}]
v5.fetch_page = lambda url: PAGES_E2E.get(url, DEFAULT_PAGE)
state6, answer6 = v5.run_research("AI Agent 市场规模如何？")
assert state6.stop_reason == "sufficient" and state6.current_round == 1
ev_first = state6.evidence_list[0]
assert ev_first["first_seen"]["round"] == 1, "证据确为第一轮登记"
refs6 = answer6.split("## 引用\n\n")[1]
assert "https://a.com/1" in refs6, "第一轮来源出现在最终 References"
assert "EV-001 → [1]" in answer6, "第一轮证据出现在结论追溯表"
assert re.search(r"\[1\]", answer6.split("## 结论与证据追溯")[0])
print("TEST6 OK: 第一轮 Evidence 被最终报告的编号/追溯表/References 引用")

# ---------- Test7: 多个 Claim 可以共享同一个 Source ----------
st = build_state()
st.claims, _ = v5.validate_claims([
    {"text": "2024 年市场规模为 42 亿元", "evidence_ids": ["EV-001"],
     "support_status": "supported"},
    {"text": "同比增长 18%（同一来源另一证据）", "evidence_ids": ["EV-002"],
     "support_status": "supported"},
], st.evidence_list)
rendered = v5.render_report(st, "正文引 [EV-001]，再引 [EV-002]。")
assert "EV-001 → [1]" in rendered and "EV-002 → [1]" in rendered, \
    "两个 Claim 引用同一 Source 应共用编号 [1]"
refs = rendered.split("## 引用\n\n")[1]
assert len(refs.strip().splitlines()) == 1, "共用编号 → Reference 恰一条"
assert refs.startswith("[1] [来源A]")
print("TEST7 OK: 多个 Claim 共享同一 Source，共用同一编号且不重复")

# ---------- Test8: 重复来源不会生成重复的参考文献条目 ----------
st = build_state()
st.claims, _ = v5.validate_claims([
    {"text": "结论一", "evidence_ids": ["EV-001", "EV-002"],
     "support_status": "supported"},
    {"text": "结论二", "evidence_ids": ["EV-002", "EV-001"],
     "support_status": "supported"},
], st.evidence_list)
rendered = v5.render_report(st, "引用 [EV-001]、[EV-002] 与 [EV-001, EV-002]。")
refs = rendered.split("## 引用\n\n")[1]
lines = refs.strip().splitlines()
assert len(lines) == 1, f"同源两条证据 + 两个 Claim → Reference 仍恰一条: {lines}"
assert lines[0].startswith("[1] [来源A]")
# 未被引用的来源绝不出现
st2 = build_state()
st2.claims = []
rendered2 = v5.render_report(st2, "只引 [EV-003]。")
refs2 = rendered2.split("## 引用\n\n")[1]
assert len(refs2.strip().splitlines()) == 1
assert "b.com" in refs2 and "a.com" not in refs2
print("TEST8 OK: 重复来源不生成重复条目；未被引用的来源不列")

# ---------- Test9: 不同来源之间的矛盾不会被简单覆盖 ----------
st = build_state()
# GapAnalyzer 已标记 A/B 两源口径冲突（涉及两方的证据）
st.identified_conflicts = [{"description": "A/B 两源市场规模口径冲突",
                            "evidence_ids": ["EV-001", "EV-003"]}]
st.claims, _ = v5.validate_claims([
    {"text": "A 源口径：2024 年 42 亿元", "evidence_ids": ["EV-001"],
     "support_status": "supported"},
    {"text": "B 源口径：2024 年 51 亿元", "evidence_ids": ["EV-003"],
     "support_status": "supported"},
], st.evidence_list)
v5._chat = semantic_supported
reports, summary = v5.validate_citation_chain(st, second_pass=False)
# ① 矛盾不得覆盖：两方证据与两个 Claim 都保留
assert len(st.evidence_list) == 3, "冲突不得丢弃任何一方证据"
assert st.claims[0]["support_status"] != "unsupported"
assert st.claims[1]["support_status"] != "unsupported"
# ② 冲突被标记而非静默（涉及冲突证据的 Claim 至少 warn）
c1 = next(r for r in reports if r["claim_id"] == "CLM-001")
assert c1["conflict_check"]["status"] == "warn"
assert set(c1["conflict_check"]["conflicting_evidence_ids"]) & {"EV-001", "EV-003"}
assert c1["conflict_check"]["descriptions"] == ["A/B 两源市场规模口径冲突"]
assert summary["conflict_claims"] >= 1
# ③ 报告显式呈现冲突，且两源都进 References（双方都可见）
rendered = v5.render_report(st, "A 说 [EV-001]，B 说 [EV-003]。")
assert "存在冲突证据" in rendered, "冲突必须出现在质量说明"
refs = rendered.split("## 引用\n\n")[1]
assert "a.com" in refs and "b.com" in refs, "矛盾双方的来源都不得被吞掉"
print("TEST9 OK: 跨来源矛盾被标记呈现，两方证据与引用都不被覆盖")

# ---------- Test10: 最终报告中的所有编号都能映射到 References ----------
# 复用 Test6 的端到端报告（state6 / answer6）
body10 = answer6.split("## 结论与证据追溯")[0]
trace_section = answer6.split("## 结论与证据追溯", 1)[1].split("## 引用质量说明")[0]
refs10 = answer6.split("## 引用\n\n")[1]
body_nums = {int(n) for n in re.findall(r"\[(\d+)\]", body10)}
trace_nums = {int(n) for n in re.findall(r"→ \[(\d+)\]", trace_section)}
ref_nums = {int(n) for n in re.findall(r"^\[(\d+)\]", refs10, re.M)}
assert body_nums, "正文应含编号"
assert body_nums == ref_nums, f"正文编号 {body_nums} 与 References {ref_nums} 必须一一对应"
assert trace_nums <= ref_nums, f"追溯表编号 {trace_nums} 也必须有对应 References"
assert ref_nums == set(range(1, len(ref_nums) + 1)), "编号应从 1 连续递增无空洞"
print("TEST10 OK: 正文/追溯表编号全部映射 References，编号连续无孤儿")

# ---------- Test11: 引用校验失败不会被静默忽略 ----------
st = build_state()
st.claims, _ = v5.validate_claims([
    {"text": "2024 年市场规模为 42 亿元", "evidence_ids": ["EV-001"],
     "support_status": "supported"},
    {"text": "编造引用的结论 2030", "evidence_ids": ["EV-999"],
     "support_status": "supported"},
    {"text": "语义不符的结论", "evidence_ids": ["EV-003"],
     "support_status": "supported"},
], st.evidence_list)


def sem_mix(messages, temperature=None):
    prompt = messages[0]["content"]
    ids = re.findall(r"证据 ID: (EV-\d+)", prompt)
    if "语义不符的结论" in prompt:
        return json.dumps({"status": "contradicted",
                           "supporting_evidence_ids": [],
                           "contradicting_evidence_ids": ids[:1],
                           "explanation": "原文不支持该结论"}, ensure_ascii=False)
    return json.dumps({"status": "supported", "supporting_evidence_ids": ids[:1],
                       "contradicting_evidence_ids": [], "explanation": "一致"},
                      ensure_ascii=False)


v5._chat = sem_mix
reports, summary = v5.validate_citation_chain(st, second_pass=False)
assert summary["fail"] >= 1, "fail 统计不得丢失"
assert summary["invalid_refs"] == 1, "无效引用计数不得丢失"
rendered = v5.render_report(st, "正文 [EV-001] 与 [EV-003]。")
assert "## 引用质量说明" in rendered
quality = rendered.split("## 引用质量说明", 1)[1]
assert "fail" in quality, "fail 统计必须出现在报告"
assert "共剔除不存在的证据引用 1 处" in quality, "无效引用剔除必须披露"
assert "⚠ CLM-002" in quality and "剔除无效引用 EV-999" in quality
assert "引用链校验未通过" in quality, "语义 fail 的结论必须逐条列出"
print("TEST11 OK: 校验失败在报告质量说明与统计中显式呈现，不静默")

# ---------- Test12: 搜索或抓取失败不会导致虚构引用 ----------
def fail_e2e_chat(messages, temperature=None):
    prompt = messages[0]["content"]
    if "研究规划器" in prompt:
        return json.dumps({"sub_questions": PLANNER}, ensure_ascii=False)
    if "网络搜索助手" in prompt:
        return json.dumps(["查询一"], ensure_ascii=False)
    if "事实核查助手" in prompt:
        return json.dumps({"evidence": [
            {"claim": "c", "evidence_text": "t",
             "source_title": "T", "source_url": ""}]}, ensure_ascii=False)
    if "研究写作助手" in prompt:
        return "小结，无引用。"
    if "研究缺口分析师" in prompt:
        return json.dumps({"gaps": [], "conflicts": [], "follow_up_questions": [],
                           "is_sufficient": True, "reasoning_summary": "足够"},
                          ensure_ascii=False)
    if "研究合著者" in prompt:
        # LLM 明知没有证据仍试图引用 —— 程序必须拒绝
        return json.dumps({"answer_markdown": "答案引用 [EV-999] 与 [EV-888]。",
                           "claims": [{"text": "凭空引用的结论 2031",
                                       "evidence_ids": ["EV-999"],
                                       "support_status": "supported"}]},
                          ensure_ascii=False)
    raise AssertionError("未预期的 prompt: " + prompt[:80])


# 场景 A：搜索抛异常
def bad_search(query):
    raise RuntimeError("Search API 报错 429: rate limit")


v5._chat = fail_e2e_chat
v5.search = bad_search
v5.fetch_page = lambda url: DEFAULT_PAGE
state_a, answer_a = v5.run_research("搜索完全失败的问题？")
assert state_a.evidence_list == [], "搜索失败不得产生任何证据"
assert len(state_a.source_registry) == 0 and len(state_a.evidence_registry) == 0
assert "[EV-999]" not in answer_a and "[EV-888]" not in answer_a, "内部引用不得泄漏"
assert "[1]" not in answer_a, "无证据不得生成任何编号"
assert "\n\n## 引用\n\n" not in answer_a, "无证据不得生成引用节（不虚构来源）"
quality_a = answer_a.split("## 引用质量说明", 1)[1]
assert "共剔除不存在的证据引用 1 处" in quality_a, "编造引用被剔除且披露"
assert "⚠ CLM-001" in quality_a

# 场景 B：搜索正常但抓取抛异常
v5.search = lambda q: [{"title": "A", "url": "https://a.com/1",
                        "content": "x" * 200}]


def bad_fetch(url):
    raise RuntimeError("fetch failed: 503 Service Unavailable")


v5.fetch_page = bad_fetch
state_b, answer_b = v5.run_research("抓取完全失败的问题？")
assert state_b.evidence_list == [], "抓取失败不得产生任何证据"
assert "[1]" not in answer_b
assert "\n\n## 引用\n\n" not in answer_b, "抓取失败同样不虚构引用"
print("TEST12 OK: 搜索/抓取失败 → 零证据、零编号、零引用节，编造引用被剔除披露")

print("ALL V5 PHASE G CITATION TESTS PASSED")