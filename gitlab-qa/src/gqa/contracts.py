"""JSON Schema 契约：表达「GitLab 承诺的响应结构」。

和 models.py 的分工：pydantic 是我这边的期望（宽松、为断言服务），
schema 是服务端的承诺（严格、为漂移检测服务）。两套一起用，
上游字段类型变了才拦得住。
"""

from __future__ import annotations

import jsonschema

VERSION = {
    "type": "object",
    "required": ["version", "revision"],
    "properties": {
        "version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+"},
        "revision": {"type": "string", "minLength": 6},
    },
}

PROJECT = {
    "type": "object",
    "required": ["id", "name", "path"],
    "properties": {
        "id": {"type": "integer"},
        "name": {"type": "string", "minLength": 1},
        "path": {"type": "string", "minLength": 1},
        "path_with_namespace": {"type": "string"},
        "visibility": {"type": "string", "enum": ["public", "internal", "private"]},
        "created_at": {"type": "string", "format": "date-time"},
        "web_url": {"type": "string"},
        # 关键：id 必须是整数。上游一旦改成字符串，所有按 id 拼路径的用例都会错位
        "namespace": {"type": "object"},
    },
}

ISSUE = {
    "type": "object",
    "required": ["id", "iid", "project_id", "title", "state"],
    "properties": {
        "id": {"type": "integer"},
        "iid": {"type": "integer"},
        "project_id": {"type": "integer"},
        "title": {"type": "string", "minLength": 1},
        "state": {"type": "string", "enum": ["opened", "closed"]},
        "created_at": {"type": "string", "format": "date-time"},
        "author": {"type": "object"},
    },
}

USER = {
    "type": "object",
    "required": ["id", "username"],
    "properties": {
        "id": {"type": "integer"},
        "username": {"type": "string", "minLength": 1},
        "name": {"type": "string"},
        "state": {"type": "string"},
    },
}

GROUP = {
    "type": "object",
    "required": ["id", "name", "path"],
    "properties": {
        "id": {"type": "integer"},
        "name": {"type": "string", "minLength": 1},
        "path": {"type": "string", "minLength": 1},
    },
}

ERROR = {
    "type": "object",
    "properties": {
        "message": {},
        "error": {"type": "string"},
    },
}


def collect_errors(instance: dict, schema: dict) -> list[str]:
    """一次列出所有不合约的字段，而不是只报第一个。"""
    validator = jsonschema.Draft202012Validator(schema)    #使用 jsonschema 库，按 Draft 2020-12 规范创建校验器。这是 JSON Schema 最新的标准版本
    return [
        f"{'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}"  #格式化每条错误：err.path 是一个类似 ['user', 'age'] 的路径，用 / 拼接成 "user/age"
                                    #如果是根级别错误（path 为空），用 '<root>' 代替；err.message 是库自动生成的错误描述，如 "5 is not of type 'string'"
        for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    ]
    '''
        validator.iter_errors(instance):核心方法：返回一个生成器，产出所有校验错误对象（ValidationError）。
        与 validator.validate() 不同，iter_errors 不会在第一个错误时抛出异常，而是收集全部错误。

        sorted(..., key=lambda e: list(e.path)):按错误路径排序，保证输出顺序稳定、可读。例如 a.b.c 会排在 a.b.d 前面。
    '''


def assert_matches(instance: dict, schema: dict) -> None:
    jsonschema.validate(instance=instance, schema=schema)
