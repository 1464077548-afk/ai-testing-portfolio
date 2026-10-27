"""批量赋值（Mass Assignment）：客户端能不能改到不该改的字段。

原理：很多服务端直接把请求体塞进模型（`Model.new(params)`），
于是客户端多传一个字段就能改到本该由服务端决定的属性。
典型后果：自己给自己提权、伪造作者、篡改计数。

测试写法上有一个关键点：**只传不该生效的字段，并且断言它没生效**，
而不是传了之后看有没有报错。因为"字段被忽略"和"字段生效了"都不会报错，
只有回读比对才能分辨。

刻意不做的一项：用 `POST /users` 传 `admin: true` 验证提权。
那样一旦防护失效，会在实例上真的留下一个管理员账号——
收益（多一条用例）小于风险（留下一个真管理员）。
"""

from __future__ import annotations

import allure
import pytest

pytestmark = [pytest.mark.security, pytest.mark.requires_token]


@allure.epic("GitLab 安全")
@allure.feature("批量赋值")
@allure.story("客户端指定的项目 id 被忽略")
@allure.severity(allure.severity_level.CRITICAL)
def test_project_ignores_client_supplied_id(target, gl, data_factory):
    """请求体里带一个 id，服务端必须用自己的主键生成逻辑覆盖它。

    如果生效了，攻击者就能创建出 id 与已有资源冲突、或指向预留 id 的项目。
    """
    resp = gl.projects.create(f"{data_factory.prefix}-ma-id", id=987654321,
                              visibility="private", initialize_with_readme=False)
    data = resp.json_data

    allure.attach(f"请求 id=987654321，实际 id={data.get('id')}",
                  name="id 比对", attachment_type=allure.attachment_type.TEXT)
    assert data.get("id") != 987654321, (
        "服务端接受了客户端指定的项目 id——主键可被伪造"
    )


@allure.epic("GitLab 安全")
@allure.feature("批量赋值")
@allure.story("只读计数字段不可注入")
@allure.severity(allure.severity_level.NORMAL)
def test_project_ignores_read_only_counters(target, gl, data_factory):
    """star_count / forks_count / open_issues_count 是派生值，不接受写入。

    这类字段被写进去不会直接造成安全问题，但说明"请求体直接进了模型"——
    同一个入口上往往还挂着真正危险的字段，所以它是个**廉价的探针**。
    """
    resp = gl.projects.create(f"{data_factory.prefix}-ma-cnt",
                              star_count=999, forks_count=999,
                              open_issues_count=999,
                              visibility="private", initialize_with_readme=False)
    data = resp.json_data

    injected = {k: data.get(k) for k in ("star_count", "forks_count", "open_issues_count")}
    allure.attach(str(injected), name="计数字段回读",
                  attachment_type=allure.attachment_type.TEXT)

    assert all(v == 0 for v in injected.values()), (
        f"只读计数字段被写入了：{injected}——请求体可能被直接灌进模型"
    )


@allure.epic("GitLab 安全")
@allure.feature("批量赋值")
@allure.story("不能顶替已存在的议题编号")
@allure.severity(allure.severity_level.CRITICAL)
def test_issue_cannot_hijack_existing_iid(target, gl, a_project, data_factory):
    """指定一个已占用的 iid 去创建议题：必须被拒，绝不能顶掉原议题。

    这条用例的判据是实测换来的。第一版写的是"客户端传的 iid 必须被忽略"，
    结果红了——实际响应里 iid 就是我传的那个值。查下来是 GitLab 的有意设计：
    为了从 Jira / GitHub 迁移时能**保留原编号**，创建接口允许指定 iid。

    所以"能指定 iid"本身不是缺陷。真正要防的是另一件事：
    能不能用一个已存在的编号把别人的议题**顶掉**。
    在靠 "#12" 引用议题的工作流里，那等于篡改历史记录。

    教训：安全用例的判据要落在"会造成什么后果"上，
    而不是落在"服务端有没有接受这个字段"上。
    """
    first = data_factory.issue(a_project.id, title="原始议题-不该被顶替")
    occupied = first["iid"]

    resp = gl.client.post(
        f"/projects/{a_project.id}/issues",
        json_body={"title": "顶替者", "iid": occupied},
        expect=(201, 400, 401, 403, 404, 409, 422),
    )
    created = resp.json_data if isinstance(resp.json_data, dict) else {}

    # 回读原议题：内容必须还是原来的
    after = gl.issues.get(a_project.id, occupied)

    allure.attach(
        f"已占用 iid={occupied}\n"
        f"顶替请求：HTTP {resp.status}，返回 iid={created.get('iid')}\n"
        f"回读该编号的标题：{after.json_data.get('title')}",
        name="顶替尝试", attachment_type=allure.attachment_type.TEXT,
    )

    assert after.json_data.get("title") == "原始议题-不该被顶替", (
        f"编号 {occupied} 上的议题被顶替成了「{after.json_data.get('title')}」"
        "——客户端可以覆盖已有议题"
    )
    if resp.status == 201:
        # 被接受了也不能占用已存在的编号
        assert created.get("iid") != occupied, (
            f"顶替请求成功且占用了已存在的编号 {occupied}"
        )


@allure.epic("GitLab 安全")
@allure.feature("批量赋值")
@allure.story("作者与时间戳不可伪造")
@allure.severity(allure.severity_level.CRITICAL)
def test_issue_ignores_author_and_timestamps(target, gl, a_project, data_factory):
    """作者、创建时间、关闭时间不能由客户端指定。

    「伪造作者」是审计场景里最要命的一条：
    日志和审计记录都以这个字段为准，能改它就能把操作嫁祸给别人。
    """
    me = gl.users.current().json_data
    forged_time = "2000-01-01T00:00:00.000Z"

    resp = gl.issues.create(
        a_project.id,
        f"{data_factory.prefix}-ma-author",
        created_at=forged_time,
        updated_at=forged_time,
        closed_at=forged_time,
        state="closed",
    )
    data = resp.json_data

    author_id = (data.get("author") or {}).get("id")
    created = data.get("created_at")

    allure.attach(
        f"当前用户 id={me.get('id')}，议题 author.id={author_id}\n"
        f"伪造 created_at={forged_time}，实际={created}\n"
        f"state={data.get('state')}",
        name="作者与时间戳", attachment_type=allure.attachment_type.TEXT,
    )

    assert author_id == me.get("id"), (
        f"议题作者不是当前用户（{author_id} vs {me.get('id')}）——作者字段可被伪造"
    )
    assert created != forged_time, (
        f"创建时间被客户端指定成了 {forged_time}——时间戳可被伪造"
    )


@allure.epic("GitLab 安全")
@allure.feature("批量赋值")
@allure.story("普通用户不能给自己提权")
@allure.severity(allure.severity_level.BLOCKER)
def test_member_cannot_promote_self(target, member_client):
    """普通用户把自己改成管理员，必须失败。

    这是批量赋值里后果最严重的一条，也是唯一必须"真的去试"的：
    只读不看写，永远不会知道防护有没有生效。
    试完立刻回读确认——不能只看接口返回了 403，
    有些实现会返回 200 但悄悄忽略字段。
    """
    me = member_client.client.get("/user", expect=(200, 401)).json_data
    uid = me["id"]

    resp = member_client.client.put(
        f"/users/{uid}",
        json_body={"admin": True, "is_admin": True},
        expect=(200, 400, 401, 403, 404, 422),
    )

    after = member_client.client.get("/user", expect=(200, 401))
    still_admin = (after.json_data or {}).get("is_admin")

    allure.attach(
        f"PUT /users/{uid} → {resp.status}\n提权后 is_admin={still_admin}",
        name="提权尝试", attachment_type=allure.attachment_type.TEXT,
    )

    assert resp.status in (400, 401, 403, 404, 422), (
        f"普通用户修改自己账号返回 {resp.status}——提权请求没有被拒绝"
    )
    assert still_admin is not True, "提权生效了：普通用户变成了管理员"
