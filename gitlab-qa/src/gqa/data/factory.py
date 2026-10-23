"""数据工厂与资源回收。

企业级框架里「测试数据」是最容易失控的一块，这里的规则有三条：
1. 每个资源带 run-scoped 前缀，多轮运行、多人并行都不会撞名；
2. 创建即登记，session 结束统一清理，用例自己不用管；
3. 清理失败要单独记录成一条 Allure 附件——污染的下游比失败的用例更贵。
"""

from __future__ import annotations

import random
import string
import time
from dataclasses import dataclass, field

from gqa.api.resources import GitLab


def _suffix(n: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.Random(time.time_ns()).choices(alphabet, k=n))


@dataclass
class ResourceLedger:
    """本次会话创建过的所有资源，按删除顺序倒序清理。"""

    projects: list[int] = field(default_factory=list)
    groups: list[int] = field(default_factory=list)
    issues: list[tuple[int, int]] = field(default_factory=list)
    cleanup_log: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.cleanup_log.append(msg)

    def report(self) -> str:
        if not (self.projects or self.groups or self.issues):
            return "本次会话未创建任何资源"
        lines = [
            f"创建资源：项目 {len(self.projects)} / 群组 {len(self.groups)} / "
            f"议题 {len(self.issues)}",
            "",
        ]
        lines += self.cleanup_log or ["（无清理记录）"]
        return "\n".join(lines)


class DataFactory:
    """生成唯一命名的测试数据，并负责回收。"""

    def __init__(self, gl: GitLab, prefix: str = "qa-auto") -> None:
        self.gl = gl
        self.prefix = f"{prefix}-{_suffix(6)}"
        self.ledger = ResourceLedger()
        self._counter = 0
        # pid -> path_with_namespace，永久删除时要带上
        self._project_paths: dict[int, str] = {}

    def _uniq(self, kind: str) -> str:
        self._counter += 1
        return f"{self.prefix}-{kind}-{self._counter}"

    # ------------------------------------------------------------ 创建
    def project(self, name: str | None = None, **extra) -> dict:
        name = name or self._uniq("proj")
        extra.setdefault("visibility", "private")
        extra.setdefault("initialize_with_readme", True)
        resp = self.gl.projects.create(name, **extra)
        data = resp.json_data
        pid = data["id"]
        self.ledger.projects.append(pid)
        # 记下完整路径：永久删除接口要求带 full_path，而清理时项目可能已改名
        self._project_paths[pid] = data.get("path_with_namespace", "")
        return data

    def issue(self, project_id: int, title: str | None = None, **extra) -> dict:
        title = title or self._uniq("issue")
        resp = self.gl.issues.create(project_id, title, **extra)
        data = resp.json_data
        self.ledger.issues.append((project_id, data["iid"]))
        return data

    def group(self, name: str | None = None) -> dict:
        name = name or self._uniq("group")
        path = name.lower().replace("_", "-")
        resp = self.gl.groups.create(name, path)
        gid = resp.json_data["id"]
        self.ledger.groups.append(gid)
        return resp.json_data

    # ------------------------------------------------------------ 回收
    def cleanup(self) -> None:
        # 先删议题（依附于项目），再删项目，最后删群组
        for project_id, iid in self.ledger.issues:
            try:
                self.gl.client.delete(
                    f"/projects/{project_id}/issues/{iid}", expect=(200, 204, 404)
                )
                self.ledger.note(f"[ok] 删除议题 {project_id}#{iid}")
            except Exception as exc:  # noqa: BLE001
                self.ledger.note(f"[残留] 议题 {project_id}#{iid}: {exc}")

        for pid in self.ledger.projects:
            try:
                # 走永久删除：GitLab 默认「延迟删除」会先把项目标记待删除再保留
                # N 天，跑几十轮测试就堆上百个僵尸项目，把项目列表页拖到超时，
                # 表现为 UI 用例莫名其妙 timeout —— 排查时会完全想不到是清理方式的问题。
                self.gl.projects.purge(pid, self._project_paths.get(pid, ""))
                self.ledger.note(f"[ok] 永久删除项目 {pid}")
            except Exception as exc:  # noqa: BLE001
                # 降级为普通删除：永久删除需要管理员权限，普通 Token 下会失败
                try:
                    self.gl.projects.delete(pid)
                    self.ledger.note(f"[ok] 删除项目 {pid}（延迟删除）")
                except Exception as exc2:  # noqa: BLE001
                    self.ledger.note(f"[残留] 项目 {pid}: {exc2}")

        for gid in self.ledger.groups:
            try:
                self.gl.groups.delete(gid)
                self.ledger.note(f"[ok] 删除群组 {gid}")
            except Exception as exc:  # noqa: BLE001
                self.ledger.note(f"[残留] 群组 {gid}: {exc}")

        self.ledger.projects.clear()
        self.ledger.groups.clear()
        self.ledger.issues.clear()
