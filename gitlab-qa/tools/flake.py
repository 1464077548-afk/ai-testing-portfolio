"""稳定性采样器：把一套用例重复跑 N 轮，算出每条用例到底稳不稳。

一次运行的结果只能回答"现在红还是绿"，回答不了"这条能不能信"。
要判断可信度必须重复测量——这就是本工具存在的全部理由。

用法：
    # 默认：冒烟套件重复 5 轮
    python tools/flake.py

    # 指定范围与轮次
    python tools/flake.py --suite api --rounds 7 -k "project"

    # 门禁：抖动率超过 5% 就退出非 0
    python tools/flake.py --suite regression --rounds 5 --max-flake-rate 0.05

    # 隔离区体检（CI 每日跑）
    python tools/flake.py --check-quarantine

    # 把一个确认抖动的用例关进隔离区，限期两周
    python tools/flake.py --quarantine-add "tests/ui/test_login.py::test_ok" \
        --reason "首登改密弹窗与选择器竞态" --owner hejia --due 2026-11-06

退出码：
    0  全部稳定（或在阈值内）
    2  存在持续失败（真缺陷，不是抖动）
    3  抖动率超过阈值
    4  隔离区体检不通过（缺原因 / 缺期限 / 已到期 / 规模超限）
    5  没收集到任何用例（筛选条件写错了）
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gqa.flake.model import (  # noqa: E402
    BROKEN, FLAKY, NO_DATA, STABLE, VERDICT_LABEL,
    build_report, read_junit,
)
from gqa.flake.quarantine import Quarantine, QuarantineEntry  # noqa: E402


_RUNNER = None


def _load_runner():
    """按路径导入 tools/run.py，复用它的套件定义，避免两处漂移。"""
    global _RUNNER
    if _RUNNER is None:
        spec = importlib.util.spec_from_file_location("qa_run", ROOT / "tools" / "run.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("找不到 tools/run.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _RUNNER = mod
    return _RUNNER


def build_cmd(args: argparse.Namespace, suite: dict, junit: Path, round_dir: Path) -> list[str]:
    ns = argparse.Namespace(
        base_url=args.base_url, env=args.env, mark=args.mark, keyword=args.keyword,
        # 传 None 才能落到套件自己的默认并行度；
        # 这里写 `or 0` 会把 api/regression 的 xdist 悄悄关掉，采样慢一倍。
        workers=args.workers, verbose=args.verbose, extra=args.extra,
    )
    cmd = _load_runner().build_pytest_cmd(ns, suite)

    # build_pytest_cmd 固定带 --alluredir <主结果目录>。采样要跑 N 轮，
    # 照单全收会把同一批用例重复写进主结果目录，Allure 的趋势
    # 会把一次采样当成 N 次运行，趋势线直接失真。
    # 剥掉它，并换成 conftest 自己的 --allure-dir 指向本轮目录，
    # 让 environment.properties 也落在本轮，不污染正式结果。
    if "--alluredir" in cmd:
        i = cmd.index("--alluredir")
        cmd[i:i + 2] = []
    cmd += ["--allure-dir", str(round_dir)]

    # junit XML 是稳定性采样唯一依赖的产物：按 nodeid 归集每一轮结果
    cmd += ["--junitxml", str(junit)]
    return cmd


def run_rounds(args: argparse.Namespace) -> int:
    run_mod = _load_runner()
    suite = run_mod.SUITES[args.suite]

    outdir = Path(args.out_dir)
    if not outdir.is_absolute():
        outdir = ROOT / outdir
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"=== 稳定性采样：{args.suite} × {args.rounds} 轮 ===\n")
    started = time.time()
    per_round: list[dict[str, str]] = []

    for i in range(1, args.rounds + 1):
        round_dir = outdir / f"round-{i}"
        # 只删本轮要写的文件：直接删整个目录在带 DLP 的机器上会被拦，
        # 而且不必要——junit.xml 是覆盖写。
        round_dir.mkdir(parents=True, exist_ok=True)
        junit = round_dir / "junit.xml"
        if junit.exists():
            junit.unlink()

        cmd = build_cmd(args, suite, junit, round_dir)
        print(f"[round {i}/{args.rounds}] $ {' '.join(cmd)}\n", flush=True)
        proc = subprocess.run(cmd, cwd=ROOT)

        if proc.returncode == 5:
            print("\n[!!] 没有收集到任何用例（pytest 退出码 5），采样中止")
            print("     这条命令只收集不执行，用它确认筛选条件：")
            print("    ", " ".join(run_mod.collect_only_cmd(cmd)))
            return 5
        if not junit.exists():
            # 例如进程被外部策略杀掉。继续跑后面的轮次只会得出偏乐观的结论
            print(f"[!!] 第 {i} 轮没产出结果文件（pytest 退出码 {proc.returncode}），采样中止")
            return 5

        results = read_junit(junit)
        per_round.append(results)
        failed = sum(1 for v in results.values() if v == "failed")
        print(f"[round {i}] {len(results)} 条，失败 {failed} 条\n", flush=True)

    duration = time.time() - started
    report = build_report(args.suite, per_round, duration)
    return finalize(report, args, outdir)


def finalize(report, args: argparse.Namespace, outdir: Path) -> int:
    print("=" * 72)
    print(report.headline())
    print("=" * 72)

    for verdict in (BROKEN, FLAKY, NO_DATA, STABLE):
        group = [t for t in report.tests if t.verdict == verdict]
        if not group:
            continue
        print(f"\n{VERDICT_LABEL[verdict]}（{len(group)}）")
        if verdict == STABLE:
            print("  全部通过，不逐条列出")
            continue
        for t in group:
            extra = ""
            if verdict == NO_DATA:
                extra = "  <- 每轮都是 skip，等于没测"
            print(f"  {t.pattern:<{args.rounds}} {t.flake_rate:>6.0%}  {t.nodeid}{extra}")

    if args.json:
        jpath = Path(args.json)
        if not jpath.is_absolute():
            jpath = ROOT / jpath
        jpath.parent.mkdir(parents=True, exist_ok=True)
        jpath.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                         encoding="utf-8")
        print(f"\n[json] {jpath}")

    # Markdown 留档：给 MR 评论、给归档、也给三个月后回头看自己在问什么
    mpath = outdir / "latest.md"
    mpath.write_text(report.to_text(), encoding="utf-8")
    stamp = outdir / f"report-{time.strftime('%Y%m%d-%H%M%S')}.md"
    stamp.write_text(report.to_text(), encoding="utf-8")
    print(f"[md]   {mpath}")
    print(f"[md]   {stamp}")

    return gate(report, args)


def gate(report, args: argparse.Namespace) -> int:
    """把结论翻译成退出码。

    顺序是有讲究的：先判持续失败，再判抖动。
    全红的用例属于缺陷，如果先按抖动处理（顺手丢进隔离区），
    真正的退化就被藏起来了。
    """
    broken = [t for t in report.tests if t.verdict == BROKEN]
    flaky = [t for t in report.tests if t.verdict == FLAKY
             and t.flake_rate > args.max_flake_rate]

    if broken:
        print(f"\n[FAIL] {len(broken)} 条持续失败 = 真缺陷，不是抖动：")
        for t in broken[:10]:
            print(f"       {t.nodeid}")
        print("       处理：打开缺陷单，不要往隔离区丢")
        return 2

    if flaky:
        print(f"\n[FAIL] {len(flaky)} 条抖动率超过 {args.max_flake_rate:.0%}：")
        for t in flaky[:10]:
            print(f"       {t.flake_rate:>6.0%}  {t.pattern}  {t.nodeid}")
        print(f"       处理：先留证据（见 latest.md），再用 --quarantine-add 限期隔离")
        return 3

    no_data = [t for t in report.tests if t.verdict == NO_DATA]
    if no_data:
        print(f"\n[warn] {len(no_data)} 条每轮都是 skip —— 它们不在保护范围内，别当成通过")

    print("\n[ok] 稳定性在阈值内")
    return 0


# ---------------------------------------------------------------- 隔离区
def load_list() -> Quarantine:
    return Quarantine.load(ROOT)


def collect_nodeids(args: argparse.Namespace) -> set[str] | None:
    """收集当前用例全集，用来发现名单里已经不存在的条目。"""
    run_mod = _load_runner()
    try:
        paths = run_mod.SUITES[args.suite]["paths"]
    except KeyError:
        return None
    # 用 run.py 同一套插件开关，否则 pytest-base-url / pytest-playwright
    # 会把这次收集本身搞崩，得到"名单里的用例都不存在"的假结论
    ns = argparse.Namespace(base_url=args.base_url, env=args.env, mark=args.mark,
                            keyword=args.keyword, workers=0, verbose=False, extra=None)
    cmd = run_mod.build_pytest_cmd(ns, {"paths": paths, "workers": 0})
    cmd = [t for t in cmd if t not in ("-q", "--alluredir", str(ROOT / "allure-results"))]
    proc = subprocess.run(cmd + ["--collect-only", "-q"], cwd=ROOT,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return {ln.strip() for ln in proc.stdout.splitlines() if "::" in ln}


def cmd_check_quarantine(args: argparse.Namespace) -> int:
    q = load_list()
    if not q.entries:
        print("[ok] 隔离区为空，没有债务")
        return 0

    print(q.markdown())
    known = collect_nodeids(args) if args.verify_nodeids else None
    problems = q.check(known_nodeids=known, max_size=args.max_quarantine_size)

    if not problems:
        print(f"\n[ok] 隔离区体检通过（{len(q.entries)} 条，均在有期状态）")
        return 0
    print(f"\n[FAIL] 隔离区体检不通过，{len(problems)} 个问题：")
    for p in problems:
        print(f"       - {p}")
    return 4


def cmd_add(args: argparse.Namespace) -> int:
    q = load_list()
    due = args.due or (date.today() + timedelta(days=args.due_days)).isoformat()
    entry = QuarantineEntry(
        nodeid=args.quarantine_add, reason=args.reason or "(未填写原因)",
        owner=args.owner, since=date.today().isoformat(),
        due=due, evidence=args.evidence,
    )
    problems = entry.check()
    if problems:
        for p in problems:
            print(f"[FAIL] {p}")
        return 4
    q.add(entry)
    path = q.save(ROOT)
    print(f"[ok] 已加入隔离区（{due} 到期）：{entry.nodeid}")
    print(f"     {path}")
    print("     到期后条目自动失效，用例会回到门禁里继续跑——别指望它能自动续期")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    q = load_list()
    if not q.remove(args.quarantine_rm):
        print(f"[warn] 名单里没有这条：{args.quarantine_rm}")
        return 0
    q.save(ROOT)
    print(f"[ok] 已移出隔离区：{args.quarantine_rm}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="稳定性采样与 flake 治理")
    p.add_argument("--suite", default="smoke")
    p.add_argument("--rounds", type=int, default=5,
                   help="重复轮次，默认 5。要发现 10%% 的抖动，至少要 10 轮")
    p.add_argument("--max-flake-rate", type=float, default=0.05,
                   help="单条用例抖动率上限，超了退出码 3")
    p.add_argument("--base-url", default=None)
    p.add_argument("--env", default=None, choices=["local", "staging", "prod"])
    p.add_argument("-m", "--mark", default=None)
    p.add_argument("-k", "--keyword", default=None)
    p.add_argument("-n", "--workers", type=int, default=None)
    p.add_argument("--out-dir", default="artifacts/flake")
    p.add_argument("--json", default=None, help="额外输出结构化报告")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--extra", default=None, help="透传给 pytest 的额外参数")
    p.add_argument("--mock", action="store_true", help="先起本地靶机再采样")

    p.add_argument("--check-quarantine", action="store_true", help="只做隔离区体检")
    p.add_argument("--verify-nodeids", action="store_true",
                   help="体检时核对名单里的用例是否还存在（会多跑一次收集）")
    p.add_argument("--max-quarantine-size", type=int, default=8)

    g = p.add_mutually_exclusive_group()
    g.add_argument("--quarantine-add", metavar="NODEID")
    g.add_argument("--quarantine-rm", metavar="NODEID")
    g.add_argument("--list-quarantine", action="store_true")

    p.add_argument("--reason", default=None, help="隔离原因（--quarantine-add 用）")
    p.add_argument("--owner", default="", help="负责人（--quarantine-add 用）")
    p.add_argument("--due", default=None, help="期限 YYYY-MM-DD")
    p.add_argument("--due-days", type=int, default=14, help="未给 --due 时的默认隔离天数")
    p.add_argument("--evidence", default="", help="抖动证据报告路径")

    args = p.parse_args()

    try:
        sys.path.insert(0, str(ROOT / "tools"))
        if args.check_quarantine:
            return cmd_check_quarantine(args)
        if args.list_quarantine:
            print(load_list().markdown())
            return 0
        if args.quarantine_add:
            return cmd_add(args)
        if args.quarantine_rm:
            return cmd_rm(args)

        server = None
        if args.mock:
            import mock_gitlab

            server = mock_gitlab.MockGitLab(port=0).start_background()
            args.base_url = server.base_url
            import os

            os.environ["GITLAB_TOKEN"] = mock_gitlab._TOKEN
            os.environ["GITLAB_ROOT_PASSWORD"] = mock_gitlab._ROOT_PASSWORD
            # 同 run.py：占位符而非空串，否则 Windows 上等同于删除变量，
            # dotenv 会把 .env 里的真实第二身份又填回来
            os.environ["GITLAB_MEMBER_TOKEN"] = "<disabled-in-mock>"
            print(f"=== 本地靶机：{server.base_url}（第二身份置空，越权用例将 skip）===")
        try:
            if args.suite not in _load_runner().SUITES:
                print(f"[FAIL] 未知套件 {args.suite}，可选：{', '.join(_load_runner().SUITES)}")
                return 5
            return run_rounds(args)
        finally:
            if server:
                server.stop()
    finally:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
