"""纯函数层：毫秒级、可穷举，不需要靶机。

分层不只是「目录分开」，而是「不同层的失败要能一眼分辨」：
这个文件红了，是算法错；tests/api 红了，是服务端行为错。
"""

from __future__ import annotations

import pytest

from apilab.paging import page_count


@pytest.mark.parametrize(
    ("total", "size", "expected"),
    [
        (0, 10, 0),
        (1, 10, 1),
        (10, 10, 1),
        (11, 10, 2),
        (25, 10, 3),
        (25, 25, 1),
        (25, 1, 25),
        (100, 50, 2),
    ],
)
def test_page_count(total, size, expected):
    assert page_count(total, size) == expected


@pytest.mark.parametrize("size", [0, -1])
def test_page_count_rejects_bad_size(size):
    with pytest.raises(ValueError):
        page_count(10, size)


def test_page_count_rejects_negative_total():
    with pytest.raises(ValueError):
        page_count(-1, 10)
