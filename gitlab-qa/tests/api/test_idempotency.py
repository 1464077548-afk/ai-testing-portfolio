"""幂等性。

为什么单独一组：

    "重复调用会发生什么"是接口契约里最容易被忽略、又最容易在上游重试
    逻辑里炸掉的部分。客户端 SDK、网关、队列消费者都会重发请求，
    而它们重发的时机恰恰是"上一次可能已经成功了"的时候。

    这里把三类行为钉死：

        删除类   → 重复删不得报错、不得让资源复活
        覆盖写类 → 重复 PUT 同一份内容不得产生第二条记录
        创建类   → 同名资源并发创建，最终只能有一个

    第三类最有价值：单线程下"先查后建"永远看不出竞态，
    只有真并发才能暴露"两个 201、两条同名记录"。

    POST 天然不幂等，这一点也显式固定下来（见 test_same_note_body_...）——
    它是框架「POST 绝不自动重试」这条规则的依据，写进用例比写在注释里可靠。
"""

from __future__ import annotations

import threading

import allure
import pytest

from gqa.api.client import GitLabClient
from gqa.api.verdict import message_of

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]

REPEAT_STATUS = (200, 202, 204, 404)


def _gone(fetch, what: str, second_delete) -> None:
    """重复删除的判据：不 5xx、之后确实查不到。"""
    assert not (500 <= second_delete.status < 600), (
        f"重复删除{what}返回了 {second_delete.status}："
        f"{str(second_delete.json_data)[:200]}\n"
        "删除是最该幂等的操作——重复删报 5xx 会让上游的重试逻辑直接失败。")
    assert second_delete.status in REPEAT_STATUS, (
        f"重复删除{what}应返回 {REPEAT_STATUS} 之一，"
        f"实际 {second_delete.status}：{str(second_delete.json_data)[:200]}")

    after = fetch()
    assert after.status == 404, (
        f"重复删除后{what}仍然能查到（HTTP {after.status}）——"
        "删除没有真正生效，或者第二次删除把它「复活」了")


def _race(settings, n: int, fn) -> list:
    """n 个线程各用一条独立连接，在同一个瞬间发起请求。

    必须每条线程一个 client：requests.Session 不是线程安全的，
    共用连接池会让请求在客户端排队，竞态窗口被人为关掉，
    于是"并发创建"变成"顺序创建"，用例永远绿——测了个寂寞。

    用 Barrier 而不是 sleep：要让服务器真的同时收到两个请求。
    """
    results: list = []
    lock = threading.Lock()
    gate = threading.Barrier(n)  
    #栅栏等待 **全部 n 个线程都到达 `gate.wait()` 这一行**，才统一放行。
    #不是 `time.sleep()` 粗略错开；sleep 方案下线程启动有先后，很难让请求在服务端时间窗口重叠。
    #1. - 保证：所有线程**尽可能同一时刻**调用 `fn(client)` 发请求，放大竞态触发概率。
    def worker() -> None:
        client = GitLabClient(settings.base_url, token=settings.token,
                              timeout=settings.timeout, retries=0)
        try:
            gate.wait()
            resp = fn(client)
            with lock:
                results.append(resp)
        finally:
            client.close()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# ==========================================================================
# 删除：重复删不得报错，资源不得复活
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("幂等性")
@allure.story("重复删除标签")
@allure.severity(allure.severity_level.NORMAL)
def test_delete_label_twice(gl, data_factory, a_project):
    label = data_factory.label(a_project.id)
    pid, lid = a_project.id, label["id"]
    gl.labels.delete(pid, lid)
    second = gl.client.delete(f"/projects/{pid}/labels/{lid}", expect=None)
    _gone(lambda: gl.labels.try_get(pid, lid), "标签", second)


@allure.epic("GitLab API")
@allure.feature("幂等性")
@allure.story("重复删除里程碑")
@allure.severity(allure.severity_level.NORMAL)
def test_delete_milestone_twice(gl, data_factory, a_project):
    ms = data_factory.milestone(a_project.id)
    pid, mid = a_project.id, ms["id"]
    gl.milestones.delete(pid, mid)
    second = gl.client.delete(f"/projects/{pid}/milestones/{mid}", expect=None)
    _gone(lambda: gl.client.get(f"/projects/{pid}/milestones/{mid}", expect=None),
          "里程碑", second)


@allure.epic("GitLab API")
@allure.feature("幂等性")
@allure.story("重复删除议题")
@allure.severity(allure.severity_level.NORMAL)
def test_delete_issue_twice(gl, data_factory, a_project):
    issue = data_factory.issue(a_project.id)
    pid, iid = a_project.id, issue["iid"]
    gl.issues.delete(pid, iid)
    second = gl.client.delete(f"/projects/{pid}/issues/{iid}", expect=None)
    _gone(lambda: gl.client.get(f"/projects/{pid}/issues/{iid}", expect=None),
          "议题", second)


@allure.epic("GitLab API")
@allure.feature("幂等性")
@allure.story("重复删除评论")
@allure.severity(allure.severity_level.MINOR)
def test_delete_note_twice(gl, data_factory, a_project):
    issue = data_factory.issue(a_project.id)
    note = data_factory.note(a_project.id, issue["iid"])
    pid, iid, nid = a_project.id, issue["iid"], note["id"]
    gl.notes.delete(pid, iid, nid)
    second = gl.client.delete(
        f"/projects/{pid}/issues/{iid}/notes/{nid}", expect=None)
    _gone(lambda: gl.client.get(
        f"/projects/{pid}/issues/{iid}/notes/{nid}", expect=None), "评论", second)


@allure.epic("GitLab API")
@allure.feature("幂等性")
@allure.story("重复删除分支")
@allure.severity(allure.severity_level.NORMAL)
def test_delete_branch_twice(gl, data_factory, a_project):
    branch = data_factory.branch(a_project.id)
    pid, name = a_project.id, branch["name"]
    gl.repository.delete_branch(pid, name)
    second = gl.client.delete(
        f"/projects/{pid}/repository/branches/{name}", expect=None)
    _gone(lambda: gl.repository.branch(pid, name), "分支", second)


@allure.epic("GitLab API")
@allure.feature("幂等性")
@allure.story("重复删除项目")
@allure.severity(allure.severity_level.CRITICAL)
def test_delete_project_twice(gl, data_factory):
    """项目删除**不**像其他资源那样幂等——这是实测出来的差异。

        标签 / 里程碑 / 议题 / 分支：第二次 DELETE → 404
        项目：第二次 DELETE → 400 "Project has already been marked for deletion"

    原因是项目走「延迟删除」：第一次把状态标成待删，第二次再删时
    服务端认为这个动作已经做过了，直接报错而不是静默接受。

    对调用方的影响是实打实的：把 400 当失败重试的清理脚本会在这里卡住，
    然后留一堆待删项目在实例上。所以这条用例记录的不是"应该 400"，
    而是"必须知道它可能 400，并且此时资源确实已经不可见了"。
    """
    proj = data_factory.project()
    pid = proj["id"]
    gl.projects.purge(pid, proj.get("path_with_namespace", ""))

    second = gl.client.delete(f"/projects/{pid}", expect=None)
    assert not (500 <= second.status < 600), (
        f"重复删除项目返回 {second.status}：{str(second.json_data)[:200]}")

    assert second.status in (200, 202, 204, 400, 404), (
        f"重复删除项目返回了意外的 {second.status}：{str(second.json_data)[:200]}")

    if second.status == 400:
        msg = message_of(second.json_data).lower()
        assert "marked for deletion" in msg, (
            f"400 的原因不是「已标记删除」，而是：{str(second.json_data)[:200]}\n"
            "那说明失败原因和本条要验的东西无关。")

    # 不管第二次返回 400 还是 404，资源都必须是不可见的
    assert gl.projects.try_get(pid).status == 404, (
        "重复删除后项目仍能查到——延迟删除的标记没有真正生效")


# ==========================================================================
# 覆盖写：重复 PUT 不得产生第二条记录
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("幂等性")
@allure.story("重复写入议题内容")
@allure.severity(allure.severity_level.NORMAL)
def test_repeated_put_issue_does_not_duplicate(gl, data_factory, a_project):
    issue = data_factory.issue(a_project.id)
    pid, iid = a_project.id, issue["iid"]
    payload = {"title": "同一标题", "description": "同一描述"}

    first = gl.client.put(f"/projects/{pid}/issues/{iid}", json_body=payload)
    second = gl.client.put(f"/projects/{pid}/issues/{iid}", json_body=payload)

    assert (first.status, second.status) == (200, 200), (
        f"同一份内容写两次应都成功，实际 {first.status} / {second.status}")

    # 关键：不能只看返回体。列表里出现两条 = 写入被当成了创建
    same = [i for i in gl.issues.list(pid) if i["iid"] == iid]
    assert len(same) == 1, (
        f"重复 PUT 之后该议题在列表里出现了 {len(same)} 条，"
        "说明覆盖写被当成了新建")

    # 内容必须是最后那次的，且没有第二份
    fetched = gl.issues.get(pid, iid).json_data
    assert fetched["title"] == payload["title"]
    assert fetched["description"] == payload["description"]


@allure.epic("GitLab API")
@allure.feature("幂等性")
@allure.story("重复写入里程碑")
@allure.severity(allure.severity_level.MINOR)
def test_repeated_put_milestone_does_not_duplicate(gl, data_factory, a_project):
    ms = data_factory.milestone(a_project.id)
    pid, mid = a_project.id, ms["id"]

    for _ in range(2):
        resp = gl.client.put(f"/projects/{pid}/milestones/{mid}",
                             json_body={"title": "同一里程碑"})
        assert resp.status == 200, f"写入失败：{resp.status} {resp.json_data}"

    same = [m for m in gl.milestones.list(pid) if m["id"] == mid]
    assert len(same) == 1, f"重复 PUT 之后里程碑出现了 {len(same)} 条"


# ==========================================================================
# 重复状态动作：结果稳定，不叠加副作用
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("幂等性")
@allure.story("重复关闭议题")
@allure.severity(allure.severity_level.NORMAL)
def test_close_issue_twice_stays_closed(gl, data_factory, a_project):
    issue = data_factory.issue(a_project.id)
    pid, iid = a_project.id, issue["iid"]

    first = gl.issues.update(pid, iid, state_event="close")
    second = gl.issues.update(pid, iid, state_event="close")

    assert first.json_data["state"] == "closed"
    assert second.json_data["state"] == "closed", (
        f"对已关闭的议题再发一次 close，状态变成了 "
        f"{second.json_data['state']}——重复动作把状态改回去了")


@allure.epic("GitLab API")
@allure.feature("幂等性")
@allure.story("重复关闭里程碑")
@allure.severity(allure.severity_level.MINOR)
def test_close_milestone_twice_stays_closed(gl, data_factory, a_project):
    ms = data_factory.milestone(a_project.id)
    pid, mid = a_project.id, ms["id"]

    gl.milestones.update(pid, mid, state_event="close")
    second = gl.milestones.update(pid, mid, state_event="close")
    assert second.json_data["state"] == "closed"


# ==========================================================================
# POST 不幂等：把这条语义固定下来
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("幂等性")
@allure.story("同内容评论两次")
@allure.severity(allure.severity_level.NORMAL)
def test_same_note_body_creates_two_notes(gl, data_factory, a_project):
    """POST 天然不幂等——两条内容相同的评论是两条评论。

    这不是缺陷，是 HTTP 语义。它的实际后果是：框架绝不能自动重试 POST。
    一旦"超时重发"，用户的议题下就会多一条重复评论，而报告上一切正常。
    所以这条用例断言的其实是「重试策略必须存在的理由」。
    """
    issue = data_factory.issue(a_project.id)
    pid, iid = a_project.id, issue["iid"]

    first = gl.notes.create(pid, iid, "同一条评论")
    second = gl.notes.create(pid, iid, "同一条评论")

    assert (first.status, second.status) == (201, 201)
    assert first.json_data["id"] != second.json_data["id"], (
        "两次 POST 返回了同一个 id，服务端做成了幂等——"
        "那 client.py 里「POST 不重试」的策略就要重新评估")

    bodies = [n["body"] for n in gl.notes.list(pid, iid) if n["body"] == "同一条评论"]
    assert len(bodies) == 2, f"应有两条同内容评论，实际 {len(bodies)} 条"


# ==========================================================================
# 并发创建：最终只能有一条
# ==========================================================================
def _successes(results) -> int:
    return sum(1 for r in results if 200 <= r.status < 300)


@allure.epic("GitLab API")
@allure.feature("幂等性")
@allure.story("并发创建同名标签")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.xfail(
    strict=False,
    reason=(
        "已知缺陷（实测 GitLab 19.0.1）：4 线程并发创建同名标签，"
        "实测返回 [201, 400, 500, 500]——其中两个请求拿到 500 Internal Server Error。"
        "串行重复创建是干净的 409，说明唯一约束存在，但并发下"
        "数据库唯一性冲突没有变成 4xx。"
        "strict=False 是刻意的：这是概率性竞态，不是每次都复现，"
        "strict=True 会让「这一轮没复现」变成构建失败，"
        "结果是用例变成掷骰子。"
    ),
)
def test_concurrent_label_create_yields_one(gl, settings, a_project):
    """两个请求同时到达时的正确结局：一个 201，其余被 4xx 拒绝。

    这里敢断言"恰好一个成功"，是因为**标签名唯一是实测过的既有行为**
    （test_labels.test_duplicate_label_conflicts 已固定：重名 409）。

    同时要求"不得出现 5xx"：冲突是业务上完全预期得到的结果。
    用 500 表达冲突，调用方只能当成服务故障去重试，越重试越乱。
    """
    pid = a_project.id
    name = "race-label"
    results = _race(settings, 4, lambda c: c.post(
        f"/projects/{pid}/labels", json_body={"name": name, "color": "#112233"}))

    codes = sorted(r.status for r in results)
    rows = [x for x in gl.labels.list(pid) if x["name"] == name]

    assert not any(500 <= c < 600 for c in codes), (
        f"并发创建同名标签触发了服务端错误：{codes}\n"
        "唯一约束冲突必须以 4xx 表达，500 会被上游当成瞬时故障无限重试。")
    assert len(rows) == 1, (
        f"并发创建同名标签后实例上出现了 {len(rows)} 条同名记录，"
        f"请求返回 {codes}")
    assert _successes(results) == 1, (
        f"标签名唯一，应只有一个请求成功，实际 {codes}")


@allure.epic("GitLab API")
@allure.feature("幂等性")
@allure.story("并发创建同名里程碑")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.xfail(
    strict=False,
    reason=(
        "已知缺陷（实测 GitLab 19.0.1）：4 线程并发创建同名里程碑，"
        "曾观测到返回 [201, 201, 400, 400] 且实例上落库 2 条同名里程碑。"
        "串行创建同名是 400 'already being used'，说明唯一性有校验，"
        "但「校验」与「插入」之间的窗口没锁住（TOCTOU）。"
        "strict=False：竞态复现与否取决于时序，"
        "把不确定的行为标成 strict 会让构建随机红。"
        "复现率低不代表不存在——这条用例的作用是持续观察，不是稳定门禁。"
    ),
)
def test_concurrent_milestone_create_yields_one(gl, settings, a_project):
    """里程碑同名在串行下是被明确拒绝的（实测 400 already being used）。

    既然串行拒绝，并发也不该放行——否则"先查后建"的校验形同虚设，
    而这类竞态单线程用例永远发现不了。

    断言的是**落库数量**，不是 201 的个数：即使两个请求都拿到 201，
    只要库里只有一条，对调用方就是安全的；反过来一个 201 落下两条更糟。
    """
    pid = a_project.id
    title = "race-milestone"
    results = _race(settings, 4, lambda c: c.post(
        f"/projects/{pid}/milestones", json_body={"title": title}))

    codes = sorted(r.status for r in results)
    rows = [x for x in gl.milestones.list(pid) if x["title"] == title]

    assert not any(500 <= c < 600 for c in codes), f"并发创建触发服务端错误：{codes}"
    assert len(rows) == 1, (
        f"并发创建同名里程碑后落库 {len(rows)} 条，"
        f"而串行创建同名会被 400 拒绝——唯一性校验在并发下失效了"
        f"（请求返回 {codes}）")


@allure.epic("GitLab API")
@allure.feature("幂等性")
@allure.story("并发创建同名分支")
@allure.severity(allure.severity_level.NORMAL)
def test_concurrent_branch_create_yields_one_ref(gl, settings, a_project):
    """同名分支在 git 层面只可能有一条 ref。

    断言的是"没有第二条 ref"和"至少有一次成功"——两次都 201 也算合理
    （创建分支本身可以做成幂等），所以不把"只能一个成功"写死。
    """
    pid = a_project.id
    name = "race-branch"
    results = _race(settings, 4, lambda c: c.post(
        f"/projects/{pid}/repository/branches",
        params={"branch": name, "ref": "main"}))

    codes = sorted(r.status for r in results)
    refs = [x["name"] for x in gl.repository.branches(pid).json_data
            if x["name"] == name]

    assert not any(500 <= c < 600 for c in codes), f"并发建分支触发服务端错误：{codes}"
    assert len(refs) == 1, f"同名分支出现了 {len(refs)} 条 ref：{codes}"
    assert _successes(results) >= 1, f"所有请求都没能建出分支：{codes}"
