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

from gqa.api.client import GitLabError
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
    labels: list[tuple[int, int]] = field(default_factory=list)
    milestones: list[tuple[int, int]] = field(default_factory=list)
    notes: list[tuple[int, int, int]] = field(default_factory=list)
    branches: list[tuple[int, str]] = field(default_factory=list)
    tags: list[tuple[int, str]] = field(default_factory=list)
    snippets: list[int] = field(default_factory=list)
    users: list[int] = field(default_factory=list)
    cleanup_log: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.cleanup_log.append(msg)

    def report(self) -> str:
        counted = (self.projects, self.groups, self.issues, self.labels,
                   self.milestones, self.notes, self.branches, self.snippets)
        if not any(counted):
            # cleanup() 会清空上面这些列表，而 conftest 恰恰是在 cleanup()
            # **之后**才调 report() 贴附件——原来的实现直接返回
            # "本次会话未创建任何资源"，把整份清理记录吞掉了：
            # 报告里那一栏永远空白，出问题时看不到哪条没删掉。
            if self.cleanup_log:
                return ("本次会话的资源已回收，明细：\n\n"
                        + "\n".join(self.cleanup_log))
            return "本次会话未创建任何资源"
        lines = [
            f"创建资源：项目 {len(self.projects)} / 群组 {len(self.groups)} / "
            f"议题 {len(self.issues)} / 标签 {len(self.labels)} / "
            f"里程碑 {len(self.milestones)} / 评论 {len(self.notes)} / "
            f"分支 {len(self.branches)} / 标签(tag) {len(self.tags)} / "
            f"片段 {len(self.snippets)}",
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

        # 建项目是所有用例的**前置动作**，它一失败整批用例报的是 fixture error
        # 而不是用例失败——报告上看不出到底哪里坏了。这里重试一次
        # （并行跑时实例偶发 429/超时），仍失败就把服务端原话抛出来。
        try:
            resp = self.gl.projects.create(name, **extra)
        except GitLabError as first:
            time.sleep(1.5)
            try:
                resp = self.gl.projects.create(f"{name}-r", **extra)
            except GitLabError as second:
                raise RuntimeError(
                    f"创建测试项目失败：首次 {first.status} {first.message}；"
                    f"重试后 {second.status} {second.message}；"
                    f"请求 name={name} extra={extra}"
                ) from second

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

    def label(self, project_id: int, name: str | None = None,
              color: str = "#0033cc") -> dict:
        name = name or self._uniq("label")
        resp = self.gl.labels.create(project_id, name, color)
        data = resp.json_data
        self.ledger.labels.append((project_id, data["id"]))
        return data

    def milestone(self, project_id: int, title: str | None = None, **extra) -> dict:
        title = title or self._uniq("milestone")
        resp = self.gl.milestones.create(project_id, title, **extra)
        data = resp.json_data
        self.ledger.milestones.append((project_id, data["id"]))
        return data

    def note(self, project_id: int, iid: int, body: str = "由自动化创建的评论") -> dict:
        resp = self.gl.notes.create(project_id, iid, body)
        data = resp.json_data
        self.ledger.notes.append((project_id, iid, data["id"]))
        return data

    def branch(self, project_id: int, name: str | None = None, ref: str = "main") -> dict:
        """创建分支。默认分支名由项目决定，写死 main 在别的实例上会 404。"""
        name = name or self._uniq("branch")
        resp = self.gl.repository.create_branch(project_id, name, ref)
        data = resp.json_data
        self.ledger.branches.append((project_id, name))
        return data

    def file(self, project_id: int, path: str, content: str, branch: str = "main"):
        """写文件。不登记回收：它随项目一起消失，单独删反而多一次失败点。"""
        return self.gl.repository.create_file(
            project_id, path, branch, content, f"qa: add {path}")

    def tag(self, project_id: int, name: str | None = None, ref: str = "main") -> dict:
        name = name or self._uniq("tag")
        resp = self.gl.repository.create_tag(project_id, name, ref)
        data = resp.json_data
        self.ledger.tags.append((project_id, name))
        return data

    def merge_request(self, project_id: int, title: str | None = None,
                      target_branch: str = "main") -> dict:
        """一条能合并的 MR 需要：独立分支 + 分支上确有改动。

        只建分支不改文件，GitLab 会返回 400（两个分支没有差异），
        而这和「接口坏了」长得一模一样——差别全靠这一步。
        """
        title = title or self._uniq("mr")
        src = self.branch(project_id, ref=target_branch)["name"]
        self.file(project_id, f"qa-{src}.txt", f"由 MR {title} 引入的改动\n", branch=src)
        resp = self.gl.merge_requests.create(project_id, src, target_branch, title)
        if resp.status != 201:
            raise RuntimeError(f"创建 MR 失败（{resp.status}）：{resp.json_data}")
        return resp.json_data

    def snippet(self, title: str | None = None) -> dict:
        title = title or self._uniq("snippet")
        resp = self.gl.snippets.create(title, "qa.txt", f"content of {title}\n")
        data = resp.json_data
        self.ledger.snippets.append(data["id"])
        return data

    # ------------------------------------------------------------ 回收
    def cleanup(self) -> None:
        # 依附型资源先删：评论 → 标签/里程碑 → 议题 → 分支 → 项目 → 群组。
        # 顺序反了会出现「删不掉的项目」：GitLab 对仍有子资源的项目删除是允许的，
        # 但清理记录里会留下一串 404，把真正该看的残留信息淹掉。
        for project_id, iid, note_id in self.ledger.notes:
            try:
                self.gl.notes.delete(project_id, iid, note_id)
                self.ledger.note(f"[ok] 删除评论 {project_id}#{iid}/{note_id}")
            except Exception as exc:  # noqa: BLE001
                self.ledger.note(f"[残留] 评论 {project_id}#{iid}/{note_id}: {exc}")

        for pid, label_id in self.ledger.labels:
            try:
                self.gl.labels.delete(pid, label_id)
                self.ledger.note(f"[ok] 删除标签 {pid}/{label_id}")
            except Exception as exc:  # noqa: BLE001
                self.ledger.note(f"[残留] 标签 {pid}/{label_id}: {exc}")

        for pid, milestone_id in self.ledger.milestones:
            try:
                self.gl.milestones.delete(pid, milestone_id)
                self.ledger.note(f"[ok] 删除里程碑 {pid}/{milestone_id}")
            except Exception as exc:  # noqa: BLE001
                self.ledger.note(f"[残留] 里程碑 {pid}/{milestone_id}: {exc}")
        # 先删议题（依附于项目），再删项目，最后删群组
        for project_id, iid in self.ledger.issues:
            try:
                self.gl.client.delete(
                    f"/projects/{project_id}/issues/{iid}", expect=(200, 204, 404)
                )
                self.ledger.note(f"[ok] 删除议题 {project_id}#{iid}")
            except Exception as exc:  # noqa: BLE001
                self.ledger.note(f"[残留] 议题 {project_id}#{iid}: {exc}")

        for pid, name in self.ledger.branches:
            try:
                self.gl.repository.delete_branch(pid, name)
                self.ledger.note(f"[ok] 删除分支 {pid}:{name}")
            except Exception as exc:  # noqa: BLE001
                # 默认分支删不掉是 GitLab 的保护，不算残留
                self.ledger.note(f"[跳过] 分支 {pid}:{name}: {exc}")

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
                # 群组默认走「延迟删除」：DELETE 返回 202 只代表"已标记"，
                # 回查仍然 200。这里必须回查一次把话说准——否则清理日志写着
                # [ok] 删除群组，实例上其实还挂着，只是没人去看清单而已。
                #
                # 顶层群组不支持 permanently_remove（实测 19.0.1 返回
                # 400 "only available for subgroups"），只能等
                # deletion_adjourned_period（本实例 1 天）到期自动清除。
                # 所以这里记录的是"已排期"，不是"已消失"。
                after = self.gl.groups.get(gid)
                if after.status == 200:
                    path = (after.json_data or {}).get("full_path", "")
                    self.ledger.note(
                        f"[延迟删除] 群组 {gid} {path}：已标记待删，保留期内仍可查到")
                else:
                    self.ledger.note(f"[ok] 删除群组 {gid}")
            except Exception as exc:  # noqa: BLE001
                self.ledger.note(f"[残留] 群组 {gid}: {exc}")

        for sid in self.ledger.snippets:
            try:
                self.gl.client.delete(f"/snippets/{sid}", expect=(200, 202, 204, 404))
                self.ledger.note(f"[ok] 删除片段 {sid}")
            except Exception as exc:  # noqa: BLE001
                self.ledger.note(f"[残留] 片段 {sid}: {exc}")

        for uid in self.ledger.users:
            try:
                self.gl.client.delete(f"/users/{uid}", expect=(200, 202, 204, 404))
                self.ledger.note(f"[ok] 删除用户 {uid}")
            except Exception as exc:  # noqa: BLE001
                self.ledger.note(f"[残留] 用户 {uid}: {exc}")

        self.ledger.projects.clear()
        self.ledger.groups.clear()
        self.ledger.issues.clear()
        self.ledger.labels.clear()
        self.ledger.milestones.clear()
        self.ledger.notes.clear()
        self.ledger.branches.clear()
        self.ledger.tags.clear()
        self.ledger.snippets.clear()
        self.ledger.users.clear()
