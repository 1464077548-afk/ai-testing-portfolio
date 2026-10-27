"""传输层与浏览器侧安全头。

API 安全头（nosniff / X-Frame-Options）在 exposure 那组已经测过，
这里补的是**Web 页面**这一侧——用户真正用浏览器访问的部分：

- Cookie 属性：HttpOnly / Secure / SameSite。API 鉴权靠 Token，
  浏览器会话靠 Cookie，两者是两套凭据，只测一套等于只锁了一扇门。
- CORS：跨域配置写宽了，任何一个恶意页面都能带着用户的登录态读数据。
- 危险 HTTP 方法：TRACE 之类的加固项，很多加固清单第一条就是它。

判定口径沿用例一贯原则：**弱项只记录，强项才断言**。
比如 CSP，GitLab 各版本差异很大，强断言会让用例天天红；
而 Cookie 的 HttpOnly 是"有没有"的二选一，没有就该红。
"""

from __future__ import annotations

import allure
import pytest

from gqa.security.raw import raw_get, raw_request

pytestmark = [pytest.mark.security]


def _set_cookies(headers: dict[str, str]) -> list[str]:
    """取所有 Set-Cookie 头（http.client 会把重复的头合并成一个逗号串，需要拆开）。"""
    raw = headers.get("set-cookie", "")
    if not raw:
        return []
    # 简单可靠的拆法：按 ", " + 下一个 cookie 名 的模式切
    parts: list[str] = []
    current = ""
    for chunk in raw.split(","):
        if "=" in chunk.split(";")[0] and current:
            parts.append(current.strip())
            current = chunk
        else:
            current = (current + "," + chunk) if current else chunk
    if current:
        parts.append(current.strip())
    return parts


@allure.epic("GitLab 安全")
@allure.feature("Cookie")
@allure.story("会话 Cookie 带 HttpOnly")
@allure.severity(allure.severity_level.CRITICAL)
def test_session_cookie_is_httponly(target):
    """会话 Cookie 必须 HttpOnly：否则一段 XSS 就能把登录态偷走。

    这一条是「XSS 能不能升级为会话劫持」的分水岭，
    所以即便注入类用例全绿，这条红了也要立刻修。
    """
    resp = raw_get(target.base_url, "/users/sign_in",
                   timeout=min(target.timeout, 10.0))
    cookies = _set_cookies(resp.headers)

    allure.attach("\n\n".join(cookies) or "（响应里没有 Set-Cookie）",
                  name="Set-Cookie", attachment_type=allure.attachment_type.TEXT)

    if not cookies:
        pytest.skip("登录页没有下发 Cookie（可能已被重定向或被代理剥离）")

    session_cookies = [c for c in cookies if "session" in c.lower()] or cookies
    for c in session_cookies:
        assert "httponly" in c.lower(), (
            f"Cookie 缺少 HttpOnly：{c.split(';')[0]}…"
            "——XSS 可直接读取该 Cookie，会话会被劫持"
        )


@allure.epic("GitLab 安全")
@allure.feature("Cookie")
@allure.story("https 下 Cookie 带 Secure")
@allure.severity(allure.severity_level.CRITICAL)
def test_session_cookie_is_secure_over_https(target):
    """只在 https 目标上断言 Secure。

    http 的本地靶场没有 Secure 是正常的，强断言会把"环境差异"
    伪装成"安全缺陷"，这正是安全用例失去信任的典型方式。
    """
    if not target.base_url.startswith("https://"):
        pytest.skip("目标是 http（本地靶场），Secure 属性不适用")

    resp = raw_get(target.base_url, "/users/sign_in",
                   timeout=min(target.timeout, 10.0))
    cookies = _set_cookies(resp.headers)
    if not cookies:
        pytest.skip("登录页没有下发 Cookie")

    session_cookies = [c for c in cookies if "session" in c.lower()] or cookies
    for c in session_cookies:
        assert "secure" in c.lower(), f"https 下 Cookie 缺少 Secure：{c.split(';')[0]}…"


@allure.epic("GitLab 安全")
@allure.feature("Cookie")
@allure.story("Cookie 声明了 SameSite")
@allure.severity(allure.severity_level.NORMAL)
def test_session_cookie_declares_samesite(target):
    """SameSite 只记录不强制断言：各版本默认值不同（有的靠浏览器默认 Lax）。

    记录进报告的意义在于：一旦要排查 CSRF 类问题，
    这里能直接看到实例当前的取值，不用再去抓包。
    """
    resp = raw_get(target.base_url, "/users/sign_in",
                   timeout=min(target.timeout, 10.0))
    cookies = _set_cookies(resp.headers)
    if not cookies:
        pytest.skip("登录页没有下发 Cookie")

    summary = []
    for c in cookies:
        name = c.split(";")[0]
        samesite = next((p.strip() for p in c.split(";")
                         if "samesite" in p.lower()), "（未声明，取浏览器默认）")
        summary.append(f"{name} → {samesite}")

    allure.attach("\n".join(summary), name="SameSite 现状",
                  attachment_type=allure.attachment_type.TEXT)


@allure.epic("GitLab 安全")
@allure.feature("CORS")
@allure.story("不回显任意 Origin、不通配 + 凭据")
@allure.severity(allure.severity_level.BLOCKER)
def test_cors_has_no_dangerous_combination(target):
    """CORS 的判据是「组合」，不是单个头。

    这里踩过一次坑，值得写下来：GitLab 的 API 默认返回
    `Access-Control-Allow-Origin: *`，第一版的用例直接断言 `!= "*"`，
    于是**每条运行都红**。但那是 GitLab 的既定设计——
    API 用 `PRIVATE-TOKEN` 头认证，不靠 Cookie，
    浏览器在没有 `Allow-Credentials` 时不会附带任何凭据，
    所以 `*` 不带凭据是安全的。

    真正危险的是两种组合：
      1. `*` 或回显请求方的 Origin，**同时** `Allow-Credentials: true`
         —— 等价于对任意站点开放带登录态的跨域读取；
      2. 回显请求方的 Origin（origin reflection）——
         说明服务端把输入原样带回头里，往往还带着别的注入面。

    把判据从"单头"改成"组合"，用例才既不会天天假红，
    又能在真的配错时红得有说服力。
    """
    evil = "https://evil.example.com"
    resp = raw_get(target.base_url, "/api/v4/version",
                   headers={"Origin": evil},
                   timeout=min(target.timeout, 10.0))

    acao = (resp.headers.get("access-control-allow-origin") or "").strip()
    acac = (resp.headers.get("access-control-allow-credentials") or "").strip().lower()

    allure.attach(
        f"Origin: {evil}\n"
        f"Access-Control-Allow-Origin: {acao or '（无）'}\n"
        f"Access-Control-Allow-Credentials: {acac or '（无）'}",
        name="CORS 响应头", attachment_type=allure.attachment_type.TEXT,
    )

    # 1) 不回显请求方的 Origin
    assert acao != evil, (
        f"CORS 回显了请求方的 Origin（{evil}）——服务端把输入原样带回响应头"
    )
    # 2) 通配符或回显，都不能同时允许携带凭据
    if acac == "true":
        assert acao not in ("*", evil), (
            f"危险组合：Allow-Origin={acao} 且 Allow-Credentials=true，"
            "任意站点都能带着用户登录态跨域读取该实例的数据"
        )


@allure.epic("GitLab 安全")
@allure.feature("HTTP 方法")
@allure.story("非常规方法被拒绝")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("method", ["TRACE", "TRACK"])
def test_dangerous_http_methods_are_rejected(target, method):
    """TRACE / TRACK 应当被拒绝（405 / 403 / 400 都可接受）。

    用 raw 发送器而不是 requests：后者对非常规方法的处理本身就有差异，
    测不到"服务端到底放不放行"。
    另外这两个方法常用于跨站追踪（XST），是加固清单里的常客。
    """
    resp = raw_request(target.base_url, method, "/api/v4/version",
                       timeout=min(target.timeout, 10.0))
    assert resp.status in (400, 403, 405, 501), (
        f"{method} /api/v4/version 返回 {resp.status}——服务端放行了非常规方法"
    )
