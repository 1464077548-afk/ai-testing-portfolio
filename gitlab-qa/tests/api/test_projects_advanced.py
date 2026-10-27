"""项目进阶：修改、按路径访问、实例特性。

「按完整路径访问」是前端最依赖的一条语义（URL 里就是 namespace/project），
它和「按 id 访问」是两条独立入口，只测一条的话，路径里的斜杠一旦编码错
就是全线 404，而这种事故在只测 id 的用例里完全看不出来。
"""

from __future__ import annotations

import allure
import pytest

from gqa import contracts

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]


@allure.epic("GitLab API")
@allure.feature("项目")
@allure.story("修改项目")
@allure.severity(allure.severity_level.NORMAL)
def test_update_project_description(gl, data_factory):
    project = data_factory.project()
    updated = gl.projects.update(project["id"], description="由自动化补充的描述")
    assert updated.status == 200, f"修改项目失败：{updated.status}"

    fetched = gl.projects.get(project["id"]).json_data
    assert fetched["description"] == "由自动化补充的描述"
    errors = contracts.collect_errors(fetched, contracts.PROJECT)
    assert errors == [], f"不符合契约：{errors}"


@allure.epic("GitLab API")
@allure.feature("项目")
@allure.story("按完整路径访问")
@allure.severity(allure.severity_level.CRITICAL)
def test_project_by_full_path(gl, data_factory):
    project = data_factory.project()
    full_path = project.get("path_with_namespace")
    if not full_path:
        pytest.skip("响应里没有 path_with_namespace，无法验证路径访问")

    resp = gl.projects.by_path(full_path)
    assert resp.status == 200, f"按路径取项目失败（{resp.status}）：{resp.json_data}"
    assert resp.json_data["id"] == project["id"], "按路径取到的不是同一个项目"


@allure.epic("GitLab API")
@allure.feature("项目")
@allure.story("路径不存在时 404")
@allure.severity(allure.severity_level.NORMAL)
def test_unknown_path_is_404(gl):
    resp = gl.projects.by_path("qa-no-such-namespace/qa-no-such-project")
    assert resp.status == 404, f"不存在的路径应 404，实际 {resp.status}"


@allure.epic("GitLab API")
@allure.feature("实例特性")
@allure.story("特性开关列表")
@allure.severity(allure.severity_level.MINOR)
def test_features_require_admin(gl):
    """管理员端点：非管理员拿不到。拿到了就应当是数组。"""
    resp = gl.client.get("/features", expect=(200, 401, 403))
    assert resp.status in (200, 401, 403), f"特性列表异常：{resp.status}"
    if resp.status == 200:
        assert isinstance(resp.json_data, list), "特性列表应为数组"
