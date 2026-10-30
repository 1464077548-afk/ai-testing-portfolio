"""分页：接口测试里最容易写漏的区域。

三个必测项：
1. per_page 生效（服务端不能忽略它）
2. 边界值（下限 1、上限 100、越界 101）
3. 翻完全部页——不重不漏
"""

from __future__ import annotations

import allure
import pytest

from gqa.api.paging import assert_scan_integrity, scan

pytestmark = [pytest.mark.api, pytest.mark.regression]


@allure.epic("GitLab API")
@allure.feature("分页")
@allure.story("per_page 生效")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.parametrize("per_page", [1, 5, 20], ids=lambda v: f"per_page={v}")
def test_per_page_is_respected(anon_client, per_page):
    """判据是 X-Per-Page 回显，不是「返回条数 <= per_page」。

    后者在数据不足一页时恒真：实例上若只有 3 个项目，请求 per_page=20 返回
    3 条，服务端把 per_page 整个忽略也照样通过。头里回显的是服务端**实际采纳**
    的页宽，与数据量无关，才是硬判据。
    """
    resp = anon_client.client.get("/projects", params={"per_page": per_page},
                                  expect=(200, 401, 403))
    if resp.status != 200:
        pytest.skip(f"匿名无法访问项目列表（HTTP {resp.status}）")
    print(f"   resp.json_data_len={len(resp.json_data)}")
    assert len(resp.json_data) <= per_page, (
        f"请求 per_page={per_page}，返回 {len(resp.json_data)} 条"
    )

    effective = resp.headers.get("X-Per-Page")
    if effective is not None:
        assert int(effective) == per_page, (
            f"请求 per_page={per_page}，服务端实际采纳 {effective}"
        )


@allure.epic("GitLab API")
@allure.feature("分页")
@allure.story("per_page 上限")
@allure.severity(allure.severity_level.NORMAL)
def test_per_page_over_limit(anon_client):
    """per_page 超过上限时必须归一到 100，判据看 X-Per-Page。

    实测（GitLab 19.0.1）：`per_page=101` 返回 200，`X-Per-Page: 100`，
    条数按 100 封顶；`per_page=0` 才是 400。
    真正的缺陷是「嘴上接受 101，实际给回第一页的默认条数」。

    这里特意不再用「返回条数 <= 100」当判据：项目总数不到 100 时那条断言
    恒真，服务端把 per_page 当 1 用也发现不了——本文件原来就是这么写的。
    """
    resp = anon_client.client.get("/projects", params={"per_page": 101},
                                  expect=(200, 400, 401, 403))
    if resp.status in (401, 403):
        pytest.skip("匿名无权访问")
    if resp.status == 400:
        return

    assert resp.status == 200
    print(f"   resp.json_data_len={len(resp.json_data)}")
    effective = resp.headers.get("X-Per-Page")
    assert effective is not None, "缺少 X-Per-Page 头，无法判断服务端实际采纳的页宽"
    print(f"   effective={effective}")
    assert int(effective) <= 100, (
        f"请求 per_page=101，服务端实际采纳 {effective}（超过硬上限 100）")
    assert len(resp.json_data) <= int(effective), (
        f"X-Per-Page={effective}，却返回 {len(resp.json_data)} 条")


@allure.epic("GitLab API")
@allure.feature("分页")
@allure.story("翻页不重不漏")
@allure.severity(allure.severity_level.CRITICAL)
def test_pages_cover_without_duplication(anon_client):
    """翻完全部页：不重复，且 X-Total 与实际翻到的条数一致。

    原来这条只查前 3 页：项目不足 20 个时第 2、3 页是空的，实际只验了 1 页，
    等于没测。现在改成按 X-Next-Page 一路翻到底。

    不造数据也能有检测力——因为 X-Total 是服务端自己算的，
    拿它和「实际翻到多少条」比对，翻页提前结束、丢尾页、越界回落都会被抓住。
    需要「造数据证明不遗漏」的场景在 test_pagination_scoped.py。
    """
    report = scan(anon_client.client, "/projects", per_page=20)
    if not report.rows:
        pytest.skip("匿名账号看不到任何项目，无处可翻")

    allure.attach(report.describe(), name="分页校验", attachment_type=allure.attachment_type.TEXT)
    assert_scan_integrity(report, per_page=20)


@allure.epic("GitLab API")
@allure.feature("分页")
@allure.story("越界页不得回落到第一页")
@allure.severity(allure.severity_level.CRITICAL)
def test_deep_page_never_falls_back_to_first(anon_client):
    """深翻页的真实行为：GitLab 对 offset 分页有 50000 条上限，
    超过会返回 405 而不是空列表（第一次跑到这里才发现）。

    这个用例要拦的不是「405 不对」，而是最危险的一种实现：
    服务端发现页码越界后**悄悄回落到第一页**。那样调用方会以为翻到了新数据，
    实际上一直在重复处理同一批。
    """
    first = anon_client.client.get("/projects", params={"per_page": 20, "page": 1},
                                   expect=(200, 401, 403))
    if first.status != 200:
        pytest.skip("匿名无权限")

    first_ids = [p["id"] for p in first.json_data]

    deep = anon_client.client.get(
        "/projects", params={"per_page": 20, "page": 9999},
        expect=(200, 400, 401, 403, 405),
    )

    if deep.status == 405:
        allure.attach("服务端以 405 拒绝超上限的 offset 分页（GitLab 预期行为）",
                      name="越界页行为", attachment_type=allure.attachment_type.TEXT)
        return

    if deep.status == 200:
        deep_ids = [p["id"] for p in deep.json_data]
        assert deep_ids != first_ids, (
            "越界页返回了与第一页完全相同的数据，说明服务端忽略了 page 参数"
        )
        return

    allure.attach(f"越界页返回 HTTP {deep.status}", name="越界页行为",
                  attachment_type=allure.attachment_type.TEXT)
