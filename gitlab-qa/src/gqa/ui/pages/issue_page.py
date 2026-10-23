"""页面对象：议题列表与新建议题。"""

from __future__ import annotations

from gqa.ui.pages.base import BasePage


class IssuesPage(BasePage):
    LIST_ITEM = [
        '[data-testid="issuable-list"] li',
        ".issue-list-item",
        '[data-qa-selector="issuable_list"] li',
    ]
    NEW_ISSUE_BTN = [
        '[data-testid="new-issue-button"]',
        'a:has-text("New issue")',
        'a[href$="/issues/new"]',
    ]
    EMPTY_STATE = ['.empty-state', '[data-testid="empty-state"]']


class NewIssuePage(BasePage):
    TITLE = [
        '[data-testid="issuable-title-field"]',
        'input[name="issue[title]"]',
        "#issue_title",
    ]
    DESCRIPTION = [
        '[data-testid="issuable-description-field"]',
        'textarea[name="issue[description]"]',
        "#issue_description",
    ]
    SUBMIT = [
        '[data-testid="issuable-create-button"]',
        'button[type="submit"]:has-text("Create issue")',
        'input[type="submit"]',
    ]

    def create(self, title: str, description: str = "") -> None:
        self.fill_first(self.TITLE, title, timeout=10000)
        if description:
            try:
                self.fill_first(self.DESCRIPTION, description, timeout=5000)
            except AssertionError:
                pass  # 描述非必填，没有就不填
        self.click_first(self.SUBMIT, timeout=10000)
        self.page.wait_for_load_state("networkidle")
