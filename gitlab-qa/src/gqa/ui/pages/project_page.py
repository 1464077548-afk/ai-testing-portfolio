"""页面对象：项目列表与项目详情。"""

from __future__ import annotations

from gqa.ui.pages.base import BasePage


class ProjectsPage(BasePage):
    PATH = "/dashboard/projects"

    LIST_CONTAINER = [
        '[data-testid="projects-list"]',
        ".projects-list",
        '[data-qa-selector="projects_list"]',
    ]
    PROJECT_LINK = [
        '[data-testid="project-name"] a',
        ".project-name a",
        '.projects-list a[href*="/"]',
    ]
    NEW_PROJECT_BTN = [
        '[data-testid="new-project-button"]',
        'a[href="/projects/new"]',
        'a:has-text("New project")',
    ]
    SEARCH_BOX = [
        '[data-testid="project-filter-form-field"]',
        'input[name="name"]',
        "#project-filter-form-field",
    ]

    def open(self) -> "ProjectsPage":
        self.goto()
        return self

    def project_count(self) -> int:
        return self.count_any(self.PROJECT_LINK, timeout=8000)

    def search(self, name: str) -> None:
        try:
            self.fill_first(self.SEARCH_BOX, name, timeout=5000)
            self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(800)
        except AssertionError:
            # 列表页没有搜索框时走 URL 参数，功能等价
            self.goto(f"/dashboard/projects?name={name}")

    def has_project(self, name: str) -> bool:
        self.search(name)
        return self.exists_any([f'a[href*="{name}"]', f'text="{name}"'], timeout=6000)

    def open_project(self, name: str) -> "ProjectPage":
        self.search(name)
        self._first([f'a[href*="{name}"]'], timeout=8000).click()
        return ProjectPage(self.page, self.base_url)


class ProjectPage(BasePage):
    """项目详情页。构造时传入 path_with_namespace 形式的路径。"""

    TITLE = [
        '[data-testid="project-name"]',
        ".breadcrumbs-list li:last-child",
        "h1",
    ]
    ISSUES_TAB = [
        '[data-testid="issues-tab"]',
        'a[href$="/issues"]',
        'nav a:has-text("Issues")',
    ]
    EMPTY_README = [
        '[data-testid="empty-state"]',
        ".empty-state",
    ]

    def title_text(self) -> str | None:
        return self.text_of_first(self.TITLE, timeout=8000)

    def goto_issues(self) -> "IssuesPage":
        self.click_first(self.ISSUES_TAB, timeout=8000)
        self.page.wait_for_load_state("networkidle")
        return IssuesPage(self.page, self.base_url)


from gqa.ui.pages.issue_page import IssuesPage  # noqa: E402  循环引用：放在末尾
