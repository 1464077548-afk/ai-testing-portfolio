"""状态流转。

三条判据，与具体状态码无关：

    1. **每一步之后的状态必须对**——不是"接口返回 200"，是回查 confirmed；
    2. **非法事件不得改动状态**——返回什么都行，状态不许变；
    3. **终态必须挡住后续流转**——已合并的 MR 不能被关闭或重启。

第 3 条是这批用例里最容易出真缺陷的地方：很多系统把"合并"实现成
设置一个状态字段，却没有在后续 update 里挡住它，于是合并后的 MR 还能被
reopen，跨资源的统计立刻对不上。

    状态机的名字在不同资源上并不统一：议题是 opened/closed，
    里程碑是 active/closed。这类"同一概念两种叫法"在联调时最容易写错，
    所以每一步都从回查里取 state，而不是从请求参数里推。
"""

from __future__ import annotations

import time

import allure
import pytest

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]

ILLEGAL_EVENTS = ["bogus", "CLOSE", "Close", "closed", "", "merge"]

# 「还没准备好合并」的三个状态码：405 方法不允许、406 不可接受、409 冲突。
# 在 GitLab 里它们也用来表达"合并引用还没算完"，与"永久不能合并"共用状态码。
MERGE_NOT_READY = (405, 406, 409)


def _merge_when_ready(gl, pid: int, iid: int, timeout: float = 90.0):
    """合并 MR，把 405/406/409 当成"还没准备好"而不是终局。

    为什么要重试而不是等一个字段：
    实测新建 MR 后立刻 merge 会连续拿到 405，约 4 秒后才返回 200
    （tools/_probe_merge_retry.py 场景 C：0s→405、2s→405、4s→200）。
    而 `merge_status` / `detailed_merge_status` 一直停在 `checking`，
    连看 12 秒都不变——**没有任何字段能可靠表示"可以合并了"**，
    只能用动作本身试。

    90 秒是给并行跑留的余量：单线程空载 4 秒就绪，-n 4 压着 Gitaly 时
    会慢一个数量级。超时后仍 405 的，调用方按"本环境不可合并"skip，
    而不是硬断言——那不是被测系统的缺陷。
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = gl.client.put(f"/projects/{pid}/merge_requests/{iid}/merge",
                             json_body={}, expect=None)
        if last.status not in MERGE_NOT_READY:
            return last
        time.sleep(3)
    return last


def _state(gl, pid: int, iid: int) -> str:
    return gl.issues.get(pid, iid).json_data["state"]


# ==========================================================================
# 议题：opened → closed → opened → closed
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("状态流转")
@allure.story("议题完整回路")
@allure.severity(allure.severity_level.CRITICAL)
def test_issue_full_cycle(gl, data_factory, a_project):
    """关→开→关 整条回路。只测"关得掉"是不够的。

    真实场景里议题被误关后重开、又再次关闭的比例不低。回路的第二步
    （reopen）才是最容易坏的一步：很多实现把 close 做成单向操作，
    或者 reopen 后 issue 的 closed_at 没清空，导致按 state 过滤时
    两边都查不到。
    """
    issue = data_factory.issue(a_project.id)
    pid, iid = a_project.id, issue["iid"]

    with allure.step("初始状态"):
        assert _state(gl, pid, iid) == "opened"

    with allure.step("关闭"):
        gl.issues.update(pid, iid, state_event="close")
        assert _state(gl, pid, iid) == "closed"

    with allure.step("重开"):
        gl.issues.update(pid, iid, state_event="reopen")
        assert _state(gl, pid, iid) == "opened", (
            "reopen 之后状态没回到 opened —— 议题被误关后无法恢复")

    with allure.step("再关一次"):
        gl.issues.update(pid, iid, state_event="close")
        assert _state(gl, pid, iid) == "closed"

    with allure.step("过滤结果与状态一致"):
        opened = [i["iid"] for i in gl.issues.list(pid, state="opened")]
        closed = [i["iid"] for i in gl.issues.list(pid, state="closed")]
        assert iid in closed, "已关闭的议题没出现在 state=closed 的列表里"
        assert iid not in opened, "已关闭的议题仍出现在 state=opened 的列表里"


@allure.epic("GitLab API")
@allure.feature("状态流转")
@allure.story("议题非法状态事件")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("event", ILLEGAL_EVENTS,
                         ids=[repr(e) for e in ILLEGAL_EVENTS])
def test_illegal_issue_event_leaves_state_untouched(gl, data_factory, a_project, event):
    """非法 state_event：可以 400，也可以 200 但什么都不做。

    唯一不能发生的是「状态被改了」或「500」。写死 400 会误伤那些
    宽松处理的实现，而只断言"没报错"又放过 500——所以只盯状态本身。
    """
    issue = data_factory.issue(a_project.id)
    pid, iid = a_project.id, issue["iid"]
    before = _state(gl, pid, iid)

    resp = gl.client.put(f"/projects/{pid}/issues/{iid}",
                         json_body={"state_event": event}, expect=None)

    assert not (500 <= resp.status < 600), (
        f"非法 state_event={event!r} 触发 {resp.status}："
        f"{str(resp.json_data)[:200]}")

    after = _state(gl, pid, iid)
    assert after == before, (
        f"非法 state_event={event!r} 把状态从 {before} 改成了 {after}"
        f"（HTTP {resp.status}）")


# ==========================================================================
# 里程碑：active → closed → active
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("状态流转")
@allure.story("里程碑完整回路")
@allure.severity(allure.severity_level.NORMAL)
def test_milestone_full_cycle(gl, data_factory, a_project):
    """里程碑用 active/closed，和议题的 opened/closed 不是一套词。

    这里连续走两轮，验证「反复开关」不会把状态卡在中间态。
    """
    ms = data_factory.milestone(a_project.id)
    pid, mid = a_project.id, ms["id"]

    def state() -> str:
        return gl.milestones.get(pid, mid).json_data["state"]

    assert state() == "active", "新建里程碑的初始状态不是 active"
    for round_no in (1, 2):
        gl.milestones.update(pid, mid, state_event="close")
        assert state() == "closed", f"第 {round_no} 轮关闭失败"
        gl.milestones.update(pid, mid, state_event="activate")
        assert state() == "active", f"第 {round_no} 轮重启失败"


@allure.epic("GitLab API")
@allure.feature("状态流转")
@allure.story("里程碑非法状态事件")
@allure.severity(allure.severity_level.MINOR)
@pytest.mark.parametrize("event", ["bogus", "CLOSE_ME", "reopen"],
                         ids=["bogus", "upper", "issue-word"])
def test_illegal_milestone_event_leaves_state_untouched(
        gl, data_factory, a_project, event):
    """特意把议题的词（reopen）丢给里程碑：两套状态机不通用，
    服务端不该把 reopen 当成 activate。"""
    ms = data_factory.milestone(a_project.id)
    pid, mid = a_project.id, ms["id"]
    before = gl.milestones.get(pid, mid).json_data["state"]

    # 用 expect=None 而不是 gl.milestones.update：后者写死了 200，
    # 服务端若选择 400 会抛异常，用例就变成"红"而不是"它选择了拒绝"。
    resp = gl.client.put(f"/projects/{pid}/milestones/{mid}",
                         json_body={"state_event": event}, expect=None)

    after = gl.milestones.get(pid, mid).json_data["state"]
    assert after == before, (
        f"非法 state_event={event!r} 把里程碑从 {before} 改成了 {after}"
        f"（HTTP {resp.status}）")


# ==========================================================================
# 合并请求：opened → closed → opened → merged
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("状态流转")
@allure.story("合并请求完整回路")
@allure.severity(allure.severity_level.CRITICAL)
def test_mr_close_reopen_then_merge(gl, data_factory, a_project):
    mr = data_factory.merge_request(a_project.id)
    pid, iid = a_project.id, mr["iid"]

    def state() -> str:
        return gl.merge_requests.get(pid, iid).json_data["state"]

    assert state() == "opened"

    gl.merge_requests.update(pid, iid, state_event="close")
    assert state() == "closed"

    gl.merge_requests.update(pid, iid, state_event="reopen")
    assert state() == "opened", "被关闭的 MR 重开后没能回到 opened"

    merged = _merge_when_ready(gl, pid, iid)
    if merged.status != 200:
        pytest.skip(f"本实例拒绝合并（HTTP {merged.status}），终态断言无法进行："
                    f"{str(merged.json_data)[:150]}")
    assert state() == "merged"


@allure.epic("GitLab API")
@allure.feature("状态流转")
@allure.story("已合并的 MR 是终态")
@allure.severity(allure.severity_level.CRITICAL)
def test_merged_mr_rejects_further_transitions(gl, data_factory, a_project):
    """合并之后不许再关闭或重开。

    这是"状态机少了最后一条边"的典型缺陷：合并只是把 state 写成 merged，
    后续 update 没拦住，于是统计里"已合并"的 MR 还能变成"已关闭"。
    """
    mr = data_factory.merge_request(a_project.id)
    pid, iid = a_project.id, mr["iid"]

    merged = _merge_when_ready(gl, pid, iid)
    if merged.status != 200:
        pytest.skip(f"本实例拒绝合并（HTTP {merged.status}），无法验证终态")

    def state() -> str:
        return gl.merge_requests.get(pid, iid).json_data["state"]

    assert state() == "merged"

    for event in ("close", "reopen"):
        resp = gl.client.put(f"/projects/{pid}/merge_requests/{iid}",
                             json_body={"state_event": event}, expect=None)
        assert not (500 <= resp.status < 600), (
            f"对已合并的 MR 发 {event} 触发 {resp.status}")
        assert state() == "merged", (
            f"已合并的 MR 被 state_event={event!r} 改成了 {state()}"
            f"（HTTP {resp.status}）——merged 应该是终点")


@allure.epic("GitLab API")
@allure.feature("状态流转")
@allure.story("已关闭的 MR 不能直接合并")
@allure.severity(allure.severity_level.NORMAL)
def test_closed_mr_cannot_be_merged(gl, data_factory, a_project):
    """关闭 → 合并 是一条不存在的边，必须先 reopen。"""
    mr = data_factory.merge_request(a_project.id)
    pid, iid = a_project.id, mr["iid"]

    gl.merge_requests.update(pid, iid, state_event="close")
    resp = gl.client.put(f"/projects/{pid}/merge_requests/{iid}/merge",
                         json_body={}, expect=None)
    assert not (500 <= resp.status < 600), f"合并已关闭的 MR 触发 {resp.status}"

    after = gl.merge_requests.get(pid, iid).json_data["state"]
    assert after != "merged", (
        f"已关闭的 MR 被直接合并了（HTTP {resp.status}）——"
        "closed → merged 这条边不该存在")


# ==========================================================================
# 删除之后：任何流转都必须 404
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("状态流转")
@allure.story("已删除议题拒绝状态动作")
@allure.severity(allure.severity_level.NORMAL)
def test_deleted_issue_rejects_transitions(gl, data_factory, a_project):
    """删除是终态里的终态。对已删除资源做状态流转必须 404，
    不能出现"删了又能改"——那意味着删除只是打了个标记却仍在接受写入。
    """
    issue = data_factory.issue(a_project.id)
    pid, iid = a_project.id, issue["iid"]
    gl.issues.delete(pid, iid)

    for body in ({"state_event": "close"}, {"title": "删除后再改"}):
        resp = gl.client.put(f"/projects/{pid}/issues/{iid}",
                             json_body=body, expect=None)
        assert resp.status == 404, (
            f"对已删除议题发 {body} 返回 {resp.status}，期望 404")


# ==========================================================================
# 项目：归档 → 只读 → 取消归档 → 恢复可写
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("状态流转")
@allure.story("归档项目变为只读")
@allure.severity(allure.severity_level.CRITICAL)
def test_archived_project_becomes_read_only(gl, data_factory):
    """归档的语义是"只读封存"，不是"隐藏"。

    如果归档后还能往仓库里提交，归档就只是个 UI 过滤条件，
    而合规场景（保留证据、冻结交付）完全依赖这个只读性。
    """
    proj = data_factory.project()
    pid = proj["id"]

    try:
        resp = gl.client.post(f"/projects/{pid}/archive", expect=None)
        assert resp.status in (200, 201), (
            f"归档失败：HTTP {resp.status} {str(resp.json_data)[:200]}")

        assert gl.projects.get(pid).json_data.get("archived") is True, (
            "archive 接口返回成功，但项目的 archived 字段不是 true")

        with allure.step("归档后写入必须被拒"):
            for what, call in (
                ("建议题", lambda: gl.client.post(
                    f"/projects/{pid}/issues", json_body={"title": "归档后"} )),
                ("写文件", lambda: gl.client.post(
                    f"/projects/{pid}/repository/files/a.txt",
                    json_body={"branch": "main", "content": "x",
                               "commit_message": "qa: 归档后写入"})),
            ):
                r = call()
                assert not (200 <= r.status < 300), (
                    f"项目已归档，但{what}成功了（HTTP {r.status}）——"
                    "归档没有变成只读")

        with allure.step("读取仍然允许"):
            assert gl.projects.get(pid).status == 200
    finally:
        # 无论断言是否通过都要取消归档：归档的项目会让回收流程变得别扭
        gl.client.post(f"/projects/{pid}/unarchive", expect=None)


@allure.epic("GitLab API")
@allure.feature("状态流转")
@allure.story("取消归档后恢复可写")
@allure.severity(allure.severity_level.NORMAL)
def test_unarchive_restores_writes(gl, data_factory):
    """取消归档不是"再归档一次"的反面——要验证写权限真的回来了。

    只断言 archived 字段变 false 是不够的：内部只读锁没释放的话，
    字段看着正常、写入仍然 403。
    """
    proj = data_factory.project()
    pid = proj["id"]

    gl.client.post(f"/projects/{pid}/archive", expect=(200, 201))
    gl.client.post(f"/projects/{pid}/unarchive", expect=(200, 201))

    assert gl.projects.get(pid).json_data.get("archived") is False

    created = gl.client.post(f"/projects/{pid}/issues",
                             json_body={"title": "取消归档后可以建"})
    assert created.status == 201, (
        f"取消归档后仍然建不了议题（HTTP {created.status}）："
        f"{str(created.json_data)[:200]}")
    gl.issues.delete(pid, created.json_data["iid"])
