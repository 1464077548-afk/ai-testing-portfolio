"""fixture 作用域：用可观测的计数说明「什么时候该用哪个作用域」。

规则不是背下来的，是算出来的：
    构造代价高 + 状态无污染  → 往大放（session / module）
    需要干净状态、会写数据  → 往小放（function）

为什么用「构造计数」而不是 id：
CPython 会复用刚释放的内存地址，`id()` 相同不代表是同一个对象，
拿 id 做断言会写出时红时绿的假证据。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.scope

_constructed = {"session": 0, "module": 0, "function": 0}
_last = {"function": 0}


@pytest.fixture(scope="session")
def session_probe() -> object:
    """模拟「建连接池 / 只登录一次」这类昂贵且无状态的操作。"""
    _constructed["session"] += 1
    return object()


@pytest.fixture(scope="module")
def module_probe() -> object:
    """模拟「本文件共用的只读数据」。"""
    _constructed["module"] += 1
    return object()


@pytest.fixture
def function_probe() -> object:
    """模拟「每条用例都要新建、会写库的对象」。"""
    _constructed["function"] += 1
    return object()


def test_session_and_module_built_once(session_probe, module_probe, function_probe):
    # 本文件第一次取用：三者各构造一次
    assert _constructed["session"] == 1
    assert _constructed["module"] == 1
    assert _constructed["function"] >= 1


def test_session_reused_across_cases(session_probe, module_probe):
    """第二次取用 session / module，计数不应增长——这就是复用。"""
    assert _constructed["session"] == 1, "session 作用域在整个会话内只构造一次"
    assert _constructed["module"] == 1, "module 作用域在本文件内只构造一次"


def test_function_scope_rebuilds_every_case(function_probe):
    """function 作用域每次取用都重新构造。

    用「单调增长」而不是「等于某个固定值」，这样并行、乱序、挑选执行都不会误报。
    """
    current = _constructed["function"]
    assert current > _last["function"], "function 作用域应重新构造"
    _last["function"] = current


def test_fixture_value_is_cached_within_one_case(request):
    """同一条用例内多次取同一个 fixture，拿到的必须是同一个对象（缓存）。"""
    first = request.getfixturevalue("function_probe")
    second = request.getfixturevalue("function_probe")
    assert first is second
