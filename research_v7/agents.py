# -*- coding: utf-8 -*-
"""Research Agent V7 —— 七个角色节点。

角色映射（复用 V5 业务函数，不重写）:
- Planner       → v5.plan_research()
- Chief         → v5._stop_reason()（唯一决策点）
- Researcher    → v5.research_sub_question()（原样调用，不改）
- Analyst       → v5.analyze_gaps() + v5.replan()（串行）
- Reviewer      → 新增 review prompt（JSON {verdict, issues}）
- Writer        → v5.synthesize_from_state() + v5.validate_citation_chain() + v5.render_report()
- Publisher     → v5.render_report 产物 + PDF(reportlab) + DOCX(python-docx)

并发安全（Phase 3 启用）:
- 并行 Researcher 只通过局部 stats 返回 report（绝不共享 stats dict）
- 注册表写入由 v5 内的锁保护（Phase 3 添加到 SourceRegistry/EvidenceRegistry）
- 本文件除 reporter 外，其余节点全部串行执行
"""

from __future__ import annotations

import datetime
import re

from langgraph.types import Command, Overwrite, Send

import research_v5 as v5
from .state import (
    MAX_PARALLEL_RESEARCHERS,
    MAX_REVIEW_CYCLES,
    OUTPUT_DIR,
    _runtime,
    _state,
)

# ------------------------- 节点 1: Planner -------------------------


def planner_node(g) -> dict:
    """[1/7] 初始规划节点：调用 V5 plan_research() 并创建 ResearchState。

    与 V6 的 plan_node 一致：接收原始问题 → 生成初始计划 → 建立
    唯一 ResearchState（含两个注册表）→ 初始化运行级统计。
    """
    rt = _runtime(g)
    question = g["original_question"]
    print("\n[1/3] Understanding Question")
    print(f"      User: {question}")
    print("\n[2/3] Research Loop")
    plan = v5.plan_research(question)
    print(f"      Generated {len(plan.sub_questions)} sub-questions\n")
    g["state"] = v5.ResearchState(question, plan, max_rounds=v5.MAX_ROUNDS)
    rt["filter_stats"] = {}
    print("      [planner] plan_node ✓")
    return g


# ------------------------- 节点 2: Chief Researcher -------------------------


def chief_researcher_node(g) -> Command:
    """[2/7] 首席研究员：唯一决策点。

    决策语义完全复用 v5._stop_reason():
    - pending_questions 非空且研究未停止 → 弹出 ≤MAX_PARALLEL 个任务并行派发
      （Command.goto=[Send("researcher_node", payload)×k]）
    - pending 为空 → 停止判定（gap 充分 / 无新证据 / 达轮次上限）
      → 研究结束 → 进入 reviewer
    首波派发前 start_round()；每波记录 round_evidence_before 供 Analyst 计算新增。
    """
    st = _state(g)
    rt = _runtime(g)

    # ---- Reviewer 整改回路：把上轮评审的 evidence 类 issue 转成研究任务 ----
    # reviewer 把 evidence 类 issue 存进 rt["last_review_issues"]；Chief 收到后
    # 挂回 pending_questions（source="reviewer"），并把研究状态恢复为 running
    # （reviewer 打回 = 研究不充分，需要继续研究）。
    issues = rt.get("last_review_issues")
    if issues:
        rt["last_review_issues"] = None   # 只处理一次
        st.status = "running"             # 打回 = 恢复研究
        st.stop_reason = ""
        for issue in issues:
            desc = str(issue.get("description") or "").strip()
            if not desc:
                continue
            rq = v5.ResearchQuestion(
                qid=f"rv{len(st.completed_questions) + 1}",
                question=desc, source="reviewer",
                reason="评审打回整改", target_gap="",
            )
            st.pending_questions.append(rq)
            print(f"      [chief] 整改任务入队: [{rq.id.upper()}] {desc}")

    # 停止判定（V5 _stop_reason 语义，唯一决策来源）:
    # - 已显式 stop（no_valid_followups）→ 停止
    # - pending 已空 → 调 _stop_reason 判充分性（sufficient / no_new_evidence /
    #   max_rounds）；命中则 stop 并进 Reviewer
    if st.status == "stopped":
        print(f"      [chief] 研究已结束（{st.stop_reason}），进入评审")
        return Command(goto="reviewer_node")

    if not st.pending_questions:
        report = rt.get("last_gap_report")
        new_evidence = int(rt.get("last_round_new_evidence", 0))
        reason = v5._stop_reason(st, report, new_evidence) if report is not None else ""
        if reason:
            st.stop(reason)
            print(f"      [chief] 触发停止条件: {reason}，进入评审")
            return Command(goto="reviewer_node")
        # 未停止但 pending 空（gap 未充分也暂无可研究问题）→ 本轮结束
        print("      [chief] 本轮已无可派发任务，回环等待下一轮")
        # 安全网：避免空转无限循环（不应发生 —— replan 应已入队或 stop）

    # 有任务：弹出并派发（pop 后清空队列，防重复派发）
    rnd = st.current_round + 1
    st.start_round(rnd)
    print(f"      [chief] 第 {rnd}/{st.max_rounds} 轮")
    # 轮首基线：只在每轮第一波设置一次（V5 语义 —— new_evidence 是整轮新增，
    # 多波派发共用同一基线，后续波次不再重设）
    if int(rt.get("current_round", 0)) != rnd:
        rt["round_evidence_before"] = len(st.evidence_list)
        rt["current_round"] = rnd

    # 弹出 ≤ MAX_PARALLEL_RESEARCHERS 个任务
    tasks: list = []
    while st.pending_questions and len(tasks) < MAX_PARALLEL_RESEARCHERS:
        tasks.append(st.pending_questions.pop(0))
    print(f"      [chief] 派发 {len(tasks)} 个并行研究任务"
          f"（{', '.join(rq.id.upper() for rq in tasks)}）")
    # payload 携带 task + 共享 ResearchState 引用 + 轮次 + 批内序号 R1..Rk
    # （内存图无序列化，对象引用可跨 Send 传递；Phase 3 依赖 v5 内注册表锁
    #   保证并行写入安全）
    payloads = [
        {"task": rq, "round_no": rnd, "shared": st, "r_index": i + 1}
        for i, rq in enumerate(tasks)
    ]
    return Command(goto=[Send("researcher_node", p) for p in payloads])


# ------------------------- 节点 3: Researcher（×N 并行）-------------------------


def researcher_node(state) -> dict:
    """[3/7] 研究员（并行实例）。

    只接收 Send payload 或并行机制触发的完整状态重放，二者都只写
    reducer 字段 researcher_reports —— 绝不全量替换/透传整份 state
    （LangGraph 1.x 中并行回调写非 reducer 键会触发 InvalidUpdateError）。

    - payload 形态（含 "task"）：本地 stats 跑 v5.research_sub_question
      原样调用，返回 {"researcher_reports": [report]}（report 携带本任务
      的全部结果与局部 filter_stats）
    - 重放形态（无 "task"）：返回 {"researcher_reports": []}（无操作）
    """
    if "task" not in state:
        return {"researcher_reports": []}

    rq = state["task"]
    rnd = state.get("round_no", 0)
    r_index = state.get("r_index", 1)
    st = state.get("shared")     # Chief 放进 payload 的共享 ResearchState 引用
    # 局部 stats：绝不让多个并行实例共享同一个 dict
    stats: dict = {}
    query_log: list[str] = []
    # [R1]..[Rk] 前缀：并行 Researcher 日志交错可见（验收点）
    print(f"      [R{r_index}][{rq.id.upper()}] Search → Fetch → Extract → Summary",
          end="", flush=True)
    result = v5.research_sub_question(
        v5.SubQuestion(rq.id, rq.question), stats=stats,
        fixed_queries=rq.suggested_queries or None,
        query_log=query_log, state=st, round_no=rnd,
    )
    print(" ✓" if result.ok else " ✗ (failed)")
    if not result.ok:
        print(f"            原因: {result.error}")
    report = {
        "question": rq,
        "round_no": rnd,
        "r_index": r_index,
        "result": result,
        "query_log": query_log,
        "filter_stats": stats,
    }
    return {"researcher_reports": [report]}


# ------------------------- 节点 4: Analyst（串行）-------------------------


def analyst_node(g) -> dict:
    """[4/7] 分析师（串行）：消费并行结果并更新研究状态。

    - 按派发序重放：complete_question / add_evidence / record_searches /
      resolve_gap（完全复用 V5 语义）
    - 合并各 Researcher 的局部 filter_stats 进全局
    - 用 Overwrite([]) 重建 researcher_reports（reducer 字段唯一合法重置，
      否则 [] 会被 operator.add 当普通累加项）
    - 本波 pending 已清空且未停止 → 跑 gap 分析 + replan（供下一波/停止判定）
    """
    st = _state(g)
    rt = _runtime(g)
    reports = list(g["researcher_reports"])

    if not reports:
        print("      [analyst] 无并行结果（不应发生）")
        return {**g, "researcher_reports": Overwrite([])}

    rnd = reports[0]["round_no"]
    print(f"      [analyst] 汇总 {len(reports)} 个并行研究结果")
    merged_stats = rt["filter_stats"]
    for report in reports:
        rq = report["question"]
        result = report["result"]
        st.record_searches(report["query_log"])
        st.complete_question(rq, result, rnd)
        st.add_evidence(result.evidence_list)
        if result.ok:
            st.resolve_gap(rq.target_gap)
        for k, v in report.get("filter_stats", {}).items():
            merged_stats[k] = merged_stats.get(k, 0) + v

    new_evidence = len(st.evidence_list) - int(rt.get("round_evidence_before", 0))
    rt["last_round_new_evidence"] = new_evidence
    print(f"      [analyst] 本轮新增证据 {new_evidence} 条，"
          f"累积 {len(st.evidence_list)} 条")

    # pending 已空且研究未停止 → gap 分析（复用 V6 的 gap_analysis_node 逻辑）
    if not st.pending_questions and st.status != "stopped":
        _gap_analysis(st, rt)

    return {**g, "researcher_reports": Overwrite([])}


def _gap_analysis(st, rt) -> None:
    """V6 gap_analysis_node 的内联版：analyze_gaps + replan。

    与 V6 语义一致：先分析缺口，再尝试 replan 生成下一轮 Follow-up；
    无产出时 stop("no_valid_followups")。
    """
    print("      [analyst] 分析研究缺口...", end="", flush=True)
    report = v5.analyze_gaps(st)
    print(" ✓" if report.ok else " ✗ (已兜底)")
    st.identified_gaps = report.gaps
    st.identified_conflicts = report.conflicts
    for gap in report.gaps:
        if gap not in st.open_gaps:
            st.open_gaps.append(gap)
    if report.ok and report.reasoning_summary:
        v5._log(f"      [analyst] {report.reasoning_summary}")
    rt["last_gap_report"] = report
    rt["gap_sufficient"] = bool(report.ok and report.is_sufficient)
    print(f"      [analyst] 充分性判定: "
          f"{'sufficient' if rt['gap_sufficient'] else 'not sufficient'}")

    if not st.pending_questions and st.status != "stopped":
        # 停止判定优先（V5 _stop_reason 语义）: sufficient / no_new_evidence /
        # max_rounds —— 命中则不入队 Follow-up，直接停止研究
        new_evidence = int(rt.get("last_round_new_evidence", 0))
        reason = v5._stop_reason(st, report, new_evidence)
        if reason:
            st.stop(reason)
            print(f"      [analyst] 触发停止条件: {reason}（跳过 replan）")
        elif report is None or report.error:
            print("      [analyst] 缺口分析失败，无 Follow-up 可用")
        else:
            added = v5.replan(st, report)
            print(f"      [analyst] replan 新增 {len(added)} 个 Follow-up 问题")
            for rq in added:
                extra = f"  ← {rq.target_gap}" if rq.target_gap else ""
                print(f"         [{rq.id.upper()}] {rq.question}{extra}")
            if not added:
                st.stop("no_valid_followups")
                print("      [analyst] 没有可执行的新研究问题，停止研究")
    if rt["filter_stats"]:
        fs = rt["filter_stats"]
        print(f"      [analyst] 共过滤 {sum(fs.values())} 条低质结果"
              f"（黑名单 {fs.get('blocked', 0)} / 空壳 {fs.get('too_short', 0)}"
              f" / 低分 {fs.get('low_score', 0)}"
              f" / 兜底回退 {fs.get('fallback', 0)} 次）")


# ------------------------- 节点 5: Reviewer -------------------------

_REVIEWER_PROMPT_TEMPLATE = """你是研究质量评审员。请根据以下研究材料判断证据是否足以
回答原始问题，并指出需要整改的问题。输出严格 JSON，不要添加解释文字。

原始问题: {original_question}

研究计划:
{plan_block}

各子问题研究结果:
{results_block}

已收集证据:
{evidence_block}

请输出 JSON 对象，格式:
{{
  "verdict": "pass" 或 "fail",
  "issues": [
    {{"type": "evidence" 或 "clarity" 或 "conflict", "description": "具体问题描述"}}
  ]
}}
- verdict=pass 表示证据充分，可以进入写作
- verdict=fail 表示证据不足，必须列出 issues
- issue.type=evidence 表示需要补充研究（这类会转成新的研究任务）
- issue.type=clarity / conflict 表示仅需在报告中说明，不触发再研究
"""


def reviewer_node(g) -> dict:
    """[5/7] 评审员：根据研究材料判定 pass/fail，产出整改问题清单。

    - 材料复用 V5 的 build_plan_block / build_round_results_block /
      build_evidence_block（与 gap 分析同源）
    - 解析 LLM JSON {verdict, issues}；失败时兜底视为 fail（保守）
    - 非 evidence 类 issue 累积进 review_notes（最终交 Writer 如实披露）
    - evidence 类 issue 由 Chief 转成新研究任务（整改回环）
    """
    st = _state(g)
    rt = _runtime(g)
    prompt = _REVIEWER_PROMPT_TEMPLATE.format(
        original_question=st.original_question,
        plan_block=v5.build_plan_block(st),
        results_block=v5.build_round_results_block(st),
        evidence_block=v5.build_evidence_block(st.evidence_list),
    )
    print("      [reviewer] 评审研究材料...", end="", flush=True)
    try:
        raw = v5._chat([{"role": "user", "content": prompt}], temperature=0.2)
        data = v5._extract_json_object(raw)
        verdict = str(data.get("verdict") or "").strip().lower()
        issues = data.get("issues") or []
        if verdict not in ("pass", "fail"):
            raise ValueError(f"非法 verdict: {verdict!r}")
    except (RuntimeError, ValueError, KeyError, AttributeError) as e:
        print(f" ✗ (解析失败，兜底 fail: {e})")
        verdict = "fail"
        issues = [{"type": "evidence",
                   "description": f"评审输出解析失败: {e}"}]
        data = {"verdict": verdict, "issues": issues}

    print(f" ✓ verdict={verdict}，issues={len(issues)}")
    # 分类 issue：
    # - evidence 类 → 存入 runtime["last_review_issues"]，由 Chief 转研究任务
    # - 其余（clarity/conflict）→ 累积进 review_notes，最终交 Writer 如实披露
    notes = list(g["review_notes"])
    evidence_issues: list = []
    for issue in issues:
        itype = str(issue.get("type") or "").strip()
        desc = str(issue.get("description") or "").strip()
        if not desc:
            continue
        if itype == "evidence":
            evidence_issues.append(issue)
        else:
            notes.append(f"[{itype}] {desc}")
    cycles = int(g["review_cycles"])
    rt = _runtime(g)
    rt["last_review_issues"] = evidence_issues if evidence_issues else None

    return {
        **g,
        "review_verdict": verdict,
        "review_notes": notes,
        "review_cycles": cycles + 1,
    }


def route_review(g) -> str:
    """Reviewer 的条件路由：pass → Writer；fail 且未超上限 → Chief（整改）；
    fail 但已超上限 → Writer（带问题清单如实呈现）。"""
    verdict = g.get("review_verdict")
    cycles = int(g.get("review_cycles", 0))
    if verdict == "pass":
        return "writer"
    # 打回上限：fail 且已打回 ≤ MAX_REVIEW_CYCLES 次 → 允许整改再评审；
    # 超过上限（> MAX_REVIEW_CYCLES）→ 强制进入写作，review_notes 如实披露。
    # 即最多 MAX_REVIEW_CYCLES 次整改、MAX_REVIEW_CYCLES+1 次评审。
    if cycles <= MAX_REVIEW_CYCLES:
        return "chief"
    return "writer"


# ------------------------- 节点 6: Writer -------------------------


def writer_node(g) -> dict:
    """[6/7] 写作节点：Synthesis → Citation Validator → Citation Renderer。

    与 V6 finalize_node 一致：合成（含 Claim–Evidence Mapping）、五级校验、
    渲染最终报告。额外把 review_notes 追加到报告末尾的「评审问题清单」。
    """
    st = _state(g)
    rt = _runtime(g)
    print("\n[3/3] Synthesizing Final Answer")
    print("      [writer] 合成结论文本...")
    final_answer, _claims = v5.synthesize_from_state(st)
    print("      [writer] 校验引用链（ID / URL / Quote / 语义 / 冲突）...")
    _reports, _vstats = v5.validate_citation_chain(st)
    final_answer = v5.render_report(st, final_answer)

    # Reviewer 整改问题如实披露（超上限进入或非 evidence 类 issue）。
    # 若到达 Writer 时仍有未整改的 evidence 类 issue（超上限被强制进入），
    # 一并披露，绝不静默吞掉。
    notes = list(g.get("review_notes") or [])
    leftover = (rt.get("last_review_issues") or [])
    for issue in leftover:
        desc = str(issue.get("description") or "").strip()
        if desc:
            notes.append(f"[evidence] {desc}")
    if notes:
        final_answer += (
            "\n\n## 评审问题清单（未完全解决）\n\n"
            + "\n".join(f"- {n}" for n in notes)
        )
    g["final_answer"] = final_answer
    print("\n" + "=" * 50)
    print("Final Answer")
    print("=" * 50)
    print("\n--- Run Summary ---")
    v5._print_run_summary(st, rt["filter_stats"])
    # V7 可观测性补充：并行批次 / 评审轮次 / 整改问题
    print(f"并行批次: 研究任务 {len(st.completed_questions)} 个"
          f"（每批 ≤ {MAX_PARALLEL_RESEARCHERS} 个并行）"
          f" | 评审轮次: {int(g.get('review_cycles', 0))} 次"
          f"（上限 {MAX_REVIEW_CYCLES}）"
          f" | 整改问题: {len(g.get('review_notes') or [])} 条")
    return g


# ------------------------- 节点 7: Publisher -------------------------


def _slugify(text: str, max_len: int = 8) -> str:
    """从问题前几个字符生成 URL 安全 slug（供输出目录名）。"""
    s = re.sub(r"[^\w一-鿿]+", "-", text.strip()).strip("-")
    if not s:
        s = "research"
    return s[:max_len].rstrip("-") or "research"


def publisher_node(g) -> dict:
    """[7/7] 发布节点：把报告写成 md / pdf / docx 三个产物。

    - md: render_report 产物原样保存
    - pdf: reportlab（中文字体必须 UnicodeCIDFont("STSong-Light")，
      用 Helvetica 会渲染成黑块）
    - docx: python-docx

    输出目录: output/<slug>-<yyyymmdd-hhmm>/
    """
    import os

    st = _state(g)
    answer = g.get("final_answer") or "（没有生成报告。）"
    slug = _slugify(st.original_question)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    out_dir = os.path.join(OUTPUT_DIR, f"{slug}-{ts}")
    os.makedirs(out_dir, exist_ok=True)

    md_path = os.path.join(out_dir, "report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(answer)
    print(f"      [publisher] 报告已写入: {md_path}")

    pdf_path = os.path.join(out_dir, "report.pdf")
    _write_pdf(answer, pdf_path)
    print(f"      [publisher] PDF 已写入: {pdf_path}")

    docx_path = os.path.join(out_dir, "report.docx")
    _write_docx(answer, docx_path)
    print(f"      [publisher] DOCX 已写入: {docx_path}")

    return {**g, "product_paths": {"md": md_path, "pdf": pdf_path,
                                   "docx": docx_path}}


def _write_pdf(text: str, path: str) -> None:
    """reportlab 渲染 PDF：中文字体用 UnicodeCIDFont("STSong-Light")。"""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import mm
    from reportlab.lib.utils import simpleSplit

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    c = canvas.Canvas(path, pagesize=A4)
    width, height = A4
    margin = 20 * mm
    c.setFont("STSong-Light", 10)
    y = height - margin
    for line in text.splitlines():
        # 简单 markdown 缩进处理（标题加大）
        if line.startswith("#"):
            font = "STSong-Light"
            size = 16 if line.startswith("# ") else 14
            c.setFont(font, size)
        else:
            c.setFont("STSong-Light", 10)
        for seg in simpleSplit(line, c._fontname, c._fontsize,
                               width - 2 * margin):
            if y < margin:
                c.showPage()
                c.setFont("STSong-Light", 10)
                y = height - margin
            c.drawString(margin, y, seg)
            y -= c._fontsize * 1.6
    c.showPage()
    c.save()


def _write_docx(text: str, path: str) -> None:
    """python-docx 渲染 DOCX。"""
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    for line in text.splitlines():
        if line.startswith("### "):
            doc.add_heading(line[4:], level=3)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=2)
        elif line.startswith("# "):
            doc.add_heading(line[2:], level=1)
        elif line.strip():
            doc.add_paragraph(line)
    doc.save(path)
