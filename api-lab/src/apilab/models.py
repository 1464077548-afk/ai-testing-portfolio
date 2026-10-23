"""响应模型：用 pydantic 表达「结构契约」。

`extra="forbid"` 是刻意的。默认 pydantic 会忽略多余字段，上游偷偷加字段
不会有任何报警；而契约测试的职责之一就是「结构变了要有人知道」。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Book(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^bk_[0-9a-zA-Z]{3,}$")
    title: str = Field(min_length=1)
    author: str = Field(min_length=1)
    genre: str
    published_year: int = Field(ge=1000, le=2100)
    price: float = Field(ge=0)
    in_stock: bool
    owner: str
    created_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class BookPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[Book]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    size: int = Field(ge=1, le=50)
    pages: int = Field(ge=0)


class TokenPair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int = Field(gt=0)


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: str
    message: str
