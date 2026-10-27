"""项目标签。

标签看着简单，却是最典型的「幂等语义」资源：同名重复创建必须冲突，
删除后必须查不到。这两条不测，数据工厂重跑时就会在奇怪的地方炸，
而且报错信息指向的是创建，真正的根因却是上一轮没清干净。
"""

from __future__ import annotations

import allure
import pytest

from gqa import contracts
from gqa.api.client import GitLabError

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]


@allure.epic("GitLab API")
@allure.feature("标签")
@allure.story("创建标签并读回")
@allure.severity(allure.severity_level.NORMAL)
def test_create_label_and_appears_in_list(data_factory, gl, a_project):
    with allure.step("创建标签"):
        label = data_factory.label(a_project.id)

    with allure.step("校验响应契约"):
        errors = contracts.collect_errors(label, contracts.LABEL)
        assert errors == [], f"不符合契约：{errors}"

    with allure.step("列表里应能查到，且字段一致"):
        found = [l for l in gl.labels.list(a_project.id) if l["id"] == label["id"]]
        assert found, f"标签 {label['name']} 没出现在列表里"
        assert found[0]["color"] == label["color"]

    with allure.step("按 id 读回"):
        assert gl.labels.try_get(a_project.id, label["id"]).json_data["name"] == label["name"]


@allure.epic("GitLab API")
@allure.feature("标签")
@allure.story("标签名必填")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("name", ["", "   "], ids=["empty", "blank"])
def test_label_name_is_required(gl, a_project, name):
    with allure.step(f"用空名创建（name={name!r}）"):
        with pytest.raises(GitLabError) as exc:
            gl.labels.create(a_project.id, name)
    assert exc.value.status == 400, f"空标签名应返回 400，实际 {exc.value.status}"


@allure.epic("GitLab API")
@allure.feature("标签")
@allure.story("同名标签冲突")
@allure.severity(allure.severity_level.NORMAL)
def test_duplicate_label_conflicts(data_factory, gl, a_project):
    """同名再建必须 409。

    这条是数据工厂「重跑不炸」的前提：允许重复创建的话，
    同一轮里两个用例的标签会互相覆盖，表现为偶发的字段不一致。
    """
    name = data_factory.label(a_project.id)["name"]
    with allure.step(f"重复创建同名标签 {name}"):
        resp = gl.labels.create(a_project.id, name)
    assert resp.status == 409, f"同名标签应冲突（409），实际 {resp.status}"


@allure.epic("GitLab API")
@allure.feature("标签")
@allure.story("修改标签")
@allure.severity(allure.severity_level.NORMAL)
def test_update_label_color_and_name(data_factory, gl, a_project):
    label = data_factory.label(a_project.id, color="#0033cc")

    with allure.step("改颜色与名字"):
        updated = gl.labels.update(a_project.id, label["id"],
                                   new_name=f"{label['name']}-v2", color="#ff0000")
        assert updated.status == 200

    with allure.step("读回应为最新值"):
        after = gl.labels.try_get(a_project.id, label["id"]).json_data
        assert after["color"] == "#ff0000", f"颜色没改过来：{after['color']}"
        assert after["name"].endswith("-v2")


@allure.epic("GitLab API")
@allure.feature("标签")
@allure.story("删除后不可访问")
@allure.severity(allure.severity_level.NORMAL)
def test_deleted_label_is_gone(data_factory, gl, a_project):
    label = data_factory.label(a_project.id)
    with allure.step("删除标签"):
        gl.labels.delete(a_project.id, label["id"])

    with allure.step("详情应 404"):
        assert gl.labels.try_get(a_project.id, label["id"]).status == 404

    with allure.step("列表里也不该再有"):
        assert label["id"] not in [l["id"] for l in gl.labels.list(a_project.id)]


@allure.epic("GitLab API")
@allure.feature("标签")
@allure.story("项目不存在时的错误语义")
@allure.severity(allure.severity_level.MINOR)
def test_labels_of_missing_project_are_404(gl):
    """不存在的父资源要 404，不能是 200 + 空列表。

    返回空列表会把「拼错项目 id」伪装成「这个项目没有标签」，
    排查时会一直往权限方向想。
    """
    resp = gl.labels.try_list(99999999)
    assert resp.status == 404, f"不存在的项目应 404，实际 {resp.status}"
    errors = contracts.collect_errors(resp.json_data, contracts.ERROR)
    assert errors == [], f"错误体不符合契约：{errors}"
