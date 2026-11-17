"""边界值。

这批用例的共同点：**不猜服务端会接受还是拒绝**。

    超长标题到底该 400 还是该 200，是 GitLab 的实现选择。把它写死成其中
    一边，只会在版本升级时换来一条无意义的红，然后被人放宽成
    `in (200, 400)`——从此这条断言不再验任何东西。

    这里守的是另一套判据（见 gqa.api.verdict）：

        接受 → 回查到的值必须与送出去的**逐字相等**
        拒绝 → 必须是 4xx，且消息里指向该字段

    它能抓到状态码断言抓不到的缺陷：标题被静默截断、数值被悄悄取整、
    日期被改写成"最接近的合法值"。这些都不报错，但用户的数据已经变了。

    「重新 GET 一次再比对」是刻意的：创建响应里回显的是**入参**，
    不是数据库里真正存下的值。只比对响应体，截断类缺陷照样漏过去。
"""

from __future__ import annotations

import allure
import pytest

from gqa.api.verdict import message_of, verdict

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]

LONG = [1, 255, 256, 1000, 5000]


def _attach(text: str, name: str) -> None:
    try:
        allure.attach(text, name=name, attachment_type=allure.attachment_type.TEXT)
    except Exception:  # noqa: BLE001
        pass


# ==========================================================================
# 字符串长度
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("边界值")
@allure.story("议题标题长度")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("n", LONG, ids=[f"len-{n}" for n in LONG])
def test_issue_title_length_is_stored_verbatim(gl, a_project, n):
    """1 / 255 / 256 / 1000 / 5000 字符：接受就必须一字不差地存下来。

    255/256 这一对是刻意的：如果服务端有 255 的上限，边界就落在两者之间，
    只测 1 和 1000 的话，"截断到 255" 和 "接受 1000" 两种实现都看不出来。
    """
    title = "标" * n
    resp = gl.client.post(f"/projects/{a_project.id}/issues",
                          json_body={"title": title})
    stored = None
    if 200 <= resp.status < 300:
        iid = resp.json_data["iid"]
        stored = gl.client.get(
            f"/projects/{a_project.id}/issues/{iid}").json_data.get("title")
        # 先回查再断言，失败信息里才有"实际存了多少字"
        _attach(f"送出 {n} 字，回查 {len(stored or '')} 字", f"标题长度 {n}")
        gl.issues.delete(a_project.id, iid)

    outcome = verdict(resp, field="title", sent=title, stored=stored)
    assert outcome in ("accepted",) or outcome.startswith("rejected"), outcome


@allure.epic("GitLab API")
@allure.feature("边界值")
@allure.story("标签名长度")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("n", [1, 255, 256, 1000], ids=lambda n: f"len-{n}")
def test_label_name_length_is_stored_verbatim(gl, a_project, n):
    """别名 title 是实测逼出来的：标签超长时报的是
    `{"title": ["is too long (maximum is 255 characters)"]}`——
    键名用了内部模型的 title，而不是入参 name。照 name 去匹配会假红。
    """
    name = "l" * n
    resp = gl.client.post(f"/projects/{a_project.id}/labels",
                          json_body={"name": name, "color": "#0033cc"})
    stored = None
    if 200 <= resp.status < 300:
        lid = resp.json_data["id"]
        stored = gl.client.get(
            f"/projects/{a_project.id}/labels/{lid}").json_data.get("name")
        gl.labels.delete(a_project.id, lid)

    verdict(resp, field="name", sent=name, stored=stored, aliases=("title",))


@allure.epic("GitLab API")
@allure.feature("边界值")
@allure.story("里程碑标题长度")
@allure.severity(allure.severity_level.MINOR)
@pytest.mark.parametrize("n", [1, 255, 256, 1000], ids=lambda n: f"len-{n}")
def test_milestone_title_length_is_stored_verbatim(gl, a_project, n):
    title = "m" * n
    resp = gl.client.post(f"/projects/{a_project.id}/milestones",
                          json_body={"title": title})
    stored = None
    if 200 <= resp.status < 300:
        mid = resp.json_data["id"]
        stored = gl.client.get(
            f"/projects/{a_project.id}/milestones/{mid}").json_data.get("title")
        gl.milestones.delete(a_project.id, mid)

    verdict(resp, field="title", sent=title, stored=stored)


@allure.epic("GitLab API")
@allure.feature("边界值")
@allure.story("项目名长度")
@allure.severity(allure.severity_level.MINOR)
@pytest.mark.parametrize("n", [1, 255, 256, 1000], ids=lambda n: f"len-{n}")
def test_project_name_length_is_stored_verbatim(gl, n):
    """项目创建不经过数据工厂：工厂失败时会重试并抛 RuntimeError，
    把"服务端拒绝"伪装成框架错误，就看不出这条边界到底是怎么处理的。

    visibility 必须显式给 private：不给就走实例默认值，
    而「默认允许建公开项目」和「默认不允许」两种实例上的结论完全不同。
    """
    name = "p" * n
    resp = gl.client.post("/projects", json_body={"name": name,
                                                  "visibility": "private"})
    stored = None
    if 200 <= resp.status < 300:
        pid = resp.json_data["id"]
        fetched = gl.client.get(f"/projects/{pid}").json_data
        stored = fetched.get("name")
        gl.projects.purge(pid, fetched.get("path_with_namespace", ""))

    verdict(resp, field="name", sent=name, stored=stored,
            aliases=("path", "project_namespace"))


# ==========================================================================
# 数值字段：CE 上根本不存在的参数
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("边界值")
@allure.story("议题 weight 在 CE 上不被识别")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("weight", [1, 0, -1, 999999, 10**9, "abc"],
                         ids=["one", "zero", "neg-1", "big-1e6", "huge-1e9", "text"])
def test_issue_weight_is_not_a_recognised_field(gl, data_factory, a_project, weight):
    """weight 是 Premium 功能，CE 上这个参数**根本不在允许列表里**。

    实测（GitLab 19.0.1 CE）：只发 weight 会得到

        400 assignee_id, ..., title, issue_type are missing,
            at least one parameter must be provided

    服务端说的不是"权重超范围"，而是"你一个有效参数都没给"——
    weight 被当成不存在的键直接丢掉了。排障时看到这句话去查
    "到底漏了哪个参数"，永远查不到 weight 头上。

    这正是版本差异类缺陷的典型长相：EE 文档写着支持，CE 上静默忽略，
    调用方拿到一条与 weight 毫无关系的报错。
    """
    issue = data_factory.issue(a_project.id)
    resp = gl.client.put(f"/projects/{a_project.id}/issues/{issue['iid']}",
                         json_body={"weight": weight})

    assert resp.status == 400, (
        f"CE 上 weight 应不被识别（实测 400），实际 {resp.status}："
        f"{str(resp.json_data)[:200]}")
    msg = message_of(resp.json_data)
    assert "at least one parameter" in msg.lower(), (
        f"错误消息没有表达「没有任何有效参数」，实际：{msg[:300]}\n"
        "若消息变成「weight 取值非法」，说明该实例已支持 weight（EE），"
        "本条用例的前提需要改写。")


@allure.epic("GitLab API")
@allure.feature("边界值")
@allure.story("议题更新的对照组")
@allure.severity(allure.severity_level.NORMAL)
def test_issue_update_without_weight_succeeds(gl, data_factory, a_project):
    """同一个请求去掉 weight 就能成功。

    没有这条对照，上一条的 400 也可能只是"这个接口今天坏了"——
    两条一起看，才能把原因锁定在 weight 这个字段上。
    """
    issue = data_factory.issue(a_project.id)
    resp = gl.client.put(f"/projects/{a_project.id}/issues/{issue['iid']}",
                         json_body={"title": "只改标题"})
    assert resp.status == 200, (
        f"对照组失败：只改 title 也报 {resp.status}，"
        f"说明问题不在 weight：{str(resp.json_data)[:200]}")
    assert resp.json_data["title"] == "只改标题"


# ==========================================================================
# 日期边界：实测发现「接受但静默丢弃」
# ==========================================================================
BAD_DATES = ["2026-02-30", "2026-13-01", "2026-00-10", "not-a-date", "2026/12/31"]


@allure.epic("GitLab API")
@allure.feature("边界值")
@allure.story("里程碑截止日期")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "已知缺陷（实测 GitLab 19.0.1）：非法 due_date 返回 201 但存成 null。"
        "2026-02-30 / 2026-13-01 / 2026-00-10 / not-a-date / 2026/12/31 "
        "五种输入全部被静默丢弃，调用方无法区分「我传了空」和「我的日期被扔了」。"
        "strict=True：一旦 GitLab 改为拒绝或改为忠实保存，这条会 XPASS 变红，"
        "提醒把这个标记去掉。"
    ),
)
@pytest.mark.parametrize("due", BAD_DATES, ids=[d.replace("/", "-") for d in BAD_DATES])
def test_milestone_bad_due_date(gl, data_factory, a_project, due):
    """2026-02-30 这种"格式对但日子不存在"的日期最容易被放过。

    判据：接受就必须存成 2026-02-30（被改成 2026-03-02 是改写），
    拒绝就必须说明 due_date 有问题。实测两者都不是——它「接受并丢弃」，
    这是三种结局里最差的一种，所以这条用例本来就应该红。
    """
    ms = data_factory.milestone(a_project.id)
    resp = gl.client.put(f"/projects/{a_project.id}/milestones/{ms['id']}",
                         json_body={"due_date": due})
    stored = None
    if 200 <= resp.status < 300:
        stored = gl.client.get(
            f"/projects/{a_project.id}/milestones/{ms['id']}").json_data.get("due_date")
        _attach(f"送出 {due!r}，回查 {stored!r}", f"due_date {due}")

    verdict(resp, field="due_date", sent=due, stored=stored)


@allure.epic("GitLab API")
@allure.feature("边界值")
@allure.story("议题截止日期")
@allure.severity(allure.severity_level.MINOR)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "已知缺陷（实测 GitLab 19.0.1）：议题 due_date 同样被静默丢弃。"
        "接口返回 200，回查 due_date 为 null，没有任何提示。"
    ),
)
@pytest.mark.parametrize("due", BAD_DATES, ids=[d.replace("/", "-") for d in BAD_DATES])
def test_issue_bad_due_date(gl, data_factory, a_project, due):
    issue = data_factory.issue(a_project.id)
    resp = gl.client.put(f"/projects/{a_project.id}/issues/{issue['iid']}",
                         json_body={"due_date": due})
    stored = None
    if 200 <= resp.status < 300:
        stored = gl.client.get(
            f"/projects/{a_project.id}/issues/{issue['iid']}").json_data.get("due_date")
        _attach(f"送出 {due!r}，回查 {stored!r}", f"due_date {due}")

    verdict(resp, field="due_date", sent=due, stored=stored)


@allure.epic("GitLab API")
@allure.feature("边界值")
@allure.story("合法截止日期作为对照")
@allure.severity(allure.severity_level.MINOR)
def test_valid_due_date_is_kept(gl, data_factory, a_project):
    """对照组：合法日期必须原样存下来。

    上面两条 xfail 如果连这条也红了，说明问题不是"非法日期被丢弃"，
    而是"due_date 根本写不进去"——那是完全不同、更严重的故障。
    """
    ms = data_factory.milestone(a_project.id)
    resp = gl.client.put(f"/projects/{a_project.id}/milestones/{ms['id']}",
                         json_body={"due_date": "2026-12-31"})
    assert resp.status == 200, f"写合法日期失败：{resp.status} {resp.json_data}"
    stored = gl.client.get(
        f"/projects/{a_project.id}/milestones/{ms['id']}").json_data.get("due_date")
    assert stored == "2026-12-31", f"合法日期被改写：送了 2026-12-31，回查 {stored!r}"


# ==========================================================================
# 权限等级边界
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("边界值")
@allure.story("成员权限等级")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.parametrize("level", [0, 5, 99, 1000],
                         ids=["zero", "five", "ninety-nine", "thousand"])
def test_member_access_level_boundaries(gl, a_project, level):
    """access_level 只有 5/10/20/30/40/50 几个合法值。

    0 和 5 最危险：0 表示"没有权限"，5 在 GitLab 里是 Minimal Access。
    如果 0 被当成合法值存下来，等于加了个看不见的成员；
    如果 5 被悄悄抬成 10，调用方以为给了最小权限，实际给了 Guest。
    """
    me = gl.users.current().json_data["id"]
    others = [u["id"] for u in gl.users.list() if u["id"] != me]
    if not others:
        pytest.skip("实例上只有当前用户，无法验证成员权限等级边界")
    target = others[0]

    resp = gl.client.post(f"/projects/{a_project.id}/members",
                          json_body={"user_id": target, "access_level": level})
    if resp.status == 409:
        # 上一轮失败没清掉成员时会走到这里。409 说的是"已经是成员"，
        # 和本条要验的 access_level 边界无关——继续判会得到一条误导性的红。
        pytest.skip(f"用户 {target} 已是本项目成员，access_level 边界无法验证")

    stored = None
    if 200 <= resp.status < 300:
        stored = gl.client.get(
            f"/projects/{a_project.id}/members/{target}").json_data.get("access_level")
        gl.members.remove(a_project.id, target)
        _attach(f"送出 {level}，回查 {stored!r}", f"access_level {level}")

    verdict(resp, field="access_level", sent=level, stored=stored)


# ==========================================================================
# 颜色值边界
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("边界值")
@allure.story("标签颜色格式")
@allure.severity(allure.severity_level.MINOR)
@pytest.mark.parametrize("color", ["#GGGGGG", "#12345", "#1234567", "#abcdef", "#ABCDEF"],
                         ids=["non-hex", "five-digits", "seven-digits",
                              "lowercase", "uppercase"])
def test_label_color_boundaries(gl, a_project, color):
    """#RRGGBB 之外的形式应被拒；大小写是合法的等价写法，必须原样保留。

    实测：`#abcdef` 存回 `#abcdef`，`#ABCDEF` 存回 `#ABCDEF`——
    服务端不做大小写归一。所以这里**不做** tolower 再比：
    先降级成小写比对，会把"服务端偷偷把 A 改成 a"这种改写放过去。

    另外实测 `red` 会被接受并存成 `#FF0000`：把颜色名翻译成等价值不算
    改写数据，所以没有把它放进这组（放进来会得到一条无意义的红）。
    """
    name = f"clr-{abs(hash(color)) % 100000}"
    resp = gl.client.post(f"/projects/{a_project.id}/labels",
                          json_body={"name": name, "color": color})
    stored = None
    if 200 <= resp.status < 300:
        lid = resp.json_data["id"]
        stored = gl.client.get(
            f"/projects/{a_project.id}/labels/{lid}").json_data.get("color")
        gl.labels.delete(a_project.id, lid)

    verdict(resp, field="color", sent=color, stored=stored)


# ==========================================================================
# 空白与特殊字符
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("边界值")
@allure.story("纯空白标题")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("title", [" ", "\t", "\n"],
                         ids=["space", "tab", "newline"])
def test_issue_title_whitespace_only(gl, a_project, title):
    """只由 ASCII 空白组成的标题必须被拒。

    与「空串」不同：`title: ""` 走的是必填校验，而 `title: " "` 长度不为零，
    很容易从校验里溜过去，最后变成列表里一条点不开的空白议题。
    """
    resp = gl.client.post(f"/projects/{a_project.id}/issues",
                          json_body={"title": title})
    stored = None
    if 200 <= resp.status < 300:
        iid = resp.json_data["iid"]
        stored = gl.client.get(
            f"/projects/{a_project.id}/issues/{iid}").json_data.get("title")
        gl.issues.delete(a_project.id, iid)

    if 200 <= resp.status < 300:
        raise AssertionError(
            f"纯空白标题被接受了（HTTP {resp.status}），"
            f"回查为 {stored!r}——列表里会多一条点不开的空白议题。")

    assert 400 <= resp.status < 500, (
        f"纯空白标题应被 4xx 拒绝，实际 {resp.status}：{message_of(resp.json_data)[:200]}")
    assert "title" in message_of(resp.json_data).lower(), (
        f"拒绝原因没指向 title：{message_of(resp.json_data)[:200]}")


@allure.epic("GitLab API")
@allure.feature("边界值")
@allure.story("全角空格标题")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "已知缺陷（实测 GitLab 19.0.1）：ASCII 空格 / Tab / 换行都被 400 拒绝，"
        "但全角空格 U+3000 组成的标题被接受（201），回查 title 就是 '\\u3000'。"
        "说明空白判定只用了 ASCII 的 strip，没做 Unicode 归一化。"
        "strict=True：修复后 XPASS 变红，提醒去掉标记。"
    ),
)
def test_issue_title_ideographic_space_is_rejected(gl, a_project):
    """全角空格是最容易漏的一种"看起来是空的"输入。

    它和前一条的 ASCII 空格在业务上完全等价，但走的是不同的校验分支——
    只测半角空格会得出"空白标题已被拦下"的错误结论。
    使用全角输入法的用户敲出来的正是这个字符。
    """
    title = "\u3000"
    resp = gl.client.post(f"/projects/{a_project.id}/issues",
                          json_body={"title": title})
    stored = None
    if 200 <= resp.status < 300:
        iid = resp.json_data["iid"]
        stored = gl.client.get(
            f"/projects/{a_project.id}/issues/{iid}").json_data.get("title")
        gl.issues.delete(a_project.id, iid)

    assert not (200 <= resp.status < 300), (
        f"全角空格标题被接受（HTTP {resp.status}），回查 {stored!r}；"
        "而同样长度的半角空格会被 400 拒绝——判定标准不一致。")
