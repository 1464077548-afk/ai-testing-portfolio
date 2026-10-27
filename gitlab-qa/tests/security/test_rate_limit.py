"""限流与暴力破解防护。

这一组有个额外的自我约束：**不允许把管理员账号锁住**。
所以失败登录用的是一批不存在的用户名——既触发失败计数，
又不会让真实账号进入锁定状态。
（GitLab 的失败登录计数器是按 IP + 用户名双重计的，
  一直用真实用户名重试会锁住账号，届时连用例都没法收拾。）

判定口径：不强断言"必须返回 429"。
GitLab CE 是否开启 Rack::Attack 取决于实例配置，
没开不是缺陷、只是没启用。真正必须成立的只有一条：
**错误凭据永远不能登录成功**。
429 出现了就在报告里明确记一笔"限流已生效"。
"""

from __future__ import annotations

import time

import allure
import pytest

from gqa.security.raw import raw_get

pytestmark = [pytest.mark.security]

ATTEMPTS = 6
FAKE_USER = "qa-no-such-user-{n}-" + str(int(time.time()) % 100000)


@allure.epic("GitLab 安全")
@allure.feature("限流")
@allure.story("连续失败登录不会成功")
@allure.severity(allure.severity_level.BLOCKER)
def test_repeated_failed_logins_never_succeed(target):
    """连试 6 次错误密码：一次都不能成功，且最好能观察到限流。

    用不存在的用户名是刻意的——真实用户名连续失败会触发账号锁定，
    把自己锁在外面不说，还会让后续所有用例一起红。
    """
    from gqa.api.client import GitLabClient

    statuses: list[str] = []
    client = GitLabClient(target.base_url, token="", timeout=min(target.timeout, 10.0))
    try:
        for i in range(ATTEMPTS):
            resp = client.post(
                "/session",
                json_body={
                    "login": FAKE_USER.format(n=i),
                    "password": "definitely-wrong-password-" + str(i),
                },
                expect=(200, 201, 400, 401, 403, 404, 409, 422, 429),
            )
            statuses.append(str(resp.status))
            if resp.status == 429:
                break   # 已经确认限流生效，不必继续加压
    finally:
        client.close()

    summary = "、".join(statuses)
    throttled = "429" in statuses

    allure.attach(
        f"尝试 {len(statuses)} 次，状态码：{summary}\n"
        f"观察到限流：{'是' if throttled else '否（该实例可能未启用 Rack::Attack）'}",
        name="失败登录尝试", attachment_type=allure.attachment_type.TEXT,
    )

    assert len(statuses) > 0, "一次请求都没发出，无法判断"
    assert not any(s in ("200", "201") for s in statuses), (
        f"错误凭据竟然登录成功（状态码序列：{summary}）——认证逻辑存在严重缺陷"
    )


@allure.epic("GitLab 安全")
@allure.feature("限流")
@allure.story("限流响应头自洽")
@allure.severity(allure.severity_level.NORMAL)
def test_rate_limit_headers_are_consistent(target):
    """如果实例声明了 RateLimit-Limit，那么 Remaining 不该超过 Limit。

    不一致说明限流计数实现有问题（比如每节点各算一份），
    用户会看到"明明还有配额却被拒"。
    没有这些头就 skip——那是没启用，不是错。
    """
    resp = raw_get(target.base_url, "/api/v4/version",
                   timeout=min(target.timeout, 10.0))
    headers = resp.headers

    limit = headers.get("ratelimit-limit") or headers.get("rate-limit-limit")
    remaining = headers.get("ratelimit-remaining") or headers.get("rate-limit-remaining")

    allure.attach(
        "\n".join(f"{k}: {v}" for k, v in sorted(headers.items())
                  if "rate" in k.lower() or "retry" in k.lower())
        or "（该实例未返回任何限流相关响应头）",
        name="限流响应头", attachment_type=allure.attachment_type.TEXT,
    )

    if not limit:
        pytest.skip("该实例未返回 RateLimit-Limit 头（限流未启用或由网关统一处理）")

    assert int(limit) > 0, f"RateLimit-Limit 不是正数：{limit}"
    if remaining is not None:
        assert int(remaining) <= int(limit), (
            f"剩余配额 {remaining} 超过了上限 {limit}——限流计数不自洽"
        )
