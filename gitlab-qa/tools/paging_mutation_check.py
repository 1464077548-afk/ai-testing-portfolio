"""分页用例的杀伤力自检：往服务端响应里注入缺陷，看用例会不会红。

绿不代表有效。这个脚本对每一种典型分页缺陷各跑一次用例，
只有「注入后确实失败」才算这条用例有检测力。

    python tools/paging_mutation_check.py

判定标准：注入缺陷后用例仍通过 = 漏网，说明断言没盯住那条不变式。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = "tools._paging_mut_plugin"
TARGET = "tests/api/test_pagination_scoped.py::test_full_scan_covers_every_record[issues]"

# 每一种缺陷都对应一条「本该被发现」的不变式。
MUTATIONS = [
    ("first_page_only", "翻页没实现：第 2 页起给空数组", "漏数据"),
    ("ignore_page", "服务端忽略 page 参数，永远回第一页", "跨页重复"),
    ("shrink_per_page", "per_page 被悄悄改成 1", "页宽失效 / 页数不符"),
    ("inflate_total", "X-Total 多报 3 条", "总数谎报"),
    ("skip_one", "offset 少算 1：第 2 页起丢掉第一条", "漏数据"),
]


def run(mutation: str) -> tuple[bool, str]:
    env = dict(os.environ)
    env["GQA_MUTATION"] = mutation
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(ROOT / "src"), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    # 沙箱批量删除守卫会掐掉 pytest 收尾时的清理动作，导致退出码假红
    env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TARGET, "-q", "-p", PLUGIN, "--no-header"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    out = proc.stdout + proc.stderr
    caught = bool(re.search(r"\b1 failed\b|\b1 error\b|assert", out)) and proc.returncode != 0
    summary = next(
        (ln.strip() for ln in reversed(out.splitlines())
         if "passed" in ln or "failed" in ln or "error" in ln), out[-200:].strip())
    return caught, summary


def main() -> int:
    print(f"目标用例：{TARGET}\n")
    rows, missed = [], []
    for mutation, desc, aspect in MUTATIONS:
        print(f"注入 {mutation:<16} {desc} ... ", end="", flush=True)
        try:
            caught, summary = run(mutation)
        except subprocess.TimeoutExpired:
            caught, summary = False, "超时（可能死循环）"
        print("抓住" if caught else "漏网")
        rows.append((mutation, aspect, "抓住" if caught else "漏网", summary))
        if not caught:
            missed.append(mutation)

    print("\n{:<18}{:<22}{:<8}{}".format("变异", "针对的不变式", "结果", "用例摘要"))
    print("-" * 96)
    for mutation, aspect, verdict, summary in rows:
        print("{:<18}{:<22}{:<8}{}".format(mutation, aspect, verdict, summary[:44]))

    print(f"\n结果：{len(MUTATIONS) - len(missed)}/{len(MUTATIONS)} 抓住")
    if missed:
        print("漏网：" + "、".join(missed))
        print("说明这些缺陷不会被现有断言发现，需要补判据。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
