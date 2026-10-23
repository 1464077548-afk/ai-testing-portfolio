"""分页算法：抽成纯函数，用单元测试覆盖，接口测试只验证「服务端实现与它一致」。

这是分层测试最实际的一个收益：向上取整的边界条件有 6 种，
全部放进接口测试要起服务、造数据，跑一轮几秒钟；
放在纯函数上跑一轮几毫秒，还能穷举。
"""

from __future__ import annotations


def page_count(total: int, size: int) -> int:
    if total < 0:
        raise ValueError("total 不能为负")
    if size <= 0:
        raise ValueError("size 必须为正")
    return (total + size - 1) // size
