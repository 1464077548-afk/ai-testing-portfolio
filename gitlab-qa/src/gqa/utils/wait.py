"""轮询等待工具。

接口测试里到处是异步：删除后台任务、搜索索引、流水线状态。
用固定 sleep 是这类用例不稳定和慢的根源——轮询才是正解。
"""

from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


def wait_until(
    predicate: Callable[[], T],
    timeout: float = 15.0,
    interval: float = 0.5,
    description: str = "条件",
) -> bool:
    """轮询直到 predicate 返回真值。

    只在「状态会变化」的地方用。用它替代 sleep 的两个收益：
    快环境下不浪费时间，慢环境下不会假失败。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001
            # 期间出现异常（如 404）视为「还没满足」，继续等
            pass
        time.sleep(interval)
    return False


def wait_for_status(
    fetch: Callable[[], int],
    expected: int | tuple[int, ...],
    timeout: float = 15.0,
) -> int:
    """等待某个接口返回期望状态码，返回最后一次的状态码。"""
    allowed = (expected,) if isinstance(expected, int) else tuple(expected)
    last = -1
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            last = fetch()
            if last in allowed:
                return last
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return last
