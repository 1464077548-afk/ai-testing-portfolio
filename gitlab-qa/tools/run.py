"""一键运行器：跑测试 → 生成 Allure 报告 → 可选打开浏览器。

存在的理由：一套测试框架真正难的不是写用例，是「换个人来也能跑出同样的结果」。
把 pytest 参数、结果目录、报告生成、历史趋势这几件事固化下来，
避免每个人跑出来的报告长得不一样、互相没法比对。

用法：
    python tools/run.py --suite smoke
    python tools/run.py --suite api --base-url http://localhost:8929
    python tools/run.py --suite all --open
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "allure-results"
REPORT = ROOT / "allure-report"
CATEGORIES = ROOT / "allure" / "categories.json"

# 分层套件：每层跑什么、要不要鉴权、要不要浏览器
SUITES: dict[str, dict] = {
    "unit": {
        "paths": ["tests/unit"],
        "desc": "框架自检（不联网）",
        "workers": 0,
    },
    "smoke": {
        "paths": ["tests/unit", "tests/smoke"],
        "desc": "冒烟：环境是否活着 + 核心只读接口",
        "workers": 0,
    },
    "api": {
        "paths": ["tests/api"],
        "desc": "接口：鉴权/CRUD/契约/分页",
        "workers": 4,
    },
    "contract": {
        "paths": ["tests/api/test_contract.py", "tests/api/test_pagination.py"],
        "desc": "契约与分页（只读，可打公网实例）",
        "workers": 0,
    },
    "ui": {
        "paths": ["tests/ui"],
        "desc": "UI：Playwright 关键路径",
        "workers": 0,
    },
    "e2e": {
        "paths": ["tests/e2e"],
        "desc": "端到端：API 建数据 → UI 验收",
        "workers": 0,
    },
    "perf": {
        "paths": ["tests/perf"],
        "desc": "性能：并发采样 + P95 门禁",
        "workers": 0,
    },
    "security": {
        "paths": ["tests/security"],
        "desc": "安全：越权/注入/泄露/暴露面（仅自有实例，夜间跑）",
        "workers": 0,
    },
    "regression": {
        "paths": ["tests/unit", "tests/smoke", "tests/api", "tests/e2e"],
        "desc": "回归全量（不含性能与 UI）",
        "workers": 4,
    },
    "all": {
        "paths": ["tests"],
        "desc": "全量",
        "workers": 4,
    },
}


def _allure_cmd() -> str | None:
    """找 allure 可执行文件：PATH → 常见安装位置。"""
    found = shutil.which("allure")
    if found:
        return found
    for base in (ROOT / "tools" / "allure", Path(r"D:\tools\allure")):
        if base.exists():
            for p in sorted(base.glob("allure-*/bin/allure.bat")) + sorted(base.glob("*/bin/allure*")):
                return str(p)
    return None


def collect_only_cmd(cmd: list[str]) -> list[str]:
    """把执行用的参数剥掉，只留筛选条件，得到一条安全的收集命令。

    带参数的选项（--alluredir <dir>、-n <n>、--dist <mode>）要连值一起去掉，
    否则留下孤零零的 "--dist" 或 "-n"，复制出去执行反而报错。
    """
    takes_value = {"--alluredir", "-n", "--dist", "-p"}
    drop_bare = {"-q", "-v"}
    out: list[str] = []
    skip_next = False
    for tok in cmd:
        if skip_next:
            skip_next = False
            continue
        if tok in takes_value:
            skip_next = True          # 连它后面的值一起丢
            continue
        if tok in drop_bare:
            continue
        out.append(tok)
    return out + ["--collect-only", "-q"]


def clean_results(keep: bool = False) -> None:
    """清掉上一次的结果，但保留 history/。

    这里不用 pytest 的 --clean-alluredir，也不用 shutil.rmtree：
    后者会把 history 目录一起删掉，而 Allure 的历史趋势正是靠它跨次累积的。
    只删本次会重新生成的文件，趋势才能连起来。

    keep=True 时完全不删：公司终端、CI 沙箱或有 DLP 的机器上，批量删除会被
    安全策略直接拦掉（甚至把进程一起干掉），表现为「没跑任何用例就退出码 1」。
    这种环境只允许追加，就让旧结果留着，由 Allure 按时间戳自行取舍。
    """
    if keep:
        print("[clean] 按 --keep-results 保留旧结果文件")
        return
    if not RESULTS.exists():
        return
    patterns = ("*-result.json", "*-attachment.*", "*-container.json",
                "environment.properties", "executor.json", "categories.json")
    removed = 0
    for pat in patterns:
        for p in RESULTS.glob(pat):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"[clean] 清理上次结果 {removed} 个文件（保留 history/）")


def build_pytest_cmd(args: argparse.Namespace, suite: dict) -> list[str]:
    cmd = [sys.executable, "-m", "pytest", "-q", *suite["paths"]]

    if args.base_url:
        cmd += ["--base-url", args.base_url]
    if args.env:
        cmd += ["--env", args.env]
        if args.env == "prod":
            # prod 下再兜一道：写操作用例在收集期就 skip
            cmd += ["-m", "not destructive"]
    if args.mark:
        cmd += ["-m", args.mark]
    if args.keyword:
        cmd += ["-k", args.keyword]

    workers = args.workers if args.workers is not None else suite["workers"]
    if workers > 1:
        # loadscope：同一模块的用例分到一个 worker，避免 fixture 重复初始化
        cmd += ["-n", str(workers), "--dist", "loadscope"]

    cmd += ["--alluredir", str(RESULTS)]
    cmd += ["-p", "no:cacheprovider"]
    # 显式再关一次：pyproject 的 addopts 可能因 rootdir 判定不同而没生效，
    # 而这两个插件一装载就会与本框架抢 --base-url / page 固件，属于必挡项。
    cmd += ["-p", "no:base_url", "-p", "no:playwright"]
    if args.verbose:
        cmd.append("-v")
    if args.extra:
        # 必须用 shlex 而不是 str.split：后者遇到 -m 'not a and not b'
        # 会把引号一起塞进参数，pytest 收到的是带引号的表达式，静默选不中任何用例
        import shlex

        cmd += shlex.split(args.extra)
    return cmd


def generate_report(name: str, locale: str | None = "zh") -> bool:
    allure = _allure_cmd()
    if not allure:
        print("[warn] 未找到 allure 命令行，跳过报告生成。")
        print("       安装：https://allurereport.org/docs/install/")
        return False

    # 目录固定为 allure-report，不按套件分开。
    # Allure 的趋势按用例 fullName 聚合成 key，不同套件跑出的同一条用例
    # 本就该连成一条趋势线；按套件分目录反而会每次都从零开始。
    report = REPORT

    # 趋势：把上次报告里的 history 搬进本次结果，Allure 才会画趋势图。
    # 少了这一步，每次报告都是孤立的，看不出「这周比上周差了」。
    prev = report / "history"
    if prev.exists():
        shutil.copytree(prev, RESULTS / "history", dirs_exist_ok=True)
        print(f"[trend] 继承历史趋势（{len(list(prev.glob('*.json')))} 条）")
    else:
        print("[trend] 首次运行，暂无历史趋势")

    # categories.json 决定报告里「缺陷归类」的样子，每次都要拷进结果目录
    if CATEGORIES.exists():
        shutil.copy(CATEGORIES, RESULTS / "categories.json")

    # executor.json：报告首页显示构建来源（本地 / CI）
    exec_info = {
        "name": os.getenv("CI_JOB_NAME") or os.getenv("GITHUB_WORKFLOW") or "Local Run",
        "type": "gitlab" if os.getenv("GITLAB_CI") else ("github" if os.getenv("GITHUB_ACTIONS") else "local"),
        "url": os.getenv("CI_JOB_URL") or os.getenv("GITHUB_SERVER_URL", ""),
        "buildName": os.getenv("CI_PIPELINE_ID") or os.getenv("GITHUB_RUN_NUMBER") or name,
        "reportName": f"GitLab QA · {name}",
    }
    import json
    (RESULTS / "executor.json").write_text(
        json.dumps(exec_info, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    subprocess.run(
        [allure, "generate", str(RESULTS), "-o", str(report), "--clean"],
        check=True,
    )
    print(f"[ok] 报告已生成：{report / 'index.html'}")

    # 汉化必须放在 generate 之后：allure 每次都会重写 index.html。
    # 失败也不该让整轮测试白跑——界面是英文照样能看结论。
    if locale:
        # 按文件路径加载，不依赖 tools 是否是个包、也不依赖 cwd
        spec = importlib.util.spec_from_file_location(
            "localize_report", ROOT / "tools" / "localize_report.py"
        )
        mod = importlib.util.module_from_spec(spec) if spec and spec.loader else None
        try:
            if mod is not None and spec is not None and spec.loader is not None:
                spec.loader.exec_module(mod)
                mod.localize(report, locale=locale)
                print(f"[i18n] 报告界面语言：{locale}")
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 报告汉化失败（不影响测试结论）：{exc}")
    return True


def main() -> int:
    p = argparse.ArgumentParser(description="gitlab-qa 一键运行器")
    p.add_argument("--suite", default="smoke", choices=list(SUITES))
    p.add_argument("--base-url", default=None)
    p.add_argument("--env", default=None, choices=["local", "staging", "prod"])
    p.add_argument("--mark", default=None, help="额外 marker 过滤，如 'smoke and not perf'")
    p.add_argument("-k", "--keyword", default=None)
    p.add_argument("-n", "--workers", type=int, default=None, help="覆盖套件默认并行度")
    p.add_argument("--no-report", action="store_true", help="只跑测试不出报告")
    p.add_argument("--keep-results", action="store_true",
                   help="不清理上次的 allure-results（安全策略禁止批量删除时用）")
    p.add_argument("--locale", default="zh",
                   help="报告界面语言，默认 zh；传空字符串的原样保留 Allure 默认英文")
    p.add_argument("--open", action="store_true", help="报告生成后用浏览器打开")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--extra", default=None, help="透传给 pytest 的额外参数")
    p.add_argument("--mock", action="store_true",
                   help="先起本地靶机再跑（无需 Docker / 真实实例），会自动注入地址与 Token")
    p.add_argument("--mock-latency-ms", type=float, default=0.0,
                   help="配合 --mock：给靶机注入延迟，用于验证性能门禁会拦")
    p.add_argument("--mock-error-rate", type=float, default=0.0,
                   help="配合 --mock：注入 503 比例，用于验证错误率门禁会拦")
    args = p.parse_args()

    server = None
    if args.mock:
        sys.path.insert(0, str(ROOT / "tools"))
        import mock_gitlab

        mock_gitlab._LATENCY_MS = args.mock_latency_ms
        mock_gitlab._ERROR_RATE = args.mock_error_rate
        server = mock_gitlab.MockGitLab(port=0).start_background()
        args.base_url = server.base_url
        # Token 与管理员密码都由靶机内置，避免「跑起来但全 skip」
        os.environ["GITLAB_TOKEN"] = mock_gitlab._TOKEN
        os.environ["GITLAB_ROOT_PASSWORD"] = mock_gitlab._ROOT_PASSWORD
        # 第二身份必须一并清掉。它是从 .env 里读的真实实例 Token，
        # 留着的话越权类用例会拿它去打靶机，靶机不认 → 401，
        # 于是「环境没配对」被伪装成「被测系统有缺陷」。
        # 清掉后这些用例按设计 skip（不是 pass），报告里一眼看得出没测。
        # 占位符而不是空串：Windows 下 `os.environ[k] = ""` 等价于删除该变量
        # （CRT putenv 的既定行为），于是 settings 里的 load_dotenv(override=False)
        # 会发现「变量不存在」，顺手把 .env 里的真实 Token 填回来——
        # 结果就是清了个寂寞，越权用例继续拿真实例凭据去打靶机。
        # 用 `<...>` 前缀与 Settings 既有约定一致（has_member_token 见到 `<` 即视为未配置），
        # 且它是非空值，dotenv 不会再覆盖。
        os.environ["GITLAB_MEMBER_TOKEN"] = "<disabled-in-mock>"
        print("[mock] 已停用 GITLAB_MEMBER_TOKEN：靶机没有第二身份，越权类用例将 skip")
        args.env = args.env or "local"
        print(f"=== 本地靶机：{server.base_url}（无需真实实例）===")

    suite = SUITES[args.suite]
    print(f"=== 套件：{args.suite} — {suite['desc']} ===")

    clean_results(keep=args.keep_results)
    cmd = build_pytest_cmd(args, suite)
    print("$", " ".join(cmd), "\n")

    try:
        proc = subprocess.run(cmd, cwd=ROOT)
    finally:
        if server:
            server.stop()
    print()

    # pytest 用退出码 5 表示「一条都没跑」。不拦住这一支最危险：
    # 过滤条件写错、目录改错、marker 拼错都会得到 5，
    # 而它很容易被当成「跑完了，没问题」——绿色报表下其实零覆盖。
    if proc.returncode == 5:
        print("[!!] 没有收集到任何用例（pytest 退出码 5）")
        print("     常见原因：-m 表达式写错、路径不存在、--extra 引号被吞")
        print("     这条命令只收集不执行，用它确认条件写对了没有：")
        print("    ", " ".join(collect_only_cmd(cmd)))
        return 5

    if args.no_report:
        return proc.returncode

    if not any(RESULTS.glob("*result.json")):
        print("[warn] 没有测试结果，跳过报告生成")
        return proc.returncode

    generate_report(args.suite, locale=args.locale)

    if args.open:
        webbrowser.open((REPORT / "index.html").as_uri())

    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
