"""暴露面：测的是"不该开的东西有没有开着、该加的头有没有加"。

这类用例最容易写成"扫一遍就完"，价值在于**可回归**：
今天关了公开注册，三个月后有人为了拉人又打开，这条用例会红。

断言口径说明（避免假绿）：
  - 安全响应头：`X-Content-Type-Options` / `X-Frame-Options` 必须存在；
    `Referrer-Policy` 只记录不强断言——GitLab 各版本对 API 响应不一致，
    强断言会变成噪声。
  - HSTS：只在 https 目标上断言。本地 http 靶场没有 HSTS 是正常的。
  - 公开注册：默认要求关闭。这条红了就是**真发现**，不是用例写错了。
"""

from __future__ import annotations

import allure
import pytest

pytestmark = [pytest.mark.security, pytest.mark.requires_token]


@allure.epic("GitLab 安全")
@allure.feature("暴露面")
@allure.story("API 响应带基础安全头")
@allure.severity(allure.severity_level.NORMAL)
def test_api_response_has_security_headers(target, gl):
    resp = gl.client.get("/version", expect=(200, 401, 403))
    headers = {k.lower(): v for k, v in resp.headers.items()}

    allure.attach(
        "\n".join(f"{k}: {v}" for k, v in sorted(headers.items())),
        name="响应头",
        attachment_type=allure.attachment_type.TEXT,
    )

    assert headers.get("x-content-type-options", "").lower() == "nosniff", (
        "缺少 X-Content-Type-Options: nosniff —— 浏览器可能对响应做 MIME 嗅探，"
        f"把 JSON 当 HTML 执行。实际：{headers.get('x-content-type-options')!r}"
    )

    xfo = headers.get("x-frame-options", "").upper()
    assert xfo in ("DENY", "SAMEORIGIN"), (
        f"X-Frame-Options 缺失或取值不安全：{xfo!r}（允许被第三方 iframe 嵌套 → 点击劫持）"
    )

    if target.base_url.startswith("https://"):
        assert "strict-transport-security" in headers, (
            "https 目标缺少 HSTS，降级到 http 时无保护"
        )


@allure.epic("GitLab 安全")
@allure.feature("暴露面")
@allure.story("公开注册处于关闭状态")
@allure.severity(allure.severity_level.BLOCKER)
def test_public_signup_is_disabled(target, gl):
    """企业内网实例必须关闭自助注册——否则任何人都能拿到一个合法账号。

    这条用例红了，说明实例配置有问题，而不是用例写错了。
    修法（二选一）：
      界面：Admin → Settings → General → Sign-up restrictions → 取消 Sign-up enabled
      API ：PUT /application/settings?signup_enabled=false
    """
    resp = gl.client.get("/application/settings", expect=(200, 403))
    if resp.status == 403:
        pytest.skip("当前 Token 不是管理员，读不到实例设置")

    body = resp.json_data if isinstance(resp.json_data, dict) else {}
    assert "signup_enabled" in body, "响应里没有 signup_enabled 字段，接口结构可能变了"

    signup = bool(body.get("signup_enabled"))
    allure.attach(f"signup_enabled={signup}", name="实例设置",
                  attachment_type=allure.attachment_type.TEXT)

    assert signup is False, (
        "公开注册（signup_enabled）处于开启状态：任何人可自助注册账号。"
        "关闭方式：Admin → Settings → General → Sign-up restrictions，"
        "或 PUT /application/settings?signup_enabled=false"
    )


@allure.epic("GitLab 安全")
@allure.feature("暴露面")
@allure.story("匿名列表只返回公开项目")
@allure.severity(allure.severity_level.BLOCKER)
def test_anonymous_list_only_returns_public_projects(target, anon_client,
                                                     data_factory):
    """匿名能列项目本身不一定是问题，泄露私有项目才是。

    所以断言写的是"每一条都必须是 public"，而不是"匿名不能访问列表"——
    后者在默认配置下就会红，属于把产品行为当成缺陷。
    """
    data_factory.project(visibility="private")  # 造一个私有项目当诱饵

    resp = anon_client.client.get("/projects", params={"per_page": 50},
                                  expect=(200, 401, 403))
    if resp.status in (401, 403):
        allure.attach("匿名访问项目列表被拒绝（比默认配置更严格）", name="说明",
                      attachment_type=allure.attachment_type.TEXT)
        return

    items = resp.json_data if isinstance(resp.json_data, list) else []
    leaked = [p for p in items if p.get("visibility") != "public"]
    assert leaked == [], (
        "匿名列表里出现非公开项目，泄露了 "
        f"{len(leaked)} 个：{[p.get('path_with_namespace') for p in leaked][:5]}"
    )


@allure.epic("GitLab 安全")
@allure.feature("暴露面")
@allure.story("管理员端点匿名不可达")
@allure.severity(allure.severity_level.BLOCKER)
@pytest.mark.parametrize("path", [
    "/application/settings",
    "/application/statistics",
    "/users",
])
def test_admin_endpoints_reject_anonymous(target, anon_client, path):
    resp = anon_client.client.get(path, expect=(200, 401, 403, 404))
    assert resp.status in (401, 403, 404), (
        f"匿名访问 {path} 返回 {resp.status}，管理端点对外可达"
    )
