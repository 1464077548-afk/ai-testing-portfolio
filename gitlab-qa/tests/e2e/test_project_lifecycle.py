"""端到端：项目生命周期在 API 与 UI 之间的一致性。

e2e 的定位是「证明两套客户端说的是同一件事」：
API 建的数据 UI 能看见，API 删掉的 UI 上也要消失。
它不重复验证单点功能，只验证串起来之后对不对。
"""

from __future__ import annotations

import allure
import pytest

from gqa.ui.pages.project_page import ProjectsPage

pytestmark = [pytest.mark.e2e, pytest.mark.destructive, pytest.mark.requires_token]


@allure.epic("端到端")
@allure.feature("项目生命周期")
@allure.story("API 创建的项目在 UI 上可见")
@allure.severity(allure.severity_level.CRITICAL)
def test_project_created_via_api_is_visible_in_ui(signed_in, settings, data_factory):
    with allure.step("用 API 创建项目"):
        project = data_factory.project()
        path = project["path"]

    with allure.step("在项目列表里搜索该项目"):
        projects = ProjectsPage(signed_in, settings.base_url)
        visible = projects.has_project(path)

    allure.attach(
        f"项目 path={path}\nUI 可见={visible}",
        name="端到端结果",
        attachment_type=allure.attachment_type.TEXT,
    )
    assert visible, f"API 创建的项目 {path} 在 UI 上搜不到"


@allure.epic("端到端")
@allure.feature("项目生命周期")
@allure.story("API 删除的项目从 UI 上消失")
@allure.severity(allure.severity_level.CRITICAL)
def test_project_deleted_via_api_disappears_from_ui(signed_in, settings, data_factory,
                                                    gl):
    """先看见、再删掉、再确认看不见。

    只测"删完之后看不见"是不够的：如果第一步的可见性本来就没生效
    （比如列表压根加载不出来），删完依然"看不见"，用例照样绿。
    所有"消失类"断言都必须先证明它**曾经出现过**。
    """
    project = data_factory.project()
    path = project["path"]

    projects = ProjectsPage(signed_in, settings.base_url)

    with allure.step("确认 UI 上先能看到"):
        assert projects.has_project(path), f"删除前 UI 上就搜不到 {path}，用例前提不成立"

    with allure.step("API 永久删除"):
        gl.projects.purge(project["id"], project.get("path_with_namespace", ""))
        gone = gl.projects.try_get(project["id"])
        assert gone.status == 404, f"删除后仍能查到项目：HTTP {gone.status}"

    with allure.step("UI 上应该搜不到了"):
        # 只查 Personal 标签：删除后 membership 立刻失效，这里马上就该空；
        # Contributed 的"贡献事件"反向滞后（删了事件还在），
        # 查通用列表只会等满超时，还可能等出假阳性。
        still = projects.has_project(path, personal_only=True)

    allure.attach(
        f"项目 path={path}\n删除前可见=True\n删除后可见={still}",
        name="端到端结果", attachment_type=allure.attachment_type.TEXT,
    )
    assert not still, f"API 已删除的项目 {path} 在 UI 上仍然可见"
