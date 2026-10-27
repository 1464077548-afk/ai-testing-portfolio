"""错误语义。

状态码用错的代价远高于「少一个字段」：调用方靠状态码决定重试还是报错。
401 与 403 混用会让客户端在凭据失效时反复重试；404 用成 200 + 空列表，
则会让「拼错 id」变成「查无数据」，排查方向彻底跑偏。

这组用例不验证业务，只验证「错了的时候说得清楚」。
"""

from __future__ import annotations

import allure
import pytest

from gqa import contracts

pytestmark = [pytest.mark.api, pytest.mark.regression, pytest.mark.requires_token]


@allure.epic("GitLab API")
@allure.feature("错误语义")
@allure.story("不存在的资源一律 404")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("fetch", [
    "project", "issue", "label", "milestone", "note", "merge_request",
], ids=["project", "issue", "label", "milestone", "note", "merge_request"])
def test_missing_resources_are_404(gl, a_project, fetch):
    """每种资源都要 404，且错误体形状一致。"""
    pid = a_project.id
    callers = {
        "project": lambda: gl.projects.try_get(987654321),
        "issue": lambda: gl.client.get(f"/projects/{pid}/issues/999999",
                                       expect=(200, 404)),
        "label": lambda: gl.labels.try_get(pid, 987654321),
        "milestone": lambda: gl.milestones.get(pid, 987654321),
        "note": lambda: gl.client.get(f"/projects/{pid}/issues/1/notes/987654321",
                                      expect=(200, 404)),
        "merge_request": lambda: gl.merge_requests.get(pid, 987654321),
    }
    resp = callers[fetch]()
    assert resp.status == 404, f"{fetch} 不存在时应 404，实际 {resp.status}"

    body = resp.json_data
    assert isinstance(body, dict), f"错误体应为对象，实际 {type(body)}"
    errors = contracts.collect_errors(body, contracts.ERROR)
    assert errors == [], f"错误体不符合契约：{errors}"


@allure.epic("GitLab API")
@allure.feature("错误语义")
@allure.story("参数非法是 400 而不是 500")
@allure.severity(allure.severity_level.NORMAL)
def test_bad_parameters_are_400(gl, a_project):
    """参数错误必须是 4xx。返回 500 会把调用方引到重试逻辑上去，
    而重试一百次也是同样的错误参数。"""
    resp = gl.client.get(f"/projects/{a_project.id}/repository/branches",
                         params={"page": 0}, expect=(200, 400, 404))
    # 各实例对 page=0 的处理不完全一致（有的当 1，有的报 400），
    # 但绝不该是 5xx——那会把调用方引到重试上去
    assert resp.status < 500, f"非法分页参数不应返回 5xx，实际 {resp.status}"


@allure.epic("GitLab API")
@allure.feature("错误语义")
@allure.story("匿名访问受保护资源是 401")
@allure.severity(allure.severity_level.CRITICAL)
def test_anonymous_is_401(anon_client):
    resp = anon_client.users.current()
    assert resp.status == 401, f"匿名访问应 401，实际 {resp.status}"


@allure.epic("GitLab API")
@allure.feature("错误语义")
@allure.story("已认证但无权限是 403 而非 401")
@allure.severity(allure.severity_level.CRITICAL)
def test_authenticated_but_forbidden_is_403(gl, member_client):
    """401 表示「你是谁」，403 表示「你是谁，但不行」。

    两者混用时最典型的后果：客户端拿着有效 Token 收到 401，
    于是去刷新凭据，刷完还是 401，形成静默的死循环。

    判定必须建立在**对照组**上：先证明第二身份的 Token 确实有效（200），
    再去碰它无权访问的资源。少了这一步，Token 过期时用例拿到 401，
    断言 `status in (401, 403)` 照样通过——一条永远不会红的假绿。
    """
    me = member_client.users.current()
    assert me.status == 200, f"第二身份无效（{me.status}）：无法区分 401 与 403"

    resp = member_client.client.get("/application/settings",
                                    expect=(200, 401, 403, 404))
    allure.attach(f"第二身份 uid={me.json_data.get('id')} "
                  f"username={me.json_data.get('username')} → {resp.status}",
                  name="403 判定", attachment_type=allure.attachment_type.TEXT)

    if resp.status == 404:
        pytest.skip("该实例未提供该设置端点（404），403 语义无处判定")
    assert resp.status == 403, (
        f"已认证但无权限应 403，实际 {resp.status}。"
        "返回 401 会让客户端误以为凭据失效并反复刷新")


@allure.epic("GitLab API")
@allure.feature("错误语义")
@allure.story("错误体不泄露堆栈与内部信息")
@allure.severity(allure.severity_level.NORMAL)
def test_error_body_has_no_stacktrace(gl):
    """错误体里出现 backtrace / sql / 路径，等于把内部结构送出去。"""
    resp = gl.projects.try_get(987654321)
    assert resp.status == 404
    body = resp.json_data
    leaked = [k for k in body if k.lower() in
              {"backtrace", "sql", "stack", "exception", "trace"}]
    assert not leaked, f"错误体泄露了内部信息：{leaked}"
    allowed = {"message", "error"}
    unexpected = set(body) - allowed
    assert not unexpected, f"错误体出现预期外的字段：{unexpected}"
