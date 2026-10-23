"""安全测试的目标守卫（信任边界）。

自动化安全测试一旦能指向任意地址，性质就变了——从"测试自己的系统"
变成"拿着工具扫别人"。所以所有 security 用例都必须先过这一关：
目标 host 在白名单里才执行，否则 **skip**（不是照跑，也不是 pass）。

白名单 = 默认本机地址 + GITLAB_BASE_URL 自己（用户明确配的，视为已授权）
       + SECURITY_ALLOWED_HOSTS 环境变量（逗号分隔，支持 * 通配）

为什么用 skip 而不是报错：安全用例挂在定时流水线里，
换个环境就报错会让整条流水线红得没有意义。
skip 会在报告里明确显示"没跑"，这跟"跑了并通过"是两回事，符合本框架的假绿零容忍。
"""

from __future__ import annotations

import fnmatch
import os
from urllib.parse import urlparse

ENV_KEY = "SECURITY_ALLOWED_HOSTS"

# 回环与容器宿主机别名：本机靶场的常规形态
DEFAULT_HOSTS = (
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "host.docker.internal",
    # CI 里用 service 容器时，别名通常就是服务名；这里放行内网段，
    # 公网地址一律不放行——需要显式授权
    "10.*.*.*",
    "192.168.*.*",
    "172.16.*.*",
    "172.17.*.*",
    "172.18.*.*",
)


def host_of(url: str) -> str:
    """取 host（小写），拿不到就返回空串——空 host 一律视为不允许。"""
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _from_env() -> list[str]:
    raw = os.getenv(ENV_KEY, "")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def allowed_hosts(base_url: str = "") -> list[str]:
    """当前生效的白名单。传 base_url 时把它自己的 host 也算进去。"""
    hosts: set[str] = set(DEFAULT_HOSTS)
    if base_url:
        h = host_of(base_url)
        if h:
            hosts.add(h)
    hosts.update(_from_env())
    return sorted(hosts)


def is_allowed(base_url: str) -> bool:
    host = host_of(base_url)
    if not host:
        return False
    return any(fnmatch.fnmatch(host, pat) for pat in allowed_hosts(base_url))


def describe(base_url: str) -> str:
    """给 skip 用的说明：为什么没跑、要跑得怎么授权。"""
    return (
        f"目标 {base_url or '<空>'}（host={host_of(base_url) or '未知'}）不在安全测试白名单内。"
        f"当前白名单：{', '.join(allowed_hosts(base_url))}。"
        f"确认目标归你所有后，用 {ENV_KEY} 显式授权（逗号分隔，支持 * 通配，"
        f"例如 {ENV_KEY}=gitlab.internal.example.com）。"
    )
