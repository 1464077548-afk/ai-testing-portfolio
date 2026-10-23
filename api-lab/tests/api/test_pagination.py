"""分页：接口测试里最经典的边界区。

参数化覆盖「页大小 × 页码」的组合，再用一条「遍历所有页」的用例兜住
参数化看不出来的问题——重复项和遗漏项。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.regression


def test_first_page_shape(clean_library, api):
    page = api.json("/api/books?page=1&size=10")

    assert page["total"] == 25
    assert page["pages"] == 3
    assert page["size"] == 10
    assert len(page["items"]) == 10


def test_last_page_is_partial(clean_library, api):
    page = api.json("/api/books?page=3&size=10")
    assert len(page["items"]) == 5


def test_page_beyond_range_is_empty_not_error(clean_library, api):
    """越界页返回空列表而不是 404——这是接口设计选择，必须与文档一致。

    同一个问题在不同团队有不同答案，测试的职责是把「本项目的答案」钉住。
    """
    page = api.json("/api/books?page=99&size=10")

    assert page["items"] == []
    # total 仍是全集大小：前端据此渲染「共 25 条，当前页无数据」
    assert page["total"] == 25


def test_pagination_covers_all_without_duplicates(clean_library, api):
    """翻完所有页，id 既不能重复也不能遗漏。"""
    collected: list[str] = []
    for page_no in range(1, 4):
        collected += [b["id"] for b in api.json(f"/api/books?page={page_no}&size=10")["items"]]

    assert len(collected) == 25
    assert len(set(collected)) == 25, "存在跨页重复项"


@pytest.mark.parametrize(
    ("size", "expected_pages"),
    [(1, 25), (5, 5), (10, 3), (25, 1), (50, 1)],
    ids=["size=1", "size=5", "size=10", "size=25", "size=50"],
)
def test_page_count_is_ceiling(clean_library, api, size, expected_pages):
    """pages = ceil(total / size)。向上取整是最容易写错的一处。"""
    page = api.json(f"/api/books?page=1&size={size}")
    assert page["pages"] == expected_pages
    assert page["size"] == size


@pytest.mark.parametrize("size", [0, -1, 51, 1000], ids=lambda v: f"size={v}")
def test_size_out_of_bounds_is_400(clean_library, api, size):
    resp = api.get(f"/api/books?page=1&size={size}")

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("page_no", [0, -1], ids=lambda v: f"page={v}")
def test_page_below_one_is_400(clean_library, api, page_no):
    assert api.get(f"/api/books?page={page_no}&size=10").status_code == 400


@pytest.mark.parametrize("raw", ["abc", "1.5", ""], ids=["alpha", "float", "empty"])
def test_non_integer_pagination_is_400(clean_library, api, raw):
    """类型错误必须走 400，不能 500。500 意味着参数直接进了运算，是缺陷。"""
    resp = api.get(f"/api/books?page={raw}&size=10")
    assert resp.status_code == 400
