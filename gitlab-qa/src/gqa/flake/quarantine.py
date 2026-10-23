"""隔离区名单：给抖动用例一个「临时 space」，但收费。

隔离区最大的风险不是技术上的，是人性上的：一旦建立，它就会变成垃圾桶——
用例丢进去就再没人看。所以这里强制三条：

1. 每条必须有 **reason**（为什么隔离）和 **due**（什么时候前修好）；
2. **到期自动失效** —— 用例重新回到门禁里跑，隔离状态不会自动续期；
3. **规模受控** —— 数量超过上限就报警，防止整体治理失控。

对应的取舍写在 README：「不删除抖动用例，但也不允许它无限期消失」。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

LIST_FILE = "flake/quarantine.json"
DEFAULT_MAX_SIZE = 8


def _today() -> date:
    return date.today()


def _parse_day(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


@dataclass
class QuarantineEntry:
    nodeid: str
    reason: str
    owner: str = ""
    since: str = ""
    due: str = ""
    evidence: str = ""

    @property
    def due_day(self) -> date | None:
        return _parse_day(self.due)

    @property
    def expired(self) -> bool:
        d = self.due_day
        return d is not None and d < _today()

    @property
    def days_left(self) -> int | None:
        d = self.due_day
        return None if d is None else (d - _today()).days

    def check(self) -> list[str]:
        """返回这条记录的问题；空列表表示合规。"""
        problems = []
        if not self.nodeid.strip():
            problems.append("缺少 nodeid")
        if not self.reason.strip():
            problems.append(f"{self.nodeid}：缺少 reason（没有说明原因的隔离无法复盘）")
        if not self.due.strip():
            problems.append(f"{self.nodeid}：缺少 due（没有期限 = 永久逃票）")
        elif self.due_day is None:
            problems.append(f"{self.nodeid}：due 格式应为 YYYY-MM-DD，实为 {self.due!r}")
        return problems

    def describe(self) -> str:
        left = self.days_left
        if left is None:
            tail = "期限未知"
        elif left < 0:
            tail = f"已超期 {-left} 天"
        elif left == 0:
            tail = "今天到期"
        else:
            tail = f"还剩 {left} 天"
        who = f" / 负责人 {self.owner}" if self.owner else ""
        return f"{self.due}（{tail}）{who} · {self.reason}"


@dataclass
class Quarantine:
    entries: list[QuarantineEntry] = field(default_factory=list)

    # ------------------------------------------------------------ 读写
    @classmethod
    def load(cls, root: Path) -> "Quarantine":
        path = root / LIST_FILE
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{path} 不是合法 JSON：{exc}\n"
                "隔离区名单损坏会让所有检查静默失效，先把它修好再跑。"
            ) from exc
        raw = data.get("entries", []) if isinstance(data, dict) else data
        known = {f.name for f in QuarantineEntry.__dataclass_fields__.values()}
        return cls([QuarantineEntry(**{k: v for k, v in e.items() if k in known})
                    for e in raw])

    def save(self, root: Path) -> Path:
        path = root / LIST_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_readme": (
                "隔离区不是垃圾桶。每条都要写清 reason 与 due；"
                "到期后条目自动失效，用例回到门禁里继续跑。"
            ),
            "entries": [asdict(e) for e in self.entries],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return path

    # ------------------------------------------------------------ 查询
    def active(self) -> list[QuarantineEntry]:
        """仍在保护期内的条目：这些用例会被 skip。"""
        return [e for e in self.entries if not e.expired]

    def expired(self) -> list[QuarantineEntry]:
        return [e for e in self.entries if e.expired]

    def active_map(self) -> dict[str, QuarantineEntry]:
        return {e.nodeid: e for e in self.active()}

    def add(self, entry: QuarantineEntry) -> None:
        # 同一条用例重复登记没有意义，直接覆盖旧记录
        self.entries = [e for e in self.entries if e.nodeid != entry.nodeid]
        self.entries.append(entry)

    def remove(self, nodeid: str) -> bool:
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.nodeid != nodeid]
        return len(self.entries) < before

    # ------------------------------------------------------------ 体检
    def check(self, known_nodeids: set[str] | None = None,
              max_size: int = DEFAULT_MAX_SIZE) -> list[str]:
        """列出所有需要人介入的问题。CI 里用退出码把问题顶到脸上。"""
        problems: list[str] = []
        for e in self.entries:
            problems += e.check()

        for e in self.expired():
            problems.append(
                f"{e.nodeid}：隔离已于 {e.due} 到期，请修好它或把它从名单里删掉"
            )

        if known_nodeids is not None:
            for e in self.entries:
                if e.nodeid not in known_nodeids:
                    problems.append(
                        f"{e.nodeid}：名单里的用例已经不存在（改名或删除），记录过期"
                    )

        if len(self.entries) > max_size:
            problems.append(
                f"隔离区已有 {len(self.entries)} 条，超过上限 {max_size}——"
                "先消化存量，否则「临时豁免」会变成常态"
            )
        return problems

    def markdown(self) -> str:
        if not self.entries:
            return "隔离区为空。"
        lines = ["| 用例 | 期限 | 负责人 | 原因 |", "| --- | --- | --- | --- |"]
        for e in self.entries:
            flag = " **已过期**" if e.expired else ""
            lines.append(f"| `{e.nodeid}` | {e.due}{flag} | {e.owner or '-'} | {e.reason} |")
        return "\n".join(lines)
