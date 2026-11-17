"""项目 Wiki 页面：slug 语义 + CRUD + 校验。

全部期望值来自本实例实测（GitLab 19.0.1）：
  · POST {title, content} → 201，slug 由 title 派生（保留大小写：
    "Home Page" → "Home-Page"）
  · **slug 查找大小写不敏感**：GET /wikis/home-page 也能命中
    slug 为 "Home-Page" 的页，且响应里存的是原始 slug
  · PUT 改 title → 200，**slug 随之重算**，旧 slug 立即 404
  · PUT 只传 title 不传 content → 200，content 保持不变（部分更新语义）
  · PUT 空 body → 200，页面原样返回（无操作）
  · 重复 title → 400 'Duplicate page: A page with that title
    already exists in the file Home-Page.md'
  · 空白 title（"  "）→ 400 "title can't be blank"
  · 缺 content → 400 'content is missing'
  · DELETE → 204；重复删 404；不存在的页 404
  · 项目不存在 → 404 'Project Not Found'
覆盖缺口来源：wikis 模块 12 个操作 QA 只覆盖 1 个（GET 列表）。
"""

from __future__ import annotations

import allure
import pytest

from gqa.data.factory import DataFactory

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]


def _attach(text: str, name: str) -> None:
    try:
        allure.attach(text, name=name, attachment_type=allure.attachment_type.TEXT)
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(scope="module")
def wiki_scope(gl, settings) -> dict:
    """共用项目（首次 POST /wikis 会自动初始化 wiki）。"""
    factory = DataFactory(gl, prefix=f"{settings.resource_prefix}-wki")
    try:
        proj = factory.project()
        yield {"pid": proj["id"], "factory": factory, "seq": 0}
    finally:
        factory.cleanup()
        _attach(factory.ledger.report(), "数据清理记录")


def _fresh_title(wiki_scope) -> str:
    wiki_scope["seq"] += 1
    return f"页面{wiki_scope['seq']}"


# ==========================================================================
# 1. 正向 CRUD 全链路
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("Wiki")
@allure.story("CRUD 全链路")
@allure.severity(allure.severity_level.BLOCKER)
def test_wiki_crud_roundtrip(wiki_scope, gl):
    """创建 → 按 slug 单查 → 更新内容 → 删除 → 404。

    slug 取自创建响应而不是自己拼——slug 派生规则（大小写、
    连字符、去空格）是服务端行为，用例里复刻一遍就是循环论证。
    """
    pid = wiki_scope["pid"]
    title = _fresh_title(wiki_scope)
    created = gl.client.post(
        f"/projects/{pid}/wikis",
        json_body={"title": title, "content": "第一版"}, expect=201).json_data
    slug = created["slug"]

    one = gl.client.get(f"/projects/{pid}/wikis/{slug}", expect=200).json_data
    assert one["title"] == title
    assert one["content"] == "第一版"

    updated = gl.client.put(f"/projects/{pid}/wikis/{slug}",
                            json_body={"content": "第二版"},
                            expect=200).json_data
    assert updated["content"] == "第二版", "PUT 200 但内容没变"

    gl.client.delete(f"/projects/{pid}/wikis/{slug}", expect=204)
    gl.client.get(f"/projects/{pid}/wikis/{slug}", expect=404)


# ==========================================================================
# 2. slug 语义（本模块最容易踩的坑）
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("Wiki")
@allure.story("slug 大小写不敏感查找")
@allure.severity(allure.severity_level.NORMAL)
def test_wiki_slug_lookup_is_case_insensitive(wiki_scope, gl):
    """实测：slug 存储保留大小写（"Home-Page"），但查找不区分——
    小写 "home-page" 同样命中，响应里回的是原始大小写。

    这条怪癖意味着"按 URL 猜 slug"的集成代码在大小写上怎么写
    都能过——但一旦上游把查找改成精确匹配，所有小写链接会同时断。
    """
    pid = wiki_scope["pid"]
    created = gl.client.post(
        f"/projects/{pid}/wikis",
        json_body={"title": "Case Probe", "content": "x"}, expect=201).json_data
    slug = created["slug"]
    try:
        assert slug == "Case-Probe", f"slug 派生规则与实测不符：{slug!r}"
        one = gl.client.get(f"/projects/{pid}/wikis/{slug.lower()}",
                            expect=200).json_data
        assert one["slug"] == slug, "小写命中后，响应里的 slug 必须仍是原始值"
    finally:
        gl.client.delete(f"/projects/{pid}/wikis/{slug}", expect=(204, 404))


@allure.epic("GitLab API")
@allure.feature("Wiki")
@allure.story("改标题重算 slug")
@allure.severity(allure.severity_level.CRITICAL)
def test_wiki_rename_changes_slug(wiki_scope, gl):
    """PUT 改 title → slug 随之重算，旧 slug 立即 404。

    这是 wiki 最坑的语义：改个标题，所有指向旧 slug 的链接
    全部失效（没有重定向）。集成方必须从更新响应里重新拿 slug。
    """
    pid = wiki_scope["pid"]
    title = _fresh_title(wiki_scope)
    created = gl.client.post(
        f"/projects/{pid}/wikis",
        json_body={"title": title, "content": "x"}, expect=201).json_data
    old_slug = created["slug"]
    new_slug = old_slug
    try:
        new_title = f"{title}改"
        updated = gl.client.put(
            f"/projects/{pid}/wikis/{old_slug}",
            json_body={"title": new_title}, expect=200).json_data
        new_slug = updated["slug"]
        assert new_slug != old_slug, "改了标题但 slug 没重算"
        gl.client.get(f"/projects/{pid}/wikis/{new_slug}", expect=200)
        gl.client.get(f"/projects/{pid}/wikis/{old_slug}", expect=404)
    finally:
        # slug 已变，按更新响应里的新 slug 清理
        gl.client.delete(f"/projects/{pid}/wikis/{new_slug}", expect=(204, 404))


@allure.epic("GitLab API")
@allure.feature("Wiki")
@allure.story("只改标题保留内容")
@allure.severity(allure.severity_level.NORMAL)
def test_wiki_update_title_keeps_content(wiki_scope, gl):
    """PUT 只传 title → 200，content 原样保留（部分更新语义，实测）。"""
    pid = wiki_scope["pid"]
    title = _fresh_title(wiki_scope)
    created = gl.client.post(
        f"/projects/{pid}/wikis",
        json_body={"title": title, "content": "必须保留的内容"},
        expect=201).json_data
    slug = created["slug"]
    try:
        updated = gl.client.put(
            f"/projects/{pid}/wikis/{slug}",
            json_body={"title": f"{title}改"}, expect=200).json_data
        assert updated["content"] == "必须保留的内容", (
            "只改标题却把内容清空了——PUT 不是整页覆盖语义")
        # 清理：slug 已重算
        gl.client.delete(f"/projects/{pid}/wikis/{updated['slug']}",
                         expect=(204, 404))
        slug = updated["slug"]  # 防止 finally 再删一次报错
    finally:
        if slug:
            gl.client.delete(f"/projects/{pid}/wikis/{slug}", expect=(204, 404))


@allure.epic("GitLab API")
@allure.feature("Wiki")
@allure.story("空 body 更新无操作")
@allure.severity(allure.severity_level.MINOR)
def test_wiki_update_empty_body_is_noop(wiki_scope, gl):
    """实测：PUT 空 body → 200，页面原样返回（既不报错也不清空）。"""
    pid = wiki_scope["pid"]
    title = _fresh_title(wiki_scope)
    created = gl.client.post(
        f"/projects/{pid}/wikis",
        json_body={"title": title, "content": "内容"}, expect=201).json_data
    slug = created["slug"]
    try:
        resp = gl.client.put(f"/projects/{pid}/wikis/{slug}", json_body={},
                             expect=200).json_data
        assert resp["content"] == "内容", "空 body 更新把内容改了"
        assert resp["slug"] == slug, "空 body 更新把 slug 改了"
    finally:
        gl.client.delete(f"/projects/{pid}/wikis/{slug}", expect=(204, 404))


# ==========================================================================
# 3. 唯一性 / 必填校验
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("Wiki")
@allure.story("重复标题拒绝")
@allure.severity(allure.severity_level.CRITICAL)
def test_wiki_duplicate_title_is_400(wiki_scope, gl):
    """同 title 二次创建 → 400 'Duplicate page'（wiki 以 title 唯一）。"""
    pid = wiki_scope["pid"]
    title = _fresh_title(wiki_scope)
    created = gl.client.post(f"/projects/{pid}/wikis",
                             json_body={"title": title, "content": "a"},
                             expect=201).json_data
    try:
        resp = gl.client.post(f"/projects/{pid}/wikis",
                              json_body={"title": title, "content": "b"},
                              expect=400)
        assert "Duplicate page" in str(resp.json_data)
    finally:
        gl.client.delete(f"/projects/{pid}/wikis/{created['slug']}",
                         expect=(204, 404))


@allure.epic("GitLab API")
@allure.feature("Wiki")
@allure.story("空白标题拒绝")
@allure.severity(allure.severity_level.NORMAL)
def test_wiki_blank_title_is_400(wiki_scope, gl):
    """实测：title="  "（纯空白）→ 400 "can't be blank"。

    空白标题会派生出空 slug，页面上表现为一条点不开的记录——
    必填校验必须把"空白"和"缺失"同等对待（实测确认是这么做的）。
    """
    resp = gl.client.post(f"/projects/{wiki_scope['pid']}/wikis",
                          json_body={"title": "  ", "content": "x"},
                          expect=400)
    assert "blank" in str(resp.json_data)


@allure.epic("GitLab API")
@allure.feature("Wiki")
@allure.story("必填 content")
@allure.severity(allure.severity_level.NORMAL)
def test_wiki_missing_content_is_400(wiki_scope, gl):
    resp = gl.client.post(f"/projects/{wiki_scope['pid']}/wikis",
                          json_body={"title": _fresh_title(wiki_scope)},
                          expect=400)
    assert "content is missing" in str(resp.json_data)


# ==========================================================================
# 4. 幂等 + 越界
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("Wiki")
@allure.story("重复删除幂等")
@allure.severity(allure.severity_level.NORMAL)
def test_wiki_double_delete_is_404(wiki_scope, gl):
    pid = wiki_scope["pid"]
    title = _fresh_title(wiki_scope)
    created = gl.client.post(f"/projects/{pid}/wikis",
                             json_body={"title": title, "content": "x"},
                             expect=201).json_data
    slug = created["slug"]
    gl.client.delete(f"/projects/{pid}/wikis/{slug}", expect=204)
    gl.client.delete(f"/projects/{pid}/wikis/{slug}", expect=404)


@allure.epic("GitLab API")
@allure.feature("Wiki")
@allure.story("删除不存在的页面")
@allure.severity(allure.severity_level.NORMAL)
def test_wiki_delete_missing_is_404(wiki_scope, gl):
    gl.client.delete(f"/projects/{wiki_scope['pid']}/wikis/NO-SUCH-PAGE-XYZ",
                     expect=404)


@allure.epic("GitLab API")
@allure.feature("Wiki")
@allure.story("404 防枚举语义")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_wikis_on_missing_project_is_404(gl, method):
    if method == "GET":
        resp = gl.client.get("/projects/99999999/wikis", expect=404)
    else:
        resp = gl.client.post("/projects/99999999/wikis",
                              json_body={"title": "t", "content": "c"},
                              expect=404)
    assert "Project Not Found" in str(resp.json_data)
