"""Playwright 会话管理。

三条硬性要求，缺一个这层就没意义：
1. 每条用例独立 context —— cookie/localStorage 不串味，用例之间不互相影响；
2. 全程开 trace —— 失败时能还原每一步，不是只有一张截图；
3. 失败必留证 —— 截图 + HTML 快照 + trace 打包，全部贴进 Allure。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright


@dataclass
class FailureArtifact:
    name: str
    path: str
    kind: str   # screenshot / html / trace


class UISession:
    """管理 playwright / browser / context 的生命周期。"""

    def __init__(
        self,
        base_url: str,
        headless: bool = True,
        slow_mo: int = 0,
        trace: bool = True,
        video: bool = False,
        artifacts_dir: str = "artifacts",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.slow_mo = slow_mo
        self.trace_enabled = trace
        self.video_enabled = video
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

        self._pw = None   # playwright 实例
        self.browser: Browser | None = None
        self.channel: str = ""   # 浏览器通道，如 "msedge" / "chrome" / "chromium"
        self._contexts: list[BrowserContext] = []  # 所有 context 实例
        self.artifacts: list[FailureArtifact] = []  # 所有失败时留证

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> "UISession":
        self._pw = sync_playwright().start()
        self.browser = self._launch_with_fallback()
        return self

    def _launch_with_fallback(self) -> Browser:
        """按顺序尝试多个浏览器通道。

        为什么需要这个：企业内网里 playwright 的浏览器 CDN 经常不通，
        `playwright install chromium` 直接失败，UI 测试就此瘫痪。
        而 Windows 自带 Edge、多数开发机装了 Chrome——它们是 Chromium 内核，
        Playwright 可以直接驱动，不需要下载任何东西。

        顺序有讲究：先用自带通道（不依赖网络），最后才回退到需要下载的 chromium。
        """
        attempts = [
            ("msedge", {"channel": "msedge"}),
            ("chrome", {"channel": "chrome"}),
            ("chromium", {}),
        ]
        errors: list[str] = []
        for label, opts in attempts:
            try:
                browser = self._pw.chromium.launch(
                    headless=self.headless,
                    slow_mo=self.slow_mo,
                    args=["--disable-dev-shm-usage"],
                    **opts,
                )
                self.channel = label
                return browser
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{label}: {str(exc).splitlines()[0][:120]}")
        raise RuntimeError(
            "没有可用的浏览器。已尝试：\n  " + "\n  ".join(errors)
            + "\n\n修复：安装 Edge/Chrome，或执行 playwright install chromium"
        )

    def new_page(self, name: str) -> Page:
        """每条用例一个新 context。名字用于给 trace 文件命名。"""
        stamp = time.strftime("%H%M%S")
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)[:60]
        ctx_opts: dict = {
            "base_url": self.base_url,
            "viewport": {"width": 1440, "height": 900},
            "locale": "zh-CN",
        }
        if self.video_enabled:
            ctx_opts["record_video_dir"] = str(self.artifacts_dir / "video")

        context = self.browser.new_context(**ctx_opts)
        self._contexts.append(context)

        if self.trace_enabled:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)

        page = context.new_page()
        page.set_default_timeout(15000)
        # 把 trace 文件名挂到 page 上，收尾时要用
        page._qa_name = f"{safe}-{stamp}"  # type: ignore[attr-defined]
        return page

    # ---------------------------------------------------------------- 失败留证
    def capture_failure(self, page: Page, label: str = "failure") -> list[FailureArtifact]:
        name = getattr(page, "_qa_name", "unknown")
        out: list[FailureArtifact] = []

        try:
            shot = self.artifacts_dir / f"{name}-{label}.png"
            page.screenshot(path=str(shot), full_page=True)
            out.append(FailureArtifact(f"{label}-截图", str(shot), "screenshot"))
        except Exception:  # noqa: BLE001
            pass

        try:
            html = self.artifacts_dir / f"{name}-{label}.html"
            html.write_text(page.content(), encoding="utf-8")
            out.append(FailureArtifact(f"{label}-DOM快照", str(html), "html"))
        except Exception:  # noqa: BLE001
            pass

        return out

    def stop_context(self, page: Page) -> list[FailureArtifact]:
        """用例结束：停 trace 并把文件登记出来。"""
        out: list[FailureArtifact] = []
        if self.trace_enabled:
            try:
                name = getattr(page, "_qa_name", "unknown")
                trace_path = self.artifacts_dir / f"{name}-trace.zip"
                page.context.tracing.stop(path=str(trace_path))
                out.append(FailureArtifact("Playwright Trace", str(trace_path), "trace"))
            except Exception:  # noqa: BLE001
                pass
        try:
            page.context.close()
        except Exception:  # noqa: BLE001
            pass
        return out

    def teardown(self) -> None:
        for ctx in self._contexts:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
        self._contexts.clear()
        if self.browser:
            try:
                self.browser.close()
            except Exception:  # noqa: BLE001
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
