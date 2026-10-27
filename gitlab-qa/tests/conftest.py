"""业务层 fixture。

根 conftest 负责环境与基础设施，这里只放「测试用例需要的东西」。
判断标准同样是一句：换个被测系统，这行还成立吗。

UI 相关的两个夹具（web_target / signed_in）也放在这里而不是 tests/ui/：
e2e 同样需要登录态，放一份两处共用，比复制一份再各自腐化强。
"""

from __future__ import annotations

import pytest

from gqa.models import Project


@pytest.fixture
def a_project(data_factory) -> Project:
    """一个已创建、用完即删的项目。"""
    data = data_factory.project()
    return Project.model_validate(data)


@pytest.fixture
def an_open_issue(data_factory, a_project: Project) -> dict:
    return data_factory.issue(a_project.id, description="由自动化创建")


# ==========================================================================
# UI / e2e 共用：Web 界面守卫与登录态
# ==========================================================================
@pytest.fixture(scope="session")
def web_target(settings):
    """过了「确实有 Web 界面」这道检查的 settings。

    为什么必须先查：API 靶机（tools/mock_gitlab.py）只实现了 /api/v4 路由，
    拿它跑 UI 用例会红一大片"元素找不到"——那是环境不对，不是被测系统有缺陷。
    先探测再决定跑不跑，报告里才是"没跑"，而不是一串误导人的红。
    """
    try:
        import requests
    except ImportError:  # pragma: no cover
        pytest.skip("未安装 requests")

    url = f"{settings.base_url}/users/sign_in"
    try:
        resp = requests.get(url, timeout=min(settings.timeout, 10.0),
                            allow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Web 界面不可达（{url}）：{type(exc).__name__}")

    ctype = (resp.headers.get("content-type") or "").lower()
    if resp.status_code != 200 or "html" not in ctype:
        pytest.skip(
            f"目标没有可测的 Web 界面（{url} → HTTP {resp.status_code}，"
            f"content-type={ctype or '未知'}）。"
            "UI/e2e 用例需要真实实例，API 靶机只实现了 /api/v4 路由。"
        )
    return settings


@pytest.fixture
def signed_in(web_target, page, settings):
    """一个已经登录的 page。

    登录失败用 fail 而不是 skip：需要登录态的用例登录不上，
    说明环境配错了；而 skip 会把这件事藏进"未执行"里，
    看上去像是这条用例今天没轮到跑。
    """
    from gqa.ui.pages.login_page import LoginPage

    if not settings.admin_password:
        pytest.skip("未配置 GITLAB_ROOT_PASSWORD，无法构造登录态")

    login = LoginPage(page, settings.base_url)
    login.open()
    login.login(settings.admin_user, settings.admin_password)

    if not login.is_logged_in:
        pytest.fail(
            f"UI 登录失败，后续断言无意义。当前 URL：{page.url}\n"
            f"页面提示：{login.error_text[:200]}\n"
            "排查：.env 里的 GITLAB_ROOT_PASSWORD 是否与实例当前密码一致"
            "（可用 tools/_run_all.ps1 对齐）"
        )
    return page


@pytest.fixture
def perf_thresholds(settings):
    """性能门禁阈值集中在一处，CI 里用命令行覆盖。"""
    return {
        "p95_ms": settings.perf_p95_ms,
        "error_rate": settings.perf_error_rate,
        "concurrency": settings.perf_concurrency,
        "samples": settings.perf_samples,
        # 吞吐与抖动是「除了延迟之外」的两个独立维度：
        # 只看延迟，一个"每秒只能处理 2 个请求"的接口也能全绿
        "min_rps": settings.perf_min_rps,
        "max_jitter": settings.perf_max_jitter,
        # 环境体检预算：0 表示按 P95 阈值的一半算
        "env_p50_ms": settings.perf_env_p50_ms,
    }
