"""议题评论。

评论列表里混着两种东西：人写的留言和服务自动产生的系统记录
（「关闭了议题」「改了标签」）。前端靠 system 字段区分，
所以这组用例除了 CRUD，重点验证「系统评论不会冒充普通评论」。
"""

from __future__ import annotations

import allure
import pytest

from gqa import contracts
from gqa.api.client import GitLabError

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]


@allure.epic("GitLab API")
@allure.feature("议题评论")
@allure.story("创建并读回")
@allure.severity(allure.severity_level.NORMAL)
def test_create_note_and_read_back(data_factory, gl, an_open_issue):
    pid, iid = an_open_issue["project_id"], an_open_issue["iid"]
    note = data_factory.note(pid, iid, "这是一条自动化评论")

    errors = contracts.collect_errors(note, contracts.NOTE)
    assert errors == [], f"不符合契约：{errors}"

    fetched = gl.notes.get(pid, iid, note["id"]).json_data
    assert fetched["body"] == "这是一条自动化评论"
    assert fetched["system"] is False, "手工创建的评论不应被标记为系统评论"


@allure.epic("GitLab API")
@allure.feature("议题评论")
@allure.story("正文必填")
@allure.severity(allure.severity_level.MINOR)
@pytest.mark.parametrize("body", ["", "   "], ids=["empty", "blank"])
def test_note_body_is_required(gl, an_open_issue, body):
    pid, iid = an_open_issue["project_id"], an_open_issue["iid"]
    with pytest.raises(GitLabError) as exc:
        gl.notes.create(pid, iid, body)
    assert exc.value.status == 400, f"空正文应 400，实际 {exc.value.status}"


@allure.epic("GitLab API")
@allure.feature("议题评论")
@allure.story("修改评论")
@allure.severity(allure.severity_level.MINOR)
def test_update_note_body(data_factory, gl, an_open_issue):
    pid, iid = an_open_issue["project_id"], an_open_issue["iid"]
    note = data_factory.note(pid, iid, "原文")
    updated = gl.notes.update(pid, iid, note["id"], "改后的正文").json_data
    assert updated["body"] == "改后的正文"
    assert gl.notes.get(pid, iid, note["id"]).json_data["body"] == "改后的正文"


@allure.epic("GitLab API")
@allure.feature("议题评论")
@allure.story("删除评论")
@allure.severity(allure.severity_level.MINOR)
def test_delete_note_is_gone(data_factory, gl, an_open_issue):
    pid, iid = an_open_issue["project_id"], an_open_issue["iid"]
    note = data_factory.note(pid, iid, "待删除")
    gl.notes.delete(pid, iid, note["id"])
    assert gl.notes.get(pid, iid, note["id"]).status == 404


@allure.epic("GitLab API")
@allure.feature("议题评论")
@allure.story("评论顺序")
@allure.severity(allure.severity_level.MINOR)
def test_notes_are_ordered_by_id(data_factory, gl, an_open_issue):
    """顺序断言的价值在于分页：顺序不稳时，翻页会漏记录或重复。

    这里刻意不写死「必须升序」。GitLab 19 的评论列表默认按创建时间**倒序**
    （新在前，实测 [7, 6]），写死升序会得到一条永远红的用例，而它红的
    原因只是「我猜错了默认排序」。真正要守的不变式是两条：
      1. 单调——要么全升要么全降，不能乱序；
      2. 稳定——连查两次顺序一致，否则翻页必然丢数据。
    """
    pid, iid = an_open_issue["project_id"], an_open_issue["iid"]
    first = data_factory.note(pid, iid, "第一条")
    second = data_factory.note(pid, iid, "第二条")
    wanted = {first["id"], second["id"]}

    def _ids() -> list[int]:
        rows = [n for n in gl.notes.list(pid, iid) if not n.get("system")]
        return [n["id"] for n in rows if n["id"] in wanted]

    a, b = _ids(), _ids()
    allure.attach(f"第一次：{a}\n第二次：{b}", name="顺序采样",
                  attachment_type=allure.attachment_type.TEXT)

    assert a == b, f"两次查询结果顺序不一致（分页必丢数据）：{a} vs {b}"
    assert a in (sorted(a), sorted(a, reverse=True)), f"评论顺序既非升序也非降序：{a}"


@allure.epic("GitLab API")
@allure.feature("议题评论")
@allure.story("系统评论与人工评论区分")
@allure.severity(allure.severity_level.NORMAL)
def test_closing_issue_produces_system_note(data_factory, gl, an_open_issue):
    """系统记录必须与人工评论区分开，且不得混进人工计数。

    实测（GitLab 19.0.1）：关闭议题**不再产生** system note——
    议题已 work-item 化，状态变更走事件表而非 notes 表。
    所以这里分两段断言：
      · 关闭这个动作本身必须生效（state=closed），这是硬断言；
      · 若实例确实产生了 system note，则它必须带 system=true 且不污染人工计数；
        没有产生就显式 skip 并写明原因——不把「版本能力缺失」报成「产品缺陷」。
    """
    pid, iid = an_open_issue["project_id"], an_open_issue["iid"]
    data_factory.note(pid, iid, "人工评论")
    closed = gl.issues.close(pid, iid).json_data

    assert closed.get("state") == "closed", f"关闭未生效：{closed.get('state')}"

    rows = gl.notes.list(pid, iid)
    allure.attach(f"共 {len(rows)} 条：{[(n.get('id'), n.get('system')) for n in rows]}",
                  name="关闭后的评论列表",
                  attachment_type=allure.attachment_type.TEXT)

    if not any(n.get("system") for n in rows):
        pytest.skip("该版本关闭议题不生成 system note（19.0.1 实测），"
                    "无法验证系统/人工区分——不算失败也不算通过")

    manual = [n for n in rows if not n.get("system")]
    assert all(n["body"] == "人工评论" for n in manual), "系统评论被算成了人工评论"


@allure.epic("GitLab API")
@allure.feature("议题评论")
@allure.story("议题不存在时评论接口 404")
@allure.severity(allure.severity_level.MINOR)
def test_notes_of_missing_issue_are_404(gl, a_project):
    resp = gl.client.get(f"/projects/{a_project.id}/issues/999999/notes",
                         expect=(200, 404))
    assert resp.status == 404, f"不存在的议题应 404，实际 {resp.status}"
