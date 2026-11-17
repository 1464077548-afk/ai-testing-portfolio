"""请求层语义：请求体、内容类型、方法、路由。

这批用例验的不是"业务规则对不对"，而是**协议层怎么表达错误**。
它们的价值在于：客户端 SDK、网关、重试中间件都靠这些信号做决策。

    畸形 JSON   → 必须是 4xx 而不是 500（否则会被当成服务故障去重试）
    text/plain  → 必须是 415（明确说"我不认这个类型"，而不是默默解析失败）
    不支持的方法 → 实测是 404 而不是 405，这条差异必须写下来
    表单编码     → 实测被接受，意味着"必须发 JSON"是个假前提

期望值全部来自实测（GitLab 19.0.1 CE），探针见 tools/_probe_edge.py 的
「D 异常：请求体畸形」「D 异常：方法与路由」两节。
"""

from __future__ import annotations

import json

import allure
import pytest

from gqa.api.client import Response

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]


def _wrap(raw) -> Response:
    """把 requests 的原始响应套上项目里的 Response，断言写法保持一致。"""
    try:
        parsed = raw.json() if raw.content else None
    except ValueError:
        parsed = raw.text[:500]
    return Response(status=raw.status_code, headers=dict(raw.headers),
                    json_data=parsed, elapsed_ms=0, url=str(raw.url))


def _post_raw(gl, path: str, *, data: str, content_type: str) -> Response:
    """绕过客户端封装直接发原始请求体。

    不用 client.request 是因为它总会 json= 序列化，发不出"畸形 JSON"
    这种关键输入——而绝大多数 500 都出在解析失败的那条分支上。
    """
    raw = gl.client.session.post(
        f"{gl.client.api_url}{path}",
        data=data.encode("utf-8"),
        headers={"PRIVATE-TOKEN": gl.client.token,
                 "Content-Type": content_type},
        timeout=gl.client.timeout,
    )
    return _wrap(raw)


@allure.epic("GitLab API")
@allure.feature("请求语义")
@allure.story("畸形 JSON 请求体")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.parametrize("payload", ['{"name": ', '{"name": "x",}', '[]', 'null'],
                         ids=["truncated", "trailing-comma", "array", "null"])
def test_malformed_json_is_a_client_error(gl, payload):
    """解析不了的请求体必须是 4xx。

    500 的代价不只是这一条失败：调用方看到 5xx 会认为服务端抖动，
    于是自动重试，把一条本该立刻失败的坏请求重放三遍。

    实测：截断的 JSON 返回 400 `{"error": "Invalid JSON format"}`。
    注意它用的是 `error` 外壳，而字段校验错误用的是 `message` 外壳——
    同一个 API 两种错误体形状，解析方必须两边都认。这一点没有写成断言
    （那属于 API 设计风格，不是缺陷），但值得留在注释里。
    """
    resp = _post_raw(gl, "/projects", data=payload, content_type="application/json")

    assert not (500 <= resp.status < 600), (
        f"畸形请求体 {payload!r} 触发 {resp.status}：{str(resp.json_data)[:200]}")
    assert 400 <= resp.status < 500, (
        f"畸形请求体 {payload!r} 应被 4xx 拒绝，实际 {resp.status}："
        f"{str(resp.json_data)[:200]}")

    body = resp.json_data
    text = json.dumps(body, ensure_ascii=False).lower() if body else ""
    assert any(k in text for k in ("invalid json", "error", "message")), (
        f"4xx 响应没有说明原因：{body}")


@allure.epic("GitLab API")
@allure.feature("请求语义")
@allure.story("不支持的 Content-Type")
@allure.severity(allure.severity_level.NORMAL)
def test_unsupported_content_type_is_415(gl):
    """text/plain 必须得到 415，而不是"当成 JSON 试着解析"。

    415 和 400 对调用方的意义完全不同：415 说的是"接口不认这种格式"，
    客户端该换序列化方式；400 说的是"你的内容有问题"，客户端会去改内容。
    把 415 写成 400，会让人在错误的方向上排查很久。
    """
    resp = _post_raw(gl, "/projects", data='{"name": "ct-test"}',
                     content_type="text/plain")

    assert resp.status == 415, (
        f"text/plain 应返回 415，实际 {resp.status}：{str(resp.json_data)[:200]}")

    # 415 也不该在实例上留下任何东西
    leftovers = [p for p in gl.projects.list(search="ct-test")]
    assert leftovers == [], f"被 415 拒绝的请求居然建出了项目：{leftovers}"


@allure.epic("GitLab API")
@allure.feature("请求语义")
@allure.story("表单编码同样被接受")
@allure.severity(allure.severity_level.MINOR)
def test_form_encoded_body_is_accepted(gl):
    """实测：`application/x-www-form-urlencoded` 也能创建项目（201）。

    这条记录的是"必须发 JSON"这个前提并不成立。它的实际影响是：
    写客户端时若用 `requests.post(url, data=...)` 也不会报错，
    于是某些字段的类型（数组、布尔）会被悄悄变成字符串——
    例如 `labels: ["a","b"]` 走表单会变成 `"['a', 'b']"` 一个标签。

    所以这里只固定"表单能被接受"这一个事实，项目用完即删。
    """
    name = f"form-{abs(hash('form')) % 100000}"
    raw = gl.client.session.post(
        f"{gl.client.api_url}/projects",
        data={"name": name, "visibility": "private"},
        headers={"PRIVATE-TOKEN": gl.client.token},
        timeout=gl.client.timeout,
    )
    resp = _wrap(raw)

    assert resp.status == 201, (
        f"表单编码应被接受（实测 201），实际 {resp.status}：{str(resp.json_data)[:200]}")

    created = resp.json_data
    assert created["name"] == name
    gl.projects.purge(created["id"], created.get("path_with_namespace", ""))


@allure.epic("GitLab API")
@allure.feature("请求语义")
@allure.story("不支持的方法返回 404")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("method,path_tpl", [
    ("PATCH", "/projects/{pid}/issues/{iid}"),
    ("POST", "/projects/{pid}/issues/{iid}"),
    ("PUT", "/version"),
    ("DELETE", "/version"),
], ids=["patch-issue", "post-issue-detail", "put-version", "delete-version"])
def test_unsupported_method_is_404_not_405(gl, data_factory, a_project, method, path_tpl):
    """实测：方法不对时 GitLab 返回 **404**，不是 405。

    这和 RFC 的期望不一致（405 Method Not Allowed 才是标准答案），
    但它是这个 API 事实上的行为，必须固定下来，因为：

      - 客户端把 404 解释成"资源不存在"，会去检查 id 而不是检查方法；
      - 契约测试若按 405 写，会在真实实例上永远红。

    /version 是只读端点，任何写方法都不该生效——这一条与"方法不支持"
    是同一个判据：不得成功。
    """
    issue = data_factory.issue(a_project.id)
    path = path_tpl.format(pid=a_project.id, iid=issue["iid"])

    resp = gl.client.request(method, path, json_body={}, expect=None)

    assert resp.status == 404, (
        f"{method} {path} 实测应为 404，实际 {resp.status}：{str(resp.json_data)[:200]}")


@allure.epic("GitLab API")
@allure.feature("请求语义")
@allure.story("只读端点的写方法不生效")
@allure.severity(allure.severity_level.NORMAL)
def test_version_endpoint_rejects_writes_without_side_effect(gl):
    """对 /version 发 PUT：404 且版本号不变。

    只看状态码不够——万一它 404 了却顺手改了点什么，状态码完全看不出来。
    所以再读一次版本，确认没有被改动。
    """
    before = gl.system.version().json_data.get("version")
    resp = gl.client.put("/version", json_body={"version": "0.0.0"}, expect=None)
    after = gl.system.version().json_data.get("version")

    assert resp.status == 404
    assert after == before, f"只读端点被写入了：{before} → {after}"
