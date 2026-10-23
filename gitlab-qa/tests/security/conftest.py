"""安全用例的公共守卫与数据。

所有 security 用例都依赖 `target` fixture —— 它是**信任边界**：

    目标不在白名单 → skip，而不是照跑。

为什么不靠人记得加参数：安全用例挂在定时流水线里，
哪天有人把 GITLAB_BASE_URL 指到公网实例，没有守卫的话，
一套"自动化安全测试"就变成了"自动化扫描他人系统"。
"""

from __future__ import annotations

import pytest

from gqa.security import guard


@pytest.fixture(scope="session")
def target(settings):
    """过了守卫的 settings。

    用 `target` 而不是直接用 `settings`，是为了让"这个用例是安全用例、
    需要过白名单"这件事在用例签名里就能看出来——读代码的人一眼能看到。
    """
    if not guard.is_allowed(settings.base_url):
        pytest.skip(guard.describe(settings.base_url))
    return settings


@pytest.fixture
def private_project(target, data_factory):
    """管理员建的私有项目，给越权用例当"别人的资源"。

    用完由 data_factory 自动回收，不在用例里手写清理。
    """
    return data_factory.project(visibility="private")
