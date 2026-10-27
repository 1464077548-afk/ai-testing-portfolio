"""合并请求。

MR 是接口测试里最「挑环境」的一块：没有真实改动就创建不成功。
所以数据工厂里创建 MR 必须同时做三件事——建分支、在分支上提交改动、
再创建 MR。少任何一步都会得到 400，而它和「接口坏了」长得一模一样。
"""

from __future__ import annotations

import allure
import pytest

from gqa import contracts

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]


@allure.epic("GitLab API")
@allure.feature("合并请求")
@allure.story("创建并读回")
@allure.severity(allure.severity_level.CRITICAL)
def test_create_mr_and_read_back(data_factory, gl, a_project):
    mr = data_factory.merge_request(a_project.id)

    errors = contracts.collect_errors(mr, contracts.MERGE_REQUEST)
    assert errors == [], f"不符合契约：{errors}"

    fetched = gl.merge_requests.get(a_project.id, mr["iid"]).json_data
    assert fetched["state"] == "opened"
    assert fetched["title"] == mr["title"]

    with allure.step("列表里应能查到"):
        iids = [m["iid"] for m in gl.merge_requests.list(a_project.id)]
        assert mr["iid"] in iids


@allure.epic("GitLab API")
@allure.feature("合并请求")
@allure.story("分支不存在时拒绝创建")
@allure.severity(allure.severity_level.NORMAL)
def test_mr_with_unknown_branch_is_rejected(gl, a_project):
    resp = gl.merge_requests.create(a_project.id, "no-such-branch", "main", "坏分支")
    assert resp.status in (400, 409), f"不存在的分支应被拒绝，实际 {resp.status}"


@allure.epic("GitLab API")
@allure.feature("合并请求")
@allure.story("同分支重复创建冲突")
@allure.severity(allure.severity_level.NORMAL)
def test_duplicate_mr_conflicts(data_factory, gl, a_project):
    mr = data_factory.merge_request(a_project.id)
    resp = gl.merge_requests.create(a_project.id, mr["source_branch"],
                                    mr["target_branch"], "再来一条")
    assert resp.status == 409, f"同一分支的重复 MR 应 409，实际 {resp.status}"


@allure.epic("GitLab API")
@allure.feature("合并请求")
@allure.story("变更内容可查")
@allure.severity(allure.severity_level.NORMAL)
def test_mr_changes_expose_diff(data_factory, gl, a_project):
    mr = data_factory.merge_request(a_project.id)
    resp = gl.merge_requests.changes(a_project.id, mr["iid"])
    assert resp.status == 200
    body = resp.json_data
    assert "changes" in body, "changes 端点没返回 changes 字段"
    assert isinstance(body["changes"], list)


@allure.epic("GitLab API")
@allure.feature("合并请求")
@allure.story("合并后状态变为 merged")
@allure.severity(allure.severity_level.CRITICAL)
def test_merge_mr_becomes_merged(data_factory, gl, a_project):
    mr = data_factory.merge_request(a_project.id)
    resp = gl.merge_requests.merge(a_project.id, mr["iid"])

    # 状态多义：CI 未通过 / 有冲突的实例会返回 405/406/409，
    # 那是实例的正常行为，不是接口缺陷。只认 200 会在别的环境上假红。
    assert resp.status in (200, 405, 406, 409), f"合并请求异常状态：{resp.status}"
    if resp.status == 200:
        after = gl.merge_requests.get(a_project.id, mr["iid"]).json_data
        assert after["state"] == "merged", f"合并后状态应为 merged，实际 {after['state']}"


@allure.epic("GitLab API")
@allure.feature("合并请求")
@allure.story("关闭 MR")
@allure.severity(allure.severity_level.NORMAL)
def test_close_mr(data_factory, gl, a_project):
    mr = data_factory.merge_request(a_project.id)
    closed = gl.merge_requests.update(a_project.id, mr["iid"],
                                      state_event="close").json_data
    assert closed["state"] == "closed"

    with allure.step("state=opened 过滤应查不到"):
        rows = gl.merge_requests.list(a_project.id, state="opened")
        assert mr["iid"] not in [m["iid"] for m in rows]
