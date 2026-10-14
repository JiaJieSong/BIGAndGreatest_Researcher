from __future__ import annotations

import html.parser
import json
import os
import re
import sys
import urllib.error
import urllib.request
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
                 max_rounds: int = MAX_ROUNDS):
        self.original_question = original_question
        self.research_plan = plan
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
        """累积证据：按 (source_url, claim) 全局去重，只增不减。

        返回本轮真正新增的条数。跨轮重复搜到的相同论断不重复入库，
        因此 Synthesis 拿到的 evidence_list 天然去重、引用列表不虚长。
        """
        seen = {(ev["source_url"], ev["claim"].strip().lower())
                for ev in self.evidence_list}
        added = 0
        for ev in evidence:
            key = (ev["source_url"], ev["claim"].strip().lower())
            if key not in seen:
                seen.add(key)
                self.evidence_list.append(ev)
                self.add_source(ev["source_title"], ev["source_url"])
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
                           query_log: list[str] | None = None
                           ) -> tuple[str, list[dict]]:
    """对单个子问题：Search → Filter → Extract → Evidence → LLM 总结。

    返回 (小结文本, 该子问题的证据列表)。
    id_prefix 用于把该子问题的证据编号加上前缀（如 q1-），
    保证多个子问题的证据汇总后 ID 仍唯一。
    stats 用于累加源质量过滤的统计，由调用方汇总输出。
    fixed_queries 非空时跳过 generate_queries，直接使用调用方给定的
    检索式（V4 Replanner 的 suggested_queries 走这条通道）。
    query_log 非空时把实际使用的检索式记录进去（V4 search_history）。
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
        if evs:
            _log(f"          [evidence] 该页提取 {len(evs)} 条证据")
            all_evidence.extend(evs)
    assign_evidence_ids(all_evidence, prefix=id_prefix)
    _log(f"          [summary] 共提取 {len(all_evidence)} 条证据")
    # 小结保留 [EV-xxx] 内部引用原样传给 Synthesis，最终答案才能指回同一批证据
    return write_cited_answer(question, all_evidence), all_evidence


def research_sub_question(sub_question: SubQuestion,
                          stats: dict | None = None,
                          fixed_queries: list[str] | None = None,
                          query_log: list[str] | None = None) -> ResearchResult:
    """串行研究单个子问题。任何一步失败都返回带 error 的结果，不抛异常。

    本函数不打印进度：由调用方在一条链上输出，保证终端简洁。
    """
    try:
        # 证据 ID 带上子问题前缀（EV-q1-001），汇总到 Synthesis 时不会撞号
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


def synthesize_from_state(state: ResearchState) -> str:
    """Synthesis (V4): 读取 ResearchState 跨轮次累积的全部证据与小结。

    - evidence_list 在入库时已全局去重，这里全量交给 LLM，
      而不是只读最后一轮的 Summary
    - 引用与来源追踪沿用 V3 机制：[EV-xxx] → [n]，同源同号，末尾引用列表
    """
    evidence = state.evidence_list
    prompt = _SYNTHESIS_PROMPT_TEMPLATE.format(
        original_question=state.original_question,
        results_block=build_round_results_block(state),
        evidence_block=build_evidence_block(evidence),
        gaps_block=build_gaps_block(state),
    )
    answer = _chat([{"role": "user", "content": prompt}], temperature=0.3)
    validate_citations(answer, evidence)
    return format_citations(answer, evidence)


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
    print(f"证据: {len(state.evidence_list)} 条 | "
          f"来源: {len(state.source_list)} 个 | "
          f"搜索: {len(state.search_history)} 次")
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
    print("Research Agent V4")
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
    final_answer = synthesize_from_state(state)

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

    print("Research Agent V4 就绪（输入 exit 退出）\n")
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