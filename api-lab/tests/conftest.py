"""业务 fixture 层：测试真正用到的东西都在这里。

为什么不把 client 直接放根 conftest：
根层一旦放业务概念，换一个被测系统就得改根，层就塌了。
判断标准很简单——「这行代码换个项目还能用吗」，能就往上放，不能就往下放。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apilab.client import ApiClient

DATA_DIR = Path(__file__).parent / "data"


@pytest.fixture(scope="session")
def api(client_factory):
    """默认主体：普通用户 alice。绝大多数用例用它。"""
    return client_factory("alice", "alice-pw")


@pytest.fixture(scope="session")
def admin_api(client_factory):
    """权限类用例专用。"""
    return client_factory("admin", "admin-pw")


@pytest.fixture
def anon(base_url, settings):
    """未登录客户端。用于验证「没令牌就进不来」。"""
    client = ApiClient(base_url=base_url, timeout=settings.timeout, retries=settings.retries)
    yield client
    client.close()


@pytest.fixture
def book_factory(api):
    """数据工厂 + 自动清理。

    yield 之后的清理必须容错：用例自己删过了就会 404，
    如果这里 assert 状态码，测试会「因为清理失败而失败」，这是最误导人的红。
    """
    created: list[str] = []

    def _create(**overrides) -> dict:
        payload = {
            "title": "临时图书",
            "author": "测试作者",
            "genre": "tech",
            "published_year": 2024,
            "price": 59.0,
        }
        payload.update(overrides)
        resp = api.post("/api/books", json=payload)
        assert resp.status_code == 201, resp.text
        book = resp.json()
        created.append(book["id"])
        return book

    yield _create

    for book_id in created:
        resp = api.delete(f"/api/books/{book_id}")
        assert resp.status_code in (204, 404), f"清理失败 {book_id}: {resp.status_code}"


@pytest.fixture
def fresh_book(book_factory) -> dict:
    return book_factory(title="单本书用例专用")


@pytest.fixture
def clean_library(lab_server):
    """把靶机数据重置成 25 本固定种子书。

    放在业务层而不是 api/ 子目录，因为契约测试也需要它——
    如果放在子目录，contract/ 里的用例就得复制一份，那是分层失效的开始。

    接真实靶场时会 skip，而不是假装成功。
    """
    if lab_server is None:
        pytest.skip("外部靶场不支持数据重置，精确计数断言不适用")
    lab_server.state.reset()
    yield
    lab_server.state.reset()


# --------------------------------------------------------------------------
# 数据驱动：用例数据放 JSON，代码里只留断言逻辑
# --------------------------------------------------------------------------
def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "book_case" in metafunc.fixturenames:
        cases = json.loads((DATA_DIR / "books_create.json").read_text(encoding="utf-8"))
        metafunc.parametrize(
            "book_case",
            cases,
            ids=[c["case_id"] for c in cases],
        )
