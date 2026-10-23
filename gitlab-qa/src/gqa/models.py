"""响应模型（pydantic）。

GitLab 字段极多且版本间有增删，这里只对**关键字段**做严格约束，
其余用 extra="allow" 放行——契约的目的是拦住「关键结构变了」，
不是拦住「上游多给了一个字段」，后者应该由契约测试单独报警。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


class Version(_Loose):
    version: str
    revision: str

    @property
    def major(self) -> int:
        return int(self.version.split(".")[0])


class Project(_Loose):
    id: int
    name: str
    path: str
    path_with_namespace: str | None = None
    visibility: str | None = None
    created_at: str | None = None
    web_url: str | None = None

    @property
    def url_path(self) -> str:
        return self.path_with_namespace or self.path


class Issue(_Loose):
    id: int
    iid: int
    project_id: int
    title: str
    state: str
    created_at: str | None = None
    author: dict | None = None

    @property
    def is_open(self) -> bool:
        return self.state == "opened"


class User(_Loose):
    id: int
    username: str
    name: str | None = None
    state: str | None = None


class Group(_Loose):
    id: int
    name: str
    path: str


class ErrorBody(_Loose):
    message: str
