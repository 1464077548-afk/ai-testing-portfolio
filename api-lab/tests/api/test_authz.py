"""权限：测的不是「能不能访问」，而是「不能访问时拒绝得是否准确」。

401 与 403 的区别是高频扣分点：
    401 = 我不知道你是谁（需要认证）
    403 = 我知道你是谁，但你不能做这件事（需要授权）
"""

from __future__ import annotations

import pytest

from apilab import contracts

pytestmark = pytest.mark.regression


def test_normal_user_cannot_read_admin_stats(api):
    resp = api.get("/api/admin/stats")

    assert resp.status_code == 403, "已认证但无权 -> 403，不能是 401"
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    contracts.assert_matches(resp.json(), contracts.ERROR)


def test_admin_can_read_admin_stats(admin_api):
    stats = admin_api.json("/api/admin/stats")

    assert set(stats) == {"total_books", "total_users", "active_tokens"}
    assert stats["total_books"] >= 1


def test_cannot_delete_others_book(admin_api, api):
    """admin 创建、alice 去删——必须 403，而不是「删了但没删掉」的 204。"""
    created = admin_api.post(
        "/api/books",
        json={"title": "管理员的书", "author": "admin", "genre": "tech"},
    )
    assert created.status_code == 201
    book_id = created.json()["id"]

    try:
        resp = api.delete(f"/api/books/{book_id}")
        assert resp.status_code == 403
        assert admin_api.get(f"/api/books/{book_id}").status_code == 200, "越权请求不得产生副作用"
    finally:
        admin_api.delete(f"/api/books/{book_id}")


def test_cannot_patch_others_book(admin_api, api):
    created = admin_api.post(
        "/api/books",
        json={"title": "不许改", "author": "admin", "genre": "tech"},
    )
    book_id = created.json()["id"]

    try:
        assert api.patch(f"/api/books/{book_id}", json={"price": 1.0}).status_code == 403
    finally:
        admin_api.delete(f"/api/books/{book_id}")


@pytest.mark.destructive
def test_admin_can_delete_any_book(admin_api):
    created = admin_api.post(
        "/api/books",
        json={"title": "管理员可删", "author": "admin", "genre": "tech"},
    )
    book_id = created.json()["id"]
    assert admin_api.delete(f"/api/books/{book_id}").status_code == 204
