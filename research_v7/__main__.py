# -*- coding: utf-8 -*-
"""python -m research_v7 入口（转发到 main.main）。"""

from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

from .main import main

if __name__ == "__main__":
    sys.exit(main())
