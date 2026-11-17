"""表情奖励（award_emoji）：议题/评论上的反应接口。

全部期望值来自本实例实测（GitLab 19.0.1）：
  · POST rocket → 201；GET 列表 200；GET 单查 200；DELETE → 204
  · **同一用户对同一对象重复同一 emoji → 404**，消息
    'Award Emoji Name has already been taken'——语义上这是个冲突（4xx 系），
    但状态码选择了 404。这条怪癖必须实测才能写对，猜 409 会假红。
  · 非法 emoji 名（含大写）→ 404 'not a valid emoji name'
  · 删除后再删 → 404（幂等）
  · 不存在的议题 → 404（防枚举）
覆盖缺口来源：award_emoji 模块 32 个操作 QA 覆盖为 0。
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
def emoji_scope(gl, settings) -> dict:
    """共用：一个项目 + 一个议题 + 一条评论。全部用例只读不冲突。"""
    factory = DataFactory(gl, prefix=f"{settings.resource_prefix}-emo")
    try:
        proj = factory.project()
        pid = proj["id"]
        issue = factory.issue(pid, title="emoji-host")
        note = factory.note(pid, issue["iid"], "emoji-note-host")
        yield {"pid": pid, "iid": issue["iid"], "nid": note["id"],
               "factory": factory}
    finally:
        factory.cleanup()
        _attach(factory.ledger.report(), "数据清理记录")


# ==========================================================================
# 1. 正向 CRUD 全链路（议题面）
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("表情奖励")
@allure.story("议题 emoji CRUD")
@allure.severity(allure.severity_level.BLOCKER)
def test_issue_emoji_crud_roundtrip(emoji_scope, gl):
    """发 → 查 → 删 → 消失，每步的 id 都取自上一步的返回。"""
    pid, iid = emoji_scope["pid"], emoji_scope["iid"]
    created = gl.client.post(
        f"/projects/{pid}/issues/{iid}/award_emoji",
        json_body={"name": "rocket"}, expect=201).json_data
    eid = created["id"]
    assert created["name"] == "rocket"

    rows = gl.client.get(f"/projects/{pid}/issues/{iid}/award_emoji",
                         expect=200).json_data
    assert any(e["id"] == eid for e in rows), "201 之后列表里没有——写入没落库"

    one = gl.client.get(f"/projects/{pid}/issues/{iid}/award_emoji/{eid}",
                        expect=200).json_data
    assert one["name"] == "rocket"

    gl.client.delete(f"/projects/{pid}/issues/{iid}/award_emoji/{eid}", expect=204)
    after = gl.client.get(f"/projects/{pid}/issues/{iid}/award_emoji",
                          expect=200).json_data
    assert not any(e["id"] == eid for e in after), "删除后 emoji 还在列表里"


@allure.epic("GitLab API")
@allure.feature("表情奖励")
@allure.story("同一对象多个不同 emoji")
@allure.severity(allure.severity_level.NORMAL)
def test_issue_accepts_multiple_distinct_emoji(emoji_scope, gl):
    """rocket 与 fire 可并存：唯一性约束在「用户 × emoji 名」组合上。

    实测依据：rocket 201（探针一）、fire 201（探针三，独立议题上）。
    这条把它们放到同一议题上，守的是"多选不互斥"。
    """
    pid, iid = emoji_scope["pid"], emoji_scope["iid"]
    ids = []
    for name in ("rocket", "fire"):
        resp = gl.client.post(
            f"/projects/{pid}/issues/{iid}/award_emoji",
            json_body={"name": name}, expect=201)
        ids.append(resp.json_data["id"])
    try:
        rows = gl.client.get(f"/projects/{pid}/issues/{iid}/award_emoji",
                             expect=200).json_data
        got = {e["id"] for e in rows}
        assert set(ids) <= got, "两个 emoji 都 201，但列表少了其中一个——写入被覆盖"
    finally:
        for eid in ids:
            gl.client.delete(f"/projects/{pid}/issues/{iid}/award_emoji/{eid}",
                             expect=(204, 404))


# ==========================================================================
# 2. 唯一性 / 校验：实测都走 404，而不是 400/409
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("表情奖励")
@allure.story("重复奖励同一 emoji")
@allure.severity(allure.severity_level.CRITICAL)
def test_duplicate_emoji_by_same_user_is_404(emoji_scope, gl):
    """同一用户重复发同一 emoji → 404 'has already been taken'。

    状态码是 404 而不是 409——这是实测出的怪癖，测试里**按实测固化**，
    并同时断言消息文本，防止服务端哪天静默改成"允许重复"（列表里
    出现两条 rocket）也不被发现。
    """
    pid, iid = emoji_scope["pid"], emoji_scope["iid"]
    eid = gl.client.post(
        f"/projects/{pid}/issues/{iid}/award_emoji",
        json_body={"name": "tada"}, expect=201).json_data["id"]
    try:
        resp = gl.client.post(
            f"/projects/{pid}/issues/{iid}/award_emoji",
            json_body={"name": "tada"}, expect=404)
        assert "already been taken" in str(resp.json_data), (
            f"404 但原因不是重复奖励：{str(resp.json_data)[:200]}")
    finally:
        gl.client.delete(f"/projects/{pid}/issues/{iid}/award_emoji/{eid}",
                         expect=(204, 404))


@allure.epic("GitLab API")
@allure.feature("表情奖励")
@allure.story("非法 emoji 名")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("name", ["not_a_real_emoji", "ROCKET", ""],
                         ids=["fake", "uppercase", "empty"])
def test_invalid_emoji_name_is_404(emoji_scope, gl, name):
    """非法/大写/空 emoji 名 → 404 'not a valid emoji name'。

    大写也拒（emoji 名大小写敏感），空串同样走这条 404 分支——
    三种输入共用同一拒绝语义，实测确认。
    """
    pid, iid = emoji_scope["pid"], emoji_scope["iid"]
    resp = gl.client.post(
        f"/projects/{pid}/issues/{iid}/award_emoji",
        json_body={"name": name}, expect=404)
    assert "valid emoji" in str(resp.json_data), (
        f"404 但原因不是 emoji 名非法：{str(resp.json_data)[:200]}")


# ==========================================================================
# 3. 幂等：重复删除
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("表情奖励")
@allure.story("重复删除幂等")
@allure.severity(allure.severity_level.CRITICAL)
def test_emoji_double_delete_is_404(emoji_scope, gl):
    pid, iid = emoji_scope["pid"], emoji_scope["iid"]
    eid = gl.client.post(
        f"/projects/{pid}/issues/{iid}/award_emoji",
        json_body={"name": "eyes"}, expect=201).json_data["id"]
    gl.client.delete(f"/projects/{pid}/issues/{iid}/award_emoji/{eid}", expect=204)
    gl.client.delete(f"/projects/{pid}/issues/{iid}/award_emoji/{eid}", expect=404)


# ==========================================================================
# 4. 评论面（二层资源）
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("表情奖励")
@allure.story("评论 emoji CRUD")
@allure.severity(allure.severity_level.NORMAL)
def test_note_emoji_crud(emoji_scope, gl):
    """评论上的 emoji 与议题同构：POST 201 / GET 200 / DELETE 204。

    评论是三层路径（项目/议题/评论），最容易因为中间层校验缺失
    出现越权读写——单独走一遍全链路。
    """
    pid, iid, nid = emoji_scope["pid"], emoji_scope["iid"], emoji_scope["nid"]
    base = f"/projects/{pid}/issues/{iid}/notes/{nid}/award_emoji"
    eid = gl.client.post(base, json_body={"name": "thumbsup"},
                         expect=201).json_data["id"]
    one = gl.client.get(f"{base}/{eid}", expect=200).json_data
    assert one["name"] == "thumbsup"
    gl.client.delete(f"{base}/{eid}", expect=204)
    gl.client.get(f"{base}/{eid}", expect=404)


# ==========================================================================
# 5. 越界：不存在的议题
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("表情奖励")
@allure.story("404 防枚举语义")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_emoji_on_missing_issue_is_404(emoji_scope, gl, method):
    """GET/POST 都必须 404，且不泄漏议题是否存在之外的信息。"""
    pid = emoji_scope["pid"]
    if method == "GET":
        resp = gl.client.get(f"/projects/{pid}/issues/999999/award_emoji",
                             expect=404)
    else:
        resp = gl.client.post(f"/projects/{pid}/issues/999999/award_emoji",
                              json_body={"name": "rocket"}, expect=404)
    assert resp.status == 404
