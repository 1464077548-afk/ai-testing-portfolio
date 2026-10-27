"""接口覆盖率采集。

三件事：
1. 归一化：把真实请求路径里的具体值换成占位符，/projects/1000/issues/3
   和 /projects/1001/issues/7 应当算作同一个端点，否则覆盖率永远接近 0；
2. 命中登记：每次请求后对照 catalog 找归属端点，计数 +1；
3. 目录外调用单独记录：框架调了没登记的端点，是目录该补还是用例写野了，
   必须让人看见，不能悄悄丢掉。

统计口径刻意只用「发出过请求」作为命中标准：断言强弱是另一回事，
覆盖率回答的是「这些接口有没有被测到」这一个问。
"""

from __future__ import annotations

import re
import threading
from urllib.parse import unquote, urlparse

from gqa.api.catalog import CATALOG, Endpoint

# 40 位 hex：commit sha
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# 8 位短 sha
_SHORT_SHA_RE = re.compile(r"^[0-9a-f]{7,12}$")
# UUID
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def normalize_path(path: str) -> str:
    """把路径里的具体值换成占位符。

    URL 编码过的 namespace（qa-org%2Fproj）必须整段认成 {path}：
    它是「按完整路径访问」这条独立语义，和按 id 访问不是一回事，
    混在一起算会让覆盖率虚高。
    """
    if "://" in path:
        path = urlparse(path).path
    # 去掉 /api/v4 前缀：目录里存的是端点相对路径
    path = re.sub(r"^/api/v\d+", "", path)
    path = path.split("?")[0]
    path = path.rstrip("/") or "/"

    segs: list[str] = []
    for seg in path.split("/"):
        if not seg:
            segs.append("")
            continue
        decoded = unquote(seg)
        if "%2F" in seg.upper() or "/" in decoded:
            segs.append("{path}")
        elif seg.isdigit():
            segs.append("{id}")
        elif _SHA_RE.match(seg):
            segs.append("{sha}")
        elif _UUID_RE.match(seg):
            segs.append("{uuid}")
        else:
            segs.append(seg)
    return "/".join(segs)


# 归一化会真正替换成字面量的占位符：它们在路径里就是 "{id}" 这几个字符，
# 必须按字面量匹配。若统一按「任意一段」处理，GET /projects/{id} 会抢走
# GET /projects/{path} 的命中——两者在 URL 里都是一段，
# 于是覆盖率看起来挺高，实际「按路径访问」这条语义从没被测到过。
_LITERAL_PLACEHOLDERS = {"{id}", "{path}", "{sha}", "{uuid}"}


def _template_regex(template: str) -> re.Pattern[str]:
    """目录模板 → 正则。"""
    parts = re.split(r"(\{[^}]+\})", template)
    out = ""
    for part in parts:
        if part.startswith("{") and part.endswith("}"):
            if part in _LITERAL_PLACEHOLDERS:
                out += re.escape(part)
            else:
                # 分支名、文件名、标签名保持原值，按任意一段匹配
                out += r"[^/]+"
        else:
            out += re.escape(part)
    return re.compile(f"^{out}$")


# 分母只取「当前版本还可达」的端点。已移除的（removed_in 非空）不参与匹配，
# 于是谁再调它们会落进 out_of_catalog——正好提示「这条路由已经没了」。
ACTIVE: list[Endpoint] = [ep for ep in CATALOG if ep.active]
REMOVED: list[Endpoint] = [ep for ep in CATALOG if not ep.active]


class Coverage:
    """一次会话内累积的端点命中情况。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.hits: dict[str, int] = {}
        self.unknown: dict[str, int] = {}
        self._regex: list[tuple[Endpoint, re.Pattern[str]]] = [
            (ep, _template_regex(ep.template)) for ep in ACTIVE
        ]

    # ------------------------------------------------------------ 采集
    def record(self, method: str, path: str) -> str | None:
        """登记一次调用，返回命中的端点 key；没命中返回 None。"""
        norm = normalize_path(path)
        method = method.upper()
        with self._lock:
            for ep, pattern in self._regex:
                if ep.method == method and pattern.match(norm):
                    key = ep.key
                    self.hits[key] = self.hits.get(key, 0) + 1
                    return key
            self.unknown[f"{method} {norm}"] = self.unknown.get(f"{method} {norm}", 0) + 1
            return None

    def merge_hits(self, hits: dict[str, int], unknown: dict[str, int] | None = None) -> None:
        """合并另一个进程的采集结果。

        xdist 并行时每个 worker 只看到自己跑过的那部分用例，
        覆盖率必须在主进程汇总，否则会低得离谱——
        表现为「明明跑了很多接口，报告上只有 20%」。
        """
        with self._lock:
            for k, v in hits.items():
                self.hits[k] = self.hits.get(k, 0) + v
            for k, v in (unknown or {}).items():
                self.unknown[k] = self.unknown.get(k, 0) + v

    # ------------------------------------------------------------ 统计
    @property
    def total(self) -> int:
        return len(ACTIVE)

    @property
    def covered(self) -> int:
        # 只数分母内的命中：命中了已移除端点不算覆盖
        active_keys = {ep.key for ep in ACTIVE}
        return len([k for k in self.hits if k in active_keys])

    @property
    def rate(self) -> float:
        return self.covered / self.total if self.total else 0.0

    def covered_endpoints(self) -> list[Endpoint]:
        keys = set(self.hits)
        return [ep for ep in ACTIVE if ep.key in keys]

    def missing_endpoints(self) -> list[Endpoint]:
        keys = set(self.hits)
        return [ep for ep in ACTIVE if ep.key not in keys]

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "covered": self.covered,
            "rate": round(self.rate, 4),
            "covered_endpoints": {k: v for k, v in sorted(self.hits.items())},
            "missing_endpoints": [ep.key for ep in self.missing_endpoints()],
            "out_of_catalog": dict(sorted(self.unknown.items())),
            "removed_endpoints": [
                {"key": ep.key, "removed_in": ep.removed_in, "note": ep.note}
                for ep in REMOVED
            ],
        }

    # ------------------------------------------------------------ 呈现
    def summary_lines(self) -> list[str]:
        lines = [
            f"接口覆盖率 {self.covered}/{self.total} = {self.rate:.1%}"
            f"（目录端点 {self.total}，本次调用过 {self.covered}）",
            f"目录外调用 {len(self.unknown)} 类"
            + ("" if not self.unknown else "：" + "、".join(list(self.unknown)[:5])),
        ]
        if REMOVED:
            lines.append(
                f"已移除端点 {len(REMOVED)} 条（不计入分母）："
                + "、".join(f"{ep.key}→{ep.note}" for ep in REMOVED)
            )
        return lines

    def markdown(self) -> str:
        """按分组输出表格，未覆盖的端点显式列出——这是改进清单。"""
        from gqa.api.catalog import by_group

        lines = [
            "# GitLab CE 接口覆盖率",
            "",
            f"- 目录端点总数：**{self.total}**",
            f"- 本次会话调用过：**{self.covered}**（{self.rate:.1%}）",
            f"- 未覆盖：**{self.total - self.covered}**",
            "",
            "## 按分组",
            "",
            "| 分组 | 覆盖 | 端点数 | 覆盖率 |",
            "| --- | --- | --- | --- |",
        ]
        for group, eps in by_group().items():
            hit = sum(1 for ep in eps if ep.key in self.hits)
            lines.append(f"| {group} | {hit} | {len(eps)} | {hit / len(eps):.0%} |")

        lines += ["", "## 已覆盖端点（调用次数）", "", "| 方法 | 端点 | 次数 | 说明 |", "| --- | --- | --- | --- |"]
        hits = {k: v for k, v in sorted(self.hits.items())}
        by_key = {ep.key: ep for ep in CATALOG}
        for key, count in hits.items():
            ep = by_key.get(key)
            if ep is None:
                continue
            lines.append(f"| {ep.method} | `{ep.template}` | {count} | {ep.name} |")

        missing = self.missing_endpoints()
        if missing:
            lines += ["", "## 未覆盖端点（改进清单）", "",
                      "| 方法 | 端点 | 说明 | 需管理员 |", "| --- | --- | --- | --- |"]
            for ep in missing:
                lines.append(f"| {ep.method} | `{ep.template}` | {ep.name} | "
                             f"{'是' if ep.admin else '否'} |")

        if self.unknown:
            lines += ["", "## 目录外调用", "",
                      "以下调用没能在目录里找到归属——要么补进 catalog.py，"
                      "要么说明用例里混进了不该有的路径。", "",
                      "| 调用 | 次数 |", "| --- | --- |"]
            for key, count in sorted(self.unknown.items()):
                lines.append(f"| `{key}` | {count} |")
        return "\n".join(lines)


_COVERAGE = Coverage()


def record_call(method: str, path: str) -> str | None:
    """供 client 埋点。"""
    return _COVERAGE.record(method, path)


def current() -> Coverage:
    return _COVERAGE
