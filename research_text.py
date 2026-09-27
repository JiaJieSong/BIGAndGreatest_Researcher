import json
import os
import sys
import urllib.error
import urllib.request

# ------------------------- 配置 -------------------------

AI_BASE_URL = ""
AI_API_KEY = ""
AI_MODEL = ""
RESEARCH_BASE_URL = ""
RESEARCH_API_KEY = ""

MAX_RESULTS = 5


def load_dotenv(path: str = ".env") -> None:
    """手动解析 .env，向 os.environ 填充（不覆盖已有环境变量）。"""
    if not os.path.exists(path):
        print(f"[load_dotenv] 警告: 未找到环境配置文件 {path}")
        return

    loaded_keys = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            k, v = key.strip(), value.strip()
            os.environ.setdefault(k, v)
            loaded_keys.append(k)

    # ---------------- 测试输出 ----------------
    print("\n" + "=" * 20 + " load_dotenv 测试输出 " + "=" * 20)
    print(f"配置文件路径: {path}")
    print(f"成功载入的环境变量名: {loaded_keys}")
    print("=" * 56 + "\n")
    # ------------------------------------------


def init_config() -> None:
    global AI_BASE_URL, AI_API_KEY, AI_MODEL
    global RESEARCH_BASE_URL, RESEARCH_API_KEY
    AI_BASE_URL = os.environ.get("AI_BASE_URL", "").rstrip("/")
    AI_API_KEY = os.environ.get("AI_API_KEY", "")
    AI_MODEL = os.environ.get("AI_MODEL", "")
    RESEARCH_BASE_URL = os.environ.get("RESEARCH_BASE_URL", "").rstrip("/")
    RESEARCH_API_KEY = os.environ.get("RESEARCH_API_KEY", "")

    # ---------------- 测试输出 ----------------
    print("\n" + "=" * 20 + " init_config 测试输出 " + "=" * 20)
    print(f"AI_BASE_URL: {AI_BASE_URL}")
    print(f"AI_MODEL: {AI_MODEL}")
    print(f"AI_API_KEY: {'已配置 (前4位: ' + AI_API_KEY[:4] + '***)' if AI_API_KEY else '未配置'}")
    print(f"RESEARCH_BASE_URL: {RESEARCH_BASE_URL}")
    print(f"RESEARCH_API_KEY: {'已配置 (前4位: ' + RESEARCH_API_KEY[:4] + '***)' if RESEARCH_API_KEY else '未配置'}")
    print("=" * 56 + "\n")
    # ------------------------------------------


# ------------------------- LLM -------------------------

def _chat(messages: list[dict], temperature: float) -> str:
    """调用 AI API，返回回复文本。"""
    base_url = AI_BASE_URL.rstrip("/")
    if not base_url.endswith("/v1") and not base_url.endswith("/chat/completions"):
        target_url = f"{base_url}/chat/completions"
    elif base_url.endswith("/chat/completions"):
        target_url = base_url
    else:
        target_url = f"{base_url}/chat/completions"

    body = json.dumps({
        "model": AI_MODEL,
        "messages": messages,
        "temperature": temperature,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AI_API_KEY}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    }

    req = urllib.request.Request(target_url, data=body, headers=headers, method="POST")

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

    reply = data["choices"][0]["message"]["content"]

    # ---------------- 测试输出 ----------------
    print("\n" + "=" * 20 + " _chat (底层网络响应) " + "=" * 20)
    print(f"目标 URL: {target_url}")
    print(f"调用参数: temperature={temperature}")
    print(f"完整回复字符数: {len(reply)}")
    print(f"回复内容预览:\n{repr(reply[:200])}")
    print("=" * 56 + "\n")
    # ------------------------------------------

    return reply


def _parse_json_list(raw: str) -> list[str]:
    """容忍 LLM 输出里多余的 ```json 围栏或前后文字，取出第一个 JSON 数组。"""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"LLM 没有输出 JSON 数组: {raw[:200]!r}")
    items = json.loads(cleaned[start:end + 1])
    parsed_result = [str(x).strip() for x in items if str(x).strip()]

    # ---------------- 测试输出 ----------------
    print("\n" + "=" * 20 + " _parse_json_list 过滤输出 " + "=" * 20)
    print(f"截取范围下标: [{start}:{end + 1}]")
    print(f"解析后的 Python 列表: {parsed_result}")
    print("=" * 56 + "\n")
    # ------------------------------------------

    return parsed_result


def generate_queries(question: str) -> list[str]:
    """LLM 第 1 步: 把问题拆成几个搜索关键词。"""
    prompt = (
        "你是一个网络搜索助手。根据下面这个问题，生成 3 个最可能搜到答案的搜索关键词。\n"
        "关键词的语言与问题保持一致。\n"
        '直接输出 JSON 字符串数组，例如 ["关键词1", "关键词2", "关键词3"]，不要输出任何其他文字。\n\n'
        f"问题: {question}"
    )

    # ---------------- 测试输出: 提示词 ----------------
    print("\n" + "=" * 20 + " generate_queries 输入 Prompt " + "=" * 20)
    print(f"输入问题: {question}")
    print(f"Prompt 正文:\n{prompt}")
    print("=" * 56 + "\n")
    # --------------------------------------------------

    raw = _chat([{"role": "user", "content": prompt}], temperature=0.5)

    print("\n" + "=" * 20 + " generate_queries LLM 原始输出 " + "=" * 20)
    print(repr(raw))
    print("=" * 56 + "\n")

    result = _parse_json_list(raw)

    print("\n" + "=" * 20 + " generate_queries 最终返回 " + "=" * 20)
    print(f"生成关键词: {result}")
    print("=" * 56 + "\n")

    return result


def answer_question(question: str, results: list[dict]) -> str:
    """LLM 第 2 步: 只依据搜索结果回答用户问题。"""
    blocks = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "（无标题）")
        url = r.get("url", "")
        content = (r.get("content") or "").strip()
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

    # ---------------- 测试输出: 拼接好的上下文 ----------------
    print("\n" + "=" * 20 + " answer_question 构造的 Prompt " + "=" * 20)
    print(f"输入参考结果数: {len(results)} 条")
    print(f"汇总后的 Prompt 总长度: {len(prompt)} 字符")
    print("=" * 56 + "\n")
    # ---------------------------------------------------------

    answer = _chat([{"role": "user", "content": prompt}], temperature=0.3)

    # ---------------- 测试输出: 最终答案 ----------------
    print("\n" + "=" * 20 + " answer_question 最终生成回答 " + "=" * 20)
    print(answer)
    print("=" * 56 + "\n")
    # ----------------------------------------------------

    return answer


# ------------------------- 搜索 (RESEARCH) -------------------------

def search_research(query: str) -> list[dict]:
    """RESEARCH API，返回 [{title, url, content}, ...]。"""
    body = json.dumps({
        "api_key": RESEARCH_API_KEY,
        "query": query,
        "search_depth": "basic",
        "max_results": MAX_RESULTS,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{RESEARCH_BASE_URL}/search",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"RESEARCH API 报错 {e.code}: {detail[:500]}") from e

    search_items = data.get("results", [])

    # ---------------- 测试输出 ----------------
    print("\n" + "=" * 20 + " search_research 检索测试 " + "=" * 20)
    print(f"检索关键词: {query}")
    print(f"返回结果数: {len(search_items)}")
    for idx, item in enumerate(search_items, 1):
        print(f"  [{idx}] 标题: {item.get('title')}")
        print(f"      链接: {item.get('url')}")
        print(f"      正文摘要: {(item.get('content') or '')[:80]}...")
    print("=" * 56 + "\n")
    # ------------------------------------------

    return search_items


def dedup_by_url(results: list[dict]) -> list[dict]:
    """按 URL 去重。"""
    seen, unique = set(), []
    dropped_urls = []

    for r in results:
        url = r.get("url")
        if url and url not in seen:
            seen.add(url)
            unique.append(r)
        elif url in seen:
            dropped_urls.append(url)

    # ---------------- 测试输出 ----------------
    print("\n" + "=" * 20 + " dedup_by_url 过滤统计 " + "=" * 20)
    print(f"去重前总数: {len(results)} 条")
    print(f"去重后留存: {len(unique)} 条")
    print(f"剔除的重复 URL 数量: {len(dropped_urls)}")
    if dropped_urls:
        print(f"部分重复 URL: {dropped_urls[:3]}")
    print("=" * 56 + "\n")
    # ------------------------------------------

    return unique


# ------------------------- 主流程 -------------------------

def research(question: str) -> tuple[str, list[str], list[dict]]:
    """跑完整管线，返回 (答案, 搜索关键词, 搜索结果)。"""
    # ---------------- 测试输出 ----------------
    print("\n" + "#" * 22 + " 进入 research 管线 " + "#" * 22)
    print(f"输入初始问题: {question}")
    print("#" * 60 + "\n")
    # ------------------------------------------

    queries = generate_queries(question)
    results = dedup_by_url([r for q in queries for r in search_research(q)])[:MAX_RESULTS]
    answer = answer_question(question, results)

    # ---------------- 测试输出 ----------------
    print("\n" + "#" * 22 + " research 管线执行完毕 " + "#" * 22)
    print(f"返回元组结构: (答案长度: {len(answer)}, 关键词列表数: {len(queries)}, 结果数: {len(results)})")
    print("#" * 60 + "\n")
    # ------------------------------------------

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
            answer, queries, results = research(question)
            print("\n" + "*" * 20 + " 终端最终展示 " + "*" * 20)
            print(answer)
            print("*" * 56 + "\n")
        except (RuntimeError, ValueError) as e:
            print(f"[错误] {e}")
        print("-" * 60)

    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    sys.exit(main())