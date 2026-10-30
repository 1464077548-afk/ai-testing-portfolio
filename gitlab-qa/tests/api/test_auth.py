"""鉴权：测的是「拒绝得是否准确」，不只是「能不能访问」。

401 与 403 的分界在这里最有价值：
    401 = 我不知道你是谁
    403 = 我知道你是谁，但你不能做
很多实现把两者混成 401，前端就只能一律踢回登录页。
"""

from __future__ import annotations

import allure
import pytest

from gqa import contracts

pytestmark = [pytest.mark.api, pytest.mark.regression]


@allure.epic("GitLab API")
@allure.feature("鉴权")
@allure.story("有效 Token 可识别身份,返回当前用户信息")
@allure.severity(allure.severity_level.BLOCKER)
@pytest.mark.requires_token
def test_valid_token_identifies_user(gl):
    """有效 Token 可识别身份"""
    resp = gl.users.current()
    assert resp.status == 200, f"Token 无效：HTTP {resp.status}"

    errors = contracts.collect_errors(resp.json_data, contracts.USER)
    assert errors == [], f"用户结构不符合契约：{errors}"
    allure.attach(
        f"username={resp.json_data.get('username')}",
        name="当前用户",
        attachment_type=allure.attachment_type.TEXT,
    )


@allure.epic("GitLab API")
@allure.feature("鉴权")
@allure.story("无效 Token 被拒绝")
@allure.severity(allure.severity_level.CRITICAL)
def test_invalid_token_is_rejected(settings):
    """伪造的 Token 必须 401，不能因为「反正拿不到数据」就返回 200 空列表。"""
    from gqa.api.client import GitLabClient

    client = GitLabClient(settings.base_url, token="glpat-xxxxxxxxxxxxxxxxxxxx",
                          timeout=settings.timeout)
    try:
        resp = client.get("/user", expect=(200, 401))
        assert resp.status == 401, f"无效 Token 应当 401，实际 {resp.status}"

        if isinstance(resp.json_data, dict) and "message" in resp.json_data:
            errors = contracts.collect_errors(resp.json_data, contracts.ERROR)
            assert errors == [], f"错误体不符合契约：{errors}"
    finally:
        client.close()


@allure.epic("GitLab API")
@allure.feature("鉴权")
@allure.story("匿名访问受保护资源被拒绝")
@allure.severity(allure.severity_level.CRITICAL)
def test_anonymous_cannot_read_private_data(anon_client):
    """匿名访问 /user 必须被拒（401）。

    如果这里返回 200，说明鉴权中间件没生效——这是 P0 级的安全缺陷。
    """
    resp = anon_client.client.get("/user", expect=(200, 401))
    assert resp.status == 401, f"匿名访问受保护接口应 401，实际 {resp.status}"


@allure.epic("GitLab API")
@allure.feature("鉴权")
@allure.story("缺权限时是 403 而不是 401")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.requires_token
def test_forbidden_is_403_not_401(gl, member_client):
    """已认证但无权 → 403，而不是 401。

    两种状态码语义完全不同：401 是「服务端没认出你」，403 是「认出你了但不给你」。
    只有第二个身份才能验证这件事 —— 用管理员访问管理员端点永远 200，断言恒真，
    是典型的假绿。没配 GITLAB_MEMBER_TOKEN 时本用例 skip，不假装通过。

    探针用 /application/settings：真实存在、只读、仅管理员可访问。
    旧版用 /admin/users，那是 Web 路径不是 API 端点，管理员访问也返回 404
    ——在 mock 靶机上能过，在真实实例上必然失败。
    """
    with allure.step("管理员访问应放行"):
        admin_resp = gl.client.get("/application/settings", expect=(200, 401, 403))
        assert admin_resp.status == 200, (
            f"管理员访问应用设置拿到 {admin_resp.status}，鉴权配置可能有问题"
        )

    with allure.step("普通用户访问应被拒绝（403 而非 401）"):
        resp = member_client.client.get("/application/settings", expect=(200, 401, 403))
        assert resp.status == 403, (
            f"普通用户拿到 {resp.status}：403 才是权限拒绝；"
            f"401 说明服务端没识别出身份，200 说明越权成功（严重缺陷）"
        )
