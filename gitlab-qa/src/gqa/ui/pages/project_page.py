"""页面对象：项目列表与项目详情。

选择器是 2026-10 在 GitLab 19 上实测抓取的，不是照抄文档。
老版本的选择器作为回退项保留——同一套框架要能打多个版本的实例。

两个实测结论值得写在这儿：

1. **列表搜索框在自动化里不可靠**。GitLab 19 的 /dashboard/projects
   用的是 `filtered-search-term-input`，它在无头浏览器里填入并回车
   **不会触发过滤**（实测列表条数不变）。用 URL 参数 `?name=` 过滤则稳定生效。
   所以 search() 走 URL 参数——UI 自动化里"绕过不稳定的控件"是正常取舍，
   用例要验证的是"列表能不能按名字过滤"，不是"搜索框好不好使"。

2. **项目详情页的标题可能在任意层级**。新版页面把项目名拆到了面包屑和
   h1 里，且内容区异步渲染，所以标题断言要能容忍"还没渲染完"。
"""

from __future__ import annotations

import time
from urllib.parse import quote

from gqa.ui.pages.base import BasePage


class ProjectsPage(BasePage):
    PATH = "/dashboard/projects"

    # GitLab 19：列表项是 projects-list-item-<id>；旧版是 .projects-list
    #
    # 必须排除 projects-list-item-**actions**：它同样以 projects-list-item- 开头，
    # 却是每行右侧的操作菜单容器。实测空列表时它依然存在，
    # 于是「数一下有没有列表项」恒为真——一个看不见的假阳性，
    # 会让「删掉之后应该消失」这类断言永远绿。
    LIST_ITEM = [
        '[data-testid^="projects-list-item-"]'
        ':not([data-testid="projects-list-item-actions"])',
        '[data-testid="projects-list"] li',
        ".projects-list li",
    ]
    LIST_CONTAINER = [
        '[data-testid^="projects-list-item-"]',
        '[data-testid="projects-list"]',
        ".projects-list",
    ]
    PROJECT_LINK = [
        '[data-testid="project-content"] a',
        '[data-testid="project-name"] a',
        ".project-name a",
    ]
    NEW_PROJECT_BTN = [
        '[data-testid="new-project-button"]',
        'a[href="/projects/new"]',
        'a:has-text("New project")',
    ]
    SEARCH_BOX = [
        'input[data-testid="filtered-search-term-input"]',
        '[data-testid="project-filter-form-field"]',
        'input[name="name"]',
    ]
    # 空态有两种，长得完全不一样：
    #   a) 一个项目都没有   → data-testid="empty-state"
    #   b) 过滤后没有命中   → 只有一段纯文案，**没有任何 testid**
    #      "No results found / To widen your search, change or remove filters above."
    # 只认 a) 的话，反向过滤断言永远等不到"挂载完成"，只能靠耗满超时判否。
    # 实测（GitLab 19.0.1）：那段文案就是唯一的信号，所以只能按文本认。
    EMPTY_STATE = [
        '[data-testid="empty-state"]',
        '[data-testid="gl-empty-state"]',
        ".empty-state",
        'text="No results found"',
        'text="No projects found"',
    ]

    def open(self) -> "ProjectsPage":
        self.goto()
        return self

    def wait_loaded(self, timeout: float = 12.0) -> bool:
        """等列表**挂载完成**，返回是否成功。

        判定"挂载完成"用两个信号中的任意一个：出现列表项，或出现空态。
        只看列表项是不够的——过滤结果为空时它永远不出现，
        于是"真的没有"和"还没渲染"两种情况无法区分，
        只能靠耗满超时，慢且不可靠。

        实测还有个坑：会话里**第一次**进入这个页面时，Vue 应用常常
        连列表组件都不挂载（页面只剩导航 chrome，既无列表项也无空态），
        第二次进入就正常。所以调用方应当按返回值决定是否重载重试，
        而不是无条件 sleep 一段时间了事。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.page.locator(self.LIST_ITEM[0]).count() > 0:
                    return True
                for sel in self.EMPTY_STATE:
                    if self.page.locator(sel).count() > 0:
                        return True
            except Exception:  # noqa: BLE001  页面正在跳转，下一轮再试
                pass
            self.page.wait_for_timeout(700)
        return False

    def project_count(self) -> int:
        self.wait_loaded()
        return self.count_any(self.LIST_ITEM, timeout=8000)

    def search(self, name: str) -> None:
        """按名字过滤。走 URL 参数而不是搜索框（原因见文件头）。

        默认进 **Personal** 标签页：实测（GitLab 19.0.1）通用列表默认停在
        Contributed 标签，它靠"贡献事件"驱动，而事件由 Sidekiq 异步写入——
        刚建的项目在 Contributed 里要滞后很久才出现，甚至等 15s+ 仍为空态。
        Personal 标签直接查 membership，建完立刻能搜到。
        """
        self.goto(self.filter_url(name))
        self.wait_loaded()

    # ---------------------------------------------------------------- 过滤 URL
    @staticmethod
    def filter_url(name: str, personal: bool = True) -> str:
        """按名字过滤的 URL。personal=True 走 Personal 标签（见 search 的注释）。"""
        q = quote(name)
        base = "/dashboard/projects/personal" if personal else "/dashboard/projects"
        return f"{base}?name={q}"

    def _filter_urls(self, name: str) -> list[str]:
        """正向查找的标签顺序：Personal 优先（确定性），通用列表兜底。

        兜底的必要性：建在**组**命名空间下的项目不属于"Personal"，
        只会出现在通用列表的 Contributed/Member 里——那种项目只能等
        事件落地，用重试吸收滞后。
        """
        return [self.filter_url(name, personal=True),
                self.filter_url(name, personal=False)]

    def expect_no_project(self, name: str, attempts: int = 2,
                          per_attempt: float = 10.0) -> bool:
        """反向断言：按 name 过滤后**两个标签页里一张项目卡片都不剩**。

        只测"能搜到"是假绿——过滤压根没生效时，返回的仍是全部列表，
        "能搜到"照样成立。所以正向必须配反向。

        判据是"项目卡片数"，不是"页面上有没有带目标名字的链接"。
        后者被实测坑过：过滤时 URL 带 ?name=xxx，左侧面包屑「Projects」
        的 href 也就跟着带上 xxx，全局匹配 `a[href*=xxx]` 必然命中一条——
        反向断言永远红，而且红得理直气壮（"过滤没生效"），
        实际是选择器在自欺。

        也不能靠"等满超时还没出现"判否：过滤为空时列表本来就没有卡片，
        "还没渲染"和"真的没有"表现完全一致。所以先等挂载完成
        （有序条目或空态文案，见 wait_loaded），挂载后仍是 0 张卡片，
        才算真的筛空了。两个标签页都空才算过——只查一个，
        另一个标签页里过滤失效时发现不了。
        """
        urls = self._filter_urls(name)
        for attempt in range(1, attempts + 1):
            all_empty = True
            for url in urls:
                self.goto(url)
                if not self.wait_loaded(timeout=per_attempt):
                    all_empty = False
                    break
                try:
                    if self.count_any(self.LIST_ITEM, timeout=1500) > 0:
                        all_empty = False
                        break
                except Exception:  # noqa: BLE001  页面正在跳转，重来一轮
                    all_empty = False
                    break
            if all_empty:
                return True
        return False

    def has_project(self, name: str, attempts: int = 3, per_attempt: float = 10.0,
                    personal_only: bool = False) -> bool:
        """过滤后目标是否出现。personal_only=True 只查 Personal 标签。

        personal_only 的用途是**消失类断言**：项目被删后 membership 立刻失效，
        Personal 列表马上就空；而 Contributed 的"事件"反向滞后同样存在——
        项目删了事件还在，通用列表里会多挂一会儿。消失断言查通用列表
        只会平白等满超时，还可能等到一个假阳性。

        三层防御，每一层都是实测踩出来的：

        1. 等的是**指向该项目的链接**，不是"列表里有没有东西"——
           列表异步渲染，数列表项会在数据到达前得出"不存在"。
        2. 每次先等列表**挂载完成**（见 wait_loaded），否则"没渲染"
           和"真的没有"无法区分。
        3. **Personal 标签优先**。通用列表默认停在 Contributed 标签，
           它靠 Sidekiq 异步写的"贡献事件"驱动——全量并发跑时事件滞后，
           项目明明存在（Personal 29 / Member 29）却"搜不到"，
           失败截图里是干干净净的 "No results found"。这种失败最迷惑人：
           看着像过滤坏了，实际是查错了标签页。Personal 直接查 membership，
           建完立刻有；通用列表作为兜底，覆盖建在组里的项目。

        另外：链接只在**列表卡片内**找——全局找 href 会被同名面包屑
        导航命中，反向用例已经因此翻过车（见 expect_no_project 的注释）。
        """
        # 只在**列表卡片内**找链接：全局找 href 会被同名面包屑导航命中，
        # 反向用例已经因此翻过车（见 expect_no_project 的注释）。
        link_sel = f'{self.LIST_ITEM[0]} a[href*="/{name}"]'
        urls = [self.filter_url(name, personal=True)] if personal_only \
            else self._filter_urls(name)
        for url in urls:
            for attempt in range(1, attempts + 1):
                self.goto(url)

                self.wait_loaded(timeout=per_attempt)
                deadline = time.time() + per_attempt
                while time.time() < deadline:
                    try:
                        if self.page.locator(link_sel).count() > 0:
                            return True
                    except Exception:  # noqa: BLE001  页面正在跳转，下一轮再试
                        pass
                    self.page.wait_for_timeout(700)
        return False

    def open_project(self, name: str) -> "ProjectPage":
        self.search(name)
        # 过滤后列表里只剩目标，点它的链接；找不到再退回直接按路径跳转
        try:
            self._first([f'a[href$="/{name}"]', f'a[href*="/{name}"]'], timeout=8000).click()
            self.page.wait_for_load_state("domcontentloaded")
        except AssertionError:
            self.goto(f"/{name}")
        return ProjectPage(self.page, self.base_url)


class ProjectPage(BasePage):
    """项目详情页（新版 URL: /<namespace>/<project>）。"""

    TITLE = [
        '[data-testid="project-name"]',
        ".breadcrumbs-list li:last-child",
        "h1",
    ]
    # GitLab 19 把议题迁到了 work items；老实例的 /-/issues 会重定向过去
    ISSUES_TAB = [
        '[data-testid="issues-tab"]',
        'a[href$="/-/work_items"]',
        'a[href$="/-/issues"]',
        'nav a:has-text("Issues")',
    ]
    EMPTY_README = [
        '[data-testid="empty-state"]',
        ".empty-state",
    ]

    def title_text(self) -> str | None:
        return self.text_of_first(self.TITLE, timeout=8000)

    def goto_issues(self) -> "IssuesPage":
        """进入议题列表。

        优先直接跳 work_items：老路径 /-/issues 在 19 上会重定向到
        /-/work_items?sort=...&state=opened，重定向后的 DOM 与列表页一致，
        但多一次跳转就多一次超时风险。
        """
        try:
            self.click_first(self.ISSUES_TAB, timeout=8000)
            self.page.wait_for_load_state("domcontentloaded")
        except AssertionError:
            self.goto(f"{self.page.url.split('/-/')[0]}/-/work_items"
                      if "/-/" in self.page.url else "/")
        return IssuesPage(self.page, self.base_url)

    def goto_issues_of(self, project_path: str) -> "IssuesPage":
        """按项目路径直接进入议题列表（跳过点击，最稳）。"""
        self.goto(f"/{project_path}/-/work_items")
        return IssuesPage(self.page, self.base_url)


from gqa.ui.pages.issue_page import IssuesPage  # noqa: E402  循环引用：放在末尾
