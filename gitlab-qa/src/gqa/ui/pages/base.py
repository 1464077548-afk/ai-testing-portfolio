"""页面对象基类：把「多候选选择器」的能力集中在这里。

GitLab 版本之间 DOM 变化频繁，写死一个选择器意味着每次升级都要改一批用例。
这里提供「按优先级尝试多个选择器」的能力，让用例表达意图而不是 CSS。
"""

from __future__ import annotations

from playwright.sync_api import Page, expect


class BasePage:
    PATH = "/"

    def __init__(self, page: Page, base_url: str = "") -> None:
        self.page = page
        self.base_url = base_url.rstrip("/")

    # ---------------------------------------------------------------- 导航
    def goto(self, path: str | None = None) -> None:
        target = path or self.PATH
        # 显式用 domcontentloaded：Playwright 默认等 load 事件，而 GitLab 页面
        # 带轮询和长连接，load 可能迟迟不触发，表现为「页面明明打开了却超时」。
        # 后续操作本来就靠元素等待，不需要 load 这个更强的信号。
        if target.startswith("http"):
            self.page.goto(target, wait_until="domcontentloaded", timeout=45000)
        else:
            self.page.goto(f"{self.base_url}{target}",
                           wait_until="domcontentloaded", timeout=45000)

    # ---------------------------------------------------------------- 多候选
    def _first(self, selectors: list[str], timeout: int = 10000):
        last_err: Exception | None = None
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                loc.wait_for(state="visible", timeout=timeout)
                return loc
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        raise AssertionError(
            f"以下选择器均未匹配到可见元素：{selectors}"
            f"{'  最后错误：' + str(last_err)[:200] if last_err else ''}"
        )

    def exists_any(self, selectors: list[str], timeout: int = 5000) -> bool:
        for sel in selectors:
            try:
                self.page.locator(sel).first.wait_for(state="visible", timeout=timeout)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def fill_first(self, selectors: list[str], value: str, timeout: int = 10000) -> None:
        self._first(selectors, timeout).fill(value)

    def click_first(self, selectors: list[str], timeout: int = 10000) -> None:
        self._first(selectors, timeout).click()

    def text_of_first(self, selectors: list[str], timeout: int = 5000) -> str | None:
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                loc.wait_for(state="visible", timeout=timeout)
                return (loc.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
        return None

    def count_any(self, selectors: list[str], timeout: int = 5000) -> int:
        for sel in selectors:
            try:
                loc = self.page.locator(sel)
                loc.first.wait_for(state="visible", timeout=timeout)
                return loc.count()
            except Exception:  # noqa: BLE001
                continue
        return 0

    # ---------------------------------------------------------------- 断言辅助
    def expect_url_contains(self, fragment: str) -> None:
        expect(self.page).to_have_url(lambda url: fragment in url)

    @property
    def title(self) -> str:
        return self.page.title()
