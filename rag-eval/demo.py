"""零依赖启动脚本：python demo.py run / python demo.py validate

避免为了跑一次冒烟还要先 pip install -e .。
真实项目里仍然保留 console_scripts 入口（rag-eval）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

from rag_eval.cli import main  # noqa: E402

if __name__ == "__main__":
    args = sys.argv[1:] or ["run"]
    raise SystemExit(main(args))
