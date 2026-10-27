"""页面对象：议题（work items）列表与新建页。

GitLab 19 把议题换成了 "work items"，页面路径与 DOM 全变了：

    路径： /-/issues      →  /-/work_items
    列表： .issue-list-item →  [data-testid="work-item-router-view"]
    新建： #issue_title     →  [data-testid="work-item-title-input"]
    提交： input[type=submit] → [data-testid="create-button"]（文案 "Create Issue"）

旧选择器作为回退项保留在候选列表后面，这样同一套用例打老实例也能跑。

一个踩过的坑：提交按钮文案是 **"Create Issue"**（Issue 首字母大写），
`button:has-text("Create issue")` 匹配不到——Playwright 的文本匹配区分大小写。
所以候选里把 data-testid 放在第一位，不要依赖文案。
"""

from __future__ import annotations

from gqa.ui.pages.base import BasePage


class IssuesPage(BasePage):
    """议题（work items）列表页。"""

    CONTAINER = [
        '[data-testid="work-item-router-view"]',
        '[data-testid="issuable-list"]',
        ".issue-list",
    ]
    LIST_ITEM = [
        '[data-testid^="work-item-"]',
        '[data-testid="issuable-list"] li',
        ".issue-list-item",
    ]
    EMPTY_STATE = [
        '[data-testid="issuable-empty-state"]',
        '[data-testid="signed-in-empty-state-block"]',
        '[data-testid="empty-state"]',
        ".empty-state",
    ]
    NEW_ISSUE_BTN = [
        '[data-testid="new-work-item-trigger"]',
        '[data-testid="new-issue-button"]',
        'a:has-text("New issue")',
    ]

    def wait_loaded(self, timeout: int = 12000, poll: int = 1500) -> None:
        """轮询等列表容器出现（新版列表同样是异步渲染）。"""
        waited = 0
        while waited < timeout:
            try:
                self.page.locator(self.CONTAINER[0]).first.wait_for(
                    state="visible", timeout=poll)
                return
            except Exception:  # noqa: BLE001
                waited += poll
                if waited < timeout:
                    self.page.wait_for_timeout(200)

    def issue_count(self) -> int:
        self.wait_loaded()
        return self.count_any(self.LIST_ITEM, timeout=8000)

    def has_empty_state(self) -> bool:
        return self.exists_any(self.EMPTY_STATE, timeout=5000)


class NewIssuePage(BasePage):
    """新建议题页。"""

    TITLE = [
        '[data-testid="work-item-title-input"]',
        '[data-testid="issuable-title-field"]',
        'input[name="issue[title]"]',
        "#issue_title",
    ]
    # 新版描述是富文本编辑器（contenteditable），fill 不一定成功，
    # 所以 create() 里对描述做了"填不上就算了"的处理
    DESCRIPTION = [
        '[data-testid="work-item-description"]',
        '[data-testid="issuable-description-field"]',
        'textarea[name="issue[description]"]',
        "#issue_description",
    ]
    SUBMIT = [
        '[data-testid="create-button"]',
        'button:has-text("Create Issue")',
        'button:has-text("Create issue")',
        'button[type="submit"]',
    ]

    def create(self, title: str, description: str = "") -> None:
        self.fill_first(self.TITLE, title, timeout=10000)
        if description:
            try:
                self.fill_first(self.DESCRIPTION, description, timeout=5000)
            except Exception:  # noqa: BLE001  富文本编辑器填不进去时跳过
                pass
        self.click_first(self.SUBMIT, timeout=10000)
        self.page.wait_for_load_state("domcontentloaded")
        self.page.wait_for_timeout(1500)
