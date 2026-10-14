"""独立诊断程序：逐项测试 new-api 及 research_v1.py 的各个网络环节。

用法: python test_newapi.py
"""
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import research_v1 as m

m.load_dotenv()
m.init_config()

results = []


def record(name, ok, detail):
    status = "[OK]  " if ok else "[FAIL]"
    results.append(f"{status} {name} | {detail}")
    print(f"{status} {name} | {detail}", flush=True)


# ---------- 环境信息 ----------
print("=" * 70)
print("环境信息:")
print(f"  AI_BASE_URL   = {m.AI_BASE_URL}")
print(f"  AI_MODEL      = {m.AI_MODEL}")
print(f"  AI_API_KEY    = {m.AI_API_KEY[:12]}...{m.AI_API_KEY[-4:]}")
print(f"  RESEARCH_BASE = {m.RESEARCH_BASE_URL}")
print(f"  系统代理       = {urllib.request.getproxies()}")
print(f"  urllib 版本    = {sys.version}")
print("=" * 70)

# ---------- 测试 1: TCP 端口连通性 ----------
parsed = urlparse(m.AI_BASE_URL)
host, port = parsed.hostname, parsed.port or 80
import socket
try:
    with socket.create_connection((host, port), timeout=5) as s:
        record(f"TCP 连接 {host}:{port}", True, "端口可达")
except Exception as e:
    record(f"TCP 连接 {host}:{port}", False, f"{type(e).__name__}: {e}")

# ---------- 测试 2: /v1/models（直接方式，绕过代理） ----------
models_url = m.AI_BASE_URL.rstrip("/") + "/models"
req = urllib.request.Request(
    models_url, headers={"Authorization": f"Bearer {m.AI_API_KEY}"})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    ids = [x.get("id") for x in data.get("data", [])]
    record("GET /v1/models (no-proxy)", True, f"可用模型: {ids}")
except urllib.error.HTTPError as e:
    record("GET /v1/models (no-proxy)", False,
           f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}")
except Exception as e:
    record("GET /v1/models (no-proxy)", False, f"{type(e).__name__}: {e}")

# ---------- 测试 3: /v1/chat/completions（直接方式，绕过代理） ----------
chat_url = m.AI_BASE_URL.rstrip("/") + "/chat/completions"
body = json.dumps({
    "model": m.AI_MODEL,
    "messages": [{"role": "user", "content": "Say OK"}],
    "temperature": 0.5,
}).encode("utf-8")
req = urllib.request.Request(
    chat_url, data=body, method="POST",
    headers={"Content-Type": "application/json",
             "Authorization": f"Bearer {m.AI_API_KEY}"})
try:
    with opener.open(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    record("POST /v1/chat/completions (no-proxy)", True, f"回复: {content!r}")
except urllib.error.HTTPError as e:
    record("POST /v1/chat/completions (no-proxy)", False,
           f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}")
except Exception as e:
    record("POST /v1/chat/completions (no-proxy)", False,
           f"{type(e).__name__}: {e}")

# ---------- 测试 4: research_v1._chat（修复后的真实代码路径） ----------
try:
    reply = m._chat([{"role": "user", "content": "Say OK"}], temperature=0.5)
    record("research_v1._chat()", True, f"回复: {reply!r}")
except Exception as e:
    record("research_v1._chat()", False, f"{type(e).__name__}: {e}")

# ---------- 测试 5: Tavily 搜索（外部 API，走系统代理） ----------
try:
    hits = m.search("capital of France")
    record("Tavily search()", True, f"返回 {len(hits)} 条结果")
except Exception as e:
    record("Tavily search()", False, f"{type(e).__name__}: {e}")

# ---------- 汇总 ----------
print("\n" + "=" * 70)
print("汇总:")
ok_count = sum(1 for r in results if r.startswith("[OK]"))
print(f"  通过 {ok_count}/{len(results)}")
for r in results:
    print(" ", r)
print("=" * 70)

with open("_test_newapi_report.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(results))
