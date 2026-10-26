"""冒烟：只关心「服务活着、基本契约没破」。

这一层的特点是**不依赖 Token**，因为它是部署后第一个要跑的东西——
如果连冒烟都要先配好认证，那部署完就没法立刻验证。

判定口径刻意宽松：只要不是 5xx 就算过。
冒烟的职责是「发现完全不可用」，不是「发现质量下降」。
"""

from __future__ import annotations

import time

import allure
import pytest

from gqa import contracts

pytestmark = [pytest.mark.smoke, pytest.mark.api]

#@allure.epic / feature / story / severity 放在测试函数或测试类上，用来给报告做分层分类
#allure.attach(...) 通常写在某个 with allure.step(...) 里面，用来挂响应体、日志、JSON、截图等。
# attachment_type 可以选 TEXT、JSON、HTML、PNG 等，默认 TEXT
@allure.epic("GitLab 可用性")
@allure.feature("系统健康")
@allure.story("服务响应正常")
@allure.severity(allure.severity_level.BLOCKER)
def test_service_is_reachable(anon_client, settings):
    """服务必须在超时时间内给出非 5xx 响应。"""
    started = time.perf_counter()
    resp = anon_client.client.get("/projects", params={"per_page": 1}, expect=(200, 401, 403))
    elapsed = (time.perf_counter() - started) * 1000

    with allure.step("断言响应可用"):
        assert resp.status in (200, 401, 403), f"服务异常，状态码 {resp.status}"
    with allure.step("断言响应及时"):
        assert elapsed < settings.timeout * 1000, f"响应耗时 {elapsed:.0f}ms，超过阈值"

    allure.attach(
        f"URL: {settings.base_url}\n状态码: {resp.status}\n耗时: {elapsed:.0f} ms",
        name="健康检查结果",
        attachment_type=allure.attachment_type.TEXT,
    )


@allure.epic("GitLab 可用性")
@allure.feature("系统健康")
@allure.story("版本接口可用")
@allure.severity(allure.severity_level.CRITICAL)
def test_version_endpoint_responds(anon_client):
    """版本接口：200（公开）或 401（需要认证）都算正常，5xx 才是故障。

    自建 GitLab CE 默认公开此接口；gitlab.com 等公共实例会要求认证。
    这里不把 401 判为失败——它是鉴权策略，不是服务故障。
    """
    resp = anon_client.client.get("/version", expect=(200, 401, 403))
    assert resp.status != 0, "连接失败，服务不可达"

    if resp.status == 200:
        errors = contracts.collect_errors(resp.json_data, contracts.VERSION)
        assert errors == [], f"版本响应不符合契约：{errors}"
        allure.attach(
            f"version={resp.json_data['version']}\nrevision={resp.json_data['revision']}",
            name="版本信息",
            attachment_type=allure.attachment_type.TEXT,
        )
    else:
        pytest.skip(f"该实例的版本接口需要认证（HTTP {resp.status}），跳过结构校验")


@allure.epic("GitLab 可用性")
@allure.feature("系统健康")
@allure.story("项目列表接口可用")
@allure.severity(allure.severity_level.CRITICAL)
def test_public_projects_list(anon_client):
    """列出可见项目。匿名时返回公开项目，这本身就验证了鉴权边界没被写错。"""
    resp = anon_client.client.get("/projects", params={"per_page": 5}, expect=(200, 401, 403))
    assert resp.status == 200, f"项目列表不可访问：{resp.status}"

    items = resp.json_data
    assert isinstance(items, list), f"预期列表，实际 {type(items)}"

    if items:
        errors = contracts.collect_errors(items[0], contracts.PROJECT)
        assert errors == [], f"项目结构不符合契约：{errors}"
        allure.attach(
            "\n".join(f"- {p.get('path_with_namespace') or p.get('name')}" for p in items[:5]),
            name="可见项目",
            attachment_type=allure.attachment_type.TEXT,
        )


@allure.epic("GitLab 可用性")
@allure.feature("系统健康")
@allure.story("分页元信息正确")
@allure.severity(allure.severity_level.NORMAL)
def test_pagination_headers(anon_client):
    """分页头必须自洽：X-Per-Page 要等于请求值。

    这个断言拦的是「服务端忽略了 per_page 参数」这类缺陷——
    正文看起来正常，只有头信息对不上。
    """
    resp = anon_client.client.get("/projects", params={"per_page": 5}, expect=200)
    headers = {k.lower(): v for k, v in resp.headers.items()}

    if "x-per-page" in headers:
        assert int(headers["x-per-page"]) == 5, (
            f"请求的 per_page=5，服务端回显 {headers['x-per-page']}"
        )
        assert len(resp.json_data) <= 5, "返回条数超过了 per_page"
    else:
        pytest.skip("该实例未返回分页头（部分代理会剥掉自定义头）")
