# -*- coding: utf-8 -*-
"""Research Agent V7 —— CLI REPL 入口。

运行:  python -m research_v7
"""

from __future__ import annotations

import sys

import research_v5 as v5
from .graph import run_research


def main() -> int:
    v5.load_dotenv()
    v5.init_config()
    if not v5.AI_API_KEY or not v5.RESEARCH_API_KEY:
        print(
            "缺少 API key。请先创建 .env 文件，内容参照 .env.example：\n"
            "  AI_API_KEY=sk-...\n"
            "  RESEARCH_API_KEY=tvly-..."
        )
        return 1

    print("Research Agent V7 就绪（Multi-Agent，输入 exit 退出）\n")
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
            _, final_answer = run_research(question)
            print(final_answer)
        except (RuntimeError, ValueError) as e:
            print(f"[错误] {e}")
        print("-" * 60)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    sys.exit(main())
