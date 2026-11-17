"""项目徽章（badges）：企业级 CRUD + 校验 + 幂等 + 越界语义。

全部期望值来自本实例实测（GitLab 19.0.1，探针 .tmp/probe_new_modules.txt）：
  · 创建 201 / 列表 200 / 单查 200 / 更新 200 / 删除 204
  · 删除后单查 404，重复删除 404（幂等）
  · **缺 name 也返回 201，且 name 忠实存为 null**——name 是可选项，
    这不是缺陷；断言写死 400 就是在猜。
  · link_url / image_url 只允许 http/https，非法 scheme 返回 400 且
    消息精确指向字段（javascript: 注入这种输入是最该拦的）
  · 项目不存在时 404 "Project Not Found"（防枚举语义与其他资源一致）

覆盖缺口来源：接口文档统计显示 badges 模块 12 个操作 QA 覆盖为 0。
"""

from __future__ import annotations

import allure
import pytest

from gqa.data.factory import DataFactory

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]

BAD_SCHEMES = [
    "javascript:alert(1)",
    "ftp://example.com/x",
    "file:///etc/passwd",
]


def _attach(text: str, name: str) -> None:
    try:
        allure.attach(text, name=name, attachment_type=allure.attachment_type.TEXT)
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(scope="module")
def badge_scope(gl, settings) -> dict:
    """本模块共用的项目。徽章用例互相独立，共享一个项目节省造数时间。"""
    factory = DataFactory(gl, prefix=f"{settings.resource_prefix}-bdg")
    try:
        proj = factory.project()
        yield {"pid": proj["id"], "factory": factory}
    finally:
        factory.cleanup()
        _attach(factory.ledger.report(), "数据清理记录")


# ==========================================================================
# 1. 正向 CRUD 全链路
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("徽章")
@allure.story("CRUD 全链路")
@allure.severity(allure.severity_level.BLOCKER)
def test_badge_crud_roundtrip(badge_scope, gl):
    """创建 → 列表可见 → 单查一致 → 更新生效 → 删除 → 消失。

    一条链路走完五个动作而不是拆五条用例：中间任何一环断了，
    后面的环节必然失败，拆开的"单环用例"反而看不出因果。
    每一步的断言值都取自**上一步创建接口的返回**，不是再查一遍列表。
    """
    pid = badge_scope["pid"]
    sent = {"name": "release-badge", "link_url": "https://example.com/rel",
            "image_url": "https://example.com/b.svg"}

    created = gl.client.post(f"/projects/{pid}/badges", json_body=sent, expect=201)
    badge = created.json_data
    assert badge["name"] == sent["name"]
    assert badge["link_url"] == sent["link_url"]
    assert badge["image_url"] == sent["image_url"]
    bid = badge["id"]

    rows = gl.client.get(f"/projects/{pid}/badges", expect=200).json_data
    assert any(b["id"] == bid for b in rows), "创建返回 201，但列表里查不到——写入没有落库"

    one = gl.client.get(f"/projects/{pid}/badges/{bid}", expect=200).json_data
    assert one["link_url"] == sent["link_url"], "单查结果与创建回显不一致"

    updated = gl.client.put(f"/projects/{pid}/badges/{bid}",
                            json_body={"name": "renamed"}, expect=200).json_data
    assert updated["name"] == "renamed", "PUT 200 但 name 没变——更新被静默丢弃"

    gl.client.delete(f"/projects/{pid}/badges/{bid}", expect=204)
    gl.client.get(f"/projects/{pid}/badges/{bid}", expect=404)


# ==========================================================================
# 2. 幂等：重复删除
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("徽章")
@allure.story("重复删除幂等")
@allure.severity(allure.severity_level.CRITICAL)
def test_badge_double_delete_is_404(badge_scope, gl):
    """删除是幂等的：第二次删除不会复活资源，也不会 5xx。

    调用方重试删除是常态（网络闪断后重发），第二次必须是干净的成功
    语义（404 视为"目标已不存在"）——实测返回 404 而不是 400/500。
    """
    pid = badge_scope["pid"]
    bid = gl.client.post(
        f"/projects/{pid}/badges",
        json_body={"name": "idem", "link_url": "https://example.com/i",
                   "image_url": "https://example.com/b.svg"},
        expect=201).json_data["id"]

    gl.client.delete(f"/projects/{pid}/badges/{bid}", expect=204)
    gl.client.delete(f"/projects/{pid}/badges/{bid}", expect=404)
    gl.client.get(f"/projects/{pid}/badges/{bid}", expect=404)


# ==========================================================================
# 3. 可选字段语义：缺 name
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("徽章")
@allure.story("可选字段 name")
@allure.severity(allure.severity_level.MINOR)
def test_badge_name_is_optional_and_stored_verbatim(badge_scope, gl):
    """实测：缺 name 返回 201，name 忠实存为 None。

    这条用例守住的是"忠实保存"而不是"必须拒绝"：name 是可选项，
    服务端选择接受。要抓的缺陷是**静默改写**——比如悄悄补一个
    空字符串或默认名。回查必须还是 null 才算忠实。
    """
    pid = badge_scope["pid"]
    resp = gl.client.post(
        f"/projects/{pid}/badges",
        json_body={"link_url": "https://example.com/nn",
                   "image_url": "https://example.com/b.svg"},
        expect=201)
    bid = resp.json_data["id"]
    try:
        stored = gl.client.get(f"/projects/{pid}/badges/{bid}",
                               expect=200).json_data
        assert stored.get("name") is None, (
            f"缺 name 创建成功后回查 name={stored.get('name')!r}——"
            "服务端擅自补了默认值，调用方无从知晓")
    finally:
        gl.client.delete(f"/projects/{pid}/badges/{bid}", expect=204)


# ==========================================================================
# 4. URL scheme 白名单（注入防护）
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("徽章")
@allure.story("link_url scheme 白名单")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.parametrize("url", BAD_SCHEMES)
def test_badge_link_url_rejects_non_http_scheme(badge_scope, gl, url):
    """javascript: / ftp: / file: 一律 400，且消息指向 link_url。

    徽章最终会渲染成用户可点击的链接——scheme 校验失效等于把
    XSS 入口直接开给所有能改项目设置的人。实测消息为
    'is blocked: Only allowed schemes are http, https'。
    """
    pid = badge_scope["pid"]
    resp = gl.client.post(
        f"/projects/{pid}/badges",
        json_body={"name": "bad-scheme", "link_url": url,
                   "image_url": "https://example.com/b.svg"},
        expect=400)
    msg = str(resp.json_data)
    assert "link_url" in msg, f"400 但没说明是 link_url 的错：{msg[:200]}"
    assert "http" in msg, f"没说明允许的 scheme：{msg[:200]}"


@allure.epic("GitLab API")
@allure.feature("徽章")
@allure.story("image_url scheme 白名单")
@allure.severity(allure.severity_level.CRITICAL)
def test_badge_image_url_rejects_javascript_scheme(badge_scope, gl):
    """image_url 的 scheme 校验与 link_url 同源但必须独立生效。"""
    pid = badge_scope["pid"]
    resp = gl.client.post(
        f"/projects/{pid}/badges",
        json_body={"name": "bad-img", "link_url": "https://example.com",
                   "image_url": "javascript:alert(1)"},
        expect=400)
    assert "image_url" in str(resp.json_data), (
        f"400 但没说明是 image_url 的错：{str(resp.json_data)[:200]}")


# ==========================================================================
# 5. 越界：不存在的父资源 / 不存在的徽章
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("徽章")
@allure.story("404 防枚举语义")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_badge_on_missing_project_is_404(gl, method):
    """不存在的项目一律 404 'Project Not Found'，GET/POST 语义一致。

    写这条而不是复用全局错误语义用例的原因：徽章路由是
    二层资源，实现里少一层 project 预检就会漏成 500 或空列表 200。
    """
    if method == "GET":
        resp = gl.client.get("/projects/99999999/badges", expect=404)
    else:
        resp = gl.client.post(
            "/projects/99999999/badges",
            json_body={"name": "x", "link_url": "https://e.com",
                       "image_url": "https://e.com/b.svg"},
            expect=404)
    assert "Project Not Found" in str(resp.json_data)


@allure.epic("GitLab API")
@allure.feature("徽章")
@allure.story("更新不存在的徽章")
@allure.severity(allure.severity_level.NORMAL)
def test_badge_update_missing_is_404(badge_scope, gl):
    """PUT 不存在的徽章 404——不是 404 之外的任何东西（实测确认）。"""
    gl.client.put(f"/projects/{badge_scope['pid']}/badges/99999999",
                  json_body={"name": "x"}, expect=404)


# ==========================================================================
# 6. 群组级徽章（同模块的群组面）
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("徽章")
@allure.story("群组徽章 CRUD")
@allure.severity(allure.severity_level.NORMAL)
def test_group_badge_crud(gl, settings):
    """群组徽章与项目徽章同构：实测 POST 201 / GET 列表 200 / DELETE 204。

    群组是延迟删除资源，用独立数据工厂造、用完整体回收，
    不进 badge_scope（那个只管项目）。
    """
    factory = DataFactory(gl, prefix=f"{settings.resource_prefix}-gbg")
    try:
        grp = factory.group()
        gid = grp["id"]
        bid = gl.client.post(
            f"/groups/{gid}/badges",
            json_body={"name": "g-badge", "link_url": "https://example.com",
                       "image_url": "https://example.com/b.svg"},
            expect=201).json_data["id"]
        rows = gl.client.get(f"/groups/{gid}/badges", expect=200).json_data
        assert any(b["id"] == bid for b in rows)
        gl.client.delete(f"/groups/{gid}/badges/{bid}", expect=204)
        gl.client.get(f"/groups/{gid}/badges/{bid}", expect=404)
    finally:
        factory.cleanup()
        _attach(factory.ledger.report(), "群组徽章清理记录")
