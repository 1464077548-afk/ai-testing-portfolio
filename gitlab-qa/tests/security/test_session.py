"""令牌与会话语义：认证环节本身有没有漏洞。

这一组测的是「门」而不是「房间」：前面几组假设"认证是可靠的"，
然后去看门里面有没有越权。这里反过来——先确认这道门本身关得上。

三条最容易被写错的地方：
1. **只测不带凭据，不测带假凭据**。前者只能证明"没钥匙进不去"，
   证明不了"假钥匙也进不去"，而这两条在实现里是不同分支。
2. **只测 Header，不测 Query**。GitLab 历史上支持 `?private_token=` 传参，
   两种入口只要有一个被漏掉鉴权，另一个正常也等于没防住。
3. **Token 明文回显没人查**。响应里把 token 原样吐回来，
   日志、代理、浏览器历史各存一份，等于到处发钥匙。
"""

from __future__ import annotations

import json

import allure
import pytest

from gqa.security import scanner
from gqa.security.raw import raw_get

pytestmark = [pytest.mark.security, pytest.mark.requires_token]


@allure.epic("GitLab 安全")
@allure.feature("会话")
@allure.story("假 Token 在任何端点都被拒")
@allure.severity(allure.severity_level.BLOCKER)
@pytest.mark.parametrize("path", ["/user", "/projects", "/application/settings"])
def test_bogus_token_rejected_everywhere(target, path):
    """拿一个形似而实非的 Token 打三类端点：私有资源、列表、管理端点。

    覆盖三个而不是一个，是因为「认证中间件」在不同路由组上可能不是同一套：
    公开路由、普通路由、管理路由各走各的，少测一个就有一处可能没挂上。
    """
    resp = raw_get(target.base_url, f"/api/v4{path}",
                   headers={"PRIVATE-TOKEN": "glpat-" + "Zx9" * 8},
                   timeout=min(target.timeout, 10.0))
    assert resp.status in (401, 403), (
        f"假 Token 访问 {path} 返回 {resp.status}（预期 401/403）。"
        f"正文：{resp.body[:200]}"
    )


@allure.epic("GitLab 安全")
@allure.feature("会话")
@allure.story("空凭据头被拒而不是当匿名放行")
@allure.severity(allure.severity_level.CRITICAL)
def test_empty_token_header_is_rejected(target):
    """显式带上空值的 PRIVATE-TOKEN 头，必须被当成"凭据无效"而不是"没带"。

    这两种输入在实现里经常被合并处理：`if token:` 判断下空串等于没带，
    于是走匿名分支——如果匿名分支恰好放行，空凭据就成了后门。
    """
    resp = raw_get(target.base_url, "/api/v4/user",
                   headers={"PRIVATE-TOKEN": ""},
                   timeout=min(target.timeout, 10.0))
    assert resp.status in (401, 403), (
        f"空凭据头返回 {resp.status}：服务端把它当成了有效凭据或匿名放行。"
        f"正文：{resp.body[:200]}"
    )


@allure.epic("GitLab 安全")
@allure.feature("会话")
@allure.story("截断的真实 Token 无效")
@allure.severity(allure.severity_level.CRITICAL)
def test_truncated_token_is_rejected(target, settings):
    """真 Token 砍掉一半必须失效：证明校验的是完整值而不是"看着像"。

    这条能拦住一类很隐蔽的实现错误：只校验前缀（比如只认 `glpat-`），
    后面随便填什么都放行。冒烟层用的是完全随机的假 Token，
    抓不到"前缀正确但值不对"这种情况。
    """
    truncated = settings.token[: max(8, len(settings.token) // 2)]
    assert truncated != settings.token, "Token 太短，无法构造截断样本"

    resp = raw_get(target.base_url, "/api/v4/user",
                   headers={"PRIVATE-TOKEN": truncated},
                   timeout=min(target.timeout, 10.0))
    assert resp.status in (401, 403), (
        f"截断的 Token 竟然通过（HTTP {resp.status}）——"
        "服务端可能只校验了前缀或长度"
    )


@allure.epic("GitLab 安全")
@allure.feature("会话")
@allure.story("Token 不在响应正文里回显")
@allure.severity(allure.severity_level.CRITICAL)
def test_token_is_never_echoed_back(target, gl, settings):
    """任何响应正文里都不该出现本次使用的 Token 明文。

    为什么要查：日志采集、APM、反向代理日志普遍会存响应体，
    一旦回显，凭据就散落到一堆地方，而轮换 Token 时没人想得到要去清那些日志。
    """
    probe = settings.token[:12]   # 只比对前 12 位：够唯一，又不会把完整凭据写进断言
    checked: list[str] = []

    for path, params in (("/user", None), ("/projects", {"per_page": 5}),
                         ("/application/settings", None)):
        resp = gl.client.get(path, params=params, expect=(200, 401, 403))
        text = json.dumps(resp.json_data, ensure_ascii=False)
        checked.append(f"{path} → {resp.status}，含 token 片段：{probe in text}")

        assert probe not in text, (
            f"{path} 的响应里出现了本次 Token 的前 12 位——凭据被回显到响应体中"
        )

    allure.attach("\n".join(checked), name="回显检查",
                  attachment_type=allure.attachment_type.TEXT)


@allure.epic("GitLab 安全")
@allure.feature("会话")
@allure.story("401 响应不带堆栈")
@allure.severity(allure.severity_level.NORMAL)
def test_unauthorized_response_has_no_stacktrace(target):
    """鉴权失败的响应体必须干净。

    401 是最容易被"调试友好"化的一条路径——有人为了让接入方好排查，
    在错误体里塞上异常名和堆栈，于是每个扫接口的脚本都能拿到内部实现细节。
    """
    resp = raw_get(target.base_url, "/api/v4/user",
                   headers={"PRIVATE-TOKEN": "glpat-" + "Zx9" * 8},
                   timeout=min(target.timeout, 10.0))
    hits = (scanner.scan_fingerprints(resp.body, include_sql=True)
            + scanner.scan_internal_paths(resp.body))
    assert hits == [], f"401 响应里出现内部信息：{hits}"


@allure.epic("GitLab 安全")
@allure.feature("会话")
@allure.story("Header 与 Query 传参的认证结论一致")
@allure.severity(allure.severity_level.CRITICAL)
def test_query_param_token_agrees_with_header(target, settings):
    """`?private_token=` 与 Header 两种传参方式必须得出同一个认证结论。

    不一致意味着有一条旁路：比如 Header 走了校验、Query 参数被框架直接
    透传到某个内部调用里。这种问题在只测一种入口时永远发现不了。
    """
    by_header = raw_get(target.base_url, "/api/v4/user",
                        headers={"PRIVATE-TOKEN": settings.token},
                        timeout=min(target.timeout, 10.0))
    by_query = raw_get(
        target.base_url,
        f"/api/v4/user?private_token={settings.token}",
        timeout=min(target.timeout, 10.0),
    )

    allure.attach(
        f"Header 传参：{by_header.status}\nQuery 传参：{by_query.status}",
        name="两种传参对比", attachment_type=allure.attachment_type.TEXT,
    )

    # 结论必须同向：要么两边都认，要么两边都不认。
    # 一边 200 一边 401 说明其中一条链路的鉴权被漏掉了。
    assert (by_header.status == 200) == (by_query.status == 200), (
        f"两种传参的认证结论不一致：Header={by_header.status}，"
        f"Query={by_query.status}。其中一条链路可能没有走鉴权"
    )
