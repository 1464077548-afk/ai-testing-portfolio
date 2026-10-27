"""页面对象基类：把「多候选选择器」的能力集中在这里。

GitLab 版本之间 DOM 变化频繁，写死一个选择器意味着每次升级都要改一批用例。
这里提供「按优先级尝试多个选择器」的能力，让用例表达意图而不是 CSS。
"""

from __future__ import annotations

import time

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
        # 关掉可能盖在页面上的弹窗，否则后续点击会被它拦截
        self.dismiss_overlays()

    # ---------------------------------------------------------------- 弹窗
    # GitLab 会在页面上叠一层升级提示 / 安全补丁弹窗（role=dialog），
    # 它盖在内容之上拦截所有点击。表现是"元素明明在，就是点不动"，
    # 报错信息里写的是"subtree intercepts pointer events"——
    # 第一次遇到时会以为是选择器写错了，实际是被弹窗挡住。
    # 所以每次导航后先尝试把这些已知的叠加层关掉。
    OVERLAY_CLOSE = [
        '[data-testid="alert-modal-remind-button"]',
        '[data-testid="close-single-origin-fallback-callout"]',
        'button[aria-label="Close"]',
    ]

    def dismiss_overlays(self, timeout: int = 1200) -> int:
        """关掉已知的模态/提示层，返回关掉的个数。"""
        closed = 0
        for sel in self.OVERLAY_CLOSE:
            try:
                loc = self.page.locator(sel).first
                loc.wait_for(state="visible", timeout=timeout)
                loc.click(timeout=timeout)
                closed += 1
                self.page.wait_for_timeout(300)
            except Exception:  # noqa: BLE001  没出现就跳过，不影响主流程
                continue
        return closed

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
    def wait_for_text(self, text: str, timeout: float = 15.0,
                      reload_once: bool = True) -> bool:
        """等页面正文里出现指定文本。

        UI 断言最常见的假红来源就是"查得太早"：提交后立刻取 `page.content()`，
        页面可能还在跳转、还在取数、还没渲染。单跑时机器快，恰好赶上了；
        全量并发跑时实例慢半拍，就红了——于是得到一条
        "单独跑绿、一起跑红"的幽灵用例。

        这里轮询正文；超时后再重载一次继续等（SPA 冷启动常有这种表现），
        仍等不到才返回 False。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if text in (self.page.content() or ""):
                    return True
            except Exception:  # noqa: BLE001  页面正在导航，content() 会抛
                pass
            self.page.wait_for_timeout(700)

        if not reload_once:
            return False
        try:
            self.page.reload(wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            return False
        deadline = time.time() + timeout / 2
        while time.time() < deadline:
            try:
                if text in (self.page.content() or ""):
                    return True
            except Exception:  # noqa: BLE001
                pass
            self.page.wait_for_timeout(700)
        return False

    def expect_url_contains(self, fragment: str) -> None:
        expect(self.page).to_have_url(lambda url: fragment in url)

    @property
    def title(self) -> str:
        return self.page.title()
