"""Research Agent V5 —— 在 V4 基础上构建 Citation System（引用追踪链）。

目标引用链: Claim → Evidence → Source → 原文 / URL，全部 ID 由程序分配。

Phase 进度:
- Phase B: Source Registry 来源注册表 —— source_id / canonical_url / 正文版本
  （page_versions）；页面正文持久保存在注册表中，供 quote 验证使用
- Phase C: Evidence Registry 证据注册表 —— evidence_id 全局分配 /
  quote 对保存正文自动验证 / locator 段落定位 / 强制关联 source_id；
  证据跨轮累积、同源同论断去重复用 ID、不同来源相同论断各自保留
- Phase D: Claim–Evidence Mapping —— Synthesis 输出结构化结论
  （answer_markdown + claims JSON），validate_claims 程序校验：
  claim_id 程序分配、evidence_ids 只认真实存在的证据、小结不充当证据、
  无证据的数字/因果结论强制 unsupported，不强行生成结论
- Phase E: Citation Validator —— 每条结论五级检查：
  ① ID（证据/来源/版本存在性）② URL（可解析；可达性可选）
  ③ Quote（对保存正文复验 + content_hash 一致）④ Semantic（LLM 结构化
  判定，程序过滤 ID 与合并裁决）⑤ Conflict（与 GapAnalyzer 冲突交集）；
  关键结论独立二次核验，不一致标 needs_review
- Phase F (本阶段): Citation Renderer —— 程序统一渲染：[EV-xxx]→[n]
  同源同号、References 每来源一条且仅列被引来源（元数据只取注册表、
  Markdown 可点击链接）、结论与证据追溯表、引用质量说明显式呈现
"""

from __future__ import annotations

import hashlib
import html.parser
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import quote, urlparse, urlunparse

# ------------------------- 配置 -------------------------

AI_BASE_URL = ""   # 在 load_dotenv() 后按顺序赋值
AI_API_KEY = ""
AI_MODEL = ""
RESEARCH_BASE_URL = ""
RESEARCH_API_KEY = ""

MAX_RESULTS = 5               # 单次搜索返回条数上限

EXTRACT_MAX_CHARS = 6000      # 单页正文最多保留的字符数（避免 prompt 过长）
EVIDENCE_MAX_PER_PAGE = 5     # 单网页最多提取的证据条数
EVIDENCE_MAX_TEXT = 400       # 单条 evidence_text 最多保留的字符数

SUB_QUESTION_MIN = 3          # Planner 子问题数量下限
SUB_QUESTION_MAX = 7          # Planner 子问题数量上限

# --- V4: Research Loop ---
MAX_ROUNDS = 3                # 研究轮次上限（含第 1 轮初始计划），防止无限循环
MAX_FOLLOWUPS_PER_ROUND = 3   # 每轮 Replanner 最多新增的 Follow-up 问题数

VERBOSE = False               # True 时打印每个网页的抓取/证据明细（排查问题用）

# --- 源质量过滤 ---
SEARCH_DEPTH = "advanced"     # Tavily 搜索深度: basic | advanced（advanced 正文质量更好、更贵）
BLOCKED_DOMAINS = {           # 命中即丢弃：搜索引擎结果页/跳转页，永远不是好来源
    "google.com", "bing.com", "baidu.com", "duckduckgo.com",
    "yahoo.com", "yandex.com", "so.com", "sogou.com",
}
MIN_RESULT_CONTENT = 80       # 搜索返回的正文短于此长度视为导航页/空壳，丢弃
MIN_RESULT_SCORE = 0.3        # 若结果带 score 字段，低于此相关度丢弃

# --- V5: Source Registry ---
SOURCE_ID_PREFIX = "SRC"             # 来源 ID 前缀，分配后形如 SRC-001
TRACKING_PARAM_PREFIXES = ("utm_",)  # canonical 化时明确删除的跟踪参数前缀

# --- V5: Evidence Registry ---
EVIDENCE_ID_PREFIX = "EV"            # 证据 ID 前缀，分配后形如 EV-001（全局唯一）
CLAIM_ID_PREFIX = "CLM"              # 结论 ID 前缀，形如 CLM-001（程序分配，不用 LLM 编号）

# --- V5: Citation Validator ---
SEMANTIC_CHECK_ENABLED = True        # 语义校验走 LLM；False 时语义检查一律标 unverified
URL_REACHABILITY_CHECK = False       # 默认不发网络请求检查 URL 可达性（离线优先）
SECOND_PASS_MAX_CLAIMS = 3           # 关键结论（含数字/因果要素）二次核验上限


def _log(msg: str) -> None:
    """细节日志：只在 VERBOSE 下输出，保证主流程终端输出简洁。"""
    if VERBOSE:
        print(msg)


def load_dotenv(path: str = ".env") -> None:
    """手动解析 .env，向 os.environ 填充（不覆盖已有环境变量）。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def init_config() -> None:
    global AI_BASE_URL, AI_API_KEY, AI_MODEL
    global RESEARCH_BASE_URL, RESEARCH_API_KEY
    AI_BASE_URL = os.environ.get("AI_BASE_URL", "").rstrip("/")
    AI_API_KEY = os.environ.get("AI_API_KEY", "")
    AI_MODEL = os.environ.get("AI_MODEL", "")
    RESEARCH_BASE_URL = os.environ.get("RESEARCH_BASE_URL", "").rstrip("/")
    RESEARCH_API_KEY = os.environ.get("RESEARCH_API_KEY", "")


def _join_url(base: str, path: str) -> str:
    """拼接 Base URL 与路径，避免 /research/research/search 这类重复前缀。

    核心规则：先去掉 base 末尾的斜杠，若 base 末尾已经是 /path 直接返回。
    无论 base 填的是 https://xxx.com 还是 https://xxx.com/research，结果都正确。
    """
    base = base.rstrip("/")                       # 去末尾斜杠
    path = path.lstrip("/")                       # 去前导斜杠
    if base.endswith("/" + path):
        return base
    return f"{base}/{path}"


# ------------------------- LLM -------------------------

def _urlopen(req: urllib.request.Request, timeout: float):
    """打开请求；目标为本机地址时绕过系统代理（Hiddify 等会把 127.0.0.1
    的请求也转发到代理端口，导致 new-api 返回 502/连接重置）。"""
    host = urlparse(req.full_url).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1") or host.startswith("127."):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _chat(messages: list[dict], temperature: float) -> str:
    """调用 AI API，返回回复文本。"""
    # 1. 规范化 Base URL，避免 /v1/v1 等重复前缀
    target_url = _join_url(AI_BASE_URL, "chat/completions")

    body = json.dumps({
        "model": AI_MODEL,
        "messages": messages,
        "temperature": temperature,
    }).encode("utf-8")

    # 2. 伪装标准浏览器 User-Agent，避免被防火墙秒断
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AI_API_KEY}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    }

    req = urllib.request.Request(target_url, data=body, headers=headers, method="POST")

    # 3. 扩大捕获范围，捕获断连和网络错误
    try:
        with _urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"AI API 报错 {e.code}: {detail[:500]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络连接失败 (请求地址: {target_url}): {e.reason}") from e
    except Exception as e:
        raise RuntimeError(f"请求发生异常 (请求地址: {target_url}): {e}") from e

    return data["choices"][0]["message"]["content"]


def _parse_json_list(raw: str) -> list[str]:
    """容忍 LLM 输出里多余的 ```json 围栏或前后文字，取出第一个 JSON 数组。"""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"LLM 没有输出 JSON 数组: {raw[:200]!r}")
    items = json.loads(raw[start:end + 1])
    return [str(x).strip() for x in items if str(x).strip()]


def _extract_json_object(raw: str) -> dict:
    """宽容解析 LLM 输出的 JSON 对象：剥围栏、定位第一个 { 到最后一个 }。

    LLM 可能输出 ```json 围栏、前后解释文字、多余换行，这里统一容忍。
    """
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"LLM 没有输出 JSON 对象: {raw[:200]!r}")
    return json.loads(raw[start:end + 1])


def generate_queries(question: str) -> list[str]:
    """LLM 第 1 步: 把问题拆成几个搜索关键词。"""
    prompt = (
        "你是一个网络搜索助手。根据下面这个问题，生成 3 个最可能搜到答案的搜索关键词。\n"
        "关键词的语言与问题保持一致。\n"
        '直接输出 JSON 字符串数组，例如 ["关键词1", "关键词2", "关键词3"]，不要输出任何其他文字。\n\n'
        f"问题: {question}"
    )
    raw = _chat([{"role": "user", "content": prompt}], temperature=0.5)
    return _parse_json_list(raw)


# ------------------------- Planner (研究规划) -------------------------

class SubQuestion:
    """V3 Planner 产出的一个子问题。每个子问题都可以独立完成一次 Web Research。"""

    def __init__(self, qid: str, question: str):
        self.id = qid
        self.question = question


class ResearchPlan:
    """Planner 的完整产出：原始问题 + 一组子问题。"""

    def __init__(self, original_question: str, sub_questions: list[SubQuestion]):
        self.original_question = original_question
        self.sub_questions = sub_questions


class ResearchResult:
    """单个子问题的研究结果：它的搜索 → 证据 → 小结（Summary）。"""

    def __init__(self, sub_question_id: str, sub_question: str,
                 evidence_list: list[dict], summary: str, error: str = ""):
        self.sub_question_id = sub_question_id
        self.sub_question = sub_question
        self.evidence_list = evidence_list      # list of [{"id": EV-xxx, "claim":..., ...}]
        self.summary = summary                  # 基于该子问题证据的一小节回答
        self.error = error                      # 非空表示该子问题研究失败

    @property
    def ok(self) -> bool:
        """研究是否成功（失败时仍保留 sub_question，便于 Synthesis 如实告知）。"""
        return not self.error


_PLANNER_PROMPT = """你是一名研究规划器 (Planner)。你的任务只有一个：把用户的原始问题拆解成一组可独立检索、可合并回答的子问题。你不负责回答问题本身。

拆解原则:
1. 理解用户真正想研究的问题，先判断它需要哪些不同维度的资料，再拆子问题。
2. 每个子问题必须能单独进行 Web Search 获取资料，是自包含的完整问句（不使用"它""该领域"等指代原问题的代词）。
3. 子问题之间的内容尽量减少重复；不要产生与原问题无关的问题。
4. 子问题数量默认 3~7 个；如果原问题非常简单，1~3 个即可，不要为了凑数量而拆。
5. 不用回答用户问题，也不需要对拆解进行任何解释。

严格输出 JSON 对象，不要输出任何其他文字，格式如下:
{{
  "sub_questions": [
    {{"id": "q1", "question": "子问题 1 的完整问句"}},
    {{"id": "q2", "question": "子问题 2 的完整问句"}},
    {{"id": "q3", "question": "子问题 3 的完整问句"}}
  ]
}}

用户问题: {question}"""


def _validate_sub_question(item: dict) -> SubQuestion | None:
    """校验单条 Planner 输出，缺 id 或 question 则丢弃。"""
    qid = (item.get("id") or "").strip().lower()
    question = (item.get("question") or "").strip()
    if not qid or not question:
        return None
    return SubQuestion(qid, question)


def _dedup_sub_questions(sub_questions: list[SubQuestion]) -> list[SubQuestion]:
    """按 (id, question) 去重，并截断到 SUB_QUESTION_MAX 个。"""
    seen, unique = set(), []
    for sq in sub_questions:
        key = (sq.id, sq.question)
        if key not in seen:
            seen.add(key)
            unique.append(sq)
    return unique[:SUB_QUESTION_MAX]


def _ensure_min_sub_questions(sub_questions: list[SubQuestion],
                              original_question: str) -> list[SubQuestion]:
    """兜底：保证计划始终至少有 SUB_QUESTION_MIN 个子问题。

    Planner 输出再离谱（0 个、重复、缺字段），最终计划也不会小于下限：
    不足部分用原问题补位，补位子问题 id 唯一 (fallback-1, fallback-2, ...)，
    且补位发生在去重之后，不会与已有 id 冲突。
    """
    for i in range(len(sub_questions), SUB_QUESTION_MIN):
        fallback = SubQuestion(f"fallback-{i + 1}", original_question)
        sub_questions.append(fallback)
    return sub_questions


def plan_research(question: str) -> ResearchPlan:
    """Planner: LLM 把原始问题拆成子问题，返回 ResearchPlan。

    数据流:
    User Question → Planner (LLM) → ResearchPlan(original_question, sub_questions)

    错误处理:
    - LLM 调用失败 (RuntimeError) / 非法 JSON / 缺字段 → 重试一次，仍失败则抛 ValueError
    - 输出不足 SUB_QUESTION_MIN 个 → 用原问题补位兜底，计划大小始终 >= SUB_QUESTION_MIN
    """
    prompt = _PLANNER_PROMPT.format(question=question)
    for attempt in (1, 2):
        try:
            raw = _chat([{"role": "user", "content": prompt}], temperature=0.3)
            data = _extract_json_object(raw)
            items = data.get("sub_questions") or []
            sub_questions = [
                sq for sq in (_validate_sub_question(i) for i in items)
                if sq is not None
            ]
        except (RuntimeError, ValueError, json.JSONDecodeError, KeyError) as e:
            if attempt == 1:
                print(f"      [planner] LLM 输出解析失败 ({e})，重试一次...")
                prompt += (
                    "\n\n注意: 上一次输出不是合法 JSON，请严格只输出 JSON 对象，"
                    "不要添加围栏或解释文字。"
                )
            else:
                raise ValueError(f"Planner 重试仍无法产出子问题: {e}") from e
        else:
            break

    sub_questions = _dedup_sub_questions(sub_questions)
    if len(sub_questions) < SUB_QUESTION_MIN:
        # 兜底: 子问题不够时，用原问题补位，保证计划大小始终 >= 下限
        n_before = len(sub_questions)
        print(f"      [planner] 子问题不足 {SUB_QUESTION_MIN} 个（仅 {n_before} 个），"
              f"用原问题补 {SUB_QUESTION_MIN - n_before} 个兜底")
        sub_questions = _ensure_min_sub_questions(sub_questions, question)
    return ResearchPlan(original_question=question, sub_questions=sub_questions)


# ------------------------- Research State (V4 新增) -------------------------

def _norm_question(q: str) -> str:
    """问题规范化（查重用）：折叠空白、转小写、去掉末尾标点。"""
    q = re.sub(r"\s+", " ", q.strip().lower())
    return q.rstrip("？?。.！!：:，,")


def _norm_query(q: str) -> str:
    """搜索 query 规范化（查重用）。"""
    return re.sub(r"\s+", " ", q.strip().lower())


class ResearchQuestion:
    """研究队列里的一个问题：初始子问题（planner）或 Follow-up（replanner）。"""

    def __init__(self, qid: str, question: str, source: str = "planner",
                 reason: str = "", target_gap: str = "",
                 suggested_queries: list[str] | None = None):
        self.id = qid
        self.question = question
        self.source = source                  # planner | replanner
        self.reason = reason                  # replanner 生成该问题的原因
        self.target_gap = target_gap          # 该问题针对的缺口
        self.suggested_queries = list(suggested_queries or [])


class ResearchState:
    """V4 的统一研究状态：整个研究过程只有这一份状态。

    Research Loop 每完成一步就更新它；所有跨轮次信息（证据、来源、
    搜索历史、缺口、冲突、轮次）都累积在这里 —— 每轮只增不覆盖，
    Synthesis 读取的是全量累积结果，而不是最后一轮的 Summary。
    """

    def __init__(self, original_question: str, plan: ResearchPlan,
                 max_rounds: int = MAX_ROUNDS,
                 source_registry: SourceRegistry | None = None,
                 evidence_registry: EvidenceRegistry | None = None):
        self.original_question = original_question
        self.research_plan = plan
        # V5: 注册表。未显式传入时自建，保证状态对象始终自洽可用；
        # 证据登记时强制校验 source_id 存在，证据必能回溯到来源与正文版本
        self.source_registry = source_registry or SourceRegistry()
        self.evidence_registry = (evidence_registry
                                  or EvidenceRegistry(self.source_registry))
        self._evidence_ids: set[str] = set()      # 已入库证据 ID（去重，只增）
        self.claims: list[dict] = []              # V5 Phase D: 校验后的结构化结论
        self.claims_stats: dict = {}              # V5 Phase D: validate_claims 统计
        self.claim_reports: list[dict] = []       # V5 Phase E: 五级校验报告（逐条结论）
        self.validation_summary: dict = {}        # V5 Phase E: 引用链校验统计
        self.pending_questions: list[ResearchQuestion] = [
            ResearchQuestion(sq.id, sq.question, source="planner")
            for sq in plan.sub_questions
        ]
        self.completed_questions: list[dict] = []   # [{question, result, round}]
        self.evidence_list: list[dict] = []         # 全局累积证据（已去重），只增不减
        self.source_list: list[dict] = []           # [{title, url}] 按 URL 去重
        self.search_history: list[str] = []         # 已执行过的搜索 query
        self.identified_gaps: list[str] = []        # 最近一轮 Gap 分析发现的缺口
        self.open_gaps: list[str] = []              # 尚未被后续研究解决的缺口
        self.identified_conflicts: list[dict] = []  # [{description, evidence_ids}]
        self.current_round = 0
        self.max_rounds = max_rounds
        self.status = "running"                     # running | stopped
        self.stop_reason = ""                       # 停止原因（停止后非空）

    # ---- 问题队列 ----
    def question_keys(self) -> set[str]:
        """已完成 + 待研究问题的规范化文本集合，供 Replanner 查重。"""
        keys = {_norm_question(q.question) for q in self.pending_questions}
        keys |= {_norm_question(c["question"].question)
                 for c in self.completed_questions}
        return keys

    def add_follow_ups(self, items: list[ResearchQuestion]) -> None:
        """把已去重的 Follow-up 问题加入待研究队列。"""
        self.pending_questions.extend(items)

    def complete_question(self, rq: ResearchQuestion, result: ResearchResult,
                          rnd: int) -> None:
        """一个问题研究完毕：移出 pending，连同轮次写入 completed。"""
        self.pending_questions = [p for p in self.pending_questions
                                  if p.id != rq.id]
        self.completed_questions.append(
            {"question": rq, "result": result, "round": rnd})

    # ---- 证据 / 来源 / 搜索历史 ----
    def add_evidence(self, evidence: list[dict]) -> int:
        """累积证据（V5）：按 evidence_id 全局去重，只增不减。

        V5 的证据由 EvidenceRegistry 登记产生：同一来源同一论断跨轮
        重复发现时复用同一个 evidence_id，因此按 ID 去重即可保证
        - 多轮累积不覆盖、不重复入库
        - 不同来源的相同论断各自保留（出处关系不丢）
        兼容：仅带旧式 "id" 字段的证据 dict 同样按 id 去重。
        """
        added = 0
        for ev in evidence:
            eid = ev.get("evidence_id") or ev.get("id") or ""
            if not eid or eid in self._evidence_ids:
                continue
            self._evidence_ids.add(eid)
            self.evidence_list.append(ev)
            added += 1
        return added

    def add_source(self, title: str, url: str) -> None:
        """按 URL 去重的来源登记。"""
        if url and all(s["url"] != url for s in self.source_list):
            self.source_list.append({"title": title, "url": url})

    def record_searches(self, queries: list[str]) -> None:
        """登记本轮实际执行过的搜索 query（供 Replanner 查重）。"""
        for q in queries:
            if q and q not in self.search_history:
                self.search_history.append(q)

    def resolve_gap(self, target_gap: str) -> None:
        """某缺口已被一轮成功的研究覆盖时，把它移出未解决缺口。"""
        if target_gap and target_gap in self.open_gaps:
            self.open_gaps.remove(target_gap)

    # ---- 轮次 / 状态 ----
    def start_round(self, rnd: int) -> None:
        self.current_round = rnd

    def stop(self, reason: str) -> None:
        self.status = "stopped"
        self.stop_reason = reason


# ------------------------- Source Registry (V5 新增) -------------------------

def _utc_now_iso() -> str:
    """当前 UTC 时间的 ISO8601 字符串（retrieved_at 的缺省值）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_page_text(text: str) -> str:
    """对**实际保存的正文**计算内容哈希（带算法前缀，便于将来演进）。

    哈希依据永远是即将存入 page_versions 的 page_text 字符串本身，
    而不是抓取过程的中间产物，保证「hash ↔ 落盘正文」严格一致。
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonicalize_url(url: str) -> str:
    """计算 URL 的规范化形式 canonical_url，仅用于来源去重，**不替代原始 URL**。

    V5 第一版规则（刻意保守 —— 宁可少合并，不可错合并；错合并会让证据
    失去真实出处）:
    - scheme / host 转小写（两者本就大小写不敏感）
    - 去掉 host 的 www. 前缀（与 _host_of 的口径一致）
    - 移除 fragment（# 后只是页内定位，不代表不同来源）
    - 仅删除明确跟踪参数（utm_* 前缀，大小写不敏感），未知 query 参数一律原样保留
    - path 与其余 query 原样保留（大小写敏感，不猜测等价形式）
    - 不合并 http/https、不跟随重定向、不发起任何网络请求

    缺 scheme 或 host 的输入抛 ValueError（由 SourceRegistry.register 捕获并拒绝）。
    """
    parts = urlparse((url or "").strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"无效 URL（缺少 scheme 或 host）: {url!r}")
    userinfo, at, hostport = parts.netloc.rpartition("@")
    hostport = hostport.lower()
    if hostport.startswith("www."):
        hostport = hostport[4:]
    netloc = f"{userinfo}@{hostport}" if at else hostport
    query = "&".join(
        p for p in parts.query.split("&")
        if p and not p.split("=", 1)[0].lower().startswith(TRACKING_PARAM_PREFIXES)
    )
    # fragment 传空串即完成移除；path/params 原样透传
    return urlunparse(
        (parts.scheme.lower(), netloc, parts.path, parts.params, query, "")
    )


class SourceRegistry:
    """V5 来源注册表：为每个来源分配稳定 source_id，保存元数据与正文版本。

    数据分两层，刻意分开存放:
    - 来源元数据（sources 里的 source dict）:
      source_id / url(原始) / canonical_url / title / domain / published_at /
      retrieved_at(首次) / content_hash(最新正文) / urls_seen / page_versions
    - 正文版本数据（source["page_versions"] 列表，append-only）:
      version_id / content_hash / page_text / char_count / retrieved_at / url
      —— 供 Phase C 的 quote 验证按版本读取原文

    关键不变式:
    - source_id 形如 SRC-001，由内部计数器分配后与来源绑定、永不改变，
      绝不用列表下标充当 ID
    - url 只保存首次注册时的原始 URL，之后任何重复注册都不改写它；
      出现过的全部原始 URL（含 utm 变体）记录在 urls_seen 与对应版本里
    - 同一 canonical_url 重复注册 → 复用已有 source_id（不新建来源）
    - 相同正文（content_hash 相同）→ 复用已有 version_id（不新建版本）
    - 正文变化 → 追加新版本，旧版本原样保留，历史证据的依据不会被覆盖
    - title / published_at 只在原值为空时用新值补齐，绝不覆盖、绝不伪造
    - version_id 的序号在分配那一刻确定并随版本永久绑定；page_versions
      只追加不删除，保证已分配的 version_id 永不重复
    """

    def __init__(self) -> None:
        self.sources: dict[str, dict] = {}        # source_id -> 来源元数据（插入序即注册序）
        self._by_canonical: dict[str, str] = {}   # canonical_url -> source_id（去重索引）
        self._counter = 0                         # source_id 分配计数器，只增不减

    # ---- 注册 ----
    def register(self, url: str, title: str = "", page_text: str = "",
                 published_at: str = "",
                 retrieved_at: str | None = None) -> dict | None:
        """注册来源并挂接正文版本。

        返回 {"source_id", "version_id", "source_new", "version_new"}:
        - version_id 为 None 表示本次没有可用正文，未创建/未复用任何版本
        - 无效 URL 返回 None（拒绝注册；不抛异常，由调用方按需跳过该来源）
        """
        raw_url = (url or "").strip()
        if not raw_url:
            return None
        try:
            canon = canonicalize_url(raw_url)
        except ValueError as e:
            _log(f"      [source] 拒绝注册无效 URL: {e}")
            return None

        now = retrieved_at or _utc_now_iso()
        sid = self._by_canonical.get(canon)
        source_new = sid is None
        if source_new:
            self._counter += 1
            sid = f"{SOURCE_ID_PREFIX}-{self._counter:03d}"
            source = {
                "source_id": sid,
                "url": raw_url,                     # 原始 URL，永不改写
                "canonical_url": canon,             # 仅用于去重
                "title": (title or "").strip(),     # 缺失就留空，不伪造
                "domain": _host_of(raw_url),
                "published_at": (published_at or "").strip(),
                "retrieved_at": now,                # 首次注册时间
                "content_hash": "",                 # 最新正文 hash（无正文为空）
                "urls_seen": [raw_url],             # 出现过的全部原始 URL（含变体）
                "page_versions": [],                # 正文版本，append-only
            }
            self.sources[sid] = source
            self._by_canonical[canon] = sid
        else:
            source = self.sources[sid]
            if raw_url not in source["urls_seen"]:
                source["urls_seen"].append(raw_url)
            # 只补空缺，不覆盖已有值 —— 元数据以首见为准
            if not source["title"] and (title or "").strip():
                source["title"] = title.strip()
            if not source["published_at"] and (published_at or "").strip():
                source["published_at"] = published_at.strip()

        # ---- 正文版本：相同 hash 复用，不同 hash 追加，旧版本永不覆盖 ----
        text = page_text or ""
        version_id: str | None = None
        version_new = False
        if text:
            content_hash = hash_page_text(text)
            existing = next((v for v in source["page_versions"]
                             if v["content_hash"] == content_hash), None)
            if existing is not None:
                version_id = existing["version_id"]
            else:
                version_id = f"{sid}-v{len(source['page_versions']) + 1}"
                source["page_versions"].append({
                    "version_id": version_id,
                    "content_hash": content_hash,
                    "page_text": text,              # 实际保存的正文，hash 依据
                    "char_count": len(text),
                    "retrieved_at": now,            # 抓到这一版正文的时间
                    "url": raw_url,                 # 本次抓取实际使用的原始 URL
                })
                version_new = True
            source["content_hash"] = content_hash   # 元数据上指向最新正文

        return {"source_id": sid, "version_id": version_id,
                "source_new": source_new, "version_new": version_new}

    # ---- 查询 ----
    def get(self, source_id: str) -> dict | None:
        """按 source_id 取来源元数据（含 page_versions），不存在返回 None。"""
        return self.sources.get(source_id)

    def get_version(self, source_id: str, version_id: str) -> dict | None:
        """按 (source_id, version_id) 取某个正文版本，供 quote 验证读取原文。"""
        source = self.sources.get(source_id)
        if not source:
            return None
        return next((v for v in source["page_versions"]
                     if v["version_id"] == version_id), None)

    def latest_version(self, source_id: str) -> dict | None:
        """来源的最新正文版本；该来源从无正文时返回 None。"""
        source = self.sources.get(source_id)
        if not source or not source["page_versions"]:
            return None
        return source["page_versions"][-1]

    def resolve_by_url(self, url: str) -> dict | None:
        """按 URL 找来源：先匹配出现过的原始 URL，再退回 canonical 匹配。"""
        raw = (url or "").strip()
        if not raw:
            return None
        for source in self.sources.values():
            if raw in source["urls_seen"]:
                return source
        try:
            canon = canonicalize_url(raw)
        except ValueError:
            return None
        sid = self._by_canonical.get(canon)
        return self.sources.get(sid) if sid else None

    def __len__(self) -> int:
        return len(self.sources)


# ------------------------- Evidence Registry (V5 新增) -------------------------

def _norm_text(text: str) -> str:
    """claim 规范化（去重键用）：折叠空白、转小写、去末尾标点。"""
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    return t.rstrip("？?。.！!：:，,")


def _fold_ws(text: str) -> tuple[str, list[int]]:
    """把连续空白折叠成单个空格，返回 (折叠文本, 位置映射)。

    位置映射[i] 是折叠文本第 i 个字符在原文本中的下标；空白段映射到
    该段最后一个字符，保证折叠匹配后能回切出原文区间（含被折叠的换行）。
    """
    chars: list[str] = []
    idx: list[int] = []
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            j = i
            while j < n and text[j].isspace():
                j += 1
            chars.append(" ")
            idx.append(j - 1)
            i = j
        else:
            chars.append(text[i])
            idx.append(i)
            i += 1
    folded = "".join(chars)
    lead = len(folded) - len(folded.lstrip())
    trail = len(folded) - len(folded.rstrip())
    if lead:
        folded = folded[lead:]
        idx = idx[lead:]
    if trail:
        folded = folded[:len(folded) - trail]
        idx = idx[:len(idx) - trail]
    return folded, idx


def _locator_for(page_text: str, start: int, end: int) -> dict:
    """根据原文区间构造 locator：段落序号（0 基）+ 全文字符区间。"""
    para_index = page_text.count("\n", 0, start)
    cross = "\n" in page_text[start:end]
    return {"type": "cross_paragraph" if cross else "paragraph",
            "index": para_index, "char_start": start, "char_end": end}


def locate_quote(quote: str, page_text: str) -> dict | None:
    """在页面正文中定位 quote，返回 locator；找不到返回 None。

    两级匹配:
    1. 快路径 —— 原文直接子串（quote 与正文逐字符一致）
    2. 慢路径 —— 双方折叠空白后再匹配（容忍换行/多空格等排版差异），
       并通过位置映射把命中区间还原为原文区间
    只做子串匹配，不做任何语义判断；匹配不到绝不硬凑。
    """
    q = (quote or "").strip()
    body = page_text or ""
    if not q or not body:
        return None
    pos = body.find(q)
    if pos != -1:
        return _locator_for(body, pos, pos + len(q))
    nbody, body_idx = _fold_ws(body)
    nq, _ = _fold_ws(q)
    if not nq:
        return None
    p = nbody.find(nq)
    if p == -1:
        return None
    start = body_idx[p]
    end = body_idx[p + len(nq) - 1] + 1
    return _locator_for(body, start, end)


def build_context(page_text: str, start: int, end: int, width: int = 120) -> str:
    """截取 quote 在原文中的前后文（各 width 字符），首尾以 … 标记截断。"""
    left = max(0, start - width)
    right = min(len(page_text), end + width)
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(page_text) else ""
    return (f"{prefix}{page_text[left:start]}"
            f"{page_text[start:end]}{page_text[end:right]}{suffix}")


def verify_quote(quote: str, page_text: str) -> tuple[bool, dict | None, str]:
    """验证 quote 是否为页面正文的原文片段。

    返回 (是否命中, locator, context)；未命中时后两项为 None / 空串。
    这是「原文摘录」与「LLM 概括」的唯一判据 —— 命中才算原文。
    """
    locator = locate_quote(quote, page_text)
    if locator is None:
        return False, None, ""
    context = build_context(page_text, locator["char_start"], locator["char_end"])
    return True, locator, context


class EvidenceRegistry:
    """V5 证据注册表：为每条证据分配稳定 evidence_id 并强制关联来源。

    每条证据保存（对应任务要求）:
    - evidence_id       全局唯一（EV-001 起递增），分配后永不改变
    - source_id         必须指向 SourceRegistry 中已存在的来源，否则拒绝登记
    - quote             证据原文片段；由 verify_quote 对照保存的正文自动验证
    - context           quote 在正文中的前后文
    - locator           段落序号 + 全文字符区间（未命中原文时为 None）
    - supporting_claim  该证据支撑的论断（LLM 抽取的 claim）
    - extraction_method llm_quote_verified（原文命中）/ llm_paraphrase（概括）
    另保存 version_id（quote 针对的正文版本）与 first_seen（轮次/问题）。

    去重规则: 按 (source_id, 规范化 claim) 去重 —— 同一来源同一论断跨轮
    重复发现时复用已有 evidence_id（first_seen 保留首见）；不同来源的
    相同论断各自登记，出处关系绝不丢失。

    每个 evidence dict 同时携带 V4 兼容字段（id/claim/evidence_text/
    source_title/source_url），Phase D 改造 Synthesis 之前，下游
    （证据块构建、引用渲染）无需任何改动即可继续工作。
    """

    def __init__(self, source_registry: SourceRegistry) -> None:
        self.source_registry = source_registry   # 来源事实源，登记时强校验
        self.evidence: dict[str, dict] = {}      # evidence_id -> 证据
        self._by_key: dict[tuple[str, str], str] = {}  # (source_id, norm claim) -> ID
        self._counter = 0                        # evidence_id 分配计数器，只增不减

    # ---- 登记 ----
    def register(self, source_id: str, claim: str, quote: str,
                 version_id: str = "", context: str = "",
                 locator: dict | None = None,
                 extraction_method: str = "llm_extractive",
                 quote_verified: bool = False,
                 round_no: int = 0, question_id: str = "") -> dict | None:
        """登记一条证据，返回 {"evidence": 证据dict, "new": 是否新建}。

        source_id 不存在、claim/quote 为空时拒绝（返回 None），绝不登记
        无法回溯来源的证据。
        """
        source = self.source_registry.get(source_id) if source_id else None
        if source is None:
            _log(f"      [evidence] 拒绝登记: source_id 不存在 ({source_id!r})")
            return None
        claim = (claim or "").strip()
        quote = (quote or "").strip()
        if not claim or not quote:
            _log("      [evidence] 拒绝登记: claim 或 quote 为空")
            return None
        key = (source_id, _norm_text(claim))
        if key in self._by_key:
            return {"evidence": self.evidence[self._by_key[key]], "new": False}
        self._counter += 1
        eid = f"{EVIDENCE_ID_PREFIX}-{self._counter:03d}"
        ev = {
            "evidence_id": eid,
            "source_id": source_id,
            "version_id": version_id or "",
            "quote": quote,
            "context": context,
            "locator": locator,
            "supporting_claim": claim,
            "extraction_method": extraction_method,
            "quote_verified": bool(quote_verified),
            "first_seen": {"round": round_no, "question_id": question_id},
            # ---- V4 兼容字段（Phase D 前的过渡视图，语义与 V4 一致）----
            "id": eid,
            "claim": claim,
            "evidence_text": quote,
            "source_title": source["title"],
            "source_url": source["url"],
        }
        self.evidence[eid] = ev
        self._by_key[key] = eid
        return {"evidence": ev, "new": True}

    def register_from_llm(self, source_id: str, version_id: str,
                          page_text: str, claim: str, llm_text: str,
                          round_no: int = 0, question_id: str = "") -> dict | None:
        """登记 LLM 抽取的证据：先对页面正文验证 quote，再落库。

        - 命中正文 → quote_verified=True，locator/context 齐全，
          extraction_method="llm_quote_verified"
        - 未命中 → 视为 LLM 概括：quote 原样保存并打上
          extraction_method="llm_paraphrase"，绝不把改写内容冒充原文
        """
        verified, locator, context = verify_quote(llm_text, page_text)
        method = "llm_quote_verified" if verified else "llm_paraphrase"
        return self.register(
            source_id=source_id, claim=claim, quote=llm_text,
            version_id=version_id, context=context, locator=locator,
            extraction_method=method, quote_verified=verified,
            round_no=round_no, question_id=question_id,
        )

    # ---- 查询 ----
    def get(self, evidence_id: str) -> dict | None:
        """按 evidence_id 取证据，不存在返回 None。"""
        return self.evidence.get(evidence_id)

    def stats(self) -> dict:
        """证据统计：总数 / 原文命中数 / 概括数（可观察性用）。"""
        verified = sum(1 for e in self.evidence.values() if e["quote_verified"])
        return {"total": len(self.evidence), "quote_verified": verified,
                "paraphrase": len(self.evidence) - verified}

    def __len__(self) -> int:
        return len(self.evidence)


# ------------------------- 搜索 (search) -------------------------

def search(query: str) -> list[dict]:
    """ Search API，返回 [{title, url, content}, ...]。"""
    body = json.dumps({
        "api_key": RESEARCH_API_KEY,
        "query": query,
        "search_depth": SEARCH_DEPTH,
        "max_results": MAX_RESULTS,
    }).encode("utf-8")
    req = urllib.request.Request(
        _join_url(RESEARCH_BASE_URL, "/search"),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Search API 报错 {e.code}: {detail[:500]}") from e
    return data.get("results", [])


def dedup_by_url(results: list[dict]) -> list[dict]:
    """按 URL 去重。"""
    seen, unique = set(), []
    for r in results:
        url = r.get("url")
        if url and url not in seen:
            seen.add(url)
            unique.append(r)
    return unique


def _host_of(url: str) -> str:
    """取 URL 的 host 并去掉 www. 前缀。"""
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_blocked_host(url: str) -> bool:
    """URL 是否命中黑名单域名（含其子域）。"""
    host = _host_of(url)
    return any(host == d or host.endswith("." + d) for d in BLOCKED_DOMAINS)


def filter_results(results: list[dict], stats: dict | None = None) -> list[dict]:
    """源质量过滤：丢掉搜索引擎结果页/跳转页、空壳页和低相关度结果。

    之所以需要它：搜索 API 会把内容农场、SERP 页、聚合页混进结果，
    这些页上的数字往往不可靠，而 LLM 会照样当真。

    过滤条数累加进 stats（由调用方汇总成一行输出，避免打断进度行），
    逐条明细走 _log（VERBOSE 可见）。
    兜底：过滤后若一条不剩，退回「只按黑名单过滤」的结果，绝不清空。
    """
    kept, dropped = [], {"blocked": [], "too_short": [], "low_score": []}
    for r in results:
        url = r.get("url", "")
        if is_blocked_host(url):
            dropped["blocked"].append(url)
            continue
        if len((r.get("content") or "").strip()) < MIN_RESULT_CONTENT:
            dropped["too_short"].append(url)
            continue
        score = r.get("score")
        if isinstance(score, (int, float)) and score < MIN_RESULT_SCORE:
            dropped["low_score"].append(url)
            continue
        kept.append(r)

    for reason, urls in dropped.items():
        for u in urls:
            _log(f"      [filter] 丢弃({reason}): {u}")

    if stats is not None:
        for reason, urls in dropped.items():
            stats[reason] = stats.get(reason, 0) + len(urls)

    if not kept and results:
        fallback = [r for r in results if not is_blocked_host(r.get("url", ""))]
        _log(f"      [filter] 警告: 过滤后无剩余结果，退回 {len(fallback)} 条未拉黑结果")
        if stats is not None:
            stats["fallback"] = stats.get("fallback", 0) + 1
        return fallback
    return kept


# ------------------------- Extract (网页正文抽取) -------------------------

class _TextExtractor(html.parser.HTMLParser):
    """用标准库 html.parser 把 HTML 剥成纯文本。"""

    _BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3",
                   "h4", "h5", "h6", "blockquote", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0            # 进入 <script>/<style> 时深度，正文不计

    def handle_starttag(self, tag: str, attrs: list) -> None:  # noqa: ARG002
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1
        if self._skip_depth == 0 and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.parts.append(data)

    def extract(self) -> str:
        """拼接文本、折叠空白、去掉多余空行。"""
        text = " ".join("".join(self.parts).split())
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        return "\n".join(lines)


def fetch_page(url: str) -> str:
    """GET 抓取网页，返回响应的文本内容（自动解码字符集）。"""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 "
                 "Safari/537.36"},
    )
    with _urlopen(req, timeout=30) as resp:
        raw = resp.read()
    # 优先用响应头里的 charset 解码，否则按 utf-8 并容错
    charset = (resp.headers.get_content_charset() or "utf-8")
    return raw.decode(charset, errors="replace")


def extract_webpage(url: str) -> str:
    """Extract 步骤：抓取单个 URL 的网页正文，截断到 EXTRACT_MAX_CHARS。"""
    html_text = fetch_page(url)
    body = _TextExtractor()
    body.feed(html_text)
    return body.extract()[:EXTRACT_MAX_CHARS]


def normalize_ascii_url(url: str) -> str:
    """把 URL 路径和查询参数里的中文、空格、特殊字符转为百分号编码，重新拼成完整 URL。

    已编码的 %XX 保留不二次编码；路径分隔符 / 与查询分隔符 =& + 也保留。
    """
    parts = urlparse(url)
    path = quote(parts.path, safe="/%@")
    query = quote(parts.query, safe="=&+%/?")
    fragment = quote(parts.fragment, safe="/%")
    return urlunparse(
        (parts.scheme, parts.netloc, path, parts.params, query, fragment)
    )


def extract_results(results: list[dict]) -> list[dict]:
    """对每个结果抓取网页正文，写入 result['extracted']。

    Fallback 流程:
    Extract → 失败 → 判断原因 →
        URL 编码问题 (UnicodeEncodeError) → 百分号编码后重试
        其他错误                           → 跳过（extracted 留空）
    """
    for r in results:
        url = r.get("url", "")
        text, retried = "", False
        try:
            text = extract_webpage(url)
        except UnicodeEncodeError:
            retried = True
            _log(f"      [extract] URL 含中文/特殊字符，百分号编码后重试: {url}")
            try:
                text = extract_webpage(normalize_ascii_url(url))
            except Exception as e:
                _log(f"      [extract] 编码重试仍失败: {url} → {e}")
        except Exception as e:
            _log(f"      [extract] 其他错误，跳过: {url} → {e}")
        r["extracted"] = text
        if text:
            suffix = " (编码后重试)" if retried else ""
            _log(f"      [extract] {url}  → {len(text)} 字符{suffix}")
    return results


# ------------------------- Evidence Extraction -------------------------

_EVIDENCE_PROMPT_TEMPLATE = """你是一名事实核查助手。根据"问题"和下面的"网页正文"，提取与研究问题真正相关的证据 (Evidence)。

要求:
1. 每条证据包含 4 个字段:
   claim: 网页正文能够支持的事实性陈述（回答问题的关键论断）
   evidence_text: 支撑该 claim 的原文摘录或忠实概括（必须来自网页正文）
   source_title: 网页标题
   source_url: 网页 URL
2. claim 和 evidence_text 必须能被网页正文验证; 网页中没有的信息禁止编造。
3. 如果该网页与问题无关，返回空数组 {{"evidence": []}}。
4. 最多提取 {max} 条，不要为了凑数量而生成。
5. 严格输出 JSON，格式如下，不要输出任何其他文字:
{{
  "evidence": [
    {{"claim": "...", "evidence_text": "...", "source_title": "...", "source_url": "..."}}
  ]
}}

问题: {question}

网页标题: {title}

网页URL: {url}

网页正文:
{content}"""


def _validate_evidence(item: dict, page_url: str, page_title: str) -> dict | None:
    """校验并规范化单条证据 LLM 输出。

    - 缺 claim 或 evidence_text 的证据直接丢弃
    - source_url 强制用页面真实 URL（不信 LLM 编的 URL），source_title 缺省用页面标题兜底
    - evidence_text 截断到 EVIDENCE_MAX_TEXT
    """
    claim = (item.get("claim") or "").strip()
    etext = (item.get("evidence_text") or "").strip()
    if not claim or not etext:
        return None
    return {
        "claim": claim,
        "evidence_text": etext[:EVIDENCE_MAX_TEXT],
        "source_title": ((item.get("source_title") or "").strip() or page_title),
        "source_url": page_url,
    }


def _dedup_evidence(evidence_list: list[dict]) -> list[dict]:
    """同一网页内按 (claim 原文, source_url) 去重重复证据。"""
    seen, unique = set(), []
    for ev in evidence_list:
        key = (ev["claim"].strip().lower(), ev["source_url"])
        if key not in seen:
            seen.add(key)
            unique.append(ev)
    return unique


def extract_evidence_from_page(question: str, page: dict) -> list[dict]:
    """Evidence Extraction：对单页抽取证据，返回 list[dict]（不含 id，未分配）。

    错误处理:
    - 正文为空 → 跳过该页
    - LLM 输出非法 JSON / 缺字段 → 重试一次，仍失败则记录错误并跳过该页（不崩溃）
    - 页内重复证据 → 去重
    """
    url = page.get("url", "")
    title = page.get("title", "") or "（无标题）"
    content = (page.get("extracted") or "").strip()
    if not content:
        _log(f"      [evidence] 正文为空，跳过: {url}")
        return []

    prompt = _EVIDENCE_PROMPT_TEMPLATE.format(
        question=question, title=title, url=url,
        content=content[:EXTRACT_MAX_CHARS], max=EVIDENCE_MAX_PER_PAGE,
    )

    for attempt in (1, 2):
        try:
            raw = _chat([{"role": "user", "content": prompt}], temperature=0.2)
            data = _extract_json_object(raw)
            items = data.get("evidence") or []
            validated = [
                ev for ev in
                (_validate_evidence(e, url, title) for e in items)
                if ev is not None
            ]
            unique = _dedup_evidence(validated)
            dropped = len(items) - len(validated)
            if dropped:
                _log(f"      [evidence] 丢弃 {dropped} 条字段不全的证据（{url}）")
            if len(validated) != len(unique):
                _log(f"      [evidence] 页内去重 {(len(validated) - len(unique))} 条")
            return unique[:EVIDENCE_MAX_PER_PAGE]
        except (ValueError, json.JSONDecodeError, KeyError) as e:
            if attempt == 1:
                _log(f"      [evidence] LLM 输出解析失败 ({e})，重试一次...")
                prompt += (
                    "\n\n注意: 上一次输出不是合法 JSON，请严格只输出 JSON 对象，"
                    "不要添加围栏或解释文字。"
                )
            else:
                _log(f"      [evidence] 重试仍失败，跳过该页: {url} → {e}")
                return []


def assign_evidence_ids(evidence_list: list[dict], prefix: str = "") -> list[dict]:
    """给每条证据分配 id，返回原列表。

    prefix 为空时是 EV-001 这类单页编号（v2 用法）；
    传 prefix 时是 EV-q1-001 这类跨子问题唯一编号 —— V3 用它保证
    多个子问题的证据汇总到 Synthesis 时 ID 不冲突。
    """
    for i, ev in enumerate(evidence_list, 1):
        ev["id"] = f"EV-{prefix}{i:03d}" if prefix else f"EV-{i:03d}"
    return evidence_list


# ------------------------- Final Writer (带引用) -------------------------

_WRITER_PROMPT_TEMPLATE = """你是一名研究写作助手。根据下面的 EVIDENCE（证据）回答用户问题。

要求:
1. 只使用 EVIDENCE 中的信息，不得编造证据中没有的内容。
2. 在支撑某一论断的句子末尾标注所用证据，格式为方括号包裹的证据 ID，
   且该 ID 必须原样复制下面 EVIDENCE 标题行里的 ID，不要自己编号、不要写占位符。
3. 用与用户问题相同的语言回答，用要点组织。
4. 若 EVIDENCE 为空或不足以回答，如实说明还缺什么信息，不要强行编造。

EVIDENCE:
{evidence_block}

问题: {question}"""

# 一个方括号里可能是单个 ID 或多个： [EV-001] / [EV-q1-003] / [EV-q3-001, EV-q3-002]
_CITE_GROUP_RE = re.compile(
    r"\[(EV-[A-Za-z0-9\-]+(?:\s*[,，、;；]\s*EV-[A-Za-z0-9\-]+)*)\]"
)
_CITE_ID_RE = re.compile(r"EV-[A-Za-z0-9\-]+")


def build_evidence_block(evidence_list: list[dict]) -> str:
    """把证据列表格式化成给 Writer 的文本块。"""
    if not evidence_list:
        return "（无可用证据）"
    blocks = []
    for ev in evidence_list:
        blocks.append(
            f"[{ev['id']}]\n"
            f"claim: {ev['claim']}\n"
            f"source_title: {ev['source_title']}\n"
            f"source_url: {ev['source_url']}\n"
            f"evidence_text: {ev['evidence_text']}"
        )
    return "\n\n".join(blocks)


def format_citations(answer: str, evidence_list: list[dict]) -> str:
    """把答案里的 [EV-xxx] 引用替换成对外编号 [n]，并在末尾附加引用列表。

    两条规则：
    1. **一处来源一个编号** —— 按 source_url 首次出现顺序编号，同一网页的
       多条证据共用同一个 [n]（同一页被多个子问题抓到时不重复列出）。
    2. 方括号里可能一次给多个 ID（[EV-q3-001, EV-q3-002]），逐个展开成 [n][m]。
    认不出的 EV 引用（LLM 抄了占位符或编了 ID）一律抹掉 —— 内部 ID 绝不能
    泄漏到用户面前，抹掉时打印条数，便于尽早发现 prompt 退化。
    """
    index: dict[str, int] = {}          # 证据 ID -> 对外编号
    refs: list[tuple[str, str]] = []    # (标题, URL)，按首次出现排序
    url_no: dict[str, int] = {}         # URL -> 对外编号，保证同源同号
    for ev in evidence_list:
        url = ev["source_url"]
        if url not in url_no:
            url_no[url] = len(refs) + 1
            refs.append((ev["source_title"], url))
        index[ev["id"]] = url_no[url]

    unknown = 0

    def repl(m: re.Match) -> str:
        nonlocal unknown
        nums: list[int] = []
        for eid in _CITE_ID_RE.findall(m.group(1)):
            if eid in index:
                nums.append(index[eid])
            else:
                unknown += 1
        if not nums:
            return ""
        # 同一括号里多个 ID 指向同一来源时只保留一个编号
        return "".join(f"[{n}]" for n in dict.fromkeys(nums))

    body = _CITE_GROUP_RE.sub(repl, answer)
    if unknown:
        print(f"      [cite] 抹掉 {unknown} 处无法解析的 EV 引用（LLM 未按证据 ID 引用）")

    if refs:
        body += "\n\n## 引用\n" + "\n".join(
            f"[{i}] {title}\nURL: {url}" for i, (title, url) in enumerate(refs, 1)
        )
    return body


def write_cited_answer(question: str, evidence_list: list[dict]) -> str:
    """LLM 依据证据写答案，**保留** [EV-xxx] 内部引用（不重编号、不加引用列表）。

    V3 用它生成子问题小结：小结里的 [EV-xxx] 原样传给 Synthesis，
    这样最终答案才能指回同一个全局证据 ID。
    """
    prompt = _WRITER_PROMPT_TEMPLATE.format(
        question=question,
        evidence_block=build_evidence_block(evidence_list),
    )
    return _chat([{"role": "user", "content": prompt}], temperature=0.3)


def write_final_answer(question: str, evidence_list: list[dict]) -> str:
    """LLM 最后一步：只依据证据写答案，然后把 [EV-xxx] 转成对外引用 [n]。"""
    return format_citations(write_cited_answer(question, evidence_list), evidence_list)


# ------------------------- 子问题研究 (Research) -------------------------

def summarize_sub_question(question: str, id_prefix: str = "",
                           stats: dict | None = None,
                           fixed_queries: list[str] | None = None,
                           query_log: list[str] | None = None,
                           source_registry: SourceRegistry | None = None,
                           evidence_registry: EvidenceRegistry | None = None,
                           round_no: int = 0,
                           question_id: str = "") -> tuple[str, list[dict]]:
    """对单个子问题：Search → Filter → Extract → Evidence → LLM 总结。

    返回 (小结文本, 该子问题的证据列表)。
    V5: 传入两个注册表时，每页正文先注册进 SourceRegistry（得到 source_id
    与正文版本 version_id），每条 LLM 证据再登记进 EvidenceRegistry：
    - quote 用页面正文自动验证，命中的保存 locator 与 context
    - 未命中的按概括（llm_paraphrase）登记，原样保存、不冒充原文
    - evidence_id 全局唯一；跨轮重复发现同一来源同一论断时复用 ID
    不传注册表时保持 V4 行为（id_prefix 前缀编号，无 quote 验证）。
    stats / fixed_queries / query_log 含义同 V4。
    """
    queries = list(fixed_queries) if fixed_queries else generate_queries(question)
    if query_log is not None:
        query_log.extend(queries)
    results = dedup_by_url([r for q in queries for r in search(q)])
    results = filter_results(results, stats=stats)[:MAX_RESULTS]
    extract_results(results)

    all_evidence: list[dict] = []
    for r in results:
        evs = extract_evidence_from_page(question, r)
        if not evs:
            continue
        if source_registry is None or evidence_registry is None:
            # V4 兼容路径：不注册、不验证，交由调用方按前缀编号
            all_evidence.extend(evs)
            continue
        page_text = r.get("extracted") or ""
        reg = source_registry.register(r.get("url", ""), title=r.get("title", ""),
                                       page_text=page_text)
        if reg is None:
            _log(f"      [evidence] 来源注册失败，跳过该页证据: {r.get('url', '')}")
            continue
        kept = 0
        for ev in evs:
            rec = evidence_registry.register_from_llm(
                source_id=reg["source_id"], version_id=reg["version_id"],
                page_text=page_text, claim=ev["claim"],
                llm_text=ev["evidence_text"],
                round_no=round_no, question_id=question_id,
            )
            if rec is None:
                continue
            all_evidence.append(rec["evidence"])
            kept += 1
        _log(f"          [evidence] {r.get('url', '')} → 登记 {kept}/{len(evs)} 条"
             f"（来源 {reg['source_id']}）")
    if source_registry is None:
        assign_evidence_ids(all_evidence, prefix=id_prefix)
    _log(f"          [summary] 共提取 {len(all_evidence)} 条证据")
    # 小结保留 [EV-xxx] 内部引用原样传给 Synthesis，最终答案才能指回同一批证据
    return write_cited_answer(question, all_evidence), all_evidence


def research_sub_question(sub_question: SubQuestion,
                          stats: dict | None = None,
                          fixed_queries: list[str] | None = None,
                          query_log: list[str] | None = None,
                          state: ResearchState | None = None,
                          round_no: int = 0) -> ResearchResult:
    """串行研究单个子问题。任何一步失败都返回带 error 的结果，不抛异常。

    本函数不打印进度：由调用方在一条链上输出，保证终端简洁。
    V5: 传入 state 时，页面正文注册进 SourceRegistry、证据经 EvidenceRegistry
    登记（quote 验证 + source_id 关联）；不传 state 时保持 V4 兼容行为。
    """
    try:
        if state is not None:
            # V5 路径：证据 ID 由 EvidenceRegistry 全局分配，quote 对正文验证
            summary, evidence = summarize_sub_question(
                sub_question.question, stats=stats,
                fixed_queries=fixed_queries, query_log=query_log,
                source_registry=state.source_registry,
                evidence_registry=state.evidence_registry,
                round_no=round_no, question_id=sub_question.id,
            )
        else:
            # V4 兼容路径：证据 ID 带子问题前缀（EV-q1-001），汇总不撞号
            summary, evidence = summarize_sub_question(
                sub_question.question, id_prefix=f"{sub_question.id}-", stats=stats,
                fixed_queries=fixed_queries, query_log=query_log,
            )
        return ResearchResult(
            sub_question_id=sub_question.id,
            sub_question=sub_question.question,
            evidence_list=evidence,
            summary=summary,
        )
    except Exception as e:   # noqa: BLE001 — 单子问题失败不能拖垮整个任务
        return ResearchResult(
            sub_question_id=sub_question.id,
            sub_question=sub_question.question,
            evidence_list=[],
            summary="",
            error=str(e),
        )


# ------------------------- Gap Analyzer (V4 新增) -------------------------

_GAP_ANALYZER_PROMPT_TEMPLATE = """你是一名研究缺口分析师 (Gap Analyzer)。一轮研究刚结束，下面是原始问题、初始研究计划、已完成的研究问题及其小结，以及目前累积的全部 EVIDENCE（跨轮次去重后的全局证据）。请只根据这些材料，判断当前研究还存在哪些缺口，是否足以回答原始问题。

判断要求:
1. 只根据已有 EVIDENCE 和小结判断，禁止虚构证据或假设还没搜到的信息。
2. 区分真正的证据冲突（同一事实在不同来源上数值/结论互相矛盾）和表面上的措辞差异（说法不同但可并存）。只有前者才能放进 conflicts。
3. 不要生成本研究已经回答过的问题；follow_up_questions 必须是尚未研究过的。
4. 不得仅凭证据数量判断研究是否充分；要看证据是否覆盖回答原始问题所需的关键维度、关键结论是否有直接支撑。
5. follow_up_questions 是下一轮最值得研究的问题，按对回答原始问题的重要性标 priority（high/medium/low），每个给出 reason 和 target_gap，并给 1~2 个 suggested_queries。宁缺毋滥，没有值得研究的就给空数组。

严格输出 JSON 对象，不要输出任何其他文字，格式如下:
{{
  "gaps": ["缺口1的描述", "缺口2的描述"],
  "conflicts": [
    {{"description": "冲突描述", "evidence_ids": ["EV-xx-001", "EV-yy-002"]}}
  ],
  "follow_up_questions": [
    {{"question": "自包含的完整问句", "reason": "为什么要研究它",
      "priority": "high", "target_gap": "对应 gaps 中的哪一条",
      "suggested_queries": ["检索式1", "检索式2"]}}
  ],
  "is_sufficient": false,
  "reasoning_summary": "一两句话说明判断理由"
}}

原始问题: {original_question}

初始研究计划:
{plan_block}

已完成的研究问题与小结:
{results_block}

EVIDENCE:
{evidence_block}"""


class GapReport:
    """GapAnalyzer 的结构化输出。"""

    def __init__(self, gaps: list[str], conflicts: list[dict],
                 follow_up_questions: list[dict], is_sufficient: bool,
                 reasoning_summary: str = "", error: str = ""):
        self.gaps = gaps                                # list[str]
        self.conflicts = conflicts                      # list[dict]
        self.follow_up_questions = follow_up_questions  # list[dict]（已校验）
        self.is_sufficient = is_sufficient
        self.reasoning_summary = reasoning_summary
        self.error = error                              # 非空 = 本轮分析失败（已兜底）

    @property
    def ok(self) -> bool:
        return not self.error


def build_plan_block(state: ResearchState) -> str:
    """把初始研究计划格式化成给 GapAnalyzer 的文本块。"""
    return "\n".join(f"- [{sq.id}] {sq.question}"
                     for sq in state.research_plan.sub_questions)


def _validate_follow_up(item: dict) -> dict | None:
    """校验并规范化单条 follow-up：缺 question 的丢弃，priority 归一化。"""
    question = (item.get("question") or "").strip()
    if not question:
        return None
    priority = (item.get("priority") or "medium").strip().lower()
    if priority not in ("high", "medium", "low"):
        priority = "medium"
    return {
        "question": question,
        "reason": (item.get("reason") or "").strip(),
        "priority": priority,
        "target_gap": (item.get("target_gap") or "").strip(),
        "suggested_queries": [
            q.strip() for q in (item.get("suggested_queries") or [])
            if isinstance(q, str) and q.strip()
        ],
    }


def analyze_gaps(state: ResearchState) -> GapReport:
    """GapAnalyzer: 根据当前状态判断缺口、冲突与证据是否充分。

    只根据已有证据判断，不虚构。错误处理: LLM 失败或输出非法 → 重试一次，
    仍失败返回带 error 的兜底报告（按「无新缺口、不足以判定充分」处理），
    绝不让分析失败拖垮整个研究。
    """
    prompt = _GAP_ANALYZER_PROMPT_TEMPLATE.format(
        original_question=state.original_question,
        plan_block=build_plan_block(state),
        results_block=build_round_results_block(state),
        evidence_block=build_evidence_block(state.evidence_list),
    )
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            raw = _chat([{"role": "user", "content": prompt}], temperature=0.2)
            data = _extract_json_object(raw)
            follow_ups = [
                fu for fu in (_validate_follow_up(i)
                              for i in (data.get("follow_up_questions") or []))
                if fu is not None
            ]
            conflicts = []
            for c in (data.get("conflicts") or []):
                desc = (c.get("description") or "").strip()
                if desc:
                    conflicts.append({
                        "description": desc,
                        "evidence_ids": [str(x) for x in (c.get("evidence_ids") or [])],
                    })
            return GapReport(
                gaps=[str(g).strip() for g in (data.get("gaps") or [])
                      if str(g).strip()],
                conflicts=conflicts,
                follow_up_questions=follow_ups,
                is_sufficient=bool(data.get("is_sufficient")),
                reasoning_summary=(data.get("reasoning_summary") or "").strip(),
            )
        except (RuntimeError, ValueError, json.JSONDecodeError,
                KeyError, AttributeError) as e:
            last_err = e
            if attempt == 1:
                _log(f"      [gap] LLM 输出解析失败 ({e})，重试一次...")
                prompt += ("\n\n注意: 上一次输出不是合法 JSON，"
                           "请严格只输出 JSON 对象，不要添加围栏或解释文字。")
    print(f"      [gap] 分析失败，本轮按「无新缺口」兜底: {last_err}")
    return GapReport(gaps=[], conflicts=[], follow_up_questions=[],
                     is_sufficient=False, reasoning_summary="",
                     error=str(last_err))


# ------------------------- Replanner (V4 新增，纯逻辑不调 LLM) -------------------------

_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def replan(state: ResearchState, report: GapReport) -> list[ResearchQuestion]:
    """根据 GapReport 生成下一轮的 Follow-up 问题并加入 pending。

    - 与已完成/待研究问题重复的（规范化文本比对）直接丢弃
    - suggested_queries 全部与搜索历史重复的问题丢弃（再搜也是重复结果）；
      部分重复时只保留全新查询
    - 数量上限 MAX_FOLLOWUPS_PER_ROUND，按 priority high > medium > low 排序
    - 分配唯一 ID（r{轮次}f{n}，最终证据编号形如 EV-r1f1-001），
      记录来源（replanner）、生成原因与针对的缺口
    返回实际加入的问题列表。
    """
    existing = state.question_keys()
    history = {_norm_query(q) for q in state.search_history}

    candidates = sorted(
        report.follow_up_questions,
        key=lambda fu: _PRIORITY_RANK.get(fu["priority"], 1),
    )
    added: list[ResearchQuestion] = []
    for fu in candidates:
        if len(added) >= MAX_FOLLOWUPS_PER_ROUND:
            break
        key = _norm_question(fu["question"])
        if key in existing:
            _log(f"      [replan] 丢弃重复问题: {fu['question']}")
            continue
        fresh = [q for q in fu["suggested_queries"]
                 if _norm_query(q) not in history]
        if fu["suggested_queries"] and not fresh:
            _log(f"      [replan] 丢弃无新查询的问题: {fu['question']}")
            continue
        rq = ResearchQuestion(
            qid=f"r{state.current_round}f{len(added) + 1}",
            question=fu["question"],
            source="replanner",
            reason=fu["reason"],
            target_gap=fu["target_gap"],
            suggested_queries=fresh,
        )
        added.append(rq)
        existing.add(key)                       # 同一批 follow-up 内也互相去重
        history.update(_norm_query(q) for q in fresh)
    if added:
        state.add_follow_ups(added)
    return added


# ------------------------- Synthesis (综合，V4: 读取全量状态) -------------------------

_SYNTHESIS_PROMPT_TEMPLATE = """你是一名研究合著者 (Synthesizer)。用户有一个原始问题，系统用多轮「研究 → 缺口分析 → 追加研究」的方式收集证据。下面是每个已研究问题的研究小结（标注了轮次，Follow-up 问题附有研究它的原因），以及跨轮次累积去重后的全部 EVIDENCE。请综合所有轮次的结果，回答用户的原始问题。

要求:
1. 用与原始问题相同的语言回答，用清晰的要点组织，结构完整。
2. 只使用 EVIDENCE 中的信息，不得编造证据中没有的内容，尤其是数字和规模。
3. 在支撑某一论断的句子末尾标注所用证据，格式为方括号包裹的证据 ID，
   且该 ID 必须原样复制下面 EVIDENCE 标题行里的 ID，不要自己编号、不要写占位符。
4. 某个问题研究失败时，不要编造其内容，可以在相关位置说明该方向暂无研究结果。
5. UNRESOLVED GAPS 列出的是研究结束时仍未解决的缺口。如果它们影响答案的完整性，必须在答案末尾用「未解决的不确定性」小节如实披露，不要假装所有问题都已解决。
6. 先总述原始问题的核心答案，再按研究问题展开。
7. (V5 新增) 整个回答必须是**一个 JSON 对象**，恰好包含两个字段:
   - "answer_markdown": 字符串。满足上述 1~6 条的完整 Markdown 正文，
     内部换行写成 \\n 转义，证据 ID 引用照常写在正文里；
   - "claims": 数组。列出 3~8 条支撑核心答案的重要结论，每条形如:
     {{"text": "结论原文，独立可验证的陈述句，不含证据 ID 引用标记",
       "evidence_ids": ["支撑该结论的 EVIDENCE ID"],
       "support_status": "supported"}}
   claims 的硬性规则:
   - evidence_ids 只能引用 EVIDENCE 块中实际存在的 ID。研究小结只是摘要，
     不是证据 —— 禁止把小结里的表述当作证据来源。
   - 结论中的数字、日期、比例、因果关系必须有对应证据 ID 支撑；
     确实没有证据支撑的结论，如实标 "unsupported"，不要硬凑或编造。
   - support_status 取值: supported / partial / unsupported / contradicted /
     unverified，反映证据对结论的支持程度。
   - 不同来源的证据内容相似不代表来源独立，判断支持程度请依据证据原文本身。
8. 只输出 JSON 对象本身，不要输出任何解释文字或代码围栏。

原问题: {original_question}

各研究问题的小结（按轮次）:

{results_block}

EVIDENCE:
{evidence_block}

UNRESOLVED GAPS（研究结束时的未解决缺口）:
{gaps_block}"""


def build_round_results_block(state: ResearchState) -> str:
    """把各轮已完成问题的研究结果拼成带轮次/来源标注的文本块。"""
    blocks = []
    for c in state.completed_questions:
        r: ResearchResult = c["result"]
        rq: ResearchQuestion = c["question"]
        head = f"[第{c['round']}轮][{r.sub_question_id}]"
        if rq.source == "replanner":
            head += f"（Follow-up: {rq.reason}）" if rq.reason else "（Follow-up）"
        if r.ok:
            blocks.append(f"{head} {r.sub_question}\n研究结果:\n{r.summary}")
        else:
            blocks.append(
                f"{head} {r.sub_question}\n"
                "研究结果: (该问题研究失败，暂无可用研究结果。)")
    if not blocks:
        return "（尚无任何研究结果。）"
    return "\n\n" + "\n\n".join(blocks) + "\n"


def build_gaps_block(state: ResearchState) -> str:
    """未解决缺口的文本块（Synthesis 披露用）。"""
    if not state.open_gaps:
        return "（无）"
    return "\n".join(f"- {g}" for g in state.open_gaps)


def validate_citations(answer: str, evidence_list: list[dict]) -> dict:
    """Citation Validation (V4 新增): 转对外编号前，检查 [EV-xxx] 引用质量。

    统计答案中出现的内部引用数、可解析数、无法解析数并打印（可观察性）；
    无法解析的引用随后由 format_citations 统一抹掉（V3 既有机制）。
    """
    known = {ev["id"] for ev in evidence_list}
    cited: list[str] = []
    for m in _CITE_GROUP_RE.finditer(answer):
        for eid in _CITE_ID_RE.findall(m.group(1)):
            if eid not in cited:
                cited.append(eid)
    stats = {
        "cited": len(cited),
        "valid": sum(1 for eid in cited if eid in known),
        "invalid": sum(1 for eid in cited if eid not in known),
    }
    print(f"      [cite-check] 内部引用 {stats['cited']} 处"
          f"（有效 {stats['valid']} / 无法解析 {stats['invalid']}"
          f"{'，无效引用将抹掉' if stats['invalid'] else ''}）")
    return stats


# ------------------------- Claim–Evidence Mapping (V5 Phase D) -------------------------

SUPPORT_STATUSES = ("supported", "partial", "unsupported", "contradicted", "unverified")

# 结论中「必须有证据支撑」的要素启发式：数字（涵盖日期/比例/金额）与因果判断。
# 第一版刻意保守 —— 含任何阿拉伯数字即视为数字结论，宁可多要求证据。
_NUM_RE = re.compile(r"\d")
_CAUSAL_RE = re.compile(
    r"导致|因为|因此|由于|造成|引发|使得|归因于|得益于|原因是|从而使|进而|由此可见")


def claim_evidence_requirements(text: str) -> list[str]:
    """检测结论中需要证据支撑的要素（数字/日期/比例/因果）。"""
    reqs: list[str] = []
    if _NUM_RE.search(text):
        reqs.append("numbers")
    if _CAUSAL_RE.search(text):
        reqs.append("causality")
    return reqs


def validate_claims(raw_claims: list, evidence_list: list[dict]) -> tuple[list[dict], dict]:
    """程序校验 Synthesis 输出的结构化结论（Phase D 核心规则）。

    程序负责裁决，LLM 的自述不算数:
    - claim_id 按序分配 CLM-001…，忽略 LLM 给的任何编号
    - evidence_ids 只保留 EVIDENCE 中实际存在的 ID（剔除的记入
      invalid_evidence_ids）—— 研究小结不是证据，冒充证据的引用一律剔除
    - support_status 归一化到五种枚举；声称 supported/partial/contradicted
      却给不出任何有效证据 ID → 强制 unsupported，不强行生成结论
    - source_ids 由 evidence_ids 反查证据数据计算 —— 两条证据内容相似
      不代表来源独立，独立性只看真实 distinct 来源数
    - text 为空 / 与已有结论重复 → 丢弃并计数
    返回 (claims, stats)，stats 供运行摘要打印与 Phase E 复用。
    """
    evidence_by_id = {e.get("id") or e.get("evidence_id"): e for e in evidence_list}
    claims: list[dict] = []
    stats = {"total": 0, "kept": 0, "dropped": 0, "invalid_evidence_refs": 0,
             "forced_unsupported": 0, "by_status": {}}
    seen_texts: set[str] = set()
    for raw in raw_claims or []:
        stats["total"] += 1
        if not isinstance(raw, dict):
            stats["dropped"] += 1
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            stats["dropped"] += 1
            continue
        norm = _norm_text(text)
        if norm in seen_texts:
            stats["dropped"] += 1
            continue
        seen_texts.add(norm)
        valid_ids: list[str] = []
        invalid_ids: list[str] = []
        for eid in raw.get("evidence_ids") or []:
            eid = str(eid).strip()
            if not eid:
                continue
            if eid in evidence_by_id and eid not in valid_ids:
                valid_ids.append(eid)
            else:
                invalid_ids.append(eid)
        stats["invalid_evidence_refs"] += len(invalid_ids)
        status = str(raw.get("support_status") or "").strip().lower()
        if status not in SUPPORT_STATUSES:
            status = "unverified"
        if not valid_ids:
            if status in ("supported", "partial", "contradicted"):
                stats["forced_unsupported"] += 1
            status = "unsupported"
        claims.append({
            "claim_id": f"{CLAIM_ID_PREFIX}-{len(claims) + 1:03d}",
            "text": text,
            "evidence_ids": valid_ids,
            "invalid_evidence_ids": invalid_ids,
            "support_status": status,
            "source_ids": sorted({evidence_by_id[i]["source_id"]
                                  for i in valid_ids
                                  if evidence_by_id[i].get("source_id")}),
            "needs_evidence": claim_evidence_requirements(text),
            "origin": "synthesis",
        })
        stats["kept"] += 1
        stats["by_status"][status] = stats["by_status"].get(status, 0) + 1
    return claims, stats


def _parse_synthesis_reply(reply: str) -> tuple[str | None, list | None]:
    """解析 Synthesis 的 JSON 回复，返回 (answer_markdown, claims)。

    结构不符合约定（无 JSON / 字段缺失 / 正文为空）时返回 (None, None)，
    由调用方决定重试或兜底 —— 绝不把解析失败静默当成「没有结论」。
    """
    try:
        data = _extract_json_object(reply)
    except ValueError:          # 无 JSON / JSON 非法（JSONDecodeError 是其子类）
        return None, None
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        return None, None
    answer = str(data.get("answer_markdown") or "").strip()
    if not answer:
        return None, None
    return answer, data["claims"]


def synthesize_from_state(state: ResearchState) -> tuple[str, list[dict]]:
    """Synthesis (V5): 读取全量证据与小结，输出 (正文, 结构化结论 claims)。

    - 双通道输出：正文 answer_markdown（内联 [EV-xxx]，**转编号由 Phase F 的
      render_report 统一负责**，这里原样返回）与 claims（重要结论的结构化
      列表）互相印证，转编号前各自过校验
    - 解析失败重试一次；仍失败按 V4 行为兜底（整段当正文、claims 为空），
      引用链在解析层不断裂，但显式打印警告，不静默吞掉
    - claims 一律经 validate_claims 程序校验后存入 state.claims
    - evidence_list 入库时已全局去重，这里全量交给 LLM（不只读最后一轮）
    """
    evidence = state.evidence_list
    prompt = _SYNTHESIS_PROMPT_TEMPLATE.format(
        original_question=state.original_question,
        results_block=build_round_results_block(state),
        evidence_block=build_evidence_block(evidence),
        gaps_block=build_gaps_block(state),
    )
    answer, claims_raw, parse_ok = "", [], False
    reply = ""
    for attempt in (1, 2):
        reply = _chat([{"role": "user", "content": prompt}], temperature=0.3)
        answer, claims_raw = _parse_synthesis_reply(reply)
        if answer is not None:
            parse_ok = True
            break
        _log(f"      [synthesis] 第 {attempt} 次输出未解析出约定 JSON，重试…")
    if not parse_ok:
        # 兜底：按 V4 行为把整段回复当正文；结构化结论为空并显式警告
        print("      [synthesis] 警告: 未解析出结构化结论（按纯文本兜底，claims 为空）")
        answer, claims_raw = reply, []
    claims, claims_stats = validate_claims(claims_raw, evidence)
    state.claims = claims
    state.claims_stats = claims_stats
    bs = claims_stats["by_status"]
    tail = (f"，剔除无效证据引用 {claims_stats['invalid_evidence_refs']} 处"
            if claims_stats["invalid_evidence_refs"] else "")
    print(f"      [claims] 结构化结论 {claims_stats['kept']} 条"
          f"（supported {bs.get('supported', 0)} / partial {bs.get('partial', 0)}"
          f" / unsupported {bs.get('unsupported', 0)}"
          f" / contradicted {bs.get('contradicted', 0)}"
          f" / unverified {bs.get('unverified', 0)}）{tail}")
    validate_citations(answer, evidence)
    # 渲染（[EV-xxx]→[n]、References、追溯表、质量说明）统一交给 render_report
    return answer, claims


# ------------------------- Citation Validator (V5 Phase E) -------------------------

_STATUS_RANK = {"contradicted": 0, "unsupported": 1, "partial": 2,
                "unverified": 3, "supported": 4}


def check_ids(claim: dict, evidence_registry: EvidenceRegistry,
              source_registry: SourceRegistry) -> dict:
    """检查 ① ID Validation：证据/来源/正文版本是否真实存在。"""
    missing_evidence: list[str] = []
    missing_source: list[str] = []
    missing_version: list[str] = []
    for eid in claim["evidence_ids"]:
        ev = evidence_registry.get(eid)
        if ev is None:
            missing_evidence.append(eid)
            continue
        sid = ev.get("source_id") or ""
        if source_registry.get(sid) is None:
            missing_source.append(eid)
            continue
        vid = ev.get("version_id") or ""
        if vid and source_registry.get_version(sid, vid) is None:
            missing_version.append(eid)
    ok = not (missing_evidence or missing_source or missing_version)
    return {"status": "pass" if ok else "fail",
            "missing_evidence_ids": missing_evidence,
            "missing_source_evidence_ids": missing_source,
            "missing_version_evidence_ids": missing_version}


def check_urls(claim: dict, source_registry: SourceRegistry,
               reachable: bool = False) -> dict:
    """检查 ② URL Validation：来源 URL 是否可解析（可选：是否可访问）。

    - 不可解析（缺 scheme/host）→ fail：来源无法追溯，属于硬伤
    - 不可达（仅 reachable=True 时检查）→ warn：网络抖动不代表来源伪造
    """
    issues: list[dict] = []
    checked: list[str] = []
    for sid in claim.get("source_ids") or []:
        source = source_registry.get(sid)
        if source is None:
            issues.append({"source_id": sid, "url": "", "problem": "来源不存在",
                           "severity": "fail"})
            continue
        url = source["url"]
        checked.append(sid)
        try:
            canonicalize_url(url)
        except ValueError:
            issues.append({"source_id": sid, "url": url,
                           "problem": "URL 无法解析（缺少 scheme 或 host）",
                           "severity": "fail"})
            continue
        if reachable and not _url_reachable(url):
            issues.append({"source_id": sid, "url": url,
                           "problem": "URL 网络可达性检查失败",
                           "severity": "warn"})
    failed = any(i["severity"] == "fail" for i in issues)
    return {"status": "fail" if failed else ("warn" if issues else "pass"),
            "checked_source_ids": checked, "issues": issues}


def _url_reachable(url: str, timeout: int = 10) -> bool:
    """可选的网络可达性检查（HEAD 请求）。默认不启用（URL_REACHABILITY_CHECK）。"""
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:   # noqa: BLE001 —— 网络层任何失败都只记 warn
        return False


def check_quotes(claim: dict, evidence_registry: EvidenceRegistry,
                 source_registry: SourceRegistry) -> dict:
    """检查 ③ Quote Validation：对保存的正文**复验**每条证据的原文摘录。

    - 重新执行 Phase C 的 verify_quote：登记时声称原文命中的，复验必须命中
    - 顺带校验正文版本 content_hash 与现算一致（防止注册后正文被改动）
    - paraphrase 证据不判 fail（Phase C 设计允许概括），记 warn 供呈现
    """
    issues: list[dict] = []
    for eid in claim["evidence_ids"]:
        ev = evidence_registry.get(eid)
        if ev is None:
            continue                     # id_check 已报，不重复
        sid, vid = ev.get("source_id") or "", ev.get("version_id") or ""
        version = source_registry.get_version(sid, vid) if vid else None
        if version is None:
            issues.append({"evidence_id": eid, "problem": "正文版本缺失，无法复验",
                           "severity": "fail"})
            continue
        if hash_page_text(version["page_text"]) != version["content_hash"]:
            issues.append({"evidence_id": eid,
                           "problem": "正文版本 content_hash 与现算不符（正文可能被改动）",
                           "severity": "fail"})
            continue
        verified, _, _ = verify_quote(ev["quote"], version["page_text"])
        if verified and ev.get("quote_verified"):
            continue                     # 原文摘录复验命中 —— 通过
        if verified and not ev.get("quote_verified"):
            issues.append({"evidence_id": eid,
                           "problem": "quote 复验命中原文，但登记时标记为概括",
                           "severity": "warn"})
        elif not verified and ev.get("quote_verified"):
            issues.append({"evidence_id": eid,
                           "problem": "登记时标记为原文命中，但复验未命中",
                           "severity": "fail"})
        else:
            issues.append({"evidence_id": eid,
                           "problem": "证据为 LLM 概括，未经原文逐字验证",
                           "severity": "warn"})
    failed = any(i["severity"] == "fail" for i in issues)
    return {"status": "fail" if failed else ("warn" if issues else "pass"),
            "issues": issues}


def check_conflicts(claim: dict, state: ResearchState) -> dict:
    """检查 ⑤ Conflict Validation：结论引用的证据是否落在已发现的冲突集合内。

    冲突来源 = GapAnalyzer 在研究轮次中标记的 identified_conflicts。
    冲突不武断改判结论（可能是真实的观点分歧），只记 warn 供报告呈现。
    """
    claim_ids = set(claim["evidence_ids"])
    conflicting: set[str] = set()
    descriptions: list[str] = []
    for conflict in state.identified_conflicts or []:
        overlap = claim_ids & set(conflict.get("evidence_ids") or [])
        if overlap:
            conflicting |= overlap
            descriptions.append(conflict.get("description") or "")
    descriptions = [d for d in descriptions if d]
    return {"status": "warn" if conflicting else "pass",
            "conflicting_evidence_ids": sorted(conflicting),
            "descriptions": descriptions}


_SEMANTIC_CHECK_PROMPT = """你是一名事实核查员。请判断下面的研究结论是否被其引用的证据原文支持。

结论: {claim_text}
该结论需要证据支撑的要素: {requirements}

证据（摘自抓取保存的网页正文）:
{evidence_block}

任务:
1. 逐条检查每条证据的原文是否支持结论，特别是数字、日期、比例和因果关系。
2. 只输出一个 JSON 对象，恰好包含四个字段:
   {{"status": "supported 或 partial 或 unsupported 或 contradicted",
     "supporting_evidence_ids": ["支持结论的证据 ID"],
     "contradicting_evidence_ids": ["与结论矛盾的证据 ID"],
     "explanation": "一句话判断依据"}}
   - status=contradicted 仅当存在与结论直接矛盾的证据
   - 支持与矛盾的证据 ID 只能从上面列出的证据 ID 中选择
   - 证据之间互相矛盾时，把矛盾方放进 contradicting_evidence_ids
   - 把握不足的要素宁可判 partial，不要臆断
"""

_SECOND_PASS_PROMPT = """你是一名独立复核员。此前另一名核查员对下述结论做过判断，但你独立工作，不要猜测他的结论，也不存在「标准答案」。

结论: {claim_text}

证据原文:
{evidence_block}

请独立判断证据是否支持该结论，只输出一个 JSON 对象:
{{"status": "supported 或 partial 或 unsupported 或 contradicted",
  "supporting_evidence_ids": ["支持结论的证据 ID"],
  "contradicting_evidence_ids": ["与结论矛盾的证据 ID"],
  "explanation": "一句话判断依据"}}"""


def _build_semantic_evidence_block(claim: dict, evidence_registry: EvidenceRegistry,
                                   source_registry: SourceRegistry) -> str:
    """把结论引用的证据拼成语义校验用的文本块（含原文验证状态标注）。"""
    lines: list[str] = []
    for idx, eid in enumerate(claim["evidence_ids"], 1):
        ev = evidence_registry.get(eid)
        if ev is None:
            continue
        source = source_registry.get(ev.get("source_id") or "")
        domain = source["domain"] if source else "未知来源"
        title = (source["title"] or "无标题") if source else "未知来源"
        state_desc = ("已程序验证：确为来源页面原文" if ev.get("quote_verified")
                      else "注意：此为 LLM 概括，未经原文逐字验证")
        lines.append(f"[{idx}] 证据 ID: {eid}｜来源域名: {domain}｜标题: {title}")
        lines.append(f"    原文摘录: \"{ev['quote']}\"")
        lines.append(f"    摘录状态: {state_desc}")
    return "\n".join(lines)


def check_semantics(claim: dict, evidence_registry: EvidenceRegistry,
                    source_registry: SourceRegistry) -> dict:
    """检查 ④ Semantic Validation：LLM 判断证据原文是否支持结论。

    必须输出结构化结果（status / supporting_evidence_ids /
    contradicting_evidence_ids / explanation），且程序做三重把关 ——
    LLM 声称「引用正确」本身不构成正确性证明:
    - supporting/contradicting ID 只保留属于本结论的引用（范围外一律剔除）
    - status 归一化到枚举；声称 supported 却给不出任何支持 ID → unverified
    - LLM 调用/解析失败 → unverified，不阻塞整体，也不静默（error 留痕）
    """
    if not SEMANTIC_CHECK_ENABLED:
        return {"status": "unverified", "supporting_evidence_ids": [],
                "contradicting_evidence_ids": [], "explanation": "语义校验已关闭",
                "error": "disabled"}
    prompt = _SEMANTIC_CHECK_PROMPT.format(
        claim_text=claim["text"],
        requirements="、".join(claim.get("needs_evidence") or []) or "无特殊要素",
        evidence_block=_build_semantic_evidence_block(claim, evidence_registry,
                                                      source_registry),
    )
    allowed = set(claim["evidence_ids"])
    try:
        raw = _chat([{"role": "user", "content": prompt}], temperature=0.2)
        data = _extract_json_object(raw)
    except Exception as e:   # noqa: BLE001 —— LLM/解析失败只降级为 unverified
        return {"status": "unverified", "supporting_evidence_ids": [],
                "contradicting_evidence_ids": [],
                "explanation": "语义校验未能完成", "error": str(e)[:200]}
    if not isinstance(data, dict):
        return {"status": "unverified", "supporting_evidence_ids": [],
                "contradicting_evidence_ids": [], "explanation": "语义校验输出异常",
                "error": "not a dict"}
    status = str(data.get("status") or "").strip().lower()
    if status not in ("supported", "partial", "unsupported", "contradicted"):
        status = "unverified"
    supporting = [str(i).strip() for i in data.get("supporting_evidence_ids") or []
                  if str(i).strip() in allowed]
    contradicting = [str(i).strip() for i in data.get("contradicting_evidence_ids") or []
                     if str(i).strip() in allowed]
    if status == "supported" and not supporting:
        status = "unverified"    # 声称支持却指不出证据 —— 不采纳其自述
    if status == "contradicted" and not contradicting:
        status = "unverified"
    return {"status": status, "supporting_evidence_ids": supporting,
            "contradicting_evidence_ids": contradicting,
            "explanation": str(data.get("explanation") or "")[:300],
            "error": None}


def _merge_final_status(phase_d_status: str, hard_fail: bool,
                        semantic_status: str) -> str:
    """合并程序检查与语义判定，得到结论的最终支持状态（保守优先）。"""
    if hard_fail:
        return "unsupported"         # 引用链断裂，结论失去依据
    if semantic_status == "contradicted":
        return "contradicted"
    if semantic_status == "unsupported":
        return "unsupported"
    if semantic_status == "partial":
        return "partial"
    return phase_d_status            # 语义 unverified / supported → 维持原判定


def second_pass_verify(claim: dict, evidence_registry: EvidenceRegistry,
                       source_registry: SourceRegistry) -> dict | None:
    """对关键结论做独立二次核验：全新 prompt、不泄露第一次的任何判断。"""
    prompt = _SECOND_PASS_PROMPT.format(
        claim_text=claim["text"],
        evidence_block=_build_semantic_evidence_block(claim, evidence_registry,
                                                      source_registry),
    )
    allowed = set(claim["evidence_ids"])
    try:
        raw = _chat([{"role": "user", "content": prompt}], temperature=0.2)
        data = _extract_json_object(raw)
    except Exception as e:   # noqa: BLE001
        return {"status": "unverified", "explanation": "二次核验未能完成",
                "error": str(e)[:200]}
    if not isinstance(data, dict):
        return {"status": "unverified", "explanation": "二次核验输出异常",
                "error": "not a dict"}
    status = str(data.get("status") or "").strip().lower()
    if status not in ("supported", "partial", "unsupported", "contradicted"):
        status = "unverified"
    return {"status": status,
            "explanation": str(data.get("explanation") or "")[:300],
            "supporting_evidence_ids": [str(i).strip()
                                        for i in data.get("supporting_evidence_ids") or []
                                        if str(i).strip() in allowed],
            "contradicting_evidence_ids": [str(i).strip()
                                           for i in data.get("contradicting_evidence_ids") or []
                                           if str(i).strip() in allowed],
            "error": None}


def validate_citation_chain(state: ResearchState, semantic: bool = True,
                            second_pass: bool = True,
                            url_reachable: bool | None = None
                            ) -> tuple[list[dict], dict]:
    """对 state.claims 逐条执行五级检查，返回 (reports, summary)。

    每条结论一份报告，overall 取最严:
    - fail：ID / URL / Quote 任一硬伤，或语义判定 contradicted
    - warn：paraphrase 证据 / 来源冲突 / 语义 partial
    - pass：五项全过
    最终支持状态由 _merge_final_status 程序合并后写回 claim["support_status"]；
    关键结论（含数字/因果要素且有证据引用）追加独立二次核验，两次判定
    不一致 → needs_review=True 并取更保守者。summary 供运行摘要打印。
    """
    if url_reachable is None:
        url_reachable = URL_REACHABILITY_CHECK
    key_claims = [c for c in state.claims
                  if c["evidence_ids"] and c.get("needs_evidence")]
    reports: list[dict] = []
    summary = {"checked": 0, "pass": 0, "warn": 0, "fail": 0,
               "invalid_refs": 0, "conflict_claims": 0,
               "second_pass": 0, "needs_review": 0, "semantic_unverified": 0}
    for claim in state.claims:
        report = {"claim_id": claim["claim_id"]}
        report["id_check"] = check_ids(claim, state.evidence_registry,
                                       state.source_registry)
        report["url_check"] = check_urls(claim, state.source_registry,
                                         reachable=url_reachable)
        report["quote_check"] = check_quotes(claim, state.evidence_registry,
                                             state.source_registry)
        hard_fail = (report["id_check"]["status"] == "fail"
                     or report["url_check"]["status"] == "fail"
                     or report["quote_check"]["status"] == "fail")
        if not claim["evidence_ids"]:
            report["semantic_check"] = {
                "status": "unsupported", "supporting_evidence_ids": [],
                "contradicting_evidence_ids": [],
                "explanation": "结论无证据引用，跳过语义校验", "error": None}
        elif semantic:
            report["semantic_check"] = check_semantics(
                claim, state.evidence_registry, state.source_registry)
        else:
            report["semantic_check"] = {"status": "unverified",
                                        "supporting_evidence_ids": [],
                                        "contradicting_evidence_ids": [],
                                        "explanation": "语义校验已关闭",
                                        "error": "disabled"}
        report["conflict_check"] = check_conflicts(claim, state)
        semantic_status = report["semantic_check"]["status"]
        final_status = _merge_final_status(claim["support_status"], hard_fail,
                                           semantic_status)
        statuses = [report["id_check"]["status"], report["url_check"]["status"],
                    report["quote_check"]["status"],
                    report["conflict_check"]["status"]]
        if hard_fail or semantic_status == "contradicted":
            overall = "fail"
        elif ("warn" in statuses
              or semantic_status in ("partial", "unsupported")):
            overall = "warn"             # 无证据/部分支持/概括证据都需注意
        else:
            overall = "pass"

        # ---- 关键结论二次核验（独立复核，上限 SECOND_PASS_MAX_CLAIMS）----
        if second_pass and any(claim is kc for kc in key_claims[:SECOND_PASS_MAX_CLAIMS]):
            sp = second_pass_verify(claim, state.evidence_registry,
                                    state.source_registry)
            report["second_pass"] = sp
            summary["second_pass"] += 1
            if sp and sp.get("status") not in (None, "unverified"):
                if _STATUS_RANK.get(sp["status"], 3) != _STATUS_RANK.get(final_status, 3):
                    claim["needs_review"] = True
                    if (_STATUS_RANK.get(final_status, 3)
                            > _STATUS_RANK.get(sp["status"], 3)):
                        final_status = sp["status"]   # 取更保守者

        claim["support_status"] = final_status
        claim["validation"] = {"overall": overall,
                               "semantic_status": semantic_status,
                               "has_conflict":
                                   report["conflict_check"]["status"] == "warn",
                               "needs_review": bool(claim.get("needs_review"))}
        if claim.get("needs_review"):
            summary["needs_review"] += 1
        summary["checked"] += 1
        summary[overall] += 1
        summary["invalid_refs"] += len(claim.get("invalid_evidence_ids") or [])
        if report["conflict_check"]["status"] == "warn":
            summary["conflict_claims"] += 1
        if semantic_status == "unverified" and claim["evidence_ids"]:
            summary["semantic_unverified"] += 1
        reports.append(report)
    state.claim_reports = reports
    state.validation_summary = summary
    print(f"      [validate] {summary['checked']} 条结论：pass {summary['pass']}"
          f" / warn {summary['warn']} / fail {summary['fail']}"
          f"（二次核验 {summary['second_pass']}"
          f"，需人工复核 {summary['needs_review']}）")
    return reports, summary


# ------------------------- Citation Renderer (V5 Phase F) -------------------------

def _source_ref_entry(state: ResearchState, evidence: dict,
                      source: dict | None) -> dict:
    """构建参考文献条目的元数据 —— **只**使用已保存的来源数据，不接受任何 LLM 输入。"""
    if source:
        return {"source_id": source["source_id"], "url": source["url"],
                "title": source["title"], "domain": source["domain"],
                "retrieved_at": source["retrieved_at"],
                "published_at": source["published_at"]}
    # 极端兜底：证据自带的、注册时保存的来源元数据（同样非 LLM 生成）
    url = evidence.get("source_url") or ""
    return {"source_id": "", "url": url, "title": evidence.get("source_title") or "",
            "domain": _host_of(url), "retrieved_at": "",
            "published_at": ""}


def _ref_link(entry: dict) -> str:
    """参考文献单行：Markdown 可点击链接 + 程序拼接的元数据（无标题不伪造）。"""
    title = entry["title"] or "(无标题)"
    link = f"[{title}]({entry['url']})" if entry["url"] else f"`{title}`"
    meta = [entry["domain"]] if entry["domain"] else []
    if entry["retrieved_at"]:
        meta.append(f"抓取于 {entry['retrieved_at'][:10]}")
    if entry["published_at"]:
        meta.append(f"发布于 {entry['published_at']}")
    return f"{link} — {'，'.join(meta)}" if meta else link


def _assign_reference_numbers(state: ResearchState, answer: str) -> tuple[dict, dict]:
    """分配引用编号：正文引用顺序 → claims 引用顺序；同源（source_id 或 URL）同号。

    返回 (num_by_evidence, ref_sources)：
    - num_by_evidence: evidence_id → 对外编号 [n]
    - ref_sources:     对外编号 → 参考文献条目元数据（仅被实际引用的来源）
    无法归属到任何来源的证据不参与编号（其正文引用会被抹掉），
    保证每个 [n] 都能对应 References 中真实存在的条目。
    """
    evidence_by_id = {e.get("id") or e.get("evidence_id"): e
                      for e in state.evidence_list}
    ordered: list[str] = []

    def _collect(eid: str) -> None:
        if eid in evidence_by_id and eid not in ordered:
            ordered.append(eid)

    for m in _CITE_GROUP_RE.finditer(answer):       # 正文首次出现顺序
        for eid in _CITE_ID_RE.findall(m.group(1)):
            _collect(eid)
    for claim in state.claims:                      # 再补 claims 引用（正文未引到的）
        for eid in claim["evidence_ids"]:
            _collect(eid)
    num_by_source: dict[str, int] = {}
    num_by_evidence: dict[str, int] = {}
    ref_sources: dict[int, dict] = {}
    for eid in ordered:
        evidence = evidence_by_id[eid]
        source = state.source_registry.get(evidence.get("source_id") or "")
        key = source["source_id"] if source else (evidence.get("source_url") or "")
        if not key:
            continue
        if key not in num_by_source:
            num = len(num_by_source) + 1
            num_by_source[key] = num
            ref_sources[num] = _source_ref_entry(state, evidence, source)
        num_by_evidence[eid] = num_by_source[key]
    return num_by_evidence, ref_sources


def _render_trace_table(state: ResearchState, num_by_evidence: dict) -> str:
    """结论与证据追溯表（Markdown）：每条结论一行，内部 Evidence ID 保留。"""
    if not state.claims:
        return ""
    lines = ["## 结论与证据追溯", "",
             "| 结论 ID | 结论 | 最终判定 | 证据 → 引用编号 |",
             "|---|---|---|---|"]
    for claim in state.claims:
        text = (claim["text"].replace("|", "\\|").replace("\n", " ")
                if claim.get("text") else "")
        status = claim["support_status"]
        if claim.get("needs_review"):
            status += " ⚠️待复核"
        cells = []
        for eid in claim["evidence_ids"]:
            num = num_by_evidence.get(eid)
            cells.append(f"{eid} → [{num}]" if num else f"{eid} → 无效")
        if not cells:
            cells = ["—（无证据）"]
        lines.append(f"| {claim['claim_id']} | {text} | {status} "
                     f"| {'、'.join(cells)} |")
    return "\n".join(lines)


def _render_quality_section(state: ResearchState) -> str:
    """引用质量说明：Phase D/E 的校验结果显式呈现，失败绝不静默省略。"""
    vs = state.validation_summary or {}
    lines: list[str] = []
    if not state.claim_reports:
        if not state.claims:
            return "- 本次未获得结构化结论，引用链未经校验。"
        return "- 本次未执行引用链校验，以上引用未经复核。"
    lines.append(f"- 校验结论：pass {vs.get('pass', 0)} / warn {vs.get('warn', 0)}"
                 f" / fail {vs.get('fail', 0)}"
                 f"，需人工复核 {vs.get('needs_review', 0)} 条。")
    if vs.get("invalid_refs"):
        lines.append(f"- 共剔除不存在的证据引用 {vs['invalid_refs']} 处"
                     "（引用不存在的 Evidence ID 已被拒绝）。")
    for claim in state.claims:
        v = claim.get("validation") or {}
        notes: list[str] = []
        if v.get("overall") == "fail":
            notes.append("引用链校验未通过")
        elif v.get("overall") == "warn":
            notes.append("需注意")
        if claim.get("invalid_evidence_ids"):
            notes.append("剔除无效引用 " + "、".join(claim["invalid_evidence_ids"]))
        if v.get("has_conflict"):
            notes.append("存在冲突证据")
        if claim.get("needs_review") or v.get("needs_review"):
            notes.append("两次独立核验不一致，建议人工复核")
        if notes:
            lines.append(f"- ⚠ {claim['claim_id']}（{claim['support_status']}）"
                         f"：{'；'.join(notes)} —— {claim.get('text', '')}")
    return "\n".join(lines)


def render_report(state: ResearchState, answer: str) -> str:
    """Citation Renderer (V5 Phase F): 程序统一生成编号、追溯表与参考文献。

    对应任务要求:
    - 文内引用 [1]、[2]…：[EV-xxx] 按首次出现顺序转编号，同源同号；
      同一括号多 ID 展开去重；无法映射到来源的引用抹掉并计数（不泄漏）
    - References 每个 Source 恰一条，且只包含**实际被引用**（正文或结论）
      的来源；每个 [n] 结构上必然对应一条 Reference
    - URL / 标题 / 域名 / 时间等元数据只取自 SourceRegistry 已保存数据，
      LLM 不参与参考文献生成；标题缺失显示 (无标题)，不伪造
    - 附结论与证据追溯表（内部 Evidence ID 保留）与引用质量说明
      （校验 fail/warn/needs_review 显式呈现，不静默）
    - 输出为项目既有 Markdown 报告格式，多轮研究内容原样保留
    """
    num_by_evidence, ref_sources = _assign_reference_numbers(state, answer)
    unknown = 0

    def _repl(m: re.Match) -> str:
        nonlocal unknown
        nums: list[int] = []
        for eid in _CITE_ID_RE.findall(m.group(1)):
            num = num_by_evidence.get(eid)
            if num is None:
                unknown += 1
            else:
                nums.append(num)
        if not nums:
            return ""
        return "".join(f"[{n}]" for n in dict.fromkeys(nums))

    body = _CITE_GROUP_RE.sub(_repl, answer).rstrip()
    if unknown:
        print(f"      [render] 抹掉 {unknown} 处无法映射到来源的引用")
    parts = [body] if body else ["（本次没有生成正文。）"]
    trace = _render_trace_table(state, num_by_evidence)
    if trace:
        parts.append(trace)
    quality = _render_quality_section(state)
    if quality:
        parts.append("## 引用质量说明\n\n" + quality)
    refs = "\n".join(f"[{n}] {_ref_link(ref_sources[n])}"
                     for n in sorted(ref_sources))
    if refs:
        parts.append("## 引用\n\n" + refs)
    return "\n\n".join(parts)


# ------------------------- 主流程 -------------------------

# ------------------------- Research Loop (V4) -------------------------

def _stop_reason(state: ResearchState, report: GapReport,
                 new_evidence: int) -> str:
    """返回本轮应触发停止的原因；空串表示继续研究。

    停止条件（按优先级）:
    - sufficient: GapAnalyzer 判定证据已足以回答原始问题
    - no_new_evidence: 本轮研究没有产生任何新证据（再研究也是原地打转）
    - max_rounds: 达到轮次上限（兜底，防无限循环）
    「没有可执行的新研究问题」在 Replanner 之后判断（见 run_research）。
    """
    if report.ok and report.is_sufficient:
        return "sufficient"
    if new_evidence == 0:
        return "no_new_evidence"
    if state.current_round >= state.max_rounds:
        return "max_rounds"
    return ""


def _print_run_summary(state: ResearchState, filter_stats: dict) -> None:
    """研究结束后的可观察性汇总：轮次/问题/证据/停止原因/未解决缺口。"""
    done = state.completed_questions
    ok_n = sum(1 for c in done if c["result"].ok)
    fu_n = sum(1 for c in done if c["question"].source == "replanner")
    print(f"停止原因: {state.stop_reason}")
    print(f"轮次: {state.current_round}/{state.max_rounds} | "
          f"研究问题: {len(done)}（成功 {ok_n} / 失败 {len(done) - ok_n}，"
          f"其中 Follow-up {fu_n} 个）")
    qv = sum(1 for e in state.evidence_list if e.get("quote_verified"))
    print(f"证据: {len(state.evidence_list)} 条"
          f"（原文命中 {qv} / 未命中 {len(state.evidence_list) - qv}） | "
          f"来源: {len(state.source_registry)} 个 | "
          f"搜索: {len(state.search_history)} 次")
    cs = state.claims_stats or {}
    if cs.get("kept"):
        bs = cs.get("by_status", {})
        others = cs["kept"] - sum(bs.get(k, 0)
                                  for k in ("supported", "partial", "unsupported"))
        print(f"结论: {cs['kept']} 条（supported {bs.get('supported', 0)}"
              f" / partial {bs.get('partial', 0)}"
              f" / unsupported {bs.get('unsupported', 0)}"
              f" / 其他 {others}）")
    vs = state.validation_summary or {}
    if vs.get("checked"):
        print(f"引用链校验: pass {vs.get('pass', 0)} / warn {vs.get('warn', 0)}"
              f" / fail {vs.get('fail', 0)}"
              f"（需人工复核 {vs.get('needs_review', 0)}）")
    if state.identified_conflicts:
        print("证据冲突:")
        for c in state.identified_conflicts:
            print(f"   - {c['description']}")
    if state.open_gaps:
        print("未解决缺口（已在最终答案中披露）:")
        for g in state.open_gaps:
            print(f"   - {g}")
    if filter_stats:
        print(f"[filter] 共过滤 {sum(filter_stats.values())} 条低质结果"
              f"（黑名单 {filter_stats.get('blocked', 0)}"
              f" / 空壳 {filter_stats.get('too_short', 0)}"
              f" / 低分 {filter_stats.get('low_score', 0)}"
              f" / 兜底回退 {filter_stats.get('fallback', 0)} 次）")


def run_research(question: str) -> tuple[ResearchState, str]:
    """跑完整 V4 管线:
    User Question → Planner → Research Loop
      （每轮: 研究 pending → 更新 ResearchState → Gap Analysis
        → 继续则 Replanner 生成 Follow-up 进入下一轮）
    → Synthesis（全量累积证据）→ Citation Validation → Final Answer
    """
    print("=" * 50)
    print("Research Agent V5")
    print("=" * 50)

    # [1/3] Understanding Question
    print("\n[1/3] Understanding Question")
    print(f"      User: {question}")

    # [2/3] Research Loop
    print("\n[2/3] Research Loop")
    plan = plan_research(question)
    print(f"      Generated {len(plan.sub_questions)} sub-questions\n")
    state = ResearchState(question, plan, max_rounds=MAX_ROUNDS)
    filter_stats: dict = {}      # 源质量过滤统计，跨轮累加，最后汇总一行

    while state.pending_questions and state.current_round < state.max_rounds:
        rnd = state.current_round + 1
        state.start_round(rnd)
        print(f"--- 第 {rnd}/{state.max_rounds} 轮 ---")

        # ---- 研究本轮所有待处理问题（串行）----
        evidence_before = len(state.evidence_list)
        for rq in list(state.pending_questions):
            print(f"      [{rq.id.upper()}] Search → Fetch → Extract → Summary",
                  end="", flush=True)
            query_log: list[str] = []
            result = research_sub_question(
                SubQuestion(rq.id, rq.question), stats=filter_stats,
                fixed_queries=rq.suggested_queries or None,
                query_log=query_log,
                state=state, round_no=rnd,
            )
            print(" ✓" if result.ok else " ✗ (failed)")
            if not result.ok:
                print(f"            原因: {result.error}")
            state.record_searches(query_log)
            state.complete_question(rq, result, rnd)
            state.add_evidence(result.evidence_list)
            if result.ok:
                state.resolve_gap(rq.target_gap)

        new_evidence = len(state.evidence_list) - evidence_before
        print(f"      [round] 本轮新增证据 {new_evidence} 条，"
              f"累积 {len(state.evidence_list)} 条\n")

        # ---- Gap Analysis: 缺口 / 冲突 / 是否充分 ----
        print("      [gap] 分析研究缺口...", end="", flush=True)
        report = analyze_gaps(state)
        print(" ✓" if report.ok else " ✗ (已兜底)")
        state.identified_gaps = report.gaps
        state.identified_conflicts = report.conflicts
        for g in report.gaps:
            if g not in state.open_gaps:
                state.open_gaps.append(g)
        if report.ok and report.reasoning_summary:
            _log(f"      [gap] {report.reasoning_summary}")

        # ---- 停止条件判断 ----
        reason = _stop_reason(state, report, new_evidence)
        if reason:
            state.stop(reason)
            print(f"      [loop] 触发停止条件: {reason}")
            break

        # ---- Replanner: 生成下一轮 Follow-up ----
        added = replan(state, report)
        print(f"      [replan] 新增 {len(added)} 个 Follow-up 问题")
        for rq in added:
            extra = f"  ← {rq.target_gap}" if rq.target_gap else ""
            print(f"         [{rq.id.upper()}] {rq.question}{extra}")
        if not added:
            state.stop("no_valid_followups")
            print("      [loop] 没有可执行的新研究问题，停止研究")
            break

    # 循环自然退出（未触发显式停止）只可能是达到轮次上限
    if state.status != "stopped":
        state.stop("max_rounds")

    if filter_stats:
        print(f"\n      [filter] 共过滤 {sum(filter_stats.values())} 条低质结果"
              f"（黑名单 {filter_stats.get('blocked', 0)}"
              f" / 空壳 {filter_stats.get('too_short', 0)}"
              f" / 低分 {filter_stats.get('low_score', 0)}"
              f" / 兜底回退 {filter_stats.get('fallback', 0)} 次）")

    # [3/3] Synthesizing Final Answer（读取跨轮次累积的全部证据）
    print("\n[3/3] Synthesizing Final Answer")
    final_answer, _claims = synthesize_from_state(state)

    # Phase E: 引用链五级校验（ID / URL / Quote / 语义 / 冲突）+ 关键结论二次核验
    print("      [validate] 校验引用链（ID / URL / Quote / 语义 / 冲突）...")
    _reports, _vstats = validate_citation_chain(state)

    # Phase F: 程序渲染 —— 引用编号 / 结论追溯表 / 参考文献 / 引用质量说明
    final_answer = render_report(state, final_answer)

    print("\n" + "=" * 50)
    print("Final Answer")
    print("=" * 50)
    print("\n--- Run Summary ---")
    _print_run_summary(state, filter_stats)
    return state, final_answer


def main() -> int:
    load_dotenv()
    init_config()
    if not AI_API_KEY or not RESEARCH_API_KEY:
        print(
            "缺少 API key。请先创建 .env 文件，内容参照 .env.example：\n"
            "  AI_API_KEY=sk-...\n"
            "  RESEARCH_API_KEY=tvly-..."
        )
        return 1

    print("Research Agent V5 就绪（输入 exit 退出）\n")
    while True:
        try:
            question = input("Q> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue
        if question.lower() in ("exit", "quit"):
            break

        try:
            # run_research 返回 (state, final_answer)；
            # 交互模式下只需要最终答案，state 供程序化调用/调试使用。
            _, final_answer = run_research(question)
            print(final_answer)
        except (RuntimeError, ValueError) as e:
            print(f"[错误] {e}")
        print("-" * 60)


if __name__ == "__main__":
    # Windows 控制台默认编码不是 UTF-8，强制一下避免中文乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    sys.exit(main())