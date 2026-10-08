# -*- coding: utf-8 -*-
"""V3 端到端离线 dry-run：mock 掉 LLM / 搜索 / 抓取，验证真实代码路径与终端输出格式。

不访问网络、不消耗 API。
    python _test_v3_dryrun.py
"""
import json
import re
import sys

import research_v3 as v3

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# 只匹配真实证据 ID（以 3 位序号结尾），避免匹配到 prompt 里的占位说明文字
_CITE_RE = re.compile(r"\[(EV-(?:[A-Za-z0-9]+-)?\d{3})\]")


def _first_evidence_id(prompt):
    """从 prompt 里的 EVIDENCE 块抓出第一个证据 ID，模拟 LLM 正确引用。"""
    m = _CITE_RE.search(prompt)
    return m.group(1) if m else "EV-000"


# ---------- mock LLM：按 prompt 内容分派 ----------
def fake_chat(messages, temperature):
    prompt = messages[0]["content"]
    if "研究规划器" in prompt:                       # Planner
        return json.dumps({
            "sub_questions": [
                {"id": "q1", "question": "目前 AI Agent 的技术发展到了什么程度？"},
                {"id": "q2", "question": "AI Agent 当前主要应用在哪些领域？"},
                {"id": "q3", "question": "AI Agent 面临哪些主要技术瓶颈？"},
            ]
        }, ensure_ascii=False)
    if "搜索关键词" in prompt:                       # generate_queries
        return '["AI Agent 技术进展 2026", "AI Agent 应用 落地", "AI Agent 技术瓶颈"]'
    if "事实核查助手" in prompt:                     # evidence extraction
        return json.dumps({"evidence": [
            {"claim": "该网页支持的一条论断", "evidence_text": "原文摘录……",
             "source_title": "示例来源", "source_url": "https://example.com/a"},
        ]}, ensure_ascii=False)
    if "研究写作助手" in prompt:                     # 子问题小结（保留内部 EV 引用）
        return f"这是该子问题的小结，引用证据 [{_first_evidence_id(prompt)}]。"
    if "研究合著者" in prompt:                       # synthesize
        return (f"## 综合结论\n\n原始问题的答案，引用证据 [{_first_evidence_id(prompt)}]。")
    raise AssertionError("未预期的 prompt: " + prompt[:80])


def fake_search(query):
    return [
        {"title": f"来源A-{query[:10]}", "url": "https://example.com/a", "content": "…"},
        {"title": f"来源B-{query[:10]}", "url": "https://example.com/b", "content": "…"},
    ]


def fake_fetch_page(url):
    return "<html><body><p>网页正文段落一。</p><p>网页正文段落二。</p></body></html>"


v3._chat = fake_chat
v3.search = fake_search
v3.fetch_page = fake_fetch_page

plan, results, answer = v3.run_research("AI Agent 未来的发展前景怎么样？")
print(answer)
print("-" * 60)
print("子问题数:", len(plan.sub_questions),
      "| 成功:", sum(1 for r in results if r.ok),
      "| 失败:", sum(1 for r in results if not r.ok))
assert len(plan.sub_questions) >= v3.SUB_QUESTION_MIN
assert all(r.ok for r in results)

# 引用必须恢复：答案里有 [n] 标注，末尾有引用列表，且不残留内部 EV ID
assert "## 引用" in answer, "最终答案缺少引用列表！"
assert re.search(r"\[\d+\]", answer), "最终答案缺少 [n] 引用标注！"
assert "EV-" not in answer, "最终答案残留内部 EV ID，未转成对外 [n]"

# 证据 ID 必须全局唯一（跨子问题不撞号）
all_ids = [ev["id"] for r in results for ev in r.evidence_list]
print("证据 ID 示例:", all_ids[:3], "| 总数:", len(all_ids))
assert len(all_ids) == len(set(all_ids)), "跨子问题证据 ID 撞号！"
assert any(i.startswith("EV-q1-") for i in all_ids), "证据 ID 缺少子问题前缀"

# 源质量过滤
raw = [
    {"title": "SERP", "url": "https://www.google.com/search?q=x", "content": "x" * 200},
    {"title": "空壳", "url": "https://example.com/empty", "content": "短"},
    {"title": "低分", "url": "https://example.com/low", "content": "y" * 200, "score": 0.1},
    {"title": "好源", "url": "https://example.com/good", "content": "z" * 200, "score": 0.9},
]
kept = v3.filter_results(raw)
print("过滤后保留:", [r["title"] for r in kept])
assert [r["title"] for r in kept] == ["好源"], "源质量过滤结果不对"

# 兜底：全部被过滤时不清空
only_bad = [{"title": "空壳", "url": "https://example.com/e", "content": "短"}]
assert len(v3.filter_results(only_bad)) == 1, "过滤后不应清空结果"

# format_citations：组合引用展开 + 同一来源共用一个编号 + 抹掉非法 ID
evs = [
    {"id": "EV-q1-001", "claim": "c1", "source_title": "来源A",
     "source_url": "https://a.com/1", "evidence_text": "t"},
    {"id": "EV-q2-001", "claim": "c2", "source_title": "来源A",
     "source_url": "https://a.com/1", "evidence_text": "t"},
    {"id": "EV-q2-002", "claim": "c3", "source_title": "来源B",
     "source_url": "https://b.com/2", "evidence_text": "t"},
]
out = v3.format_citations(
    "论点一 [EV-q1-001, EV-q2-001]，论点二 [EV-q2-001, EV-q2-002]，坏引用 [EV-xxx]。", evs)
assert "EV-" not in out, f"内部 ID 泄漏: {out}"
assert "[1]" in out and "[2]" in out, "组合引用未正确展开"
assert out.count("来源A") == 1, "同一来源在引用列表里重复出现"
assert out.count("来源B") == 1
print("CITATION/FILTER OK")

# ---------- 失败路径：Q2 研究失败，整条流程不崩 ----------
print("\n\n===== 失败路径验证（Q2 搜索抛异常）=====\n")

_real_summarize = v3.summarize_sub_question


def flaky_summarize(question, id_prefix="", stats=None):
    if "应用" in question:       # 只让 Q2（问“应用领域”的那个）失败
        raise RuntimeError("Search API 报错 429: rate limit")
    return _real_summarize(question, id_prefix=id_prefix, stats=stats)


v3.summarize_sub_question = flaky_summarize
plan, results, answer = v3.run_research("AI Agent 未来的发展前景怎么样？")
print(answer)
print("-" * 60)
ok = {r.sub_question_id: r.ok for r in results}
print("各子问题状态:", ok)
assert ok["q2"] is False, "Q2 应标记为失败"
assert any(r.ok for r in results), "其他子问题仍应成功"
print("FAIL-PATH OK")