"""数据可见范围（横向越权）：别人的数据会不会出现在我的视野里。

和 test_authz.py 的分工：

    test_authz.py      —— 直接点名访问（我知道 id，能不能拿到）
    test_data_scope.py —— 不点名，从列表 / 搜索 / 统计里"漏"出来

后者更贴近真实泄露：攻击者往往拿不到确切 id，
但列表接口、全局搜索、导出功能会把私有数据整批带出来。
这一类缺陷在"点名访问"的测试里永远是绿的。

另一条纪律同 authz：**403 和 404 都接受，200 一律不接受**。
但对列表类接口要反过来判——"列表里没有它"才是正确结果。
"""

from __future__ import annotations

import allure
import pytest

pytestmark = [pytest.mark.security, pytest.mark.requires_token]


@allure.epic("GitLab 安全")
@allure.feature("数据范围")
@allure.story("普通用户的项目列表里没有管理员的私有项目")
@allure.severity(allure.severity_level.BLOCKER)
def test_member_project_list_excludes_admins_private_project(target, gl, member_client,
                                                             private_project):
    """列全部项目，私有项目不能出现在普通用户的列表里。

    这是「批量泄露」最典型的形态：单个详情接口都守得住，
    列表接口按 visible_to_user 过滤时漏了一个条件就全漏了。
    """
    pid = private_project["id"]
    name = private_project.get("path_with_namespace") or str(pid)

    with allure.step("管理员自己能看到（确认数据存在）"):
        mine = [p["id"] for p in gl.projects.list()]
        assert pid in mine, "管理员看不到自己刚建的项目，用例前提不成立"

    with allure.step("普通用户列表里不该有它"):
        theirs = member_client.projects.list()
        leaked = [p for p in theirs if p["id"] == pid]

        allure.attach(
            f"私有项目：{name}（id={pid}）\n"
            f"普通用户可见项目数：{len(theirs)}\n"
            f"是否泄露：{bool(leaked)}",
            name="列表比对", attachment_type=allure.attachment_type.TEXT,
        )
        assert leaked == [], f"私有项目 {name} 出现在普通用户的项目列表里——批量泄露"


@allure.epic("GitLab 安全")
@allure.feature("数据范围")
@allure.story("全局搜索不命中管理员的私有项目")
@allure.severity(allure.severity_level.BLOCKER)
def test_member_search_misses_private_project(target, member_client, private_project):
    """搜索是另一个"批量泄露"的高发区。

    搜索往往走独立索引，权限过滤和列表接口不是同一套代码，
    列表守住了、搜索没守住的情况非常常见（真实事故里的常客）。
    """
    term = private_project.get("name") or private_project.get("path") or ""
    assert term, "拿不到项目名，无法构造搜索词"

    resp = member_client.search.run("projects", term)
    hits = resp.json_data if isinstance(resp.json_data, list) else []
    leaked = [h for h in hits if h.get("id") == private_project["id"]]

    allure.attach(
        f"搜索词：{term}\n命中数：{len(hits)}\n命中私有项目：{bool(leaked)}",
        name="搜索比对", attachment_type=allure.attachment_type.TEXT,
    )
    assert leaked == [], f"全局搜索命中了私有项目 {term}——搜索未做权限过滤"


@allure.epic("GitLab 安全")
@allure.feature("数据范围")
@allure.story("普通用户读不到私有议题")
@allure.severity(allure.severity_level.CRITICAL)
def test_member_cannot_read_private_issue(target, gl, member_client,
                                          private_project, data_factory):
    """私有项目里的议题，普通用户点名访问也要被拒。"""
    issue = data_factory.issue(private_project["id"], description="越权测试用")

    with allure.step("管理员读得到（确认数据存在）"):
        ok = gl.issues.get(private_project["id"], issue["iid"])
        assert ok.status == 200, "管理员读不到自己的议题，用例前提不成立"

    with allure.step("普通用户应被拒"):
        resp = member_client.client.get(
            f"/projects/{private_project['id']}/issues/{issue['iid']}",
            expect=(200, 401, 403, 404),
        )
        assert resp.status in (401, 403, 404), (
            f"越权成功：普通用户读到了私有议题（HTTP {resp.status}）"
        )


@allure.epic("GitLab 安全")
@allure.feature("数据范围")
@allure.story("普通用户不能在私有议题下留言")
@allure.severity(allure.severity_level.CRITICAL)
def test_member_cannot_comment_on_private_issue(target, gl, member_client,
                                                private_project, data_factory):
    """写权限比读权限更容易漏：读可能被缓存挡住，写一定落到业务逻辑。"""
    issue = data_factory.issue(private_project["id"], description="越权写入测试")

    resp = member_client.client.post(
        f"/projects/{private_project['id']}/issues/{issue['iid']}/notes",
        json_body={"body": "越权留言测试"},
        expect=(200, 201, 401, 403, 404, 422),
    )
    assert resp.status in (401, 403, 404), (
        f"越权成功：普通用户在私有议题下留言成功（HTTP {resp.status}）"
    )

    with allure.step("确认没真的写进去"):
        notes = gl.notes.list(private_project["id"], issue["iid"])
        assert not any("越权留言测试" in (n.get("body") or "") for n in notes), (
            "越权留言真的落库了"
        )


@allure.epic("GitLab 安全")
@allure.feature("数据范围")
@allure.story("普通用户不能把自己加成项目成员")
@allure.severity(allure.severity_level.BLOCKER)
def test_member_cannot_add_self_to_project(target, gl, member_client, private_project):
    """把自己加进别人的项目 = 一次完整的权限提升。

    这条比"读不到"更严重：成功一次，后面所有越权断言都会失效，
    因为那个身份从此就是合法成员了。
    """
    me = member_client.client.get("/user", expect=(200, 401)).json_data

    resp = member_client.client.post(
        f"/projects/{private_project['id']}/members",
        json_body={"user_id": me["id"], "access_level": 40},   # 40 = Maintainer
        expect=(200, 201, 400, 401, 403, 404, 409, 422),
    )
    assert resp.status != 201, (
        f"越权成功：普通用户把自己加成了项目成员（HTTP {resp.status}）"
    )

    with allure.step("确认成员列表没变"):
        members = gl.members.list(private_project["id"])
        assert not any(m.get("id") == me["id"] for m in members), (
            f"用户 {me.get('username')} 真的被加进了项目成员"
        )


@allure.epic("GitLab 安全")
@allure.feature("数据范围")
@allure.story("普通用户改不了实例级设置")
@allure.severity(allure.severity_level.BLOCKER)
def test_member_cannot_change_application_settings(target, gl, member_client):
    """改实例设置是最高权限操作，普通 Token 必须被拒。

    而且不能只看返回码——有些实现返回 200 但什么都没改。
    所以改之前先读一份，改之后再读一次做比对。
    """
    before = gl.client.get("/application/settings", expect=(200, 403))
    if before.status == 403:
        pytest.skip("当前管理员 Token 读不到实例设置，无法做前后比对")

    resp = member_client.client.put(
        "/application/settings",
        json_body={"signup_enabled": True},
        expect=(200, 400, 401, 403, 404, 422),
    )

    after = gl.client.get("/application/settings", expect=(200, 403))
    before_signup = (before.json_data or {}).get("signup_enabled")
    after_signup = (after.json_data or {}).get("signup_enabled")

    allure.attach(
        f"PUT /application/settings → {resp.status}\n"
        f"signup_enabled：{before_signup} → {after_signup}",
        name="设置改动比对", attachment_type=allure.attachment_type.TEXT,
    )

    assert resp.status in (400, 401, 403, 404, 422), (
        f"普通用户修改实例设置返回 {resp.status}——管理端点未做权限校验"
    )
    assert before_signup == after_signup, (
        f"实例设置被改动了：signup_enabled {before_signup} → {after_signup}"
    )
