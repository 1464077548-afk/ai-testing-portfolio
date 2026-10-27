"""UI 冒烟：登录流程。

UI 用例的纪律：
1. 只验证「界面能不能完成这件事」，不重复验证 API 已经验证过的业务规则；
2. 断言用「登录后跳转到了哪里」而不是「有没有报错条」——后者会有假阴性；
3. 每条用例独立 context，失败自动留截图与 trace。
"""

from __future__ import annotations

import allure
import pytest

from gqa.ui.pages.login_page import LoginPage

pytestmark = [pytest.mark.ui, pytest.mark.smoke]


@allure.epic("GitLab UI")
@allure.feature("登录")
@allure.story("登录页可打开")
@allure.severity(allure.severity_level.BLOCKER)
def test_login_page_renders(page, settings, request):
    ui = request.getfixturevalue("ui_session")  #动态获取另一个 fixture 
    login = LoginPage(page, settings.base_url)

    with allure.step("打开登录页"):
        login.open()

    with allure.step("断言关键控件存在"):
        assert login.exists_any(LoginPage.USERNAME, timeout=10000), "未找到用户名输入框"
        assert login.exists_any(LoginPage.PASSWORD, timeout=5000), "未找到密码输入框"
        assert login.exists_any(LoginPage.SUBMIT, timeout=5000), "未找到提交按钮"

    allure.attach(page.title(), name="页面标题",
                  attachment_type=allure.attachment_type.TEXT)


@allure.epic("GitLab UI")
@allure.feature("登录")
@allure.story("错误密码登录失败")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.destructive
def test_login_with_wrong_password_fails(page, settings):
    """用错误密码登录必须失败，且不能进入已登录状态。

    这条用例的价值是验证「失败路径被正确拦截」——
    只看成功路径的 UI 测试等于没测。
    """
    login = LoginPage(page, settings.base_url)

    with allure.step("用错误密码登录"):
        login.open()
        login.login(settings.admin_user, "definitely-wrong-password-12345")

    with allure.step("断言仍处于未登录状态"):
        assert not login.is_logged_in, "错误密码竟然登录成功了"


@allure.epic("GitLab UI")
@allure.feature("登录")
@allure.story("正确凭据可登录")
@allure.severity(allure.severity_level.BLOCKER)
@pytest.mark.requires_token
def test_login_success(page, settings):
    """正确凭据登录成功。需要管理员密码，未配置时跳过。"""
    if not settings.admin_password:
        pytest.skip("未配置 GITLAB_ROOT_PASSWORD，无法验证登录成功路径")

    login = LoginPage(page, settings.base_url)
    with allure.step("使用正确凭据登录"):
        login.open()
        login.login(settings.admin_user, settings.admin_password)

    with allure.step("断言已登录"):
        assert login.is_logged_in, (
            f"登录失败。当前 URL：{page.url}，页面提示：{login.error_text[:120]}"
        )
