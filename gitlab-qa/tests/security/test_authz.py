"""越权（授权绕过）：测的是"别人能不能动我的东西"。

这一组是四类里最容易被写成假绿的。两个前提缺一不可：

1. **必须有第二个身份**。用管理员去访问管理员资源永远 200，断言恒真。
   没配 GITLAB_MEMBER_TOKEN 时这类用例 skip（根 conftest 的 member_client 已处理）。

2. **403 和 404 都要接受**。GitLab 对"无权限的私有资源"刻意返回 404 而不是 403，
   因为 403 会泄露"这个资源存在"。所以断言写 `in (403, 404)`，
   但**绝不能接受 200** —— 200 就是越权成功，是最严重的缺陷。

顺便覆盖"字段级越权"：端点放行了，但普通用户不该看到管理员的邮箱。
端点级都没问题的系统，字段级经常漏。
"""

from __future__ import annotations

import allure
import pytest

pytestmark = [pytest.mark.security, pytest.mark.requires_token]


@allure.epic("GitLab 安全")
@allure.feature("越权")
@allure.story("对照组：第二身份本身有效且不是管理员")
@allure.severity(allure.severity_level.BLOCKER)
def test_member_identity_is_valid(target, member_client):
    """这一条不测越权，测的是**下面所有越权用例的前提**。

    没有它，GITLAB_MEMBER_TOKEN 一旦过期，越权用例会拿到 401，
    而 401 在"允许的状态码"里 —— 全套越权用例照样绿。
    那是比不做测试更危险的假绿：报告上一片通过，实际一条都没验证。

    所以先证明：这个身份能用（200）、且确实不是管理员（is_admin=False）。
    """
    resp = member_client.client.get("/user", expect=(200, 401))
    assert resp.status == 200, (
        f"第二身份不可用（HTTP {resp.status}）。"
        "重签：python tools/ensure_member.py --rotate"
    )

    body = resp.json_data if isinstance(resp.json_data, dict) else {}
    # GitLab 只在管理员身上返回 is_admin: true，普通用户响应里根本没有这个字段。
    # 所以判据是「不能为 True」，而不是「必须为 False」——写成 is False 会误报。
    assert body.get("is_admin") is not True, (
        f"第二身份竟然是管理员（{body.get('username')}）——越权断言会恒真，失去意义"
    )
    assert body.get("username"), "响应里没有 username，无法确认身份"

    allure.attach(
        f"username={body.get('username')}\n"
        f"is_admin={body.get('is_admin', '（字段缺失 = 非管理员）')}",
        name="第二身份", attachment_type=allure.attachment_type.TEXT,
    )


@allure.epic("GitLab 安全")
@allure.feature("越权")
@allure.story("普通用户读不到管理员的私有项目")
@allure.severity(allure.severity_level.BLOCKER)
def test_member_cannot_read_admins_private_project(target, gl, member_client,
                                                   private_project):
    pid = private_project["id"]

    with allure.step("管理员自己读得到（确认资源确实存在）"):
        mine = gl.projects.get(pid)
        assert mine.status == 200, f"管理员读自己的项目拿到 {mine.status}"

    with allure.step("普通用户读应被拒绝（403 或 404，不能是 200）"):
        resp = member_client.client.get(f"/projects/{pid}",
                                        expect=(200, 401, 403, 404))
        assert resp.status in (401, 403, 404), (
            f"越权成功：普通用户读到了管理员的私有项目 {pid}（HTTP {resp.status}）。"
            f"响应片段：{str(resp.json_data)[:200]}"
        )
        # 404 是 GitLab 的刻意设计：不暴露资源存在性。
        # 但无论 403 还是 404，正文里都不该带出项目内容
        if resp.status == 200:
            allure.attach(str(resp.json_data)[:1000], name="越权响应",
                          attachment_type=allure.attachment_type.TEXT)


@allure.epic("GitLab 安全")
@allure.feature("越权")
@allure.story("普通用户删不掉管理员的项目")
@allure.severity(allure.severity_level.BLOCKER)
def test_member_cannot_delete_admins_project(target, gl, member_client,
                                             private_project):
    pid = private_project["id"]

    resp = member_client.client.delete(f"/projects/{pid}",
                                       expect=(200, 202, 204, 401, 403, 404))
    assert resp.status in (401, 403, 404), (
        f"越权成功：普通用户删掉了管理员的项目 {pid}（HTTP {resp.status}）"
    )

    with allure.step("确认项目仍在（防止删除真的生效了）"):
        still = gl.projects.try_get(pid)
        assert still.status == 200, "项目被普通用户删除了，这是严重缺陷"


@allure.epic("GitLab 安全")
@allure.feature("越权")
@allure.story("普通用户不能在别人的项目里写数据")
@allure.severity(allure.severity_level.CRITICAL)
def test_member_cannot_create_issue_in_admins_project(target, gl, member_client,
                                                      private_project):
    pid = private_project["id"]

    resp = member_client.client.post(
        f"/projects/{pid}/issues",
        json_body={"title": "越权写入测试", "description": "不应该被创建"},
        expect=(200, 201, 401, 403, 404, 422),
    )
    assert resp.status in (401, 403, 404), (
        f"越权成功：普通用户在管理员项目里创建了 issue（HTTP {resp.status}）"
    )

    with allure.step("确认没真的写进去"):
        issues = gl.issues.list(pid)
        assert not any(i.get("title") == "越权写入测试" for i in issues), (
            "越权写入的 issue 真的落库了"
        )


@allure.epic("GitLab 安全")
@allure.feature("越权")
@allure.story("匿名访问私有项目被拒")
@allure.severity(allure.severity_level.CRITICAL)
def test_anonymous_cannot_read_private_project(target, anon_client,
                                               private_project):
    pid = private_project["id"]
    resp = anon_client.client.get(f"/projects/{pid}", expect=(200, 401, 403, 404))
    assert resp.status in (401, 403, 404), (
        f"匿名访问私有项目返回 {resp.status}，资源对外可见"
    )


@allure.epic("GitLab 安全")
@allure.feature("越权")
@allure.story("普通用户看不到管理员的邮箱（字段级）")
@allure.severity(allure.severity_level.NORMAL)
def test_member_cannot_see_admin_email(target, gl, member_client):
    """端点级放行不等于字段安全。

    普通用户可以 GET /users/:id（公开档案），但不该拿到 email——
    GitLab 只给管理员返回该字段。这类"字段级越权"端点扫描器扫不出来，
    只有写断言才能拦住回归。
    """
    admin = gl.users.current().json_data
    admin_id = admin["id"]

    resp = member_client.client.get(f"/users/{admin_id}", expect=(200, 403, 404))
    if resp.status != 200:
        # 整个端点都拒绝，比只藏字段更严格，同样通过
        allure.attach(f"普通用户访问用户端点返回 {resp.status}（端点级拒绝，更严格）",
                      name="说明", attachment_type=allure.attachment_type.TEXT)
        return

    body = resp.json_data if isinstance(resp.json_data, dict) else {}
    assert "email" not in body, (
        f"普通用户能看到管理员邮箱（字段级越权）：{body.get('email')}"
    )
