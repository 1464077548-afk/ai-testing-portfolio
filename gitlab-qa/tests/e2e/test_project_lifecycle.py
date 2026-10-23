"""端到端：API 与 UI 协同完成一次完整业务流。

e2e 的定位是「证明两套客户端说的是同一件事」：
API 建的数据 UI 能看见，UI 的操作 API 能查到。
它不重复验证单点功能，只验证串起来之后对不对。
"""

from __future__ import annotations

import allure
import pytest

from gqa.ui.pages.login_page import LoginPage
from gqa.ui.pages.project_page import ProjectsPage

pytestmark = [pytest.mark.e2e, pytest.mark.destructive, pytest.mark.requires_token]


@allure.epic("端到端")
@allure.feature("项目生命周期")
@allure.story("API 创建的项目在 UI 上可见")
@allure.severity(allure.severity_level.CRITICAL)
def test_project_created_via_api_is_visible_in_ui(page, settings, data_factory, request):
    if not settings.admin_password:
        pytest.skip("未配置 GITLAB_ROOT_PASSWORD，无法完成 UI 部分")

    with allure.step("用 API 创建项目"):
        project = data_factory.project()
        path = project["path"]

    with allure.step("UI 登录"):
        login = LoginPage(page, settings.base_url)
        login.open()
        login.login(settings.admin_user, settings.admin_password)
        assert login.is_logged_in, "UI 登录失败，后续断言无意义"

    with allure.step("在项目列表里搜索该项目"):
        projects = ProjectsPage(page, settings.base_url)
        projects.open()
        visible = projects.has_project(path)

    allure.attach(
        f"项目 path={path}\nUI 可见={visible}",
        name="端到端结果",
        attachment_type=allure.attachment_type.TEXT,
    )
    assert visible, f"API 创建的项目 {path} 在 UI 上搜不到"
