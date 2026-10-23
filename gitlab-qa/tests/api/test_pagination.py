"""分页：接口测试里最容易写漏的区域。

三个必测项：
1. per_page 生效（服务端不能忽略它）
2. 边界值（下限 1、上限 100、越界 101）
3. 翻完全部页——不重不漏
"""

from __future__ import annotations

import allure
import pytest

pytestmark = [pytest.mark.api, pytest.mark.regression]


@allure.epic("GitLab API")
@allure.feature("分页")
@allure.story("per_page 生效")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.parametrize("per_page", [1, 5, 20], ids=lambda v: f"per_page={v}")
def test_per_page_is_respected(anon_client, per_page):
    resp = anon_client.client.get("/projects", params={"per_page": per_page},
                                  expect=(200, 401, 403))
    if resp.status != 200:
        pytest.skip(f"匿名无法访问项目列表（HTTP {resp.status}）")
    assert len(resp.json_data) <= per_page, (
        f"请求 per_page={per_page}，返回 {len(resp.json_data)} 条"
    )


@allure.epic("GitLab API")
@allure.feature("分页")
@allure.story("per_page 上限")
@allure.severity(allure.severity_level.NORMAL)
def test_per_page_over_limit(anon_client):
    """per_page 超过上限时的行为。

    实测 GitLab 的处理是「截断到 100」而不是报 400。两种做法都说得通，
    但必须明确：如果返回 200，条数就一定不能超过 100。
    真正的缺陷是「嘴上接受 101，实际给回第一页的默认条数」。
    """
    resp = anon_client.client.get("/projects", params={"per_page": 101},
                                  expect=(200, 400, 401, 403))
    if resp.status in (401, 403):
        pytest.skip("匿名无权访问")
    if resp.status == 400:
        return
    assert resp.status == 200
    assert len(resp.json_data) <= 100, (
        f"per_page=101 被接受，但返回了 {len(resp.json_data)} 条（超过硬上限 100）"
    )


@allure.epic("GitLab API")
@allure.feature("分页")
@allure.story("翻页不重不漏")
@allure.severity(allure.severity_level.CRITICAL)
def test_pages_cover_without_duplication(anon_client):
    """取前 3 页，id 集合不能有重复。

    这条用例抓的是「排序不稳定导致跨页重复」——
    单看每一页都正常，合起来才有问题，参数化测不出来。
    """
    seen: list[int] = []
    for page in (1, 2, 3):
        resp = anon_client.client.get(
            "/projects", params={"per_page": 20, "page": page}, expect=(200, 401, 403)
        )
        if resp.status != 200:
            pytest.skip(f"匿名无法访问项目列表（HTTP {resp.status}）")
        rows = resp.json_data
        if not rows:
            break
        seen.extend(r["id"] for r in rows)

    if not seen:
        pytest.skip("没有可分页的数据")

    dup = [i for i in set(seen) if seen.count(i) > 1]
    assert not dup, f"跨页出现重复项目 id：{dup[:5]}"
    allure.attach(f"共取 {len(seen)} 条，去重后 {len(set(seen))} 条",
                  name="分页校验", attachment_type=allure.attachment_type.TEXT)


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
