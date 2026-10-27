"""项目相关的界面用例。

UI 层只回答一个问题：**用户能不能用界面完成这件事**。
业务规则由 API 层验证过，这里不重复——
重复验证的代价是每次 DOM 改动都要同时改两层，最后两层一起腐化。

所以断言都落在"界面状态"上：列出来了 / 没列出来、过滤之后还剩几条、
点进去标题对不对，而不是"接口返回了什么"。
"""

from __future__ import annotations

import allure
import pytest

from gqa.ui.pages.project_page import ProjectsPage

pytestmark = [pytest.mark.ui]


@allure.epic("GitLab UI")
@allure.feature("项目列表")
@allure.story("登录后能看到项目列表区域")
@allure.severity(allure.severity_level.CRITICAL)
def test_projects_page_renders(signed_in, settings):
    """列表页要么有项目，要么有空态——但不能两者都没有。

    "两者都没有"是界面真的坏了（前端报错后白屏），
    而只断言"有项目"会在空实例上假红。
    """
    projects = ProjectsPage(signed_in, settings.base_url)
    projects.open()
    projects.wait_loaded()

    count = projects.project_count()
    empty = projects.exists_any(ProjectsPage.EMPTY_STATE, timeout=4000)

    allure.attach(f"项目条目数：{count}\n空态可见：{empty}\nURL：{signed_in.url}",
                  name="列表页状态", attachment_type=allure.attachment_type.TEXT)

    assert count > 0 or empty, (
        f"项目列表页既没有项目也没有空态，页面可能渲染失败。URL：{signed_in.url}"
    )


@allure.epic("GitLab UI")
@allure.feature("项目列表")
@allure.story("按名字过滤能筛出目标项目")
@allure.severity(allure.severity_level.CRITICAL)
def test_project_filter_narrows_list(signed_in, settings, data_factory):
    """过滤一个真实存在的项目能留下它，过滤不存在的名字列表应为空。

    只测"能搜到"是不够的：如果过滤压根没生效、返回的还是全部列表，
    "能搜到"照样成立——那就成了假绿。所以必须配一条**反向断言**。
    """
    project = data_factory.project()
    name = project["path"]

    projects = ProjectsPage(signed_in, settings.base_url)

    with allure.step(f"过滤 {name} → 列表非空"):
        hit = projects.has_project(name)
        allure.attach(f"过滤 {name}：{hit}", name="正向过滤",
                      attachment_type=allure.attachment_type.TEXT)
        assert hit, f"按名字过滤后找不到刚创建的项目 {name}"

    with allure.step("过滤一个不存在的名字 → 列表被筛空"):
        # 反向专用方法：先等列表挂载完成（有条目或空态文案），
        # 再数项目卡片。数卡片而不是找 href——过滤时面包屑导航的 href
        # 也带着这个名字，全局找 href 必然命中，反向断言会恒红。
        miss = projects.expect_no_project("qa-no-such-project-zzz-9999")
        allure.attach(f"过滤后列表为空：{miss}\nURL：{signed_in.url}",
                      name="反向过滤", attachment_type=allure.attachment_type.TEXT)
        assert miss, (
            "按不存在的名字过滤后列表仍有项目卡片，过滤没有真正生效。"
            f"URL：{signed_in.url}"
        )


@allure.epic("GitLab UI")
@allure.feature("项目详情")
@allure.story("能进入项目详情且标题正确")
@allure.severity(allure.severity_level.NORMAL)
def test_project_detail_shows_name(signed_in, settings, data_factory):
    """点进项目详情，页面上应当能看到项目名。"""
    project = data_factory.project()
    name = project["path"]
    short = name.split("/")[-1]

    projects = ProjectsPage(signed_in, settings.base_url)
    projects.open()
    detail = projects.open_project(name)

    title = detail.title_text() or ""
    allure.attach(f"项目：{name}\n页面标题：{title}\nURL：{signed_in.url}",
                  name="详情页", attachment_type=allure.attachment_type.TEXT)

    assert short in title or short in signed_in.url, (
        f"详情页标题「{title}」里没有项目名 {short}，URL：{signed_in.url}"
    )
