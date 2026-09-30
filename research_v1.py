import html.parser
import json
import os
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

MAX_RESULTS = 5          # 单次搜索返回条数上限


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

    核心规则：先去掉 base 末尾的斜杠和多余的路径段，再补一个 /path。
    无论 base 填的是 https://xxx.com 还是 https://xxx.com/research，结果都正确。
    """
    base = base.rstrip("/")                       # 去末尾斜杠
    path = path.lstrip("/")                       # 去前导斜杠
    # 若 base 末尾已经是 /path，直接作为完整 URL，不再追加
    if base.endswith("/" + path):
        return base
    return f"{base}/{path}"


# ------------------------- LLM-------------------------

def _chat(messages: list[dict], temperature: float) -> str:
    """调用 AI API，返回回复文本。"""
    # 1. 规范化 Base URL，去除末尾斜杠，避免 /v1/v1 等重复前缀
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
    # 去掉 markdown 围栏
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"LLM 没有输出 JSON 数组: {raw[:200]!r}")
    items = json.loads(raw[start:end + 1])
    return [str(x).strip() for x in items if str(x).strip()]


def generate_queries(question: str) -> list[str]:
    """LLM 第 1 步: 把问题拆成几个搜索关键词。"""
    prompt = (
        "你是一个网络搜索助手。根据下面这个问题，生成 3 个最可能搜到答案的搜索关键词。\n"
        "关键词的语言与问题保持一致。\n"
        '直接输出 JSON 字符串数组，例如 ["关键词1", "关键词2", "关键词3"]，不要输出任何其他文字。\n\n'
        f"问题: {question}"
    )
    raw = _chat([{"role": "user", "content": prompt}], temperature=0.5)
    # ---------------- 核心测试代码 ----------------
    print("\n" + "=" * 20 + "generate_queries LLM 原始输出实况 " + "=" * 20)
    print(repr(raw))  # repr 会显式打印所有换行符 \n、空格和 ``` 标记
    print("=" * 56 + "\n")
    # ----------------------------------------------
    return _parse_json_list(raw)      #  json.loads(raw) 查看LLM原始输出   _parse_json_list(raw) 承接结果


def answer_question(question: str, results: list[dict]) -> str:
    """LLM 第 2 步: 只依据搜索结果回答用户问题。优先使用 Extract 的网页正文。"""
    blocks = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "（无标题）")
        url = r.get("url", "")
        # 优先用 Extract 抓到的网页正文，没有则退回搜索摘要
        content = (r.get("extracted") or r.get("content") or "").strip()
        # ---------------- 打印检索结果 ----------------
        print("\n" + "=" * 20 + f" 搜索结果{i} " + "=" * 20)
        print(f"\n结果: {i}")
        print(f"标题: {title}")
        print(f"URL: {url}")
        print(f"内容({len(content)}字符): {content[:300]}")
        print("=" * 56 + "\n")
        # ----------------------------------------------
        blocks.append(f"[{i}] {title}\n来源: {url}\n{content}")
    prompt = (
        "你是一个研究助手。基于下面的搜索结果回答用户提出的问题。\n"
        "要求:\n"
        "1. 只依据搜索结果（含网页正文），不要凭空编造。\n"
        "2. 用与用户问题相同的语言回答。\n"
        "3. 用要点列出关键信息，并在每条后面标注来源编号，如 (来源: 1)。\n"
        "4. 若搜索结果不足以回答，如实说明还缺什么信息。\n\n"
        "搜索结果:\n"
        + "\n\n".join(blocks)
        + f"\n\n问题: {question}"
    )
    return _chat([{"role": "user", "content": prompt}], temperature=0.3)


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

EXTRACT_MAX_CHARS = 6000     # 单页正文最多保留的字符数（避免 prompt 过长）


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

    已编码的 %XX 保留不二次编码；路径分隔符 / 与查询分隔符 =& + 也保留，
    只编码真正非法的字符（中文、空格等）。
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
    Tavily Extract → 失败 → 判断原因 →
        URL 编码问题 (UnicodeEncodeError) → 百分号编码后重试
        其他错误                           → 跳过（extracted 留空，LLM 用搜索摘要兜底）
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


# ------------------------- 主流程 -------------------------

def research(question: str) -> tuple[str, list[str], list[dict]]:
    """跑完整管线，返回 (答案, 搜索关键词, 搜索结果)。

    管线: 问题 → LLM生成关键词 → Search API 搜 5 个 URL
          → Extract 逐个抓网页正文 → LLM 生成答案
    """
    queries = generate_queries(question)
    results = dedup_by_url([r for q in queries for r in search(q)])[:MAX_RESULTS]
    extract_results(results)
    answer = answer_question(question, results)
    return answer, queries, results


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

    print("Research Agent 就绪（输入 exit 退出）\n")
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
            print("[1/3] 生成搜索关键词...")
            queries = generate_queries(question)
            print(f"      关键词: {' | '.join(queries)}")

            print("[2/4] Search 搜索...")
            results = dedup_by_url(
                [r for q in queries for r in search(q)]
            )[:MAX_RESULTS]
            print(f"      搜到 {len(results)} 条结果")

            print("[3/4] Extract 网页正文...")
            extract_results(results)

            print("[4/4] 生成答案...\n")
            answer = answer_question(question, results)
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