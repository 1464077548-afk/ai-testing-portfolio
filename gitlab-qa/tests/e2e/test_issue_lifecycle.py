"""端到端：议题在两个客户端之间来回走一遍。

e2e 的价值不在于"多跑一遍功能"，而在于证明**两套客户端说的是同一件事**：
API 写进去的，界面能看见；界面上操作的，接口能查到。
任何一端有自己的缓存 / 权限过滤 / 索引延迟，都会在这里现形——
而单端的用例永远是绿的。

所以每条用例都写成"一头写、另一头读"，绝不两头都用同一端。
"""

from __future__ import annotations

import allure
import pytest

from gqa.ui.pages.issue_page import IssuesPage, NewIssuePage
from gqa.ui.pages.project_page import ProjectPage

pytestmark = [pytest.mark.e2e, pytest.mark.destructive, pytest.mark.requires_token]


@allure.epic("端到端")
@allure.feature("议题生命周期")
@allure.story("API 创建的议题在 UI 上可见")
@allure.severity(allure.severity_level.CRITICAL)
def test_issue_created_via_api_is_visible_in_ui(signed_in, settings, data_factory):
    """API 写 → UI 读。"""
    project = data_factory.project()
    issue = data_factory.issue(project["id"], title=f"{data_factory.prefix}-e2e-issue")

    page = ProjectPage(signed_in, settings.base_url)
    issues = page.goto_issues_of(project["path_with_namespace"])
    issues.wait_loaded()

    visible = issue["title"] in (signed_in.content() or "")
    allure.attach(
        f"项目：{project['path_with_namespace']}\n"
        f"议题：#{issue['iid']} {issue['title']}\n"
        f"UI 可见：{visible}",
        name="端到端结果", attachment_type=allure.attachment_type.TEXT,
    )
    assert visible, f"API 创建的议题 {issue['title']} 在 UI 上看不到"


@allure.epic("端到端")
@allure.feature("议题生命周期")
@allure.story("UI 创建的议题 API 查得到，关闭后 UI 状态同步")
@allure.severity(allure.severity_level.CRITICAL)
def test_issue_created_in_ui_is_queryable_and_closable(signed_in, settings,
                                                       data_factory, gl):
    """UI 写 → API 读 → API 改 → UI 读。四个方向都走一遍。

    单向的 e2e（比如只验 UI 建的 API 能查到）价值有限：
    真实缺陷往往出在"状态变更之后另一端的视图没更新"上。
    """
    from gqa.ui.pages.issue_page import NewIssuePage

    project = data_factory.project()
    title = f"{data_factory.prefix}-e2e-ui-issue"

    with allure.step("UI 上创建议题"):
        new_page = NewIssuePage(signed_in, settings.base_url)
        new_page.goto(f"/{project['path_with_namespace']}/-/work_items/new")
        new_page.create(title)

    with allure.step("API 回读：必须查得到"):
        existing = gl.issues.list(project["id"])
        titles = [i.get("title") for i in existing]
        assert title in titles, f"UI 创建的议题 API 查不到：{titles}"

        target = next(i for i in existing if i.get("title") == title)
        assert target["state"] == "opened", f"新建议题状态异常：{target['state']}"

    with allure.step("API 关闭它"):
        closed = gl.issues.close(project["id"], target["iid"])
        assert closed.json_data.get("state") == "closed", "API 关闭议题失败"

    with allure.step("UI 上按已关闭过滤，应该能看到它"):
        page = ProjectPage(signed_in, settings.base_url)
        page.goto(f"/{project['path_with_namespace']}/-/work_items?state=closed")
        IssuesPage(signed_in, settings.base_url).wait_loaded()
        still_visible = title in (signed_in.content() or "")

        allure.attach(
            f"议题：{title}\n关闭后在 UI 的已关闭列表里可见：{still_visible}\n"
            f"URL：{signed_in.url}",
            name="状态同步", attachment_type=allure.attachment_type.TEXT,
        )
        assert still_visible, "API 关闭后，UI 的已关闭列表里看不到该议题"
