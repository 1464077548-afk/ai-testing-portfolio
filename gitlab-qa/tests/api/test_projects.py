"""项目 CRUD 与数据依赖。

这组用例是接口测试的基本盘：创建 → 读回一致 → 列表可见 → 删除 → 确认消失。
每一步都只依赖上一步的产物，不依赖任何预置数据。
"""

from __future__ import annotations

import allure
import pytest

from gqa import contracts
from gqa.models import Project

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]


@allure.epic("GitLab API")
@allure.feature("项目")
@allure.story("创建项目并读回")
@allure.severity(allure.severity_level.CRITICAL)
def test_create_project_and_read_back(data_factory, gl):
    with allure.step("创建项目"):
        created = data_factory.project()
        project = Project.model_validate(created)

    with allure.step("校验响应结构"):
        errors = contracts.collect_errors(created, contracts.PROJECT)
        assert errors == [], f"不符合契约：{errors}"

    with allure.step("按 id 读回，字段应一致"):
        fetched = gl.projects.get(project.id)
        assert fetched.json_data["name"] == project.name
        assert fetched.json_data["path"] == project.path
        assert fetched.json_data["id"] == project.id

    allure.attach(
        f"id={project.id}\nname={project.name}\npath={project.path}\n"
        f"visibility={created.get('visibility')}",
        name="项目信息",
        attachment_type=allure.attachment_type.TEXT,
    )


@allure.epic("GitLab API")
@allure.feature("项目")
@allure.story("删除后不可访问")
@allure.severity(allure.severity_level.CRITICAL)
def test_deleted_project_is_gone(gl, data_factory):
    with allure.step("创建项目"):
        pid = data_factory.project()["id"]

    with allure.step("删除项目"):
        resp = gl.projects.delete(pid)
        assert resp.status in (202, 204), f"删除失败：{resp.status}"

    with allure.step("再次读取应不可用"):
        # GitLab 删除是异步的；且 16 起默认开启「延迟删除」——
        # 项目会先标记待删除并在保留期后真正清除，期间仍能以 200 +
        # marked_for_deletion 读到。所以"消失"有两种合法表现，
        # 只认 404 会在开启延迟删除的实例上误报。
        from gqa.utils.wait import wait_until

        def is_gone() -> bool:
            r = gl.projects.try_get(pid)
            if r.status == 404:
                return True
            if r.status == 200:
                # 注意是本框架的 Response：已解析的 JSON 在 .json_data 字段上，
                # 不是 requests 的 .json() 方法
                body = r.json_data
                if not isinstance(body, dict):
                    return False
                # 字段名实测为 marked_for_deletion_at（另有 ..._on 同义字段）
                return bool(body.get("marked_for_deletion_at")
                            or body.get("marked_for_deletion_on"))
            return False

        gone = wait_until(is_gone, timeout=20)
        assert gone, "项目删除后仍可作为正常项目访问（既没 404 也没标记待删除）"


@allure.epic("GitLab API")
@allure.feature("项目")
@allure.story("项目名校验")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("name", ["", "   "], ids=["empty", "blank"])
def test_project_name_is_required(gl, data_factory, name):
    from gqa.api.client import GitLabError

    with allure.step(f"用空名创建项目（name={name!r}）"):
        with pytest.raises(GitLabError) as exc:
            gl.projects.create(name)
    assert exc.value.status == 400, f"空项目名应返回 400，实际 {exc.value.status}"


@allure.epic("GitLab API")
@allure.feature("项目")
@allure.story("可见性参数生效")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("visibility", ["private", "internal", "public"],
                         ids=["private", "internal", "public"])
def test_visibility_is_applied(data_factory, gl, visibility):
    created = data_factory.project(visibility=visibility)
    assert created["visibility"] == visibility, (
        f"请求 visibility={visibility}，实际 {created['visibility']}"
    )


@allure.epic("GitLab API")
@allure.feature("项目")
@allure.story("创建后可在列表中搜到")
@allure.severity(allure.severity_level.NORMAL)
def test_project_is_searchable(data_factory, gl):
    created = data_factory.project()
    name = created["path"]

    from gqa.utils.wait import wait_until
    # 搜索索引有延迟，用轮询而不是 sleep 固定时长——
    # 固定时长在慢机器上会假失败，在快机器上浪费时间
    found = wait_until(lambda: any(p["id"] == created["id"]
                                   for p in gl.projects.search(name)), timeout=20)
    assert found, f"项目 {name} 创建后搜不到"
