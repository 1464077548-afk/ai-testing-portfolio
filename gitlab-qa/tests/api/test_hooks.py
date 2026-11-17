"""项目 Webhooks（hooks）：事件推送配置的 CRUD 与 URL 校验。

全部期望值来自本实例实测（GitLab 19.0.1）：
  · POST {url, push_events, issues_events} → 201
  · **重复 URL 不去重：第二个同 URL 钩子同样 201**——钩子没有
    唯一约束（对比标签的 409）。这不是缺陷断言，是行为固化：
    测试守住"两次成功 = 列表里真有两条"的一致性。
  · GET 列表 200 / 单查 200 / PUT 更新 200 / DELETE → 204
  · 删除后 404，重复删 404（幂等）
  · URL 校验：空串 / 非法串 / ftp:// → **422** 'Invalid url given'
    （注意不是 400；且只允许 http/https）
  · 项目不存在 → 404 'Project Not Found'
覆盖缺口来源：hooks 模块 22 个操作 QA 覆盖为 0。
"""

from __future__ import annotations

import allure
import pytest

from gqa.data.factory import DataFactory

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]

BAD_URLS = [
    "",                      # 空串：必填校验
    "notaurl",               # 不是 URL
    "ftp://example.com/x",   # scheme 白名单之外
]


def _attach(text: str, name: str) -> None:
    try:
        allure.attach(text, name=name, attachment_type=allure.attachment_type.TEXT)
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(scope="module")
def hook_scope(gl, settings) -> dict:
    """共用项目。钩子 URL 在项目内可以重复，用例间用不同 URL 互不干扰。"""
    factory = DataFactory(gl, prefix=f"{settings.resource_prefix}-hok")
    try:
        proj = factory.project()
        yield {"pid": proj["id"], "factory": factory, "seq": 0}
    finally:
        factory.cleanup()
        _attach(factory.ledger.report(), "数据清理记录")


def _fresh_url(hook_scope) -> str:
    hook_scope["seq"] += 1
    return f"https://example.com/hook-{hook_scope['seq']}"


# ==========================================================================
# 1. 正向 CRUD 全链路
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("Webhooks")
@allure.story("CRUD 全链路")
@allure.severity(allure.severity_level.BLOCKER)
def test_hook_crud_roundtrip(hook_scope, gl):
    """创建（带事件开关）→ 列表可见 → 单查一致 → 改事件开关 → 删除。

    事件开关（push_events / issues_events）是钩子唯一的意义所在：
    创建时带 true、回查必须仍是 true——开关被静默丢掉的话，
    用户配了钩子却收不到任何推送，且界面上看不出异常。
    """
    pid = hook_scope["pid"]
    url = _fresh_url(hook_scope)
    created = gl.client.post(
        f"/projects/{pid}/hooks",
        json_body={"url": url, "push_events": True, "issues_events": True},
        expect=201).json_data
    hid = created["id"]
    assert created["push_events"] is True
    assert created["issues_events"] is True

    rows = gl.client.get(f"/projects/{pid}/hooks", expect=200).json_data
    assert any(h["id"] == hid for h in rows), "201 之后列表里没有——写入没落库"

    one = gl.client.get(f"/projects/{pid}/hooks/{hid}", expect=200).json_data
    assert one["url"] == url

    updated = gl.client.put(f"/projects/{pid}/hooks/{hid}",
                            json_body={"push_events": False},
                            expect=200).json_data
    assert updated["push_events"] is False, "PUT 200 但事件开关没变——更新被静默丢弃"
    assert updated["issues_events"] is True, "只改一个开关，另一个被改了"

    gl.client.delete(f"/projects/{pid}/hooks/{hid}", expect=204)
    gl.client.get(f"/projects/{pid}/hooks/{hid}", expect=404)


# ==========================================================================
# 2. 唯一性：实测无约束
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("Webhooks")
@allure.story("重复 URL 不去重")
@allure.severity(allure.severity_level.NORMAL)
def test_duplicate_url_is_accepted_consistently(hook_scope, gl):
    """实测：同 URL 第二次创建同样 201，钩子没有唯一约束。

    固化的是一致性而不是"应该拒绝"：N 个成功响应必须对应列表里
    N 条钩子。少一条 = 创建凭空消失；多一条 = 写入被重复执行。
    （如果哪天服务端加了唯一约束，这条会以 400 出现——届时按新实测改。）
    """
    pid = hook_scope["pid"]
    url = _fresh_url(hook_scope)
    ids = []
    try:
        for _ in range(2):
            resp = gl.client.post(f"/projects/{pid}/hooks",
                                  json_body={"url": url}, expect=201)
            ids.append(resp.json_data["id"])
        rows = gl.client.get(f"/projects/{pid}/hooks", expect=200).json_data
        got = [h["id"] for h in rows if h["url"] == url]
        assert sorted(got) == sorted(ids), (
            f"两次创建都返回 201（{ids}），列表里同 URL 钩子为 {got}——"
            "成功响应数与落库数对不上")
    finally:
        for hid in ids:
            gl.client.delete(f"/projects/{pid}/hooks/{hid}", expect=(204, 404))


# ==========================================================================
# 3. URL 校验：422（实测，不是 400）
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("Webhooks")
@allure.story("URL scheme 白名单")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.parametrize("url", BAD_URLS, ids=["empty", "not-a-url", "ftp-scheme"])
def test_hook_bad_url_is_422(hook_scope, gl, url):
    """空/非法/ftp 的 URL → 422 'Invalid url given'。

    状态码是 422 而不是 400——实测固化。钩子是 SSRF 高危面：
    服务端会主动向这个 URL 发请求，scheme 白名单（http/https）失效
    意味着攻击者能让实例去访问内网任意地址。
    """
    resp = gl.client.post(f"/projects/{hook_scope['pid']}/hooks",
                          json_body={"url": url}, expect=422)
    assert "Invalid url" in str(resp.json_data), (
        f"422 但消息不是 URL 无效：{str(resp.json_data)[:200]}")


# ==========================================================================
# 4. 幂等 + 越界
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("Webhooks")
@allure.story("重复删除幂等")
@allure.severity(allure.severity_level.CRITICAL)
def test_hook_double_delete_is_404(hook_scope, gl):
    pid = hook_scope["pid"]
    hid = gl.client.post(f"/projects/{pid}/hooks",
                         json_body={"url": _fresh_url(hook_scope)},
                         expect=201).json_data["id"]
    gl.client.delete(f"/projects/{pid}/hooks/{hid}", expect=204)
    gl.client.delete(f"/projects/{pid}/hooks/{hid}", expect=404)


@allure.epic("GitLab API")
@allure.feature("Webhooks")
@allure.story("404 防枚举语义")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_hooks_on_missing_project_is_404(gl, method):
    if method == "GET":
        resp = gl.client.get("/projects/99999999/hooks", expect=404)
    else:
        resp = gl.client.post("/projects/99999999/hooks",
                              json_body={"url": "https://example.com/h"},
                              expect=404)
    assert "Project Not Found" in str(resp.json_data)
