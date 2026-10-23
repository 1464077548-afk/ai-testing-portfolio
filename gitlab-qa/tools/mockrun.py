"""起一个本地靶机，然后对着它跑测试。

把「起服务 → 跑测试 → 收服务」合成一条命令，避免手工起停时的两类问题：
端口忘记关导致下次跑连到旧进程上；服务没起来就跑，全红却以为是代码问题。

    python tools/mockrun.py --suite all
    python tools/mockrun.py --suite perf --latency-ms 3000   # 让性能门禁变红
    python tools/mockrun.py --suite perf --error-rate 0.3    # 让错误率门禁变红
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

SUITES = {
    "smoke": {"desc": "冒烟（无需 Token）", "path": "tests/smoke", "extra": []},
    "api": {"desc": "接口（含写操作）", "path": "tests/api", "extra": []},
    "perf": {"desc": "性能门禁", "path": "tests/perf", "extra": []},
    "ui": {"desc": "UI（Playwright）", "path": "tests/ui", "extra": []},
    "e2e": {"desc": "端到端流程", "path": "tests/e2e", "extra": []},
    "contract": {"desc": "契约校验", "path": "tests/api/test_contract.py", "extra": []},
    # 对公共/真实实例跑时排除 UI：UI 需要自建实例的页面与凭据
    "all": {"desc": "全部（跳过 UI）", "path": "tests", "extra": ["-m", "not ui"]},
    # 对靶机跑时用 full：UI、E2E、性能一起过一遍
    "full": {"desc": "全部（含 UI/E2E）", "path": "tests", "extra": []},
}

MOCK_TOKEN = "mock-token"


def main() -> int:
    p = argparse.ArgumentParser(description="本地靶机 + 测试运行器")
    p.add_argument("--suite", default="all", choices=sorted(SUITES))
    p.add_argument("--port", type=int, default=0, help="0 表示自动选空闲端口")
    p.add_argument("--latency-ms", type=float, default=0.0,
                   help="注入固定延迟，用于验证性能门禁会拦")
    p.add_argument("--jitter-ms", type=float, default=0.0)
    p.add_argument("--error-rate", type=float, default=0.0,
                   help="注入 503 的比例，用于验证错误率门禁会拦")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--p95-ms", type=int, default=800)
    p.add_argument("--samples", type=int, default=30)
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--extra", default="", help="追加给 pytest 的参数（支持引号）")
    args = p.parse_args()

    import shlex

    import mock_gitlab
    mock_gitlab._LATENCY_MS = args.latency_ms
    mock_gitlab._JITTER_MS = args.jitter_ms
    mock_gitlab._ERROR_RATE = args.error_rate
    mock_gitlab._TOKEN = MOCK_TOKEN

    suite = SUITES[args.suite]
    srv = mock_gitlab.MockGitLab(port=args.port or 0).start_background()
    print(f"=== 靶机：{srv.base_url}（{suite['desc']}）===")
    if args.latency_ms or args.jitter_ms:
        print(f"   延迟注入 {args.latency_ms}ms + 抖动 {args.jitter_ms}ms")
    if args.error_rate:
        print(f"   错误注入 {args.error_rate:.0%}")

    env = dict(os.environ)
    env.update({
        "GITLAB_TOKEN": MOCK_TOKEN,
        "PERF_P95_MS": str(args.p95_ms),
        "PERF_SAMPLES": str(args.samples),
        "PERF_CONCURRENCY": str(args.concurrency),
        "PYTHONPATH": str(ROOT / "src"),
    })

    cmd = [sys.executable, "-m", "pytest", suite["path"], "-q",
           "--base-url", srv.base_url, "-p", "no:cacheprovider"]
    cmd += suite["extra"]
    if args.workers > 1:
        cmd += ["-n", str(args.workers), "--dist", "loadscope"]
    if args.extra:
        cmd += shlex.split(args.extra)

    import subprocess
    try:
        proc = subprocess.run(cmd, cwd=ROOT, env=env)
        rc = proc.returncode
    finally:
        srv.stop()

    if rc == 5:
        print("\n[!!] 没有收集到任何用例（退出码 5）。检查 --suite 与 --extra 是否把用例筛没了。")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
