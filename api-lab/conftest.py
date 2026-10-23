"""根 conftest：全局配置、环境守卫、跨包共享的基础设施。

分层原则（这是本项目最想讲清的一件事）：
    根 conftest.py     → 环境、靶机、全局策略，与业务无关
    tests/conftest.py  → 业务 fixture（客户端、数据工厂），测试可用
    tests/api/conftest → 该子目录专属的前置条件

同名的 fixture 在子层会覆盖父层，所以「局部特化、全局兜底」是天然成立的。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "src"))

from apilab.client import ApiClient  # noqa: E402
from apilab.fake_server import ApiLabServer  # noqa: E402

DESTRUCTIVE_MARKERS = {"destructive"}


@dataclass
class Settings:
    base_url: str
    env: str
    timeout: float
    retries: int
    owns_server: bool


# --------------------------------------------------------------------------
# 命令行与全局钩子
# --------------------------------------------------------------------------
def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("apilab", "接口测试靶场配置")
    group.addoption(
        "--base-url",
        default=os.getenv("APILAB_BASE_URL", ""),
        help="被测系统地址；不传则自动启动本地靶机",
    )
    group.addoption(
        "--env",
        default=os.getenv("APILAB_ENV", "local"),
        choices=["local", "staging", "prod"],
        help="运行环境；prod 会自动跳过 destructive 用例",
    )
    group.addoption("--api-timeout", type=float, default=5.0)
    group.addoption("--api-retries", type=int, default=2)
    group.addoption(
        "--schema-drift",
        action="store_true",
        help="让靶机故意返回漂移字段，验证契约测试能拦住",
    )


def pytest_report_header(config: pytest.Config) -> list[str]:
    base_url = config.getoption("--base-url") or "<自动启动本地靶机>"
    return [
        f"apilab  environment={config.getoption('--env')}  target={base_url}",
        f"        timeout={config.getoption('--api-timeout')}s  retries={config.getoption('--api-retries')}",
    ]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """生产环境禁止一切写操作。这条守卫比任何流程规范都可靠。"""
    if config.getoption("--env") != "prod":
        return
    skip = pytest.mark.skip(reason="prod 环境禁止执行 destructive 用例")
    for item in items:
        if DESTRUCTIVE_MARKERS & set(item.keywords):
            item.add_marker(skip)


# --------------------------------------------------------------------------
# 靶机（session 级：整个测试会话只起一次）
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def settings(request: pytest.FixtureRequest) -> Settings:
    return Settings(
        base_url=request.config.getoption("--base-url"),
        env=request.config.getoption("--env"),
        timeout=request.config.getoption("--api-timeout"),
        retries=request.config.getoption("--api-retries"),
        owns_server=not request.config.getoption("--base-url"),
    )


@pytest.fixture(scope="session")
def lab_server(settings: Settings, request: pytest.FixtureRequest) -> ApiLabServer | None:
    """只有没指定 --base-url 时才启动本地靶机。

    port=0 让内核分配端口：xdist 的每个 worker 都是独立进程，
    各自起一个实例、各自持有一份数据，测试之间不会互相踩。
    """
    if not settings.owns_server:
        yield None
        return

    server = ApiLabServer(port=0)
    if request.config.getoption("--schema-drift"):
        server.state.schema_drift = True
    server.start_background()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def base_url(settings: Settings, lab_server: ApiLabServer | None) -> str:
    return settings.base_url or lab_server.base_url  # type: ignore[union-attr]


# --------------------------------------------------------------------------
# 客户端工厂
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def client_factory(base_url: str, settings: Settings):
    """每次调用产出一个独立会话。

    为什么不是单例：token 过期、越权、并发这几类用例必须有自己的会话，
    共用单例会让「上一个用例把令牌吊销了」变成随机失败。
    """

    created: list[ApiClient] = []

    def _make(username: str = "alice", password: str = "alice-pw", ttl: int = 3600) -> ApiClient:
        client = ApiClient(
            base_url=base_url,
            timeout=settings.timeout,
            retries=settings.retries,
        )
        client.login(username, password, ttl=ttl)
        created.append(client)
        return client

    yield _make

    for client in created:
        client.close()


# --------------------------------------------------------------------------
# pytest-html 报告定制：多一列「环境 + 标记」，排障时能直接定位
# --------------------------------------------------------------------------
@pytest.hookimpl(optionalhook=True)
def pytest_html_results_table_header(cells):
    # pytest-html 4.x 的 cells 是 HTML 字符串列表，不再提供 pytest_html.html 构造器
    cells.insert(2, "<th>场景</th>")


@pytest.hookimpl(optionalhook=True)
def pytest_html_results_table_row(report, cells):
    cells.insert(2, f"<td>{getattr(report, 'scene', '')}</td>")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    report = yield
    markers = [m.name for m in item.iter_markers()]
    env = item.config.getoption("--env")
    report.scene = f"{env} · {','.join(markers) if markers else 'default'}"
    return report
