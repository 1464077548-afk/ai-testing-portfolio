"""不稳定用例：重试是止血带，不是治疗。

这里放两类用例：
1. 真的能靠重试救回的瞬时故障（演示 --reruns 生效）；
2. 永久故障（演示「重试不能掩盖 bug」，所以绝不能全局无条件开重试）。
"""

from __future__ import annotations

import uuid

import pytest
import requests


@pytest.mark.flaky(reruns=3, reruns_delay=0.1)
def test_transient_503_is_rescued_by_rerun(base_url, request):
    """靶机对同一 key 的前 2 次调用返回 503，第 3 次成功。

    两个容易踩的细节：
    1. 用 requests 直连而不是 ApiClient——后者自带重试，会把 pytest 的重试掩盖掉；
    2. key 必须可复现。用 uuid 的话每次重试都是新 key，故障也就永远「第一次」，
       测试会稳定地失败且看不出原因。这里用 nodeid，重试时它保持不变。
    """
    key = request.node.nodeid
    resp = requests.get(
        f"{base_url}/api/flaky",
        params={"key": key, "fail_times": 2},
        timeout=5,
    )
    assert resp.status_code == 200
    assert resp.json()["attempt"] >= 3, "前两次应当失败，重试到第 3 次才成功"


def test_permanent_failure_is_not_masked(base_url):
    """永远坏的接口，重试一万次也还是坏。

    这条用例的意义是给团队一个警示：`addopts = --reruns 2` 写进全局配置后，
    真实的 5xx 缺陷会被同一次运行内的重试吃掉，CI 从此永远是绿的。
    """
    key = f"broken-{uuid.uuid4().hex[:8]}"
    resp = requests.get(
        f"{base_url}/api/flaky",
        params={"key": key, "fail_times": 999},
        timeout=5,
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"


@pytest.mark.slow
def test_slow_endpoint_respects_client_timeout(base_url, settings):
    """超时要断言「抛的是超时」，而不是「跑得慢」。

    客户端超时设 1 秒、服务端睡 2 秒，必须抛 Timeout。
    """
    with pytest.raises(requests.Timeout):
        requests.get(f"{base_url}/api/slow", params={"ms": 2000}, timeout=1)
