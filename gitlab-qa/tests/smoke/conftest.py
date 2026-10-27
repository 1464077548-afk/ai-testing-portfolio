"""冒烟层专用夹具。

两个东西是冒烟层特有的，放这里而不是根 conftest：

1. **假 Token 客户端**。冒烟要在「还没配好认证」时就能跑，
   所以它不能依赖真实 Token；但要验证「鉴权没被关掉」，
   又必须拿一个凭证去试。用一个形似而实非的 Token 正好：
   真实 Token 缺失时它照样能测出「服务端到底认不认证」。

2. **非 API 路径的探针**。GitLabClient 会给路径自动拼 `/api/v4` 前缀，
   而 `/-/health` 这类运维探针在 API 命名空间之外——拼上前缀得到的 404
   是测试自己造的，不是服务的真实状态。所以探针走 raw 发送器原样上线。
"""

from __future__ import annotations

import pytest

from gqa.api.client import GitLabClient
from gqa.security.raw import raw_get


@pytest.fixture(scope="session")
def bogus_client(settings):
    """持有一个「看起来像 glpat- 其实是随机串」的 Token 的客户端。"""
    c = GitLabClient(
        settings.base_url,
        token="glpat-" + "Zx9" * 8,   # 长度和形态都像真的，值不存在
        timeout=settings.timeout,
    )
    yield c
    c.close()


@pytest.fixture(scope="session")
def probe(settings):
    """发一个原样上线的 GET，用于 /-/health 这类 API 之外的路径。"""

    def _get(path: str, headers: dict[str, str] | None = None):
        return raw_get(settings.base_url, path, headers=headers,
                       timeout=min(settings.timeout, 10.0))
    return _get
