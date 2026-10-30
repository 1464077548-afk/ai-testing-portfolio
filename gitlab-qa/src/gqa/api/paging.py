"""跨页完整性判据：翻完所有页，然后证明「不重复、不遗漏」。

为什么单独抽一层
----------------
分页断言最容易写成两种假绿：

1. **只看第一页**。`len(rows) <= per_page` 这种断言，在数据不足一页时恒真——
   服务端把 `page` 参数整个吞掉也照样通过。实测本框架原有的
   `test_per_page_over_limit` 就是这种情况：项目只有几十条，
   断言"返回不超过 100 条"永远不会失败。
2. **拿实现证明实现**。先把列表全量拉一次当"标准答案"，再逐页拉一次比对——
   两次走的是同一段代码，同一个缺陷会同时污染两边，比较结果永远相等。
   所以这里的 `expected` 必须来自**创建时记录的 id**，不是另一次列表查询。

本模块提供的判据，每条都对应一类真实缺陷：

    ┌──────────────────────┬──────────────────────────────────────────┐
    │ 判据                 │ 抓的缺陷                                 │
    ├──────────────────────┼──────────────────────────────────────────┤
    │ 跨页无重复           │ 排序不稳定 / 越界页偷偷回落到第一页       │
    │ 全集无遗漏           │ 只取第一页、offset 计算错、丢尾页         │
    │ 每页条数 == per_page │ 服务端把 per_page 悄悄改成默认 20         │
    │ X-Page 回显正确      │ 服务端根本没看 page 参数                  │
    │ X-Total 与实际一致   │ 总数谎报（前端据此画页码，一定会漏）      │
    │ 页数 == ceil(T/pp)   │ X-Total-Pages 算错                        │
    └──────────────────────┴──────────────────────────────────────────┘
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from gqa.api.client import GitLabClient

PAGE_HEADERS = ("X-Total", "X-Total-Pages", "X-Per-Page", "X-Page",
                "X-Next-Page", "X-Prev-Page")


def _int(value: str | None) -> int | None:
    """分页头是字符串，空串表示"没有下一页"，不能当 0 也不能当 None 混用。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class PageSlice:
    """一页的元信息。保留原始头，出问题时能直接贴进报告。"""

    page: int
    size: int
    total: int | None
    total_pages: int | None
    per_page: int | None
    next_page: int | None
    echoed_page: int | None 


@dataclass
class ScanReport:
    """一次完整跨页扫描的结果。"""

    path: str
    per_page: int
    key: str = "id"
    slices: list[PageSlice] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list) # 异常情况的描述

    # ---------------------------------------------------------------- 派生
    @property
    def ids(self) -> list[Any]:
        return [row.get(self.key) for row in self.rows]

    @property
    def duplicates(self) -> list[Any]:
        """跨页重复的值（按首次重复顺序，不重复列出）。"""
        seen: set[Any] = set()
        dup: list[Any] = []
        for value in self.ids:
            if value in seen and value not in dup:
                dup.append(value)
            seen.add(value)
        return dup

    @property
    def total_header(self) -> int | None:
        return self.slices[0].total if self.slices else None

    @property
    def declared_pages(self) -> int | None:
        return self.slices[0].total_pages if self.slices else None

    @property
    def page_sizes(self) -> list[int]:
        return [s.size for s in self.slices]

    def describe(self) -> str:
        lines = [
            f"{self.path}  per_page={self.per_page}  实扫 {len(self.slices)} 页 "
            f"/ {len(self.rows)} 条 / 去重 {len(set(self.ids))} 条",
            f"X-Total={self.total_header}  X-Total-Pages={self.declared_pages}",
            f"每页条数={self.page_sizes}",
        ]
        if self.duplicates:
            lines.append(f"重复值={self.duplicates[:10]}")
        lines.extend(f"异常：{a}" for a in self.anomalies)
        return "\n".join(lines)


def scan(client: GitLabClient, path: str, params: dict | None = None,
         per_page: int = 3, max_pages: int = 50, key: str = "id") -> ScanReport:
    """按 X-Next-Page 逐页翻到底，返回全量与每页元信息。

    刻意不用 `page += 1`：那会把"服务端说没有下一页了"这件事掩盖掉，
    也测不出 X-Next-Page 本身错到指回前面的页。用服务端给的下标推进，
    再由环路检测兜住"next 指回已访问页"的死循环。
    """
    report = ScanReport(path=path, per_page=per_page, key=key)
    query = dict(params or {})
    query["per_page"] = per_page
    page: int | None = 1
    visited: set[int] = set()

    while page is not None:
        if page in visited:
            report.anomalies.append(f"X-Next-Page 指回已访问的第 {page} 页（会死循环）")
            break
        if len(visited) >= max_pages:
            report.anomalies.append(f"翻页超过 {max_pages} 页仍未结束")
            break
        visited.add(page)
        query["page"] = page

        resp = client.get(path, params=query, expect=200)
        rows = resp.json_data
        if not isinstance(rows, list):
            report.anomalies.append(
                f"第 {page} 页返回的不是数组：{str(rows)[:120]}")
            break

        report.slices.append(PageSlice(
            page=page,
            size=len(rows),
            total=_int(resp.headers.get("X-Total")),
            total_pages=_int(resp.headers.get("X-Total-Pages")),
            per_page=_int(resp.headers.get("X-Per-Page")),
            next_page=_int(resp.headers.get("X-Next-Page")),
            echoed_page=_int(resp.headers.get("X-Page")),
        ))
        report.rows.extend(rows)

        next_page = _int(resp.headers.get("X-Next-Page"))
        # 空页却还给了下一页：不是致命错误，但意味着后面全是空页，
        # 继续跟下去只会把「多余的空页」记成正常。
        if not rows and next_page is not None:
            report.anomalies.append(f"第 {page} 页为空，却仍给出 X-Next-Page={next_page}")
            break
        page = next_page

    return report


def assert_scan_integrity(report: ScanReport, expected: list[Any] | None = None,
                          *, per_page: int | None = None, exact: bool = True,
                          check_total: bool = True) -> None:
    """把一次扫描的全部结论一次性判完，问题多时一次性列全。

    逐条 assert 的坏处是第一条失败就看不到后面的——排查时往往要跑好几轮
    才能把问题看全。这里收集成列表再抛，一次给出完整诊断。

    expected：创建时记录的 id（**不要**用另一次列表查询的结果当基准）。
    exact：扫描到的必须都在 expected 里。项目内新建资源一般成立；
           若列表里本来就有系统产生的记录（如 system note），传 False。
    """
    step = per_page or report.per_page
    problems: list[str] = list(report.anomalies)
    ids = report.ids
    unique = set(ids)

    # 1) 跨页重复
    if report.duplicates:
        problems.append(
            f"跨页出现重复 {len(report.duplicates)} 个：{report.duplicates[:8]}"
            "（排序不稳定，或越界页回落到第一页）")

    # 2) 遗漏 / 多余
    if expected is not None:
        wanted = set(expected)
        missing = [x for x in expected if x not in unique]
        if missing:
            problems.append(
                f"漏掉 {len(missing)}/{len(wanted)} 条：{missing[:8]}"
                "（只取第一页、offset 算错、或尾页丢失）")
        if exact:
            extra = [x for x in ids if x not in wanted]
            if extra:
                problems.append(
                    f"多出 {len(extra)} 条不在本次造数范围内：{extra[:8]}"
                    "（过滤条件没生效，或翻页串到了别的作用域）")

    # 3) 每页条数：除最后一页外必须满载
    for s in report.slices[:-1]:
        if s.size != step:
            problems.append(
                f"第 {s.page} 页只有 {s.size} 条，per_page={step} 时应满载"
                "（服务端把 per_page 悄悄改了，或中间页被截断）")

    # 4) X-Page 回显：服务端有没有真的看 page 参数
    for s in report.slices:
        if s.echoed_page is not None and s.echoed_page != s.page:
            problems.append(
                f"请求 page={s.page}，X-Page 回显 {s.echoed_page}（page 参数被忽略）")

    # 5) X-Total 与实际扫描量一致
    if check_total and report.total_header is not None:
        if report.total_header != len(unique):
            problems.append(
                f"X-Total 宣称 {report.total_header} 条，实际翻到 {len(unique)} 条"
                "（总数谎报，前端据此画页码必然漏数据）")

    # 6) 页数
    if report.declared_pages is not None:
        if report.total_header:
            expected_pages = math.ceil(report.total_header / step)
            if report.declared_pages != expected_pages:
                problems.append(
                    f"X-Total-Pages={report.declared_pages}，"
                    f"按 X-Total/per_page 应为 {expected_pages}")
        if report.declared_pages != len(report.slices):
            problems.append(
                f"X-Total-Pages 宣称 {report.declared_pages} 页，实际翻了 "
                f"{len(report.slices)} 页（翻页提前结束或越界）")

    assert not problems, (
        f"分页完整性校验失败（{report.path}，per_page={step}）：\n  - "
        + "\n  - ".join(problems)
        + "\n\n扫描明细：\n" + report.describe()
    )
