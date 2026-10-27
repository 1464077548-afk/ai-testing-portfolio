"""接口覆盖率查看器。

跑完测试后看这一份就够：
    python tools/coverage_report.py              # 打印未覆盖清单
    python tools/coverage_report.py --group 仓库  # 只看某一组
    python tools/coverage_report.py --all        # 连已覆盖的一起列

没跑过测试也能看：直接读 catalog.py 输出完整目录和「全部未覆盖」的初始状态。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gqa.api.catalog import CATALOG, by_group  # noqa: E402
from gqa.api.coverage import Coverage  # noqa: E402

DATA = ROOT / "artifacts" / "coverage" / "api_coverage.json"


def load() -> tuple[Coverage, bool]:
    """读上次运行结果；没有就用空覆盖率，代表「一条都没跑」。"""
    cov = Coverage()
    if not DATA.exists():
        return cov, False
    try:
        data = json.loads(DATA.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[warn] 覆盖率数据无法解析（{exc}），按未运行处理")
        return cov, False
    cov.merge_hits(data.get("covered_endpoints", {}), data.get("out_of_catalog", {}))
    return cov, True


def main() -> int:
    p = argparse.ArgumentParser(description="GitLab CE 接口覆盖率")
    p.add_argument("--group", default=None, help=f"只看某一组：{'/'.join(by_group())}")
    p.add_argument("--all", action="store_true", help="连已覆盖的端点一起列")
    p.add_argument("--missing-only", action="store_true", help="只列未覆盖端点")
    args = p.parse_args()

    cov, has_data = load()
    if not has_data:
        print("[i] 没有找到运行记录，以下按「全部未覆盖」展示。")
        print(f"    跑一次测试后数据会写入 {DATA}\n")

    print(f"GitLab CE 接口目录：{cov.total} 个端点")
    print(f"本次已覆盖        ：{cov.covered}（{cov.rate:.1%}）\n")

    print(f"{'分组':<10}{'覆盖':>6}{'总数':>6}{'覆盖率':>9}")
    print("-" * 34)
    groups = by_group()
    for group, eps in groups.items():
        if args.group and group != args.group:
            continue
        hit = sum(1 for ep in eps if ep.key in cov.hits)
        print(f"{group:<10}{hit:>6}{len(eps):>6}{hit / len(eps):>8.0%}")

    if args.missing_only or not args.all:
        missing = [ep for ep in cov.missing_endpoints()
                   if not args.group or ep.group == args.group]
        print(f"\n未覆盖 {len(missing)} 个端点：")
        for ep in missing:
            flag = " [管理员]" if ep.admin else ""
            print(f"  {ep.method:<6} {ep.template:<52} {ep.name}{flag}")

    if args.all:
        by_key = {ep.key: ep for ep in CATALOG}
        print("\n已覆盖端点：")
        for key, count in sorted(cov.hits.items()):
            ep = by_key.get(key)
            if ep is None or (args.group and ep.group != args.group):
                continue
            print(f"  {ep.method:<6} {ep.template:<52} ×{count}  {ep.name}")

    if cov.unknown:
        print(f"\n目录外调用 {len(cov.unknown)} 类（应补进 catalog.py 或修正用例）：")
        for key, count in sorted(cov.unknown.items()):
            print(f"  {key}  ×{count}")

    print(f"\n完整报告：{ROOT / 'artifacts' / 'coverage' / 'api_coverage.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
