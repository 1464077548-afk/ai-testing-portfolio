"""议题（work items）相关的界面用例。

GitLab 19 把议题迁到了 work items，路径从 /-/issues 变成 /-/work_items，
DOM 也整套换掉了。这个文件的用例如果打老实例，页面对象里的回退选择器
仍然有效——这就是为什么选择器要写成"候选列表"而不是单个字符串。

这里刻意保留一条**失败路径**用例（空标题提交）：
只看成功路径的界面测试等于没测——
按钮点了、页面跳了，但"该拦的没拦住"这类缺陷一个都发现不了。
"""

from __future__ import annotations

import allure
import pytest

from gqa.ui.pages.issue_page import NewIssuePage
from gqa.ui.pages.project_page import ProjectPage

pytestmark = [pytest.mark.ui]


@allure.epic("GitLab UI")
@allure.feature("议题")
@allure.story("议题列表页能打开")
@allure.severity(allure.severity_level.CRITICAL)
def test_issues_page_renders(signed_in, settings, data_factory):
    """议题列表页：有条目或有空态，二者必居其一。"""
    project = data_factory.project()
    data_factory.issue(project["id"], title="列表渲染用议题")

    page = ProjectPage(signed_in, settings.base_url)
    issues = page.goto_issues_of(project["path_with_namespace"])
    issues.wait_loaded()

    listed = issues.issue_count() > 0
    empty = issues.has_empty_state()

    allure.attach(f"列表条目：{listed}\n空态：{empty}\nURL：{signed_in.url}",
                  name="议题列表", attachment_type=allure.attachment_type.TEXT)
    assert listed or empty, (
        f"议题列表页既没条目也没空态，页面可能渲染失败。URL：{signed_in.url}"
    )


@allure.epic("GitLab UI")
@allure.feature("议题")
@allure.story("能在界面上新建议题")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.destructive
def test_create_issue_via_ui(signed_in, settings, data_factory, gl):
    """走界面建一条议题，然后回读确认真的建出来了。

    只断言"提交后跳转了"是不够的：按钮点了、页面也跳了，
    数据却可能压根没落库（前端吞掉了错误）。
    所以最后一步一定要回到数据上确认——UI 用例同样需要"回读"这一步。
    """
    project = data_factory.project()
    title = f"{data_factory.prefix}-ui-issue"

    page = NewIssuePage(signed_in, settings.base_url)
    page.goto(f"/{project['path_with_namespace']}/-/work_items/new")
    page.create(title, description="由 UI 自动化创建")

    allure.attach(f"提交后 URL：{signed_in.url}", name="提交结果",
                  attachment_type=allure.attachment_type.TEXT)

    # 轮询而不是立刻取 content()：提交后的跳转与渲染都是异步的，
    # 查得太早会得到"单独跑绿、并发跑红"的幽灵用例
    visible = page.wait_for_text(title, timeout=15.0)
    created = [i.get("title") for i in gl.issues.list(project["id"])]

    allure.attach(f"页面含标题：{visible}\nAPI 回读：{created}", name="双重确认",
                  attachment_type=allure.attachment_type.TEXT)

    assert visible, f"提交后页面上找不到议题标题「{title}」，URL：{signed_in.url}"
    assert title in created, f"页面显示成功但接口查不到该议题：{created}"


@allure.epic("GitLab UI")
@allure.feature("议题")
@allure.story("空标题提交被拦住")
@allure.severity(allure.severity_level.NORMAL)
def test_empty_title_is_rejected(signed_in, settings, data_factory, gl):
    """标题留空提交，不应该创建成功。

    用 API 回读作判据而不是看页面提示：
    提示条的文案和 DOM 每个版本都在变，
    而"库里有没有多出一条空标题议题"是稳定的事实。
    """
    project = data_factory.project()

    page = NewIssuePage(signed_in, settings.base_url)
    page.goto(f"/{project['path_with_namespace']}/-/work_items/new")
    try:
        page.create("")
    except Exception as exc:  # noqa: BLE001  按钮禁用点不动，属"被拦住"的一种表现
        allure.attach(f"提交未成功（预期内）：{type(exc).__name__}", name="提交被拦",
                      attachment_type=allure.attachment_type.TEXT)

    titles = [i.get("title") for i in gl.issues.list(project["id"])]
    allure.attach(f"议题标题列表：{titles}", name="回读",
                  attachment_type=allure.attachment_type.TEXT)
    assert "" not in titles, "空标题的议题被创建成功了——必填校验没生效"
