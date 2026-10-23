"""议题：跨资源的数据依赖与状态流转。

议题依附于项目，所以这组用例天然演示了「资源之间的依赖怎么组织」——
用 data_factory 一次创建，用例结束后按 议题→项目 的顺序回收。
"""

from __future__ import annotations

import allure
import pytest

from gqa import contracts
from gqa.models import Issue

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]


@allure.epic("GitLab API")
@allure.feature("议题")
@allure.story("创建议题")
@allure.severity(allure.severity_level.CRITICAL)
def test_create_issue(data_factory, a_project, gl):
    with allure.step(f"在项目 {a_project.id} 下创建议题"):
        raw = data_factory.issue(a_project.id, description="自动化创建的议题")
        issue = Issue.model_validate(raw)

    with allure.step("校验结构"):
        assert contracts.collect_errors(raw, contracts.ISSUE) == []

    with allure.step("新议题默认处于 opened 状态"):
        assert issue.is_open, f"新议题状态应为 opened，实际 {issue.state}"

    with allure.step("读回校验"):
        fetched = gl.issues.get(a_project.id, issue.iid)
        assert fetched.json_data["title"] == issue.title
        assert fetched.json_data["project_id"] == a_project.id


@allure.epic("GitLab API")
@allure.feature("议题")
@allure.story("议题状态流转")
@allure.severity(allure.severity_level.CRITICAL)
def test_close_issue(gl, a_project, data_factory):
    """opened → closed 是一次完整的状态流转，必须能回读确认。"""
    with allure.step("创建议题"):
        issue = data_factory.issue(a_project.id)

    with allure.step("关闭议题"):
        resp = gl.issues.close(a_project.id, issue["iid"])
        assert resp.json_data["state"] == "closed", (
            f"关闭后状态应为 closed，实际 {resp.json_data['state']}"
        )

    with allure.step("读回确认状态已落库"):
        fetched = gl.issues.get(a_project.id, issue["iid"])
        assert fetched.json_data["state"] == "closed", "状态未持久化"


@allure.epic("GitLab API")
@allure.feature("议题")
@allure.story("议题列表包含新创建的议题")
@allure.severity(allure.severity_level.NORMAL)
def test_issue_appears_in_list(gl, a_project, data_factory):
    created = data_factory.issue(a_project.id)
    issues = gl.issues.list(a_project.id, per_page=100)
    assert any(i["iid"] == created["iid"] for i in issues), "新议题未出现在列表里"


@allure.epic("GitLab API")
@allure.feature("议题")
@allure.story("议题标题必填")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("title", ["", "   "], ids=["empty", "blank"])
def test_issue_title_required(gl, a_project, title):
    from gqa.api.client import GitLabError

    with pytest.raises(GitLabError) as exc:
        gl.issues.create(a_project.id, title)
    assert exc.value.status == 400, f"空标题应 400，实际 {exc.value.status}"


@allure.epic("GitLab API")
@allure.feature("议题")
@allure.story("只读接口不得产生副作用")
@allure.severity(allure.severity_level.NORMAL)
def test_listing_does_not_create_issues(gl, a_project):
    """列表是只读操作，调两次结果条数必须一致。

    这条用例盯的是「GET 里夹带写逻辑」这类实现错误——
    在别的系统里见过：GET /list 顺手初始化了一条默认记录。
    """
    first = len(gl.issues.list(a_project.id, per_page=100))
    second = len(gl.issues.list(a_project.id, per_page=100))
    assert first == second, f"连续两次列表条数不一致：{first} → {second}"
