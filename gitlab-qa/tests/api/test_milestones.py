"""里程碑。

重点是状态机：active → closed → active。里程碑的 state 命名和议题不一致
（议题是 opened/closed，里程碑是 active/closed），这类「同一个概念两种叫法」
在跨资源联调时最容易写错断言，所以专门留一组用例。
"""

from __future__ import annotations

import allure
import pytest

from gqa import contracts
from gqa.api.client import GitLabError

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]


@allure.epic("GitLab API")
@allure.feature("里程碑")
@allure.story("创建并读回")
@allure.severity(allure.severity_level.NORMAL)
def test_create_milestone_and_read_back(data_factory, gl, a_project):
    ms = data_factory.milestone(a_project.id)
    errors = contracts.collect_errors(ms, contracts.MILESTONE)
    assert errors == [], f"不符合契约：{errors}"

    fetched = gl.milestones.get(a_project.id, ms["id"]).json_data
    assert fetched["title"] == ms["title"]
    assert fetched["state"] == "active", f"新建里程碑应为 active，实际 {fetched['state']}"


@allure.epic("GitLab API")
@allure.feature("里程碑")
@allure.story("标题必填")
@allure.severity(allure.severity_level.MINOR)
@pytest.mark.parametrize("title", ["", "   "], ids=["empty", "blank"])
def test_milestone_title_is_required(gl, a_project, title):
    with pytest.raises(GitLabError) as exc:
        gl.milestones.create(a_project.id, title)
    assert exc.value.status == 400


@allure.epic("GitLab API")
@allure.feature("里程碑")
@allure.story("关闭与重启")
@allure.severity(allure.severity_level.NORMAL)
def test_milestone_state_transitions(data_factory, gl, a_project):
    ms = data_factory.milestone(a_project.id)

    with allure.step("关闭"):
        closed = gl.milestones.update(a_project.id, ms["id"], state_event="close").json_data
        assert closed["state"] == "closed"

    with allure.step("按 state=closed 过滤应能查到"):
        rows = gl.milestones.list(a_project.id, state="closed")
        assert ms["id"] in [r["id"] for r in rows]

    with allure.step("重启"):
        reopened = gl.milestones.update(a_project.id, ms["id"],
                                        state_event="activate").json_data
        assert reopened["state"] == "active"

    with allure.step("按 state=closed 过滤应查不到"):
        rows = gl.milestones.list(a_project.id, state="closed")
        assert ms["id"] not in [r["id"] for r in rows]


@allure.epic("GitLab API")
@allure.feature("里程碑")
@allure.story("修改标题与截止日期")
@allure.severity(allure.severity_level.MINOR)
def test_update_milestone_fields(data_factory, gl, a_project):
    ms = data_factory.milestone(a_project.id)
    updated = gl.milestones.update(a_project.id, ms["id"],
                                   title="改过的标题", due_date="2026-12-31").json_data
    assert updated["title"] == "改过的标题"
    assert updated["due_date"] == "2026-12-31"


@allure.epic("GitLab API")
@allure.feature("里程碑")
@allure.story("里程碑下的议题")
@allure.severity(allure.severity_level.NORMAL)
def test_issues_can_be_grouped_by_milestone(data_factory, gl, a_project):
    ms = data_factory.milestone(a_project.id)
    issue = data_factory.issue(a_project.id, milestone_id=ms["id"])

    rows = gl.milestones.issues(a_project.id, ms["id"])
    assert issue["iid"] in [i["iid"] for i in rows], "挂了里程碑的议题没出现在里程碑下"


@allure.epic("GitLab API")
@allure.feature("里程碑")
@allure.story("删除后不可访问")
@allure.severity(allure.severity_level.NORMAL)
def test_deleted_milestone_is_gone(data_factory, gl, a_project):
    ms = data_factory.milestone(a_project.id)
    gl.milestones.delete(a_project.id, ms["id"])
    assert gl.milestones.get(a_project.id, ms["id"]).status == 404
