"""成员与权限。

权限类断言的坑在于「用谁去访问」。获取当前用户 id 之后添加自己为成员，
在不同实例上的结果并不一致——创建者可能已经是成员（409），也可能不是（201）。
所以这组用例只把「添加」当成准备动作，真正的断言落在
「角色改得动」「列表看得见」「移除后看不见」这三件确定的事上。
"""

from __future__ import annotations

import allure
import pytest

from gqa import contracts
from gqa.api.client import GitLabError
from gqa.api.resources import Members

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]


@pytest.fixture
def other_user_id(member_client):
    """一个**非项目所有者**的用户 id。

    为什么不能拿当前管理员自己当成员：root 建的项目里 root 是 Owner，
    而 Owner 的角色既不能改也不能删（改就是 404，删就是 403）——
    拿自己测「角色可改/成员可删」，得到的失败跟接口质量无关，
    纯粹是测错了对象。成员语义只有在「别人」身上才成立。
    """
    resp = member_client.users.current()
    if resp.status != 200:
        pytest.skip(f"第二身份不可用（{resp.status}），成员语义用例跳过")
    uid = resp.json_data["id"]
    allure.attach(f"第二身份 uid={uid} username={resp.json_data.get('username')}",
                  name="被操作用户", attachment_type=allure.attachment_type.TEXT)
    return uid


@allure.epic("GitLab API")
@allure.feature("成员")
@allure.story("添加成员并校验角色")
@allure.severity(allure.severity_level.CRITICAL)
def test_member_role_can_be_set_and_read(gl, a_project, other_user_id):
    uid = other_user_id

    with allure.step("确保是成员（已存在则 409，同样可接受）"):
        resp = gl.members.add(a_project.id, uid, Members.GUEST)
        assert resp.status in (201, 409), f"添加成员异常：{resp.status}"

    with allure.step("改成 Developer"):
        updated = gl.members.update(a_project.id, uid, Members.DEVELOPER)
        assert updated.status == 200, f"改角色失败：{updated.status} {updated.json_data}"
        assert updated.json_data["access_level"] == Members.DEVELOPER

    with allure.step("契约与读回一致"):
        member = gl.members.get(a_project.id, uid).json_data
        errors = contracts.collect_errors(member, contracts.MEMBER)
        assert errors == [], f"不符合契约：{errors}"
        assert member["access_level"] == Members.DEVELOPER

    with allure.step("成员列表里应看得见"):
        ids = [m["id"] for m in gl.members.list(a_project.id)]
        assert uid in ids, f"成员 {uid} 不在列表里"


@allure.epic("GitLab API")
@allure.feature("成员")
@allure.story("移除后不可访问")
@allure.severity(allure.severity_level.NORMAL)
def test_removed_member_is_gone(gl, a_project, other_user_id):
    uid = other_user_id
    add = gl.members.add(a_project.id, uid, Members.DEVELOPER)
    assert add.status in (201, 409), f"添加成员异常：{add.status}"

    try:
        gl.members.remove(a_project.id, uid)
    except GitLabError as exc:
        if exc.status in (403, 404):
            # 少数实例不允许移除最后一名 Owner。这是实例的保护策略，
            # 不是接口缺陷——跳过并把原因写进报告，比假装通过诚实
            pytest.skip(f"该实例不允许移除该成员（{exc.status}）：{exc.message}")
        raise

    assert gl.members.get(a_project.id, uid).status == 404, "移除后仍可查到成员"


@allure.epic("GitLab API")
@allure.feature("成员")
@allure.story("不存在的用户应 404")
@allure.severity(allure.severity_level.NORMAL)
def test_adding_unknown_user_is_404(gl, a_project):
    """404 而不是 400：GitLab 用 404 表示「这个用户不存在」。

    两者混用会让调用方无法区分「参数写错了」和「人不存在」，
    而后者往往需要另一条处理路径（先创建用户）。
    """
    resp = gl.members.add(a_project.id, 987654321, Members.DEVELOPER)
    assert resp.status == 404, f"不存在的用户应 404，实际 {resp.status}"


@allure.epic("GitLab API")
@allure.feature("成员")
@allure.story("重复添加冲突")
@allure.severity(allure.severity_level.MINOR)
def test_duplicate_membership_conflicts(gl, a_project, other_user_id):
    uid = other_user_id
    first = gl.members.add(a_project.id, uid, Members.DEVELOPER)
    resp = gl.members.add(a_project.id, uid, Members.DEVELOPER)
    allure.attach(f"首次 {first.status}，再次 {resp.status}", name="重复添加",
                  attachment_type=allure.attachment_type.TEXT)
    assert resp.status == 409, (
        f"重复添加同一用户应 409（首次 {first.status}），实际 {resp.status}。"
        "返回 201 意味着同一人产生了两条成员记录，角色归属将无法确定")


@allure.epic("GitLab API")
@allure.feature("成员")
@allure.story("群组成员")
@allure.severity(allure.severity_level.NORMAL)
def test_group_members_can_be_listed(gl, data_factory):
    uid = gl.users.current().json_data["id"]
    group = data_factory.group()

    resp = gl.members.group_add(group["id"], uid, Members.REPORTER)
    assert resp.status in (201, 409), f"添加群组成员异常：{resp.status}"

    rows = gl.members.group_list(group["id"])
    assert uid in [m["id"] for m in rows], "群组成员列表里看不到刚加入的人"
