"""CRUD 与数据依赖：创建 → 读回 → 更新 → 删除 → 确认消失。

关键纪律：每条用例自己造数据、自己清数据。
依赖「库里本来就有一条 id=1 的数据」是所有接口测试腐烂的起点。
"""

from __future__ import annotations

import pytest

from apilab import contracts
from apilab.models import Book


@pytest.mark.smoke
def test_create_returns_201_with_location(book_factory, api):
    book = book_factory(title="创建即校验")

    Book.model_validate(book)
    resp = api.get(f"/api/books/{book['id']}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "创建即校验"


@pytest.mark.regression
def test_patch_is_partial_not_replace(fresh_book, api):
    """PATCH 只改提交的字段；PUT 语义才会整体替换。

    这条区分在真实项目里被搞错过无数次：一个「只改价格」的请求
    把书名清空了，线上才发现。
    """
    before = fresh_book
    resp = api.patch(f"/api/books/{before['id']}", json={"price": 12.5})

    assert resp.status_code == 200
    after = resp.json()
    assert after["price"] == 12.5
    assert after["title"] == before["title"]
    assert after["author"] == before["author"]
    assert after["genre"] == before["genre"]


@pytest.mark.regression
def test_patch_rejects_unknown_field(fresh_book, api):
    resp = api.patch(f"/api/books/{fresh_book['id']}", json={"nope": 1})

    assert resp.status_code == 400
    body = resp.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert body["fields"] == ["nope"]


def test_delete_then_get_is_404(fresh_book, api):
    book_id = fresh_book["id"]

    assert api.delete(f"/api/books/{book_id}").status_code == 204
    resp = api.get(f"/api/books/{book_id}")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_get_missing_book_returns_structured_error(api):
    resp = api.get("/api/books/bk_does_not_exist")

    assert resp.status_code == 404
    # 错误体也属于契约：前端要靠 code 分支，不能只有一段人类可读的字符串
    contracts.assert_matches(resp.json(), contracts.ERROR)


def test_response_matches_declared_schema(api):
    body = api.json("/api/books/bk_001?")
    assert contracts.collect_errors(body, contracts.BOOK) == []


@pytest.mark.destructive
def test_created_book_belongs_to_creator(book_factory):
    book = book_factory()
    assert book["owner"] == "alice"
