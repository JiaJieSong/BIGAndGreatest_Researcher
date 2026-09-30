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


# ------------------------- 搜索 (search) -------------------------

def search(query: str) -> list[dict]:
    """ Search API，返回 [{title, url, content}, ...]。"""
    body = json.dumps({
        "api_key": RESEARCH_API_KEY,
        "query": query,
        "search_depth": "basic",
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
            print(f"      [extract] URL 含中文/特殊字符，百分号编码后重试: {url}")
            try:
                text = extract_webpage(normalize_ascii_url(url))
            except Exception as e:
                print(f"      [extract] 编码重试仍失败: {url} → {e}")
        except Exception as e:
            print(f"      [extract] 其他错误，跳过: {url} → {e}")
        r["extracted"] = text
        if text:
            suffix = " (编码后重试)" if retried else ""
            print(f"      [extract] {url}  → {len(text)} 字符{suffix}")
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
        print(f"      [evidence] 正文为空，跳过: {url}")
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
                print(f"      [evidence] 丢弃 {dropped} 条字段不全的证据（{url}）")
            if len(validated) != len(unique):
                print(f"      [evidence] 页内去重 {(len(validated) - len(unique))} 条")
            return unique[:EVIDENCE_MAX_PER_PAGE]
        except (ValueError, json.JSONDecodeError, KeyError) as e:
            if attempt == 1:
                print(f"      [evidence] LLM 输出解析失败 ({e})，重试一次...")
                prompt += (
                    "\n\n注意: 上一次输出不是合法 JSON，请严格只输出 JSON 对象，"
                    "不要添加围栏或解释文字。"
                )
            else:
                print(f"      [evidence] 重试仍失败，跳过该页: {url} → {e}")
                return []


def assign_evidence_ids(evidence_list: list[dict]) -> list[dict]:
    """给每条证据分配全局唯一 id (EV-001, EV-002, ...)，返回原列表。"""
    for i, ev in enumerate(evidence_list, 1):
        ev["id"] = f"EV-{i:03d}"
    return evidence_list


# ------------------------- Final Writer (带引用) -------------------------

_WRITER_PROMPT_TEMPLATE = """你是一名研究写作助手。根据下面的 EVIDENCE（证据）回答用户问题。

要求:
1. 只使用 EVIDENCE 中的信息，不得编造证据中没有的内容。
2. 在支撑某一论断的句子末尾标注所用证据，格式为 [EV-xxx]（例如 [EV-001]）。
3. 用与用户问题相同的语言回答，用要点组织。
4. 若 EVIDENCE 为空或不足以回答，如实说明还缺什么信息，不要强行编造。

EVIDENCE:
{evidence_block}

问题: {question}"""

_CITE_RE = re.compile(r"\[EV-(\d+)\]")   # 匹配 [EV-001] 这类引用


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
    """把答案里的 [EV-001] 引用替换成 [1]，并在末尾附加引用列表。

    [EV-001] 是给 Writer 看的内部 ID；对外呈现用连续的 [1][2]...，
    末尾附上对应来源的完整引用。
    """
    index = {ev["id"]: i + 1 for i, ev in enumerate(evidence_list)}

    def repl(m: re.Match) -> str:
        eid = f"EV-{m.group(1)}"
        return f"[{index[eid]}]" if eid in index else m.group(0)

    body = _CITE_RE.sub(repl, answer)

    refs = [
        f"[{i}] {ev['source_title']}\nURL: {ev['source_url']}"
        for i, ev in enumerate(evidence_list, 1)
    ]
    if refs:
        body += "\n\n## 引用\n" + "\n".join(refs)
    return body


def write_final_answer(question: str, evidence_list: list[dict]) -> str:
    """LLM 最后一步：只依据证据写答案，然后把 [EV-xxx] 转成对外引用 [n]。"""
    prompt = _WRITER_PROMPT_TEMPLATE.format(
        question=question,
        evidence_block=build_evidence_block(evidence_list),
    )
    answer = _chat([{"role": "user", "content": prompt}], temperature=0.3)
    return format_citations(answer, evidence_list)


# ------------------------- 主流程 -------------------------

def research(question: str) -> tuple[str, list[str], list[dict], list[dict]]:
    """跑完整 v2 管线，返回 (最终答案, 搜索关键词, 搜索结果, 证据列表)。

    数据流:
    User Question → Search → Fetch Web Pages → Extract/Clean
    → Evidence Extraction → Evidence List → LLM Final Answer（带 [n] 引用）
    """
    queries = generate_queries(question)
    results = dedup_by_url([r for q in queries for r in search(q)])[:MAX_RESULTS]

    # Extract: 抓取网页正文
    extract_results(results)

    # Evidence Extraction: 每个网页单独抽取证据
    all_evidence: list[dict] = []
    for r in results:
        evs = extract_evidence_from_page(question, r)
        if evs:
            print(f"      [evidence] 该页提取 {len(evs)} 条证据")
            all_evidence.extend(evs)
    assign_evidence_ids(all_evidence)
    print(f"[evidence] 共提取 {len(all_evidence)} 条证据")

    answer = write_final_answer(question, all_evidence)
    return answer, queries, results, all_evidence


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

    print("Research Agent v2 (Evidence) 就绪（输入 exit 退出）\n")
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
            print("[1/5] 生成搜索关键词...")
            queries = generate_queries(question)
            print(f"      关键词: {' | '.join(queries)}")

            print("[2/5] Search 搜索...")
            results = dedup_by_url(
                [r for q in queries for r in search(q)]
            )[:MAX_RESULTS]
            print(f"      搜到 {len(results)} 条结果")

            print("[3/5] Extract 网页正文...")
            extract_results(results)

            print("[4/5] Evidence Extraction...")
            all_evidence: list[dict] = []
            for r in results:
                evs = extract_evidence_from_page(question, r)
                if evs:
                    print(f"      该页提取 {len(evs)} 条证据")
                    all_evidence.extend(evs)
            assign_evidence_ids(all_evidence)
            print(f"      共提取 {len(all_evidence)} 条证据")

            # 打印证据清单
            print("\n" + "=" * 20 + " Evidence 清单 " + "=" * 20)
            for ev in all_evidence:
                print(f"{ev['id']}  claim: {ev['claim'][:60]}")
                print(f"        source: {ev['source_title'][:30]} | {ev['source_url']}")
            print("=" * 60 + "\n")

            print("[5/5] 生成最终答案（带引用）...\n")
            answer = write_final_answer(question, all_evidence)
            print(answer)
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