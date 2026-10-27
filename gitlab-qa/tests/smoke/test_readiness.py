"""部署后即时验证：服务活着、鉴权开着、错误体正常。

冒烟层有两条硬纪律，写在最前面：

1. **不依赖 Token**。它是部署完第一个要跑的东西，
   如果连冒烟都要先配好认证，那部署完就没法立刻验证。

2. **判定口径"宽松但不空转"**。
   宽松是因为不同实例的公开策略不同（有的 /version 要认证，有的不要），
   把策略差异判成失败会让冒烟天天红、最后没人看；
   不空转是因为每条都必须有一个「真错了会红」的判据——
   状态码属于 (200/401/403/404) 就过，但 5xx 和连接失败必须红。

这组用例里最容易写错的是鉴权那几条：
用「假 Token 访问 /user 必须 401」而不是「真 Token 能用」——
后者在 Token 配错时会整片 skip，看起来风平浪静，实际什么都没验证。
"""

from __future__ import annotations

import json

import allure
import pytest

from gqa import contracts
from gqa.security import scanner

pytestmark = [pytest.mark.smoke, pytest.mark.api]


# ---------------------------------------------------------------- 探针
@allure.epic("GitLab 可用性")
@allure.feature("运维探针")
@allure.story("健康检查端点不报 5xx")
@allure.severity(allure.severity_level.BLOCKER)
@pytest.mark.parametrize("probe_path", ["/-/health", "/-/liveness", "/-/readiness"])
def test_health_probe_responds(probe, probe_path):
    """运维探针只要不是 5xx、不是连不上，就算服务活着。

    为什么允许 404：这些端点受实例配置影响（有的只对监控网段开放、
    有的版本没开 readiness）。404 是"这个探针没开"，不是"服务挂了"。
    5xx 和连接失败才是真故障——判据收敛在这两点上，才不会天天误报。
    """
    resp = probe(probe_path)

    allure.attach(
        f"路径：{probe_path}\n状态：{resp.status}\n正文前 200 字：{resp.body[:200]}",
        name="探针响应",
        attachment_type=allure.attachment_type.TEXT,
    )

    assert resp.status != 0, f"{probe_path} 连接失败，服务不可达"
    assert resp.status < 500, f"{probe_path} 返回 {resp.status}，服务异常"


@allure.epic("GitLab 可用性")
@allure.feature("运维探针")
@allure.story("API 入口存活")
@allure.severity(allure.severity_level.BLOCKER)
def test_api_entrypoint_is_alive(probe):
    """API 根命名空间必须能应答：200（公开）或 401（要认证）都算活着。"""
    resp = probe("/api/v4/version")

    assert resp.status in (200, 401, 403), (
        f"/api/v4/version 返回 {resp.status}——要么是服务没起来，"
        f"要么是反向代理把 /api 前缀吞了。正文：{resp.body[:200]}"
    )


@allure.epic("GitLab 可用性")
@allure.feature("运维探针")
@allure.story("Web 根路径可达")
@allure.severity(allure.severity_level.CRITICAL)
def test_web_root_is_reachable(probe):
    """网页端也要活着。

    只测 API 是不够的：Nginx 配错时常见「API 全通、页面全 502」，
    而用户报障的第一句话永远是"网页打不开"。
    """
    resp = probe("/")

    assert resp.status in (200, 301, 302, 401), (
        f"Web 根路径返回 {resp.status}。正文前 200 字：{resp.body[:200]}"
    )


# ---------------------------------------------------------------- 鉴权
@allure.epic("GitLab 可用性")
@allure.feature("鉴权")
@allure.story("无凭据读用户被拒")
@allure.severity(allure.severity_level.BLOCKER)
def test_missing_token_cannot_read_user(probe):
    """不带任何凭据读 /user，必须是 401。

    这条红的唯一可能是「鉴权被关了」——那是比服务宕机更严重的事故，
    因为它往往意味着有人为了图方便改了配置却没人发现。
    """
    resp = probe("/api/v4/user")
    assert resp.status == 401, (
        f"无凭据访问 /user 返回 {resp.status}（预期 401）。"
        f"若实例确实配置为匿名可读，需要显式确认这是有意为之。"
        f"正文：{resp.body[:200]}"
    )


@allure.epic("GitLab 可用性")
@allure.feature("鉴权")
@allure.story("假 Token 被拒绝")
@allure.severity(allure.severity_level.BLOCKER)
def test_bogus_token_is_rejected(bogus_client):
    """形似而实非的 Token 必须被拒：证明服务端**真的在校验 Token**。

    只测「无凭据被拒」还不够——那只能证明"没带不行"，
    证明不了"带了假的也不行"。这两者在实现上是两条不同的分支。
    """
    resp = bogus_client.get("/user", expect=(401, 403))
    assert resp.status in (401, 403), (
        f"假 Token 竟然通过了鉴权（HTTP {resp.status}）——服务端没有校验 Token 有效性"
    )


@allure.epic("GitLab 可用性")
@allure.feature("鉴权")
@allure.story("畸形凭据被拒绝")
@allure.severity(allure.severity_level.CRITICAL)
def test_malformed_token_is_rejected(probe):
    """把 OAuth 的 `Bearer` 前缀塞进 PRIVATE-TOKEN 头，必须被拒。

    这类"用错认证方式"的请求最容易撞上实现里的兜底分支：
    有的服务端遇到不认识的 scheme 会走「当作没有」而不是「当作无效」，
    两者的差别就是「默认拒绝」和「默认放行」。
    """
    resp = probe("/api/v4/user", headers={"PRIVATE-TOKEN": "Bearer glpat-not-a-real-token"})
    assert resp.status in (401, 403), (
        f"畸形凭据返回 {resp.status}（预期 401/403）。正文：{resp.body[:200]}"
    )


@allure.epic("GitLab 可用性")
@allure.feature("鉴权")
@allure.story("匿名写操作被拒")
@allure.severity(allure.severity_level.BLOCKER)
def test_anonymous_write_is_rejected(anon_client, settings):
    """匿名创建项目必须失败。

    「读可以不鉴权」是产品策略，「写可以不鉴权」是缺陷。
    这条用例是这两者的分界线，不能合并进上一条。
    """
    resp = anon_client.client.post(
        "/projects",
        json_body={"name": f"{settings.resource_prefix}-anon-write-should-fail"},
        expect=(401, 403, 405, 422),
    )
    assert resp.status != 201, "匿名请求竟然创建成功了项目——写接口完全没有鉴权"


# ---------------------------------------------------------------- 错误语义
@allure.epic("GitLab 可用性")
@allure.feature("错误语义")
@allure.story("未知端点返回标准错误信封")
@allure.severity(allure.severity_level.NORMAL)
def test_unknown_endpoint_returns_error_envelope(anon_client):
    """访问不存在的端点：404 + 形如 {"message": ...} 的信封。

    断言信封而不只是状态码，是因为客户端代码普遍依赖 `message` 字段——
    哪天服务端改成 {"error": ...} 或返回 HTML 错误页，
    状态码还是 404，但调用方会全线炸在解析上。
    """
    resp = anon_client.client.get("/qa-no-such-endpoint-xyz", expect=404)
    body = resp.json_data

    assert isinstance(body, dict), f"错误响应不是 JSON 对象：{type(body)}"
    assert "message" in body or "error" in body, (
        f"错误响应里既没有 message 也没有 error，客户端无法取错误信息：{body}"
    )

    errors = contracts.collect_errors(body, contracts.ERROR)
    assert errors == [], f"错误响应不符合 ERROR 契约：{errors}"


@allure.epic("GitLab 可用性")
@allure.feature("错误语义")
@allure.story("错误响应不吐堆栈")
@allure.severity(allure.severity_level.CRITICAL)
def test_error_body_has_no_stacktrace(anon_client):
    """错误响应里不能出现堆栈 / SQL / 内部路径。

    这是「信息泄露」的冒烟版：不用专门的扫描器，
    只看最常见的 401/404 有没有把内部实现吐出来。
    """
    bodies: list[str] = []
    for path in ("/qa-no-such-endpoint-xyz", "/user"):
        resp = anon_client.client.get(path, expect=(401, 404))
        bodies.append(json.dumps(resp.json_data, ensure_ascii=False))

    text = "\n".join(bodies)
    hits = scanner.scan_fingerprints(text, include_sql=True) + \
        scanner.scan_internal_paths(text)

    allure.attach(text[:2000], name="错误响应正文",
                  attachment_type=allure.attachment_type.TEXT)
    assert hits == [], f"错误响应里出现了内部信息：{hits}"


# ---------------------------------------------------------------- 一致性
@allure.epic("GitLab 可用性")
@allure.feature("版本一致性")
@allure.story("/version 与 /metadata 报同一个版本")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.requires_token
def test_version_matches_metadata(gl):
    """两个版本接口必须报同一个版本号。

    升级过程中（尤其是灰度 / 多节点）这两个接口来自不同的数据源，
    对不上说明有节点没升上去，而这种问题在功能测试里完全看不出来。
    """
    ver = gl.system.version()
    meta = gl.system.metadata()

    v1 = (ver.json_data or {}).get("version")
    v2 = (meta.json_data or {}).get("version")

    allure.attach(f"/version={v1}\n/metadata={v2}", name="版本对比",
                  attachment_type=allure.attachment_type.TEXT)

    assert v1, "/version 响应里没有 version 字段"
    assert v2, "/metadata 响应里没有 version 字段"
    assert v1 == v2, f"两个接口报的版本不一致：{v1} vs {v2}（可能有节点未升级）"
