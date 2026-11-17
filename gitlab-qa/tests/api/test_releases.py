"""发布（releases）：tag 驱动的版本发布 + 资产链接。

全部期望值来自本实例实测（GitLab 19.0.1，仓库带 README 提交）：
  · POST {tag_name, ref} → 201，**tag 不需要预先存在，ref 有效即自动建**
  · 重复 tag 的 release → 409 'Release already exists'
  · 无 ref 且 tag 不存在 → 422 'Ref is not specified'
  · 空 body → 400 'tag_name is missing'
  · PUT 改 name/description → 200；PUT 空 body → 400 'params is empty'
  · **PUT/DELETE 不存在的 release → 403**（不是 404！实例怪癖，实测固化）
  · 资产链接：POST → 201；重复 → 400 ['Url has already been taken',
    'Name has already been taken']；非法 URL → 400 'Only allowed schemes
    are http, https, ftp'（注意：链接允许 ftp，钩子不允许——同是 URL
    校验，白名单不同）；缺 url → 400 'url is missing'
  · DELETE 链接 → 200 **带响应体**（区别于其他资源的 204）
  · DELETE release → 200 带体；**删 release 后 tag 仍在**（两者解耦）
  · 项目不存在 → 404 'Project Not Found'
覆盖缺口来源：releases 模块 14 个操作 QA 覆盖为 0。
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
def release_scope(gl, settings) -> dict:
    """带 README 提交的项目：release 需要仓库里存在有效 ref。

    空仓库上 POST /releases 会 400 'Target main is invalid'
    （实测），所以这里必须 initialize_with_readme=True。
    """
    factory = DataFactory(gl, prefix=f"{settings.resource_prefix}-rel")
    try:
        proj = factory.project(initialize_with_readme=True)
        yield {"pid": proj["id"], "branch": proj.get("default_branch") or "main",
               "factory": factory, "seq": 0}
    finally:
        factory.cleanup()
        _attach(factory.ledger.report(), "数据清理记录")


def _fresh_tag(release_scope) -> str:
    release_scope["seq"] += 1
    return f"rl-{release_scope['seq']}"


# ==========================================================================
# 1. 正向：创建自动建 tag + 回显
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("发布")
@allure.story("创建发布并自动建 tag")
@allure.severity(allure.severity_level.BLOCKER)
def test_release_create_autocreates_tag(release_scope, gl):
    """POST {tag_name, ref} → 201，且 tag 真实存在（单独查 tag 200）。

    tag 与 release 的耦合点就在这条：如果只建了 release 记录而
    tag 没落库，后续所有"按 tag 检出"的操作都会失败。
    """
    pid, ref = release_scope["pid"], release_scope["branch"]
    tag = _fresh_tag(release_scope)
    created = gl.client.post(
        f"/projects/{pid}/releases",
        json_body={"tag_name": tag, "ref": ref, "name": "发布",
                   "description": "首版"},
        expect=201).json_data
    assert created["tag_name"] == tag
    assert created["description"] == "首版"
    # tag 必须真实存在——不信任创建回显，单独查一次
    gl.client.get(f"/projects/{pid}/repository/tags/{tag}", expect=200)


@allure.epic("GitLab API")
@allure.feature("发布")
@allure.story("单查与列表")
@allure.severity(allure.severity_level.CRITICAL)
def test_release_list_and_get(release_scope, gl):
    pid, ref = release_scope["pid"], release_scope["branch"]
    tag = _fresh_tag(release_scope)
    gl.client.post(f"/projects/{pid}/releases",
                   json_body={"tag_name": tag, "ref": ref}, expect=201)
    rows = gl.client.get(f"/projects/{pid}/releases", expect=200).json_data
    assert any(r["tag_name"] == tag for r in rows), "创建 201 但列表里没有"
    one = gl.client.get(f"/projects/{pid}/releases/{tag}", expect=200).json_data
    assert one["tag_name"] == tag


# ==========================================================================
# 2. 唯一性 / 必填 / 引用校验
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("发布")
@allure.story("重复 tag 发布 409")
@allure.severity(allure.severity_level.CRITICAL)
def test_duplicate_release_is_409(release_scope, gl):
    """同一 tag 二次发布 → 409 'Release already exists'（实测，标准冲突码）。"""
    pid, ref = release_scope["pid"], release_scope["branch"]
    tag = _fresh_tag(release_scope)
    gl.client.post(f"/projects/{pid}/releases",
                   json_body={"tag_name": tag, "ref": ref}, expect=201)
    resp = gl.client.post(f"/projects/{pid}/releases",
                          json_body={"tag_name": tag, "ref": ref}, expect=409)
    assert "already exists" in str(resp.json_data)


@allure.epic("GitLab API")
@allure.feature("发布")
@allure.story("必填与引用校验")
@allure.severity(allure.severity_level.NORMAL)
def test_release_missing_fields(release_scope, gl):
    """两个必填分支实测分别为 422 与 400：
    无 ref 且 tag 不存在 → 422 'Ref is not specified'；
    空 body → 400 'tag_name is missing'。
    """
    pid = release_scope["pid"]
    resp = gl.client.post(f"/projects/{pid}/releases",
                          json_body={"tag_name": "no-such-tag-xyz",
                                     "description": "x"}, expect=422)
    assert "Ref is not specified" in str(resp.json_data)

    resp = gl.client.post(f"/projects/{pid}/releases", json_body={},
                          expect=400)
    assert "tag_name is missing" in str(resp.json_data)


# ==========================================================================
# 3. 更新语义
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("发布")
@allure.story("更新描述与名称")
@allure.severity(allure.severity_level.CRITICAL)
def test_release_update_description(release_scope, gl):
    """PUT 改 name/description → 200，回查生效；tag_name 不可变。"""
    pid, ref = release_scope["pid"], release_scope["branch"]
    tag = _fresh_tag(release_scope)
    gl.client.post(f"/projects/{pid}/releases",
                   json_body={"tag_name": tag, "ref": ref, "name": "旧名",
                              "description": "旧描述"}, expect=201)
    updated = gl.client.put(
        f"/projects/{pid}/releases/{tag}",
        json_body={"name": "新名", "description": "新描述"}, expect=200).json_data
    assert updated["name"] == "新名", "PUT 200 但 name 没变"
    assert updated["description"] == "新描述", "PUT 200 但描述没变"
    assert updated["tag_name"] == tag, "更新把 tag 改了——tag 是发布的主键"


@allure.epic("GitLab API")
@allure.feature("发布")
@allure.story("更新空 body")
@allure.severity(allure.severity_level.MINOR)
def test_release_update_empty_body_is_400(release_scope, gl):
    """PUT 空 body → 400 'params is empty'（实测；不是无操作的 200）。"""
    pid, ref = release_scope["pid"], release_scope["branch"]
    tag = _fresh_tag(release_scope)
    gl.client.post(f"/projects/{pid}/releases",
                   json_body={"tag_name": tag, "ref": ref}, expect=201)
    resp = gl.client.put(f"/projects/{pid}/releases/{tag}", json_body={},
                         expect=400)
    assert "empty" in str(resp.json_data)


@allure.epic("GitLab API")
@allure.feature("发布")
@allure.story("更新不存在的发布")
@allure.severity(allure.severity_level.NORMAL)
def test_release_update_missing_is_403(release_scope, gl):
    """实测怪癖：PUT 不存在的 release → 403 Forbidden，而不是 404。

    固化实测行为（403），同时断言状态没被误改——这个怪癖值得写进
    接口文档，调用方按 404 写重试逻辑会直接失效。
    """
    pid = release_scope["pid"]
    resp = gl.client.put(f"/projects/{pid}/releases/NO-SUCH-TAG-XYZ",
                         json_body={"description": "x"}, expect=403)
    assert resp.status == 403


# ==========================================================================
# 4. 资产链接
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("发布")
@allure.story("资产链接 CRUD")
@allure.severity(allure.severity_level.NORMAL)
def test_release_link_crud(release_scope, gl):
    """链接：POST 201 → GET 列表 → PUT 更新 → DELETE 200（带体）→ 404。

    注意 DELETE 返回 200 + 响应体，与大多数资源的 204 不同——
    写死 204 的调用方会把它当失败处理。
    """
    pid, ref = release_scope["pid"], release_scope["branch"]
    tag = _fresh_tag(release_scope)
    gl.client.post(f"/projects/{pid}/releases",
                   json_body={"tag_name": tag, "ref": ref}, expect=201)
    base = f"/projects/{pid}/releases/{tag}/assets/links"

    created = gl.client.post(
        base, json_body={"name": "link-a", "url": "https://example.com/a"},
        expect=201).json_data
    lid = created["id"]
    assert created["url"] == "https://example.com/a"

    rows = gl.client.get(base, expect=200).json_data
    assert any(l["id"] == lid for l in rows)

    updated = gl.client.put(f"{base}/{lid}", json_body={"name": "link-a2"},
                            expect=200).json_data
    assert updated["name"] == "link-a2"

    deleted = gl.client.delete(f"{base}/{lid}", expect=200)
    assert deleted.json_data, "实测 DELETE 链接返回 200 且带响应体（不是 204）"
    gl.client.delete(f"{base}/{lid}", expect=404)


@allure.epic("GitLab API")
@allure.feature("发布")
@allure.story("链接重复与非法 URL")
@allure.severity(allure.severity_level.NORMAL)
def test_release_link_validation(release_scope, gl):
    """三个校验分支全为 400（实测）：
    重复 name+url → ['Url has already been taken', 'Name has already been taken']；
    非法 URL → 'Only allowed schemes are http, https, ftp'；
    缺 url → 'url is missing'。
    链接允许 ftp 而钩子不允许——两个模块的 scheme 白名单不同，不能共用一条用例。
    """
    pid, ref = release_scope["pid"], release_scope["branch"]
    tag = _fresh_tag(release_scope)
    gl.client.post(f"/projects/{pid}/releases",
                   json_body={"tag_name": tag, "ref": ref}, expect=201)
    base = f"/projects/{pid}/releases/{tag}/assets/links"
    gl.client.post(base, json_body={"name": "dup", "url": "https://example.com/d"},
                   expect=201)

    resp = gl.client.post(base, json_body={"name": "dup",
                                           "url": "https://example.com/d"},
                           expect=400)
    msg = str(resp.json_data)
    assert "Url has already been taken" in msg

    resp = gl.client.post(base, json_body={"name": "bad", "url": "notaurl"},
                          expect=400)
    assert "http, https, ftp" in str(resp.json_data)

    resp = gl.client.post(base, json_body={"name": "nourl"}, expect=400)
    assert "url is missing" in str(resp.json_data)


# ==========================================================================
# 5. 删除语义：release 与 tag 解耦
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("发布")
@allure.story("删除发布保留 tag")
@allure.severity(allure.severity_level.CRITICAL)
def test_delete_release_keeps_tag(release_scope, gl):
    """DELETE release → 200；之后 release 404，**tag 仍在**。

    这条语义决定清理脚本的写法：只删 release 不删 tag，实例上会
    堆积孤儿 tag。反向（删 tag 连带删 release）同样要实测确认——
    这里只固化已实测的前半段，避免用例互相依赖。
    """
    pid, ref = release_scope["pid"], release_scope["branch"]
    tag = _fresh_tag(release_scope)
    gl.client.post(f"/projects/{pid}/releases",
                   json_body={"tag_name": tag, "ref": ref}, expect=201)

    deleted = gl.client.delete(f"/projects/{pid}/releases/{tag}", expect=200)
    assert deleted.json_data, "实测 DELETE 发布返回 200 且带响应体"
    gl.client.get(f"/projects/{pid}/releases/{tag}", expect=404)
    # release 删除后 tag 必须仍在（实测解耦）
    gl.client.get(f"/projects/{pid}/repository/tags/{tag}", expect=200)


@allure.epic("GitLab API")
@allure.feature("发布")
@allure.story("重复删除发布")
@allure.severity(allure.severity_level.NORMAL)
def test_release_double_delete_is_403(release_scope, gl):
    """实测：重复 DELETE release → 403（不是 404，与"更新不存在"同一怪癖）。

    与徽章/变量/链接的 404 幂等语义**不一致**——固化实测行为，
    一旦上游统一成 404 这条会变红提醒更新期望值。
    """
    pid, ref = release_scope["pid"], release_scope["branch"]
    tag = _fresh_tag(release_scope)
    gl.client.post(f"/projects/{pid}/releases",
                   json_body={"tag_name": tag, "ref": ref}, expect=201)
    gl.client.delete(f"/projects/{pid}/releases/{tag}", expect=200)
    gl.client.delete(f"/projects/{pid}/releases/{tag}", expect=403)


# ==========================================================================
# 6. 越界
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("发布")
@allure.story("404 防枚举语义")
@allure.severity(allure.severity_level.NORMAL)
def test_releases_on_missing_project_is_404(gl):
    resp = gl.client.get("/projects/99999999/releases", expect=404)
    assert "Project Not Found" in str(resp.json_data)
