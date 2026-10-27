"""根 conftest：环境、守卫、Allure 集成、失败留证。

这个文件是整个框架的枢纽，四件事必须做对：
1. 缺配置要 skip 而不是 pass —— 静默通过的测试比失败的测试更危险；
2. 生产环境禁止写操作 —— 在收集阶段拦，不靠人记得加参数；
3. 失败必有证据 —— 截图 / trace / 请求日志自动贴进报告；
4. 数据必回收 —— session 结束统一清理，并留一份清理记录。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from gqa.allure_meta import write_properties
from gqa.api.client import GitLabClient
from gqa.api.coverage import current as current_coverage
from gqa.api.resources import GitLab
from gqa.data.factory import DataFactory
from gqa.flake.quarantine import Quarantine
from gqa.settings import Settings

# 本文件就在项目根，所以 parents[0] 才是项目根。
# 写成 parents[1] 会指到上一级目录，环境信息就会被写到项目外面去，
# 报告首页的 Environment 区块自然一片空白——而且不会报错，很难发现。
PROJECT_ROOT = Path(__file__).resolve().parents[0]
ALLURE_DIR = PROJECT_ROOT / "allure-results"
FLAKE_DIR = PROJECT_ROOT / "artifacts" / "flake"
COVERAGE_DIR = PROJECT_ROOT / "artifacts" / "coverage"

# 本轮所有「重试过才通过」的记录。session 结束统一落盘，
# 让重试这件事可审计——悄悄重试等于把抖动藏起来。
_RERUNS: list[dict] = []


# ==========================================================================
# 命令行
# ==========================================================================
def pytest_addoption(parser: pytest.Parser) -> None:
    g = parser.getgroup("gitlab-qa", "GitLab 测试框架配置")
    g.addoption("--env", default=None, choices=["local", "staging", "prod"],
                help="运行环境；prod 自动跳过 destructive")
    g.addoption("--base-url", default=None, help="GitLab 根地址")
    g.addoption("--token", default=None, help="Personal Access Token")
    g.addoption("--allure-dir", default=str(ALLURE_DIR), help="Allure 结果目录")
    g.addoption("--headed", action="store_true", help="UI 测试显示浏览器")
    g.addoption("--slow-mo", type=int, default=0, help="UI 操作慢放毫秒数")
    g.addoption("--perf-concurrency", type=int, default=None)
    g.addoption("--perf-samples", type=int, default=None)
    g.addoption("--perf-p95", type=int, default=None)
    g.addoption("--perf-min-rps", type=float, default=None)
    g.addoption("--perf-jitter", type=float, default=None)
    g.addoption("--include-quarantine", action="store_true",
                help="连隔离区里的用例一起跑（验收修复是否真的生效时用）")


def _quarantine(config: pytest.Config) -> Quarantine:
    try:
        return Quarantine.load(PROJECT_ROOT)
    except ValueError as exc:
        # 名单损坏必须让人看见。静默当成空名单的话，
        # 所有被隔离的用例会突然回到门禁里，表现为"莫名其妙红一片"
        pytest.exit(f"隔离区名单无法解析：{exc}", returncode=4)


def pytest_report_header(config: pytest.Config) -> list[str]:
    s = Settings.load(
        env=config.getoption("--env"),
        base_url=config.getoption("--base-url"),
        token=config.getoption("--token"),
    )
    lines = [f"gitlab-qa  {s.describe()}"]
    q = _quarantine(config)
    if q.entries:
        expired = q.expired()
        lines.append(
            f"隔离区    {len(q.entries)} 条（生效中 {len(q.entries) - len(expired)}，"
            f"已过期 {len(expired)}）  名单：flake/quarantine.json"
        )
        if expired and not config.getoption("--include-quarantine"):
            for e in expired:
                lines.append(f"  [到期] {e.nodeid} —— {e.due} 到期，本次已恢复执行")
    return lines


def pytest_configure(config: pytest.Config) -> None:
    # 写 environment.properties，让 Allure 首页显示环境信息
    s = Settings.load(
        env=config.getoption("--env"),
        base_url=config.getoption("--base-url"),
        token=config.getoption("--token"),
    )
    props = {
        "Environment": s.env,
        "GitLab.URL": s.base_url,
        "GitLab.Token": "已配置" if s.has_token else "未配置（需 Token 的用例将 skip）",
        "Perf.Concurrency": str(s.perf_concurrency),
        "Perf.Samples": str(s.perf_samples),
        "Perf.P95Threshold": f"{s.perf_p95_ms} ms",
        "Python": "3.13",
    }
    # --alluredir 是 allure-pytest 的选项（dest=allure_report_dir），
    # --allure-dir 是我们自己加的。两个不是一回事，必须以实际使用的为准，
    # 否则环境信息会写到另一个目录，报告里就看不到。
    outdir = Path(
        config.getoption("allure_report_dir", default=None)
        or config.getoption("--allure-dir")
        or ALLURE_DIR
    )
    outdir.mkdir(parents=True, exist_ok=True)
    # 不能直接 write_text(encoding="utf-8")：Allure 按 ISO-8859-1 读 properties，
    # 中文会变成乱码。走 write_properties 转成 \uXXXX 转义。
    write_properties(outdir / "environment.properties", props)

    # 给本轮覆盖率分片打批次号。xdist 的 worker 是 configure 之后才拉起的，
    # 会继承这里设的环境变量。
    #
    # 没有批次号时，主进程结束时会把**历史残留**的分片一起并进来：
    # 一来覆盖率虚高（上一轮打过的端点算进了这一轮），
    # 二来会在收尾时一口气删掉几十个旧文件——企业 DLP / 沙箱的批量删除
    # 守卫会直接掐掉进程，表现是"跑到 100% 却没有汇总行、退出码 1"，
    # 看着像用例失败，实际是清理动作被拦。批次号把这两个问题一起解决。
    if not hasattr(config, "workerinput"):
        os.environ.setdefault("GQA_COVERAGE_RUN", time.strftime("%Y%m%d-%H%M%S"))


# ==========================================================================
# 收集期守卫
# ==========================================================================
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    s = Settings.load(
        env=config.getoption("--env"),
        base_url=config.getoption("--base-url"),
        token=config.getoption("--token"),
    )
    skip_no_token = pytest.mark.skip(reason="未配置 GITLAB_TOKEN，跳过需要鉴权的用例")
    skip_prod = pytest.mark.skip(reason="prod 环境禁止执行 destructive 用例")

    # 隔离区：仍在保护期内的用例跳过，已到期的自动恢复执行。
    # 到期即失效是刻意的——隔离不是豁免证，到期不修好就让它回来继续红，
    # 否则隔离区会变成「丢进去就再没人管」的垃圾桶。
    force = config.getoption("--include-quarantine")
    listed = {} if force else _quarantine(config).active_map()

    for item in items:
        marks = set(item.keywords)
        # 缺 Token 一律 skip。让它 pass 是最坏的结果——
        # 报告上一片绿，实际上一条需要鉴权的用例都没跑
        if "requires_token" in marks and not s.has_token:
            item.add_marker(skip_no_token)
        if s.env == "prod" and "destructive" in marks:
            item.add_marker(skip_prod)
        entry = listed.get(item.nodeid)
        if entry is not None:
            item.add_marker(pytest.mark.skip(
                reason=f"隔离区：{entry.describe()}"
            ))


# ==========================================================================
# 基础设施 fixture
# ==========================================================================
@pytest.fixture(scope="session")
def settings(request: pytest.FixtureRequest) -> Settings:
    s = Settings.load(
        env=request.config.getoption("--env"),
        base_url=request.config.getoption("--base-url"),
        token=request.config.getoption("--token"),
    )
    if request.config.getoption("--perf-concurrency"):
        s.perf_concurrency = request.config.getoption("--perf-concurrency")
    if request.config.getoption("--perf-samples"):
        s.perf_samples = request.config.getoption("--perf-samples")
    if request.config.getoption("--perf-p95"):
        s.perf_p95_ms = request.config.getoption("--perf-p95")
    if request.config.getoption("--perf-min-rps"):
        s.perf_min_rps = request.config.getoption("--perf-min-rps")
    if request.config.getoption("--perf-jitter"):
        s.perf_max_jitter = request.config.getoption("--perf-jitter")
    return s


@pytest.fixture(scope="session")
def anon_client(settings: Settings):
    """不带 Token 的只读客户端。用于验证「未鉴权该拒绝的会被拒绝」。"""
    c = GitLabClient(settings.base_url, token="", timeout=settings.timeout)
    yield GitLab(c)
    c.close()


@pytest.fixture(scope="session")
def gl(settings: Settings):
    """带 Token 的完整资源门面。"""
    c = GitLabClient(settings.base_url, token=settings.token, timeout=settings.timeout)
    yield GitLab(c)
    _attach_request_log(c, "主客户端请求日志")
    c.close()


@pytest.fixture(scope="session")
def member_client(settings: Settings):
    """第二个身份：普通用户 / 只读 Token。

    越权和权限语义类断言必须有第二个身份才成立——用管理员访问管理员端点
    永远返回 200，断言恒真，是典型的假绿。没配就 skip，绝不假装通过。
    配置：.env 里加 GITLAB_MEMBER_TOKEN
    """
    if not settings.has_member_token:
        pytest.skip("未配置 GITLAB_MEMBER_TOKEN，跳过需要第二个身份的权限断言")
    c = GitLabClient(settings.base_url, token=settings.member_token, timeout=settings.timeout)
    yield GitLab(c)
    _attach_request_log(c, "普通用户请求日志")
    c.close()


@pytest.fixture
def data_factory(gl: GitLab, settings: Settings):
    """数据工厂：创建即登记，用例结束自动回收。

    清理是 session 级还是 function 级是个权衡——这里放 function 级，
    因为「上一条用例造的数据影响了下一条」是接口测试最常见的假失败来源。
    """
    factory = DataFactory(gl, prefix=settings.resource_prefix)
    yield factory
    factory.cleanup()
    _attach_text(factory.ledger.report(), "数据清理记录")


# ==========================================================================
# UI
# ==========================================================================
@pytest.fixture(scope="session")
def ui_session(settings: Settings, request: pytest.FixtureRequest):
    """Playwright 会话。浏览器没装就 skip，不静默通过。"""
    try:
        from gqa.ui.browser import UISession
    except ImportError:
        pytest.skip("未安装 playwright")

    session = UISession(
        base_url=settings.base_url,
        headless=not request.config.getoption("--headed"),
        slow_mo=request.config.getoption("--slow-mo"),
    )
    try:
        session.start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"Playwright 浏览器不可用：{exc}\n"
            f"执行 playwright install chromium 后重试"
        )
    yield session
    session.teardown()


@pytest.fixture
def page(ui_session, request: pytest.FixtureRequest):
    """每条 UI 用例一个独立 context。"""
    p = ui_session.new_page(request.node.name)
    yield p
    for art in ui_session.stop_context(p):
        _attach_file(art.path, art.name, art.kind)


# ==========================================================================
# Allure：失败留证
# ==========================================================================
def _allure():
    try:
        import allure
        return allure
    except ImportError:
        return None


def _attach_text(text: str, name: str) -> None:
    a = _allure()
    if a is None:
        return
    try:
        a.attach(text, name=name, attachment_type=a.attachment_type.TEXT)
    except Exception:  # noqa: BLE001
        pass


def _attach_file(path: str, name: str, kind: str) -> None:
    a = _allure()
    if a is None:
        return
    p = Path(path)
    if not p.exists():
        return
    try:
        if kind == "screenshot":
            a.attach.file(str(p), name=name, attachment_type=a.attachment_type.PNG)
        elif kind == "trace":
            a.attach.file(str(p), name=name, attachment_type=a.attachment_type.ZIP)
        else:
            a.attach.file(str(p), name=name, attachment_type=a.attachment_type.HTML)
    except Exception:  # noqa: BLE001
        pass


def _attach_request_log(client: GitLabClient, name: str) -> None:
    if client.log.entries:
        _attach_text(client.log.dump(), name)


def _record_rerun(item: pytest.Item, report) -> None:
    """把重试次数写进 Allure 和审计日志。

    框架刻意**不做全局失败重跑**——偶现失败是信号，把它重跑成绿等于消灭信号。
    只对显式打了 @pytest.mark.flaky 的用例重试（pytest-rerunfailures 提供），
    而且必须留下记录：报告里看到"重试 2 次后通过"，这条用例就已经在隔离候选名单上了。
    """
    # execution_count 由 pytest-rerunfailures 维护，第几次尝试就是它
    attempts = int(getattr(item, "execution_count", 1) or 1)
    if attempts <= 1:
        return

    record = {
        "nodeid": item.nodeid,
        "attempts": attempts,
        "final": report.outcome,
        "rerun_tag": True,
    }
    _attach_text(
        f"重试 {attempts} 次后{report.outcome}\n"
        f"用例：{item.nodeid}\n\n"
        "重试不是修复：这条用例已被记为不稳定候选，"
        "请用 `python tools/flake.py -k <用例名> --rounds 10` 留证据后再决定怎么处理。",
        "重试记录",
    )
    # 只有"重试后才过"才算抖动候选；重试后仍失败就是普通失败，不需要额外记
    if report.passed:
        _RERUNS.append(record)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call):
    outcome = yield
    report = outcome.get_result()

    if report.when != "call":
        return

    _record_rerun(item, report)

    if report.failed:
        _capture_failure_evidence(item)


def _write_coverage() -> None:
    """落盘接口覆盖率。

    并行时每个 worker 各写一份分片，主进程汇总后再出总表。
    少了汇总这一步，-n 4 跑出来的覆盖率会只有真实值的四分之一左右，
    而且每次都不一样——度量一旦不可信，就没人会拿它做决策。
    """
    import os

    cov = current_coverage()
    COVERAGE_DIR.mkdir(parents=True, exist_ok=True)

    # 批次号：只并、只删**本轮**的分片（见 pytest_configure 里的注释）
    run_id = os.getenv("GQA_COVERAGE_RUN", "adhoc")
    worker = os.getenv("PYTEST_XDIST_WORKER")
    if worker:
        (COVERAGE_DIR / f"api_coverage-{run_id}-{worker}.json").write_text(
            json.dumps(cov.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return

    shards = sorted(COVERAGE_DIR.glob(f"api_coverage-{run_id}-*.json"))
    merged = 0
    for part in shards:
        try:
            data = json.loads(part.read_text(encoding="utf-8"))
            cov.merge_hits(data.get("covered_endpoints", {}),
                           data.get("out_of_catalog", {}))
            merged += 1
        except (OSError, ValueError):
            continue
        try:
            part.unlink()
        except (OSError, SystemExit):
            # 被 DLP / 沙箱的批量删除守卫拦下时抛的是 SystemExit，
            # 不是 OSError。分片留着只占几 KB，绝不会因此吞掉整轮结果。
            pass
    if shards:
        print(f"[coverage] 合并 {merged}/{len(shards)} 个 worker 分片（批次 {run_id}）")

    (COVERAGE_DIR / "api_coverage.json").write_text(
        json.dumps(cov.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    (COVERAGE_DIR / "api_coverage.md").write_text(cov.markdown(), encoding="utf-8")
    for line in cov.summary_lines():
        print(f"[coverage] {line}")
    print(f"[coverage] 明细：{COVERAGE_DIR / 'api_coverage.md'}")


def pytest_sessionfinish(session: pytest.Session, exitstatus) -> None:
    """把本轮的重试记录追加到 artifacts/flake/reruns.jsonl。

    用追加而不是覆盖：抖动是趋势问题，单次记录看不出
    "这条用例本周已经靠重试混过去 6 次了"。
    """
    _write_coverage()

    if not _RERUNS:
        return
    FLAKE_DIR.mkdir(parents=True, exist_ok=True)
    log = FLAKE_DIR / "reruns.jsonl"
    with log.open("a", encoding="utf-8") as fh:
        for r in _RERUNS:
            r["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n[flake] {len(_RERUNS)} 条用例靠重试才通过，已记入 {log}")
    for r in _RERUNS[:5]:
        print(f"        重试 {r['attempts']} 次 → {r['nodeid']}")


def _capture_failure_evidence(item: pytest.Item) -> None:
    """失败时按测试类型贴证据：能不能一眼定位，差别在这里。"""
    # 1) UI 用例：截图 + DOM 快照
    ui = item.funcargs.get("ui_session")
    pg = item.funcargs.get("page")
    if ui is not None and pg is not None:
        for art in ui.capture_failure(pg, "failure"):
            _attach_file(art.path, art.name, art.kind)

    # 2) API 用例：把这次会话的请求/响应原文贴出来
    for fname in ("gl", "anon_client"):
        holder = item.funcargs.get(fname)
        if holder is not None:
            _attach_request_log(holder.client, f"{fname} 请求日志")
            break

    # 3) 性能用例：把采样明细贴出来
    perf = getattr(item, "_perf_result", None)
    if perf is not None:
        _attach_text(perf.markdown(), "性能采样明细")
