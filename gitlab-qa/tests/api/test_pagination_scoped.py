"""按资源分页扫描：造够数据跨页，证明「不重复、不遗漏」。

和 `tests/api/test_pagination.py` 的分工
----------------------------------------
那个文件测的是**分页参数语义**（per_page 生效、越界页不回落），
走匿名客户端看 `/projects`，不造数据。
这个文件测的是**分功能列表在真实数据深度下翻页是否完整**——
每条资源造 7 条、per_page=3、翻 3 页，用创建时记下的 id 做全集比对。

为什么非要造数据
----------------
列表不足一页时，任何「只取第一页」的实现都能通过。本框架原有的
`test_per_page_over_limit` 就是这样恒真的：实例上只有几十个项目，
断言「返回不超过 100 条」永远成立，服务端就算把 per_page 改成 1 也发现不了。
所以这里的每一条用例都先把数据造到**至少 3 页**，再做断言。

为什么基准用 id 而不是再查一次列表
----------------------------------
拿列表当标准答案是循环论证：两次查询走同一段代码，同一个缺陷会同时污染
两边，比对结果永远相等。这里的预期集一律来自**创建接口的返回值**。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import allure
import pytest

from gqa.api.paging import assert_scan_integrity, scan
from gqa.data.factory import DataFactory

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]

# 每条资源造多少条。7 条 / 每页 3 条 = 满 2 页 + 1 条尾页，
# 正好能覆盖「中间页满载」和「尾页不满」两种形态。
DEPTH = 7
STEP = 3


@dataclass(frozen=True)
class Resource:
    """一个可分页的列表端点。

    key 用 name 而不是 id 的两个端点（branches / tags）本身没有 id 字段，
    写死 key="id" 会让 expected 全变成 None，然后比对永远通过——
    这是最隐蔽的一种假绿。
    """

    name: str
    path: str
    key: str = "id"

RESOURCES = [
    # 议题用 iid 而不是 id：id 是实例级全局编号，iid 才是项目内编号。
    # 拿 id 比对也没错，但造数与断言两边必须同一个字段——第一版这里
    # expected 存 iid、扫描取 id，直接报"漏 7 条 + 多 8 条"。
    Resource("issues", "/projects/{pid}/issues", key="iid"),
    Resource("labels", "/projects/{pid}/labels"),
    Resource("milestones", "/projects/{pid}/milestones"),
    Resource("notes", "/projects/{pid}/issues/{iid}/notes"),
    Resource("branches", "/projects/{pid}/repository/branches", key="name"),
    Resource("tags", "/projects/{pid}/repository/tags", key="name"),
    Resource("group_projects", "/groups/{gid}/projects"),
]

# per_page=1 是最能暴露 offset 计算错误的粒度：翻 7 次页，任何 off-by-one
# 都会表现为少一条或多一条。只挑三个代表资源，避免整组跑太慢。
FINE_GRAINED = ["issues", "labels", "notes"]

# 实测（GitLab 19.0.1）：这两个端点**忽略** order_by/sort，恒按 id 升序返回。
# issues / notes 则真的按参数排序。差异写在这里，用例才不会互相矛盾。
SORTABLE = ["issues", "notes"]
SORT_IGNORED = ["labels", "milestones"]


def _attach(text: str, name: str) -> None:
    try:
        allure.attach(text, name=name, attachment_type=allure.attachment_type.TEXT)
    except Exception:  # noqa: BLE001  附件失败不该影响结论
        pass


def _path(res: Resource, scope: dict) -> str:
    return res.path.format(pid=scope["pid"], gid=scope["gid"],
                           iid=scope["host_iid"])


# ==========================================================================
# 数据集
# ==========================================================================
@pytest.fixture(scope="module")
def paged_scope(gl, settings) -> dict:
    """一个塞满数据的项目 + 一个装得下 7 个项目的群组。

    为什么用 module 级而不是复用 function 级的 data_factory：
    每条用例都重造 50 多个对象，7 条用例就是 350+ 次写请求，时间全花在造数上。
    模块内共享是安全的——这一组扫描用例**只读**，唯一的写操作用例
    （翻页中插入）自带 function 级数据集。
    """
    factory = DataFactory(gl, prefix=f"{settings.resource_prefix}-pg")
    scope: dict = {"expected": {}}
    try:
        proj = factory.project(initialize_with_readme=True)
        pid = proj["id"]
        default_branch = proj.get("default_branch") or "main"
        scope["pid"] = pid
        scope["default_branch"] = default_branch

        scope["expected"]["issues"] = [
            factory.issue(pid, title=f"{factory.prefix}-iss-{i}")["iid"]
            for i in range(DEPTH)
        ]
        scope["expected"]["labels"] = [
            factory.label(pid, name=f"{factory.prefix}-lbl-{i}")["id"]
            for i in range(DEPTH)
        ]
        scope["expected"]["milestones"] = [
            factory.milestone(pid, title=f"{factory.prefix}-ms-{i}")["id"]
            for i in range(DEPTH)
        ]

        # 评论都挂在同一个议题上：notes 是二层资源，分页作用域由父对象决定
        host = factory.issue(pid, title=f"{factory.prefix}-note-host")
        scope["host_iid"] = host["iid"]
        # 这个 host 议题本身也在 issues 列表里，不登记进预期集就会在
        # exact 比对时报"多出一条"——那是造数的账没算平，不是产品缺陷
        scope["expected"]["issues"].append(host["iid"])
        scope["expected"]["notes"] = [
            factory.note(pid, host["iid"], f"{factory.prefix}-note-{i}")["id"]
            for i in range(DEPTH)
        ]

        factories = [
            factory.branch(pid, name=f"{factory.prefix}-br-{i}", ref=default_branch)
            for i in range(DEPTH)
        ]
        # 默认分支也在列表里，漏掉它 exact 比对会报"多出一条"
        scope["expected"]["branches"] = [default_branch] + [b["name"] for b in factories]
        scope["expected"]["tags"] = [
            factory.tag(pid, name=f"{factory.prefix}-tag-{i}", ref=default_branch)["name"]
            for i in range(DEPTH)
        ]

        group = factory.group()
        scope["gid"] = group["id"]
        scope["expected"]["group_projects"] = [
            factory.project(name=f"{factory.prefix}-gp-{i}", namespace_id=group["id"],
                            initialize_with_readme=False)["id"]
            for i in range(DEPTH)
        ]

        _attach(
            "\n".join(f"{k}: {len(v)} 条" for k, v in scope["expected"].items()),
            "本次造数规模")
        yield scope
    finally:
        factory.cleanup()
        _attach(factory.ledger.report(), "数据清理记录")


# ==========================================================================
# 1. 核心：翻完所有页，不重不漏
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("分页")
@allure.story("分功能跨页扫描")
@allure.severity(allure.severity_level.BLOCKER)
@pytest.mark.parametrize("res", RESOURCES, ids=lambda r: r.name)
def test_full_scan_covers_every_record(paged_scope, gl, res: Resource):
    """造 7 条、每页 3 条、翻到底：必须一条不少、一条不重。

    这条用例同时盯四个缺陷，任何一个都会让前端列表"看起来正常但少几条"：
      · 只取第一页（翻页逻辑没实现）→ 漏 4 条
      · offset 算错（每页偏移少 1）→ 重复 + 漏
      · 排序不稳定（created_at 同秒时顺序漂移）→ 重复 + 漏
      · 越界页回落到第一页 → 大量重复
    """
    expected = paged_scope["expected"][res.name]
    report = scan(gl.client, _path(res, paged_scope), per_page=STEP, key=res.key)

    _attach(report.describe(), f"{res.name} 扫描明细")
    assert_scan_integrity(report, expected, per_page=STEP)


@allure.epic("GitLab API")
@allure.feature("分页")
@allure.story("最小页宽的极限扫描")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.parametrize("name", FINE_GRAINED)
def test_scan_is_complete_at_page_size_one(paged_scope, gl, name):
    """per_page=1 时翻 7 次，任何 off-by-one 都无所遁形。

    满载判据在这里退化为"每页恰好 1 条"，比 per_page=3 更严格：
    per_page=3 时尾页不满是正常的，容易掩盖"最后一页少返回一条"；
    per_page=1 时每页都必须有且只有 1 条，边界错误无处可藏。
    """
    res = next(r for r in RESOURCES if r.name == name)
    expected = paged_scope["expected"][name]
    report = scan(gl.client, _path(res, paged_scope), per_page=1, key=res.key)

    _attach(report.describe(), f"{name} per_page=1 埋点")
    assert_scan_integrity(report, expected, per_page=1)
    assert len(report.slices) == len(expected), (
        f"应翻 {len(expected)} 页，实际 {len(report.slices)} 页")


# ==========================================================================
# 2. per_page 的边界：用分页头判，不靠数据量
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("分页")
@allure.story("per_page 超上限的归一化")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.parametrize("requested", [101, 500, 1000],
                         ids=lambda v: f"per_page={v}")
def test_per_page_over_cap_is_normalised(paged_scope, gl, requested):
    """超过硬上限时必须归一到 100，判据看 X-Per-Page 头。

    为什么不能用「返回条数 <= 100」判：数据只有 7 条时这个断言恒真，
    服务端把 per_page 直接当 1 用也照样绿——这就是原有那条用例的漏洞。
    X-Per-Page 回显的是服务端**实际采纳**的值，与数据量无关，才是硬判据。
    """
    res = RESOURCES[0]  # issues
    resp = gl.client.get(_path(res, paged_scope), params={"per_page": requested},
                         expect=(200, 400))
    if resp.status == 400:
        return  # 明确拒绝也是可接受的设计，只要不是"嘴上接受、实际乱来"

    effective = resp.headers.get("X-Per-Page")
    assert effective is not None, "缺少 X-Per-Page 头，无法判断服务端实际采纳的页宽"
    assert 1 <= int(effective) <= 100, (
        f"请求 per_page={requested}，服务端实际采纳 {effective}（超出硬上限 100）")
    assert len(resp.json_data) <= int(effective), (
        f"X-Per-Page={effective}，却返回了 {len(resp.json_data)} 条")


@allure.epic("GitLab API")
@allure.feature("分页")
@allure.story("非法 per_page 不得变成偏移量")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("bad,ok_status", [
    (0, (400, 422)),
    (-1, (200, 400, 422)),
    (-100, (200, 400, 422)),
], ids=lambda v: str(v))
def test_illegal_per_page_never_becomes_offset(paged_scope, gl, bad, ok_status):
    """负数 per_page 最危险的实现是「当成偏移量」：返回第 N 条之后的数据。

    实测 GitLab 19.0.1：per_page=0 → 400；per_page=-1 → 200 且 X-Per-Page
    回落成默认 20。两种都算合理，但都必须满足同一条不变式：
    **返回的必须是列表开头，而不是被跳过一段之后的尾巴。**
    """
    res = RESOURCES[0]
    resp = gl.client.get(_path(res, paged_scope),
                         params={"per_page": bad, "page": 1}, expect=ok_status)
    if resp.status != 200:
        return

    baseline = gl.client.get(_path(res, paged_scope),
                             params={"per_page": 100, "page": 1}, expect=200)
    head = [row["iid"] for row in baseline.json_data][:1]
    got = [row["iid"] for row in resp.json_data][:1]
    assert got == head, (
        f"per_page={bad} 返回的首条是 {got}，不是列表第一条 {head}——"
        "说明非法值被当成了偏移量，跳过了开头的数据")


# ==========================================================================
# 3. 排序：翻页正确性的前提
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("分页")
@allure.story("排序参数生效性")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.parametrize("name", SORTABLE)
def test_sort_parameter_really_reverses_order(paged_scope, gl, name):
    """asc 的第一条必须等于 desc 的最后一条。

    这比「asc 结果递增」更强：只检查递增的话，服务端完全忽略 sort、
    永远返回升序，用例照样绿。镜像关系一验，sort 参数是否真的生效就藏不住了。
    """
    res = next(r for r in RESOURCES if r.name == name)
    path = _path(res, paged_scope)
    key = res.key

    asc = scan(gl.client, path, {"order_by": "created_at", "sort": "asc"},
               per_page=STEP, key=key)
    desc = gl.client.paginate(path, {"order_by": "created_at", "sort": "desc"})

    assert asc.ids and desc, "排序对比需要非空列表"
    assert asc.ids[0] == desc[-1][key], (
        f"asc 首条 {asc.ids[0]} != desc 末条 {desc[-1][key]}——"
        "排序参数未真正生效（两个方向返回了同一批顺序）")

    # 全序也要反得过来，不能只反第一页
    assert asc.ids == [row[key] for row in reversed(desc)], (
        "asc 全序与 desc 逆序不一致，说明排序只在第一页生效")


@allure.epic("GitLab API")
@allure.feature("分页")
@allure.story("不支持排序的端点必须保持稳定顺序")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("name", SORT_IGNORED)
def test_unsortable_endpoint_keeps_stable_order(paged_scope, gl, name):
    """labels / milestones 实测忽略 order_by/sort（恒升序）。

    忽略参数本身不是缺陷，但**顺序必须稳定**：同一批数据连查两次，
    顺序不同就意味着翻页必然丢记录。这条用例守的是稳定性，不是排序方向。
    """
    res = next(r for r in RESOURCES if r.name == name)
    path = _path(res, paged_scope)

    first = scan(gl.client, path, {"order_by": "created_at", "sort": "desc"},
                 per_page=STEP, key=res.key)
    second = scan(gl.client, path, {"order_by": "updated_at", "sort": "asc"},
                  per_page=STEP, key=res.key)

    _attach(f"第一次：{first.ids}\n第二次：{second.ids}", f"{name} 两次顺序采样")
    assert first.ids == second.ids, (
        "两次查询顺序不一致——翻页时同一条记录会落在不同页上，必然重复或遗漏")


# ==========================================================================
# 4. 过滤 + 分页：作用域不能被翻页撕裂
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("分页")
@allure.story("过滤条件与翻页组合")
@allure.severity(allure.severity_level.CRITICAL)
def test_filter_and_paging_partition_the_whole_set(data_factory, gl):
    """关掉一部分议题后，opened / closed 两路必须**恰好瓜分**全集。

    过滤 + 分页是缺陷高发组合：过滤在内存里做（先取一页再筛）就会出现
    "某条在 opened 里没有、在 closed 里也没有"——两边都说不在。这条用例
    验的是分区完整性，不是单个过滤条件对不对。
    """
    proj = data_factory.project(initialize_with_readme=True)
    pid = proj["id"]

    issues = [data_factory.issue(pid, title=f"filter-{i}") for i in range(DEPTH)]
    closed = issues[:2]
    for item in closed:
        gl.issues.close(pid, item["iid"])

    closed_iids = {i["iid"] for i in closed}
    opened_iids = {i["iid"] for i in issues} - closed_iids
    path = f"/projects/{pid}/issues"

    opened = scan(gl.client, path, {"state": "opened"}, per_page=2, key="iid")
    closed_scan = scan(gl.client, path, {"state": "closed"}, per_page=2, key="iid")

    _attach(f"opened: {opened.ids}\nclosed: {closed_scan.ids}", "过滤分页结果")

    # 各自不重不漏
    assert_scan_integrity(opened, sorted(opened_iids), per_page=2)
    assert_scan_integrity(closed_scan, sorted(closed_iids), per_page=2)

    # 两路必须恰好瓜分全集：交集为空、并集等于全集
    assert not (set(opened.ids) & set(closed_scan.ids)), "同一议题同时出现在 opened 与 closed"
    assert set(opened.ids) | set(closed_scan.ids) == {i["iid"] for i in issues}, (
        "opened ∪ closed 不等于全集——有议题两边都查不到（被翻页吃掉了）")


# ==========================================================================
# 5. 翻页过程中数据变化：允许漂移，不允许静默丢失
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("分页")
@allure.story("翻页期间插入新记录")
@allure.severity(allure.severity_level.NORMAL)
def test_insert_during_scan_never_silently_drops_records(data_factory, gl):
    """翻到一半时插入新数据，offset 分页会出现重复——这是固有行为，不是缺陷。

    真正要拦的是「静默丢失」：新数据插到最前面，整体后移一位，
    如果实现按 offset 硬跳，就会跳过一条从未返回过的记录。
    所以判据是：**旧的 7 条必须全部出现过**，重复可以容忍。
    """
    proj = data_factory.project(initialize_with_readme=True)
    pid = proj["id"]
    path = f"/projects/{pid}/issues"

    created = [data_factory.issue(pid, title=f"drift-{i}") for i in range(DEPTH)]
    wanted = {i["iid"] for i in created}

    # 第 1 页取完就插入一条新的（默认 desc 排序，它会排到最前面）
    first = gl.client.get(path, params={"per_page": STEP, "page": 1}, expect=200)
    data_factory.issue(pid, title="drift-intruder")

    seen = [row["iid"] for row in first.json_data]
    page = first.headers.get("X-Next-Page") or ""
    while page:
        resp = gl.client.get(path, params={"per_page": STEP, "page": int(page)},
                             expect=200)
        seen.extend(row["iid"] for row in resp.json_data)
        page = resp.headers.get("X-Next-Page") or ""

    _attach(f"可以看到的：{seen}\n必须看到的：{sorted(wanted)}", "漂移采样")
    missing = wanted - set(seen)
    assert not missing, (
        f"翻页期间插入 1 条，导致 {len(missing)} 条旧记录从未出现过：{sorted(missing)[:8]}\n"
        "重复是可以接受的漂移，静默丢失不是")


# ==========================================================================
# 6. 末尾与越界
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("分页")
@allure.story("尾页与越界页")
@allure.severity(allure.severity_level.CRITICAL)
def test_last_page_closes_the_chain(paged_scope, gl):
    """尾页必须没有 X-Next-Page；再往后翻必须是空数组。

    最危险的实现是越界后**回落第一页**：调用方以为翻到了新数据，
    实际上一直在重复处理同一批。这里连"空页却还给下一页"这种
    更隐蔽的情况一起验（那会让调用方空转几十次）。
    """
    res = RESOURCES[0]
    path = _path(res, paged_scope)
    total = len(paged_scope["expected"]["issues"])
    pages = math.ceil(total / STEP)

    last = gl.client.get(path, params={"per_page": STEP, "page": pages}, expect=200)
    assert not (last.headers.get("X-Next-Page") or ""), (
        f"第 {pages} 页是尾页，却仍给出 X-Next-Page="
        f"{last.headers.get('X-Next-Page')}")

    beyond = gl.client.get(path, params={"per_page": STEP, "page": pages + 1},
                           expect=(200, 400, 405))
    if beyond.status != 200:
        return
    assert isinstance(beyond.json_data, list), "越界页返回的不是数组"
    assert beyond.json_data == [], (
        f"越界页返回了 {len(beyond.json_data)} 条数据（应为空）——"
        "若与第一页相同，说明服务端忽略了 page 参数")
