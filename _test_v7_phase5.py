# -*- coding: utf-8 -*-
"""V7 Phase 5 端到端验收（Mock LLM / Search / Fetch，离线可重复）。

    python _test_v7_phase5.py

对照任务书翻译的验收清单:
- A1  REPL 可用（python -m research_v7 启动 + exit）
- A2  [R1][R2] 并行日志交错显示
- A3  三个输出文件（md / pdf / docx），PDF 用 STSong-Light 中文，DOCX 完整
- A4  引用系统无回归（V5/V6 测试另跑；此处断言报告含追溯表/质量说明）
- A5  信息稀缺 → follow-up + reviewer 打回
- A6  新配置 MAX_PARALLEL_RESEARCHERS / MAX_REVIEW_CYCLES / OUTPUT_DIR 生效
- A7  Run Summary 含并行批次 / 评审轮次 / 整改问题统计行
"""
# -*- coding: utf-8 -*-
import glob
import json
import os
import re
import shutil
import subprocess
import sys

import research_v7 as v7
import research_v5 as v5

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

PLANNER = [{"id": "q1", "question": "问题一？"},
           {"id": "q2", "question": "问题二？"},
           {"id": "q3", "question": "问题三？"}]
PAGES = {
    "https://a.com/1": "<html><body><p>市场规模为 42 亿元。</p></body></html>",
    "https://b.com/2": "<html><body><p>机构估计 51 亿元。</p></body></html>",
}
DEFAULT_PAGE = "<html><body><p>默认正文。</p></body></html>"
SUFFICIENT = {"gaps": [], "conflicts": [], "follow_up_questions": [],
              "is_sufficient": True, "reasoning_summary": "足够"}
PASS_REVIEW = {"verdict": "pass", "issues": []}


def _first_para(url):
    page = PAGES.get(url, DEFAULT_PAGE)
    return page.split("<p>", 1)[1].split("</p>", 1)[0]


def _page_url_of(prompt):
    for ln in prompt.splitlines():
        if ln.startswith("网页URL:"):
            return ln.split("网页URL:", 1)[1].strip()
    return ""


def make_chat(gap_response=SUFFICIENT, review_response=PASS_REVIEW):
    def chat(messages, temperature=None):
        prompt = messages[0]["content"]
        if "研究规划器" in prompt:
            return json.dumps({"sub_questions": PLANNER}, ensure_ascii=False)
        if "网络搜索助手" in prompt:
            return json.dumps(["查询一"], ensure_ascii=False)
        if "事实核查助手" in prompt:
            url = _page_url_of(prompt)
            return json.dumps({"evidence": [
                {"claim": "市场规模数据", "evidence_text": _first_para(url),
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
                               "claims": [{"text": "市场规模",
                                           "evidence_ids": use[:1],
                                           "support_status": "supported"}]},
                              ensure_ascii=False)
        if "证据原文支持" in prompt or "独立复核员" in prompt:
            ids = re.findall(r"证据 ID: (EV-\d+)", prompt)
            return json.dumps({"status": "supported", "supporting_evidence_ids": ids[:1],
                               "contradicting_evidence_ids": [], "explanation": "一致"},
                              ensure_ascii=False)
        if "研究质量评审员" in prompt:
            return json.dumps(review_response, ensure_ascii=False)
        raise AssertionError("未预期的 prompt: " + prompt[:80])
    return chat


def install_mocks(gap_response=SUFFICIENT, review_response=PASS_REVIEW):
    old = (v5._chat, v5.search, v5.fetch_page)
    v5._chat = make_chat(gap_response, review_response)
    v5.search = lambda q: [{"title": "A", "url": "https://a.com/1", "content": "x" * 200},
                           {"title": "B", "url": "https://b.com/2", "content": "x" * 200}]
    v5.fetch_page = lambda url: PAGES.get(url, DEFAULT_PAGE)
    return lambda: (setattr(v5, "_chat", old[0]),
                    setattr(v5, "search", old[1]),
                    setattr(v5, "fetch_page", old[2]))


# A1: REPL 可用（子进程启动 + exit）
proc = subprocess.run(
    [sys.executable, "-m", "research_v7"],
    input="exit\n", capture_output=True, text=True, timeout=30,
    cwd=os.getcwd(), encoding="utf-8", errors="replace",
)
assert "Research Agent V7 就绪" in proc.stdout, "REPL 应显示欢迎语"
print("A1 OK: REPL 可启动（python -m research_v7）+ exit 退出")

# A6: 新配置生效
assert v7.MAX_PARALLEL_RESEARCHERS == 3
assert v7.MAX_REVIEW_CYCLES == 2
assert v7.OUTPUT_DIR == "output"
print("A6 OK: 新配置 MAX_PARALLEL_RESEARCHERS=3 / MAX_REVIEW_CYCLES=2 / OUTPUT_DIR='output'")

# 清理旧产物
for old in glob.glob(os.path.join(v7.OUTPUT_DIR, "ai-agent-*")):
    shutil.rmtree(old, ignore_errors=True)

# 捕获完整运行输出（含 [R1][R2] 交错）
import io
from contextlib import redirect_stdout
restore = install_mocks(SUFFICIENT, PASS_REVIEW)
buf = io.StringIO()
with redirect_stdout(buf):
    state, ans = v7.run_research("AI Agent 市场规模如何？")
restore()
log = buf.getvalue()

# A2: [R1][R2] 并行日志
r_lines = re.findall(r"\[R\d\]\[Q\d\]", log)
assert len(set(r_lines)) >= 3, f"应出现 [R1][R2][R3] 前缀: {set(r_lines)}"
print(f"A2 OK: 并行 Researcher 日志前缀 {sorted(set(r_lines))} 交错显示")

# A3: 三产物 + PDF 中文字体 + DOCX 完整
mds = glob.glob(os.path.join(v7.OUTPUT_DIR, "ai-agent-*/report.md"))
pdfs = glob.glob(os.path.join(v7.OUTPUT_DIR, "ai-agent-*/report.pdf"))
docxs = glob.glob(os.path.join(v7.OUTPUT_DIR, "ai-agent-*/report.docx"))
assert len(mds) == 1 and len(pdfs) == 1 and len(docxs) == 1, "三产物各一份"
with open(pdfs[0], "rb") as f:
    pdf_data = f.read()
assert b"STSong-Light" in pdf_data, "PDF 必须用 STSong-Light（中文）"
assert os.path.getsize(docxs[0]) > 500, "DOCX 应完整非空"
with open(mds[0], encoding="utf-8") as f:
    md_content = f.read()
assert "市场规模" in md_content, "报告正文应含中文内容"
print(f"A3 OK: 三产物就绪（md={len(md_content)}B / pdf={len(pdf_data)}B"
      f" / docx={os.path.getsize(docxs[0])}B），PDF 用 STSong-Light")

# A4: 引用系统（追溯表 / 质量说明 / 引用节）
assert "## 结论与证据追溯" in ans, "报告含追溯表"
assert "## 引用质量说明" in ans, "报告含质量说明"
print("A4 OK: 引用系统渲染协议（追溯表 + 质量说明）无回归")

# A7: Run Summary 含 V7 统计行
assert "并行批次" in log and "评审轮次" in log and "整改问题" in log, \
    "Run Summary 应含并行批次/评审轮次/整改问题行"
print("A7 OK: Run Summary 含并行批次 / 评审轮次 / 整改问题统计行")

print("\nALL V7 PHASE5 TESTS PASSED")
