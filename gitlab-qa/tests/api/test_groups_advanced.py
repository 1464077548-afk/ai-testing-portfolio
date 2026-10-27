"""群组进阶：详情、修改、群组内项目、子群组、群组标签。

群组是「命名空间」也是「权限容器」，两条身份叠在一个对象上，
所以这里除了 CRUD，还要验证「在群组下建的项目确实归到该群组」——
这条语义错了，后面所有按群组聚合的统计都会静默偏。
"""

from __future__ import annotations

import allure
import pytest

from gqa import contracts
from gqa.api.client import GitLabError
from gqa.utils.wait import wait_until

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]


def _is_deletion_scheduled(group: dict) -> bool:
    """群组删除是**延迟删除**：DELETE 立刻回 202，但对象不会马上消失。

    GitLab 先把名字改成 `<原名>-deletion_scheduled-<id>` 把路径让出来，
    真正的清理由后台任务完成——实测 20 秒后 GET 仍是 200。
    所以「删完立刻断言 404」必然假红。这里认两个信号：
    对象消失，或者已打上删除标记。两者都证明删除确实生效了。
    """
    if not group:
        return True
    name = (group.get("name") or "") + (group.get("path") or "")
    return "deletion_scheduled" in name or bool(group.get("marked_for_deletion"))


@allure.epic("GitLab API")
@allure.feature("群组")
@allure.story("创建、修改、删除")
@allure.severity(allure.severity_level.NORMAL)
def test_group_lifecycle(gl):
    with allure.step("创建群组"):
        created = gl.groups.create("qa-auto-group", "qa-auto-group")
        assert created.status == 201, f"创建群组失败：{created.status}"
        gid = created.json_data["id"]
        errors = contracts.collect_errors(created.json_data, contracts.GROUP)
        assert errors == [], f"不符合契约：{errors}"

    with allure.step("读回：路径与名字一致"):
        fetched = gl.groups.get(gid).json_data
        assert fetched["name"] == "qa-auto-group"
        assert fetched["path"] == "qa-auto-group"

    with allure.step("改名后应生效"):
        updated = gl.groups.update(gid, name="qa-renamed", path="qa-renamed")
        assert updated.status == 200
        assert gl.groups.get(gid).json_data["path"] == "qa-renamed"

    with allure.step("删除：接受后应进入删除流程"):
        deleted = gl.groups.delete(gid)
        assert deleted.status in (202, 204), f"删除未受理：{deleted.status}"

        # 不立刻断言 404：延迟删除下对象会先改名再异步清理
        converged = wait_until(
            lambda: gl.groups.get(gid).status == 404
            or _is_deletion_scheduled(gl.groups.get(gid).json_data or {}),
            timeout=15.0, interval=1.0, description=f"群组 {gid} 进入删除态")
        last = gl.groups.get(gid)
        allure.attach(f"DELETE {deleted.status}；最终 GET {last.status} "
                      f"name={(last.json_data or {}).get('name')}",
                      name="删除收敛", attachment_type=allure.attachment_type.TEXT)
        assert converged, f"删除后群组既未消失也未标记删除（仍为 {last.status}）"


@allure.epic("GitLab API")
@allure.feature("群组")
@allure.story("在群组下创建项目")
@allure.severity(allure.severity_level.NORMAL)
def test_project_created_under_group(gl, data_factory):
    group = data_factory.group()
    project = data_factory.project(namespace_id=group["id"])

    with allure.step("群组项目列表里应能查到"):
        ids = [p["id"] for p in gl.groups.projects(group["id"])]
        assert project["id"] in ids, "群组下建的项目没出现在群组项目列表里"

    with allure.step("项目的命名空间应指向该群组"):
        namespace = project.get("namespace") or {}
        assert namespace.get("id") == group["id"], f"命名空间不对：{namespace}"


@allure.epic("GitLab API")
@allure.feature("群组")
@allure.story("群组列表")
@allure.severity(allure.severity_level.NORMAL)
def test_group_appears_in_list(gl, data_factory):
    group = data_factory.group()
    rows = gl.groups.list()
    assert group["id"] in [g["id"] for g in rows], "新建群组没出现在列表里"
    for row in rows[:5]:
        errors = contracts.collect_errors(row, contracts.GROUP)
        assert errors == [], f"不符合契约：{errors}"


@allure.epic("GitLab API")
@allure.feature("群组")
@allure.story("用群组端点创建项目")
@allure.severity(allure.severity_level.NORMAL)
def test_create_project_via_group_endpoint(gl, data_factory):
    """「在群组下建项目」这条语义必须成立。

    GitLab 19 把 `POST /groups/{id}/projects` 撤了（实测 404），
    唯一入口是 `POST /projects` + `namespace_id`（封装层已改走这条）。
    这条用例守的是**结果**而不是具体路径：建出来的项目必须归到该群组下。
    路径变了用例不用改——这正是「资源封装层」存在的意义。
    """
    group = data_factory.group()
    resp = gl.groups.create_project(group["id"], f"qa-in-group-{group['id']}")
    assert resp.status == 201, f"在群组下建项目失败（{resp.status}）：{resp.json_data}"

    with allure.step("命名空间必须指向该群组"):
        ns = resp.json_data.get("namespace") or {}
        assert ns.get("id") == group["id"], f"命名空间未指向群组：{ns}"

    pid = resp.json_data["id"]
    try:
        ids = [p["id"] for p in gl.groups.projects(group["id"])]
        assert pid in ids, "通过群组端点建的项目没出现在群组项目列表里"
    finally:
        # 这条路径不走数据工厂，必须自己收尾，否则每跑一轮多一个僵尸项目
        gl.projects.purge(pid, resp.json_data.get("path_with_namespace", ""))


@allure.epic("GitLab API")
@allure.feature("群组")
@allure.story("子群组")
@allure.severity(allure.severity_level.NORMAL)
def test_subgroups_are_listed(gl, data_factory):
    group = data_factory.group()
    sub = gl.groups.create_subgroup(group["id"], "qa-sub", "qa-sub")
    assert sub.status == 201, f"创建子群组失败：{sub.status}"

    errors = contracts.collect_errors(sub.json_data, contracts.GROUP)
    assert errors == [], f"不符合契约：{errors}"

    rows = gl.groups.subgroups(group["id"])
    assert sub.json_data["id"] in [g["id"] for g in rows], "子群组没出现在列表里"


@allure.epic("GitLab API")
@allure.feature("群组")
@allure.story("群组标签")
@allure.severity(allure.severity_level.MINOR)
def test_group_labels_are_listed(gl, data_factory):
    group = data_factory.group()
    rows = gl.labels.group_list(group["id"])
    assert isinstance(rows, list), "群组标签列表应为数组"
    for row in rows[:5]:
        errors = contracts.collect_errors(row, contracts.LABEL)
        assert errors == [], f"不符合契约：{errors}"


@allure.epic("GitLab API")
@allure.feature("群组")
@allure.story("群组不存在时 404")
@allure.severity(allure.severity_level.MINOR)
def test_missing_group_is_404(gl):
    assert gl.groups.get(987654321).status == 404
    with pytest.raises(GitLabError) as exc:
        gl.groups.update(987654321, name="x")
    assert exc.value.status == 404
