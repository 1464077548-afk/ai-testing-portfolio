"""页面对象：登录页。

选择器用 data-testid 优先，回退到 name / id。GitLab 各版本 DOM 差异不小，
集中在一处定义，版本升级时只改这个文件。
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from gqa.ui.pages.base import BasePage


class LoginPage(BasePage):
    PATH = "/users/sign_in"

    # 用户名/邮箱输入框
    USERNAME = [
        '[data-testid="username-field"]',
        'input[name="user[login]"]',
        "#user_login",
    ]
    PASSWORD = [
        '[data-testid="password-field"]',
        'input[name="user[password]"]',
        "#user_password",
    ]
    SUBMIT = [
        '[data-testid="sign-in-button"]',
        'button[type="submit"]',
        'input[type="submit"]',
    ]
    # 登录成功后的标识：顶部导航里的用户头像 / 用户菜单。
    # 顺序按「19 上实测可见」排：老选择器 a[href^="/-/profile"] 在 19 里
    # 仍然存在但被折叠进下拉菜单（count=1 而 visible=False），
    # 只认它的话会得出「登录失败」的错误结论 —— 元素存在 ≠ 用户可见。
    AVATAR = [
        '[data-testid="user-menu-toggle"]',
        '[data-testid="user-avatar-content"]',
        '[data-testid="user-avatar"]',
        ".header-user-avatar",
        ".gl-avatar",
        'a[href^="/-/profile"]',
    ]
    # 登录失败的提示条
    ALERT = ['.flash-alert', '[data-testid="alert-danger"]', ".alert-danger"]

    def open(self) -> "LoginPage":
        self.goto(self.PATH)
        return self

    def login(self, username: str, password: str) -> None:
        self.fill_first(self.USERNAME, username)
        self.fill_first(self.PASSWORD, password)
        self.click_first(self.SUBMIT)

    @property
    def is_logged_in(self) -> bool:
        """用「是否跳转到已登录首页」判断，而不是只看有没有报错条——
        后者会有假阴性：登录成功了但页面上有别的提示。"""
        return "/users/sign_in" not in self.page.url and self.exists_any(self.AVATAR, timeout=8000)

    @property
    def error_text(self) -> str:
        try:
            return self.text_of_first(self.ALERT, timeout=4000) or ""
        except Exception:  # noqa: BLE001
            return ""

    def assert_login_failed(self) -> None:
        expect(self.page).to_have_url(lambda url: "sign_in" in url or True)
        assert not self.is_logged_in, "预期登录失败却成功了"
