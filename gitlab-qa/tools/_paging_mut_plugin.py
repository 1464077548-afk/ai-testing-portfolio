"""变异注入插件：篡改列表响应，模拟服务端分页缺陷。

用法（由 tools/paging_mutation_check.py 调用，不给人手工跑）：

    GQA_MUTATION=ignore_page pytest ... -p tools._paging_mut_plugin

为什么要这么做
--------------
「用例全绿」只证明了用例能跑通，证明不了它能抓到缺陷。真正要回答的问题是：
**如果服务端分页坏了，这些断言会不会红？** 把 5 种典型缺陷逐一注入，
每一种都必须让用例失败——否则那条用例就是装饰品。

作用范围刻意收得很窄：只打 target 项目下的 issues 列表，且只改 GET。
造数全是 POST，清理走 DELETE，都不会被误伤——变异本身要是把环境搞坏了，
后面看到的红就分不清是缺陷还是自伤。
"""

from __future__ import annotations

import os

MUTATION = os.getenv("GQA_MUTATION", "").strip()
TARGET_SUFFIX = "/issues"


def _is_target(path: str, method: str) -> bool:
    return method.upper() == "GET" and path.rstrip("/").endswith(TARGET_SUFFIX)


def _wrap(orig):
    def inner(self, method, path, params=None, json_body=None, expect=None):
        resp = orig(self, method, path, params=params, json_body=json_body,
                    expect=expect)
        if not MUTATION or not _is_target(path, method):
            return resp

        query = dict(params or {})
        try:
            page = int(query.get("page") or 1)
        except (TypeError, ValueError):
            page = 1

        if MUTATION == "first_page_only" and page > 1:
            # 模拟「翻页逻辑没实现」：请求第 2 页给空
            resp.json_data = []
            resp.headers["X-Next-Page"] = ""

        elif MUTATION == "ignore_page" and page > 1:
            # 模拟「服务端根本不看 page 参数」：永远回第一页
            again = dict(query, page=1)
            first = orig(self, method, path, params=again,
                         json_body=None, expect=expect)
            resp.json_data = first.json_data

        elif MUTATION == "shrink_per_page":
            # 模拟「页宽被悄悄改成 1」：X-Per-Page 与条数一起缩水
            again = dict(query, per_page=1)
            small = orig(self, method, path, params=again,
                         json_body=None, expect=expect)
            resp.json_data = small.json_data
            for key, value in small.headers.items():
                if key.startswith("X-"):
                    resp.headers[key] = value

        elif MUTATION == "inflate_total":
            # 模拟「总数谎报」：前端按它画页码，最后几页一定是空的
            try:
                resp.headers["X-Total"] = str(int(resp.headers.get("X-Total", "0")) + 3)
            except ValueError:
                pass

        elif MUTATION == "skip_one" and page >= 2 and resp.json_data:
            # 模拟「offset 少算 1 条」：从第 2 页起整段前移，丢掉一条
            resp.json_data = resp.json_data[1:]

        return resp

    return inner


def pytest_configure(config):
    if not MUTATION:
        return
    from gqa.api.client import GitLabClient

    if getattr(GitLabClient.request, "_gqa_mutated", False):
        return
    wrapped = _wrap(GitLabClient.request)
    wrapped._gqa_mutated = True  # type: ignore[attr-defined]
    GitLabClient.request = wrapped  # type: ignore[method-assign]
