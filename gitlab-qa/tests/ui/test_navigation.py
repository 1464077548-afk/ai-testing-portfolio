"""导航与会话：用户"进不去"和"出得来"这两件事。

导航类用例最容易被写成"点一下菜单看跳没跳"，
那样测的是前端路由，价值很低。这里只留三类真正会出事的：

1. **未登录访问内页必须被拦** —— 这条红了就是事故级：
   意味着有页面绕过了登录态校验。
2. **会话失效后要被踢回登录页** —— 不是测"登出按钮长什么样"
   （那个 DOM 每次改版都要重写），而是测"没凭据就不能待在里面"。
3. **登录页不能有未捕获的 JS 异常** —— 前端报错往往只出现在控制台，
   页面上看起来完全正常，等用户点某个按钮才发现整片功能不可用。
"""

from __future__ import annotations

import allure
import pytest

from gqa.ui.pages.login_page import LoginPage

pytestmark = [pytest.mark.ui]


@allure.epic("GitLab UI")
@allure.feature("访问控制")
@allure.story("未登录访问内页被拦回登录页")
@allure.severity(allure.severity_level.BLOCKER)
def test_dashboard_requires_login(web_target, page, settings):
    """未登录直接访问仪表盘，必须跳到登录页。

    用 URL 断言而不是"页面上有登录框"：
    有些实现会把内页渲染出来再弹一个模态框，
    数据其实已经发出去了——URL 没变就说明根本没有重定向。
    """
    page.goto(f"{settings.base_url}/dashboard/projects", wait_until="domcontentloaded")
    page.wait_for_timeout(1000)

    allure.attach(f"访问 /dashboard/projects 后落在：{page.url}", name="跳转结果",
                  attachment_type=allure.attachment_type.TEXT)

    assert "sign_in" in page.url, (
        f"未登录访问仪表盘没有被拦回登录页，当前 URL：{page.url}"
    )


@allure.epic("GitLab UI")
@allure.feature("访问控制")
@allure.story("会话失效后不能停留在内页")
@allure.severity(allure.severity_level.CRITICAL)
def test_cleared_session_is_kicked_out(signed_in, settings):
    """清掉 Cookie 再访问内页，必须回到登录页。

    刻意用"清 Cookie"而不是去点登出按钮：
    登出按钮的选择器每个大版本都在变（从链接变成表单再变成菜单项），
    为它维护选择器成本高、收益低；
    而"没有会话凭据就应该被拦住"这条语义是稳定的，
    用哪种方式失去凭据并不重要。
    """
    signed_in.context.clear_cookies()
    signed_in.goto(f"{settings.base_url}/dashboard/projects",
                   wait_until="domcontentloaded")
    signed_in.wait_for_timeout(1000)

    allure.attach(f"清除 Cookie 后落在：{signed_in.url}", name="跳转结果",
                  attachment_type=allure.attachment_type.TEXT)
    assert "sign_in" in signed_in.url, (
        f"清除会话后仍能停留在内页：{signed_in.url}"
    )


@allure.epic("GitLab UI")
@allure.feature("前端健康")
@allure.story("登录页无未捕获 JS 异常")
@allure.severity(allure.severity_level.NORMAL)
def test_login_page_has_no_uncaught_errors(web_target, page, settings):
    """登录页加载期间不能有未捕获的 JS 异常。

    只判 `pageerror`（未捕获异常），不判 console.error：
    后者里混着大量 404、favicon、第三方资源失败，
    强断言会变成天天红的噪声，最后没人看。
    而未捕获异常几乎总是真的坏了——它意味着某段脚本没跑完。
    """
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"[pageerror] {exc}"))
    console: list[str] = []
    page.on("console", lambda msg: console.append(f"[{msg.type}] {msg.text}")
            if msg.type == "error" else None)

    LoginPage(page, settings.base_url).open()
    page.wait_for_timeout(1500)

    allure.attach(
        "\n".join(console[:30]) or "（无 console.error）",
        name="console.error（仅记录）",
        attachment_type=allure.attachment_type.TEXT,
    )
    allure.attach("\n".join(errors) or "（无未捕获异常）", name="未捕获异常",
                  attachment_type=allure.attachment_type.TEXT)

    assert errors == [], f"登录页有未捕获的 JS 异常：\n" + "\n".join(errors)
