"""JSON Schema 契约。

pydantic 管「我期望的数据长什么样」，这里管「服务端承诺的数据长什么样」。
两者不是重复：pydantic 模型是随手改的，Schema 是从 OpenAPI / 契约文档来的，
一旦对不上，说明上游改了协议而你的模型没跟上——这本身就是缺陷。
"""

from __future__ import annotations

import jsonschema

BOOK = {
    "type": "object",
    "required": [
        "id",
        "title",
        "author",
        "genre",
        "published_year",
        "price",
        "in_stock",
        "owner",
        "created_at",
    ],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "pattern": "^bk_[0-9a-zA-Z]{3,}$"},
        "title": {"type": "string", "minLength": 1},
        "author": {"type": "string", "minLength": 1},
        "genre": {"type": "string", "enum": ["fiction", "tech", "history", "science"]},
        # 关键：类型写死 integer，上游一旦返回 "2020" 字符串，这里必红
        "published_year": {"type": "integer", "minimum": 1000, "maximum": 2100},
        "price": {"type": "number", "minimum": 0},
        "in_stock": {"type": "boolean"},
        "owner": {"type": "string", "minLength": 1},
        "created_at": {"type": "string", "format": "date-time"},
    },
}

BOOK_PAGE = {
    "type": "object",
    "required": ["items", "total", "page", "size", "pages"],
    "additionalProperties": False,
    "properties": {
        "items": {"type": "array", "items": BOOK},
        "total": {"type": "integer", "minimum": 0},
        "page": {"type": "integer", "minimum": 1},
        "size": {"type": "integer", "minimum": 1, "maximum": 50},
        "pages": {"type": "integer", "minimum": 0},
    },
}

ERROR = {
    "type": "object",
    "required": ["error"],
    "properties": {
        "error": {
            "type": "object",
            "required": ["code", "message"],
            "properties": {
                "code": {"type": "string"},
                "message": {"type": "string", "minLength": 1},
            },
        }
    },
}


def assert_matches(instance: dict, schema: dict) -> None:
    """校验失败时抛 jsonschema 异常，pytest 会打印完整路径。"""
    jsonschema.validate(instance=instance, schema=schema)


def collect_errors(instance: dict, schema: dict) -> list[str]:
    """不抛异常版本，用于「一次报告所有不合约的字段」而不是只报第一个。"""
    validator = jsonschema.Draft202012Validator(schema)
    return [
        f"{'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}"
        for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    ]
