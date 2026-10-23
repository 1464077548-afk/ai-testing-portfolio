"""稳定性度量与 flake 治理的核心模型。

之所以单独成一个模块，是因为「稳定性」不是一个布尔值，而是四类结论。
把抖动和真失败混为一谈，是 flake 治理最常见的起点错误：

    STABLE    N 轮全绿              —— 结论可信
    BROKEN    N 轮全红              —— 这是缺陷，不是抖动，别把它放进隔离区
    FLAKY     有绿有红              —— 真抖动，抖动率 = 红 /（绿 + 红）
    NO_DATA   N 轮全是 skip         —— 没测量到，把它算成稳定是最危险的假象

最后一类最容易被忽略：一条因缺少 Token 而 skip 的用例，在报告里同样「没红」，
和通过长得一模一样。区分不了这两件事，稳定性度量就是自欺欺人。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
# 某一轮里这条用例压根没出现在结果里（那轮提前崩了 / 收集中断）。
# 不计为通过——少一次测量不能凭空补成绿。
MISSING = "missing"

STABLE = "stable"
BROKEN = "broken"
FLAKY = "flaky"
NO_DATA = "no_data"

VERDICT_LABEL = {
    STABLE: "稳定",
    BROKEN: "持续失败",
    FLAKY: "抖动",
    NO_DATA: "无有效数据",
}

# 报告里按严重程度排序：先看真失败，再看抖动，最后才是免检
VERDICT_ORDER = {BROKEN: 0, FLAKY: 1, NO_DATA: 2, STABLE: 3}


def nodeid_from_junit(case: ET.Element) -> str:
    """从 JUnit 的 <testcase> 还原 pytest 的 nodeid（`路径::类名::用例名`）。

    JUnit XML 把模块写成包路径（tests.api.test_auth），把类塞进 classname，
    两边拼回来的规则不一样，容易错位——而 nodeid 一旦对不上，
    隔离区名单就永远匹配不到任何用例（表现为"加了隔离却照跑不误"）。
    """
    file_attr = (case.get("file") or "").replace("\\", "/")
    classname = case.get("classname") or ""
    name = case.get("name") or ""

    if file_attr:
        module_pkg = file_attr[:-3].replace("/", ".") if file_attr.endswith(".py") else ""
        cls = ""
        if module_pkg and classname.startswith(module_pkg + "."):
            cls = classname[len(module_pkg) + 1:]
        elif module_pkg and classname != module_pkg:
            cls = classname
        base = file_attr
    else:
        # 兜底：没有 file 属性时只能按「包名 → 路径」硬推，类信息会丢
        base = classname.replace(".", "/") + ".py"
        cls = ""
    return f"{base}::{cls}::{name}" if cls else f"{base}::{name}"


def read_junit(path: Path) -> dict[str, str]:
    """读一轮结果：nodeid → 结果。

    error 与 failure 一并算 failed：对本模型关心的"这条用例稳不稳"来说，
    fixture 报错导致的 error 同样是没跑过。
    """
    out: dict[str, str] = {}
    root = ET.parse(path).getroot()
    for case in root.iter("testcase"):
        nodeid = nodeid_from_junit(case)
        if case.find("failure") is not None or case.find("error") is not None:
            out[nodeid] = FAILED
        elif case.find("skipped") is not None:
            out[nodeid] = SKIPPED
        else:
            out[nodeid] = PASSED
    return out


@dataclass
class TestStability:
    """单条用例跨 N 轮的稳定画像。"""

    nodeid: str
    outcomes: tuple[str, ...] = ()

    @property
    def passed(self) -> int:
        return self.outcomes.count(PASSED)

    @property
    def failed(self) -> int:
        return self.outcomes.count(FAILED)

    @property
    def skipped(self) -> int:
        return self.outcomes.count(SKIPPED)

    @property
    def missing(self) -> int:
        return self.outcomes.count(MISSING)

    @property
    def effective(self) -> int:
        """真正得出结论的轮次：skip 不算测量。"""
        return self.passed + self.failed

    @property
    def verdict(self) -> str:
        if self.effective == 0:
            return NO_DATA
        if self.failed == 0:
            return STABLE
        if self.passed == 0:
            return BROKEN
        return FLAKY

    @property
    def flake_rate(self) -> float:
        """失败占比。全红是 1.0，但它属于 BROKEN 而不是"100% 抖动"。"""
        return self.failed / self.effective if self.effective else 0.0

    @property
    def pattern(self) -> str:
        """每一轮结果的紧凑写法，例如 PFPFP —— 一眼看出是不是周期性失败。"""
        return "".join(o[0].upper() for o in self.outcomes)

    def to_dict(self) -> dict:
        return {
            "nodeid": self.nodeid,
            "verdict": self.verdict,
            "verdict_label": VERDICT_LABEL[self.verdict],
            "pattern": self.pattern,
            "rounds": len(self.outcomes),
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "flake_rate": round(self.flake_rate, 4),
        }


@dataclass
class StabilityReport:
    suite: str
    rounds: int
    started: str = ""
    duration_sec: float = 0.0
    tests: list[TestStability] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.started:
            self.started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.tests.sort(key=lambda t: (VERDICT_ORDER[t.verdict], -t.flake_rate, t.nodeid))

    # ---------------------------------------------------------------- 统计
    def counts(self) -> dict[str, int]:
        c = dict.fromkeys(VERDICT_ORDER, 0)
        for t in self.tests:
            c[t.verdict] += 1
        return c

    @property
    def total(self) -> int:
        return len(self.tests)

    def worst(self, verdict: str, limit: int = 10) -> list[TestStability]:
        return [t for t in self.tests if t.verdict == verdict][:limit]

    @property
    def measured(self) -> int:
        return sum(1 for t in self.tests if t.verdict != NO_DATA)

    @property
    def overall_flake_rate(self) -> float:
        """整套抖动的总体占比：失败轮次 / 有效轮次。"""
        tot_f = sum(t.failed for t in self.tests)
        tot_e = sum(t.effective for t in self.tests)
        return tot_f / tot_e if tot_e else 0.0

    def headline(self) -> str:
        c = self.counts()
        if not self.total:
            return "没有收集到任何用例"
        return (
            f"{self.rounds} 轮 · {self.total} 条用例 · "
            f"稳定 {c[STABLE]} / 抖动 {c[FLAKY]} / 持续失败 {c[BROKEN]} / 无数据 {c[NO_DATA]}"
            f" · 总体失败轮占比 {self.overall_flake_rate:.1%}"
        )

    # ---------------------------------------------------------------- 输出
    def to_dict(self) -> dict:
        return {
            "suite": self.suite,
            "rounds": self.rounds,
            "started": self.started,
            "duration_sec": round(self.duration_sec, 2),
            "counts": self.counts(),
            "overall_flake_rate": round(self.overall_flake_rate, 4),
            "tests": [t.to_dict() for t in self.tests],
        }

    def to_text(self) -> str:
        lines = [f"# 稳定性报告 · {self.suite}", "",
                 f"- 开始时间：{self.started}",
                 f"- 重复轮次：{self.rounds}",
                 f"- 耗时：{self.duration_sec:.1f}s",
                 f"- 结论：{self.headline()}", ""]

        for verdict in (BROKEN, FLAKY, NO_DATA, STABLE):
            group = [t for t in self.tests if t.verdict == verdict]
            if not group:
                continue
            title = {
                BROKEN: "## 持续失败（真缺陷，优先修）",
                FLAKY: "## 抖动（需证据，进隔离区并限期修复）",
                NO_DATA: "## 无有效数据（全是 skip，等于没测）",
                STABLE: "## 稳定",
            }[verdict]
            lines.append(title)
            lines.append("")
            if verdict == STABLE:
                lines.append(f"- {len(group)} 条全部通过，不逐条列出")
                lines.append("")
                continue
            lines.append("| 用例 | 各轮 | 通过 | 失败 | 抖动率 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for t in group:
                lines.append(
                    f"| `{t.nodeid}` | {t.pattern} | {t.passed} | {t.failed} "
                    f"| {t.flake_rate:.0%} |"
                )
            lines.append("")
        return "\n".join(lines)

    # 兼容：Makefile / README 里叫 markdown 更直观
    @property
    def markdown(self) -> str:
        return self.to_text()


def build_report(suite: str, per_round: list[dict[str, str]], duration_sec: float = 0.0) -> StabilityReport:
    """把 N 轮的字典结果合成一份报告。

    某条用例在某一轮里没出现（比如那轮提前崩了），记为缺而不是"通过"——
    少一次测量不能凭空补成绿。
    """
    nodeids = sorted({n for r in per_round for n in r})
    tests = [
        TestStability(nodeid=n, outcomes=tuple(r.get(n, "missing") for r in per_round))
        for n in nodeids
    ]
    return StabilityReport(suite=suite, rounds=len(per_round),
                           tests=tests, duration_sec=duration_sec)
