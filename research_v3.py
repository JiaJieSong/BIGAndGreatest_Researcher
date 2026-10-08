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
        with urllib.request.urlopen(req, timeout=120) as resp:
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
        with urllib.request.urlopen(req, timeout=60) as resp:
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
    with urllib.request.urlopen(req, timeout=30) as resp:
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
                           stats: dict | None = None) -> tuple[str, list[dict]]:
    """对单个子问题：Search → Filter → Extract → Evidence → LLM 总结。

    返回 (小结文本, 该子问题的证据列表)。
    id_prefix 用于把该子问题的证据编号加上前缀（如 q1-），
    保证多个子问题的证据汇总后 ID 仍唯一。
    stats 用于累加源质量过滤的统计，由调用方汇总输出。
    """
    queries = generate_queries(question)
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
                          stats: dict | None = None) -> ResearchResult:
    """串行研究单个子问题。任何一步失败都返回带 error 的结果，不抛异常。

    本函数不打印进度：由调用方在一条链上输出，保证终端简洁。
    """
    try:
        # 证据 ID 带上子问题前缀（EV-q1-001），汇总到 Synthesis 时不会撞号
        summary, evidence = summarize_sub_question(
            sub_question.question, id_prefix=f"{sub_question.id}-", stats=stats,
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


# ------------------------- Synthesis (综合) -------------------------

_SYNTHESIS_PROMPT_TEMPLATE = """你是一名研究合著者 (Synthesizer)。用户有一个原始问题，我们先把它拆成若干子问题分别研究，下面是每个子问题的研究小结，以及支撑这些小结的全部 EVIDENCE（证据）。请综合所有子问题的结果，回答用户的原始问题。

要求:
1. 用与原始问题相同的语言回答，用清晰的要点组织，结构完整。
2. 只使用 EVIDENCE 中的信息，不得编造证据中没有的内容，尤其是数字和规模。
3. 在支撑某一论断的句子末尾标注所用证据，格式为方括号包裹的证据 ID，
   且该 ID 必须原样复制下面 EVIDENCE 标题行里的 ID，不要自己编号、不要写占位符。
4. 综合时如实使用各子问题的结果；某个子问题未研究成功时，不要编造其内容，可以在相关位置说明该方向暂无研究结果。
5. 先总述原始问题的核心答案，再按子问题展开。

原问题: {original_question}

各子问题的研究小结:

{results_block}

EVIDENCE:
{evidence_block}"""


def build_results_block(results: list[ResearchResult]) -> str:
    """把各子问题的研究结果拼成带明确边界的文本块（而不是无结构大字符串）。"""
    blocks = []
    for r in results:
        if r.ok:
            blocks.append(
                f"[子问题 {r.sub_question_id}] {r.sub_question}\n"
                f"研究结果:\n{r.summary}"
            )
        else:
            blocks.append(
                f"[子问题 {r.sub_question_id}] {r.sub_question}\n"
                "研究结果: (该子问题研究失败，暂无可用研究结果。)"
            )
    return "\n\n" + "\n\n".join(blocks) + "\n"


def collect_evidence(results: list[ResearchResult]) -> list[dict]:
    """把各子问题的证据按子问题顺序拍平成一份全局证据列表，并按 (来源, claim) 去重。

    各子问题的证据 ID 在生成时就带了自己 id_prefix，所以拍平后不会撞号。
    同一个网页常被多个子问题重复搜到，其中的相同 claim 会在综合阶段变成
    重复上下文（也让引用列表虚长），这里按 (source_url, claim) 去掉完全重复项。
    """
    seen, unique = set(), []
    for r in results:
        if not r.ok:
            continue
        for ev in r.evidence_list:
            key = (ev["source_url"], ev["claim"].strip().lower())
            if key not in seen:
                seen.add(key)
                unique.append(ev)
    return unique


def synthesize(original_question: str, research_results: list[ResearchResult]) -> str:
    """Synthesis: 把所有子问题结果 + 全部证据交给最终 LLM，回答原始问题。

    最后把答案里的 [EV-xxx] 转成对外引用 [n]，并附上全局引用列表 ——
    最终答案的每个论断都能追回到具体来源，这是 V3 相对 v2 必须保留的能力。
    """
    evidence = collect_evidence(research_results)
    prompt = _SYNTHESIS_PROMPT_TEMPLATE.format(
        original_question=original_question,
        results_block=build_results_block(research_results),
        evidence_block=build_evidence_block(evidence),
    )
    answer = _chat([{"role": "user", "content": prompt}], temperature=0.3)
    return format_citations(answer, evidence)


# ------------------------- 主流程 -------------------------

def run_research(question: str) -> tuple[ResearchPlan, list[ResearchResult], str]:
    """跑完整 V3 管线:
    User Question → Planner → 各子问题 Research → Synthesis → Final Answer
    """
    print("=" * 50)
    print("Research Agent V3")
    print("=" * 50)

    # [1/4] Understanding Question
    print("\n[1/4] Understanding Question")
    print(f"      User: {question}")

    # [2/4] Planning Research
    print("\n[2/4] Planning Research")
    plan = plan_research(question)
    print(f"      Generated {len(plan.sub_questions)} sub-questions\n")
    for sq in plan.sub_questions:
        print(f"      {sq.id.upper()}: {sq.question}")

    # [3/4] Researching（串行）
    print("\n[3/4] Researching\n")
    results: list[ResearchResult] = []
    filter_stats: dict = {}      # 源质量过滤统计，各子问题累加，最后汇总一行
    for sq in plan.sub_questions:
        print(f"      [{sq.id.upper()}] Search → Fetch → Extract → Summary",
              end="", flush=True)
        result = research_sub_question(sq, stats=filter_stats)
        print(" ✓" if result.ok else " ✗ (failed)")
        if not result.ok:
            print(f"            原因: {result.error}")
        results.append(result)

    if filter_stats:
        print(f"      [filter] 共过滤 {sum(filter_stats.values())} 条低质结果"
              f"（黑名单 {filter_stats.get('blocked', 0)}"
              f" / 空壳 {filter_stats.get('too_short', 0)}"
              f" / 低分 {filter_stats.get('low_score', 0)}"
              f" / 兜底回退 {filter_stats.get('fallback', 0)} 次）")

    # [4/4] Synthesizing Final Answer
    print("\n[4/4] Synthesizing Final Answer")
    final_answer = synthesize(plan.original_question, results)

    print("\n" + "=" * 50)
    print("Final Answer")
    print("=" * 50)
    return plan, results, final_answer


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

    print("Research Agent V3 就绪（输入 exit 退出）\n")
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
            # run_research 同时返回 (plan, results, final_answer)，
            # 交互模式下只需要最终答案；结构化数据供程序化调用使用。
            _, _, final_answer = run_research(question)
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