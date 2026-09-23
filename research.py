import json
import os
import sys
import urllib.error
import urllib.request

# ------------------------- 配置 -------------------------

AI_BASE_URL = ""   # 在 load_dotenv() 后按顺序赋值
AI_API_KEY = ""
AI_MODEL = ""
TAVILY_BASE_URL = ""
TAVILY_API_KEY = ""

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
    global TAVILY_BASE_URL, TAVILY_API_KEY
    AI_BASE_URL = os.environ.get("AI_BASE_URL", "").rstrip("/")
    AI_API_KEY = os.environ.get("AI_API_KEY", "")
    AI_MODEL = os.environ.get("AI_MODEL", "")
    TAVILY_BASE_URL = os.environ.get("TAVILY_BASE_URL", "https://api.tavily.com").rstrip("/")
    TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")


# ------------------------- LLM (DeepSeek) -------------------------

def _chat(messages: list[dict], temperature: float) -> str:
    """调用 DeepSeek Chat Completions API，返回回复文本。"""
    body = json.dumps({
        "model": AI_MODEL,
        "messages": messages,
        "temperature": temperature,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{AI_BASE_URL}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {AI_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DeepSeek API 报错 {e.code}: {detail[:500]}") from e
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
    print("\n" + "=" * 20 + " LLM 原始输出实况 " + "=" * 20)
    print(repr(raw))  # repr 会显式打印所有换行符 \n、空格和 ``` 标记
    print("=" * 56 + "\n")
    # ----------------------------------------------
    return _parse_json_list(raw)


def answer_question(question: str, results: list[dict]) -> str:
    """LLM 第 2 步: 只依据搜索结果回答用户问题。"""
    blocks = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "（无标题）")
        url = r.get("url", "")
        content = (r.get("content") or "").strip()
        # ---------------- 打印检索结果 ----------------
        print(f"\n结果: {i}")
        print(f"标题: {title}")
        print(f"URL: {url}")
        print(f"内容: {content[:300]}")
        # ----------------------------------------------
        blocks.append(f"[{i}] {title}\n来源: {url}\n{content}")
    prompt = (
        "你是一个研究助手。基于下面的搜索结果回答用户提出的问题。\n"
        "要求:\n"
        "1. 只依据搜索结果，不要凭空编造。\n"
        "2. 用与用户问题相同的语言回答。\n"
        "3. 用要点列出关键信息，并在每条后面标注来源编号，如 (来源: 1)。\n"
        "4. 若搜索结果不足以回答，如实说明还缺什么信息。\n\n"
        "搜索结果:\n"
        + "\n\n".join(blocks)
        + f"\n\n问题: {question}"
    )
    return _chat([{"role": "user", "content": prompt}], temperature=0.3)


# ------------------------- 搜索 (Tavily) -------------------------

def search_tavily(query: str) -> list[dict]:
    """Tavily Search API，返回 [{title, url, content}, ...]。"""
    body = json.dumps({
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": "basic",
        "max_results": MAX_RESULTS,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{TAVILY_BASE_URL}/search",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Tavily API 报错 {e.code}: {detail[:500]}") from e
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


# ------------------------- 主流程 -------------------------

def research(question: str) -> tuple[str, list[str], list[dict]]:
    """跑完整管线，返回 (答案, 搜索关键词, 搜索结果)。"""
    queries = generate_queries(question)
    results = dedup_by_url([r for q in queries for r in search_tavily(q)])[:MAX_RESULTS]
    answer = answer_question(question, results)
    return answer, queries, results


def main() -> int:
    load_dotenv()
    init_config()
    if not AI_API_KEY or not TAVILY_API_KEY:
        print(
            "缺少 API key。请先创建 .env 文件，内容参照 .env.example：\n"
            "  AI_API_KEY=sk-...\n"
            "  TAVILY_API_KEY=tvly-..."
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

            print("[2/3] Tavily 搜索...")
            results = dedup_by_url(
                [r for q in queries for r in search_tavily(q)]
            )[:MAX_RESULTS]
            print(f"      搜到 {len(results)} 条结果")

            print("[3/3] 生成答案...\n")
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