"""JSON Schema 契约：表达「GitLab 承诺的响应结构」。

和 models.py 的分工：pydantic 是我这边的期望（宽松、为断言服务），
schema 是服务端的承诺（严格、为漂移检测服务）。两套一起用，
上游字段类型变了才拦得住。
"""

from __future__ import annotations

import jsonschema


def _nullable_text(**extra) -> dict:
    """GitLab 的「未填写」文本字段一律返回 null，不是空串。

    这是踩过的坑：把 description 写成 {"type": "string"}，
    于是每个不填描述的资源都被判成契约违约，红了一片却全是假红。
    真要防的是「类型漂移」（string 变 int/时间戳），null 不在其列。
    """
    return {"type": ["string", "null"], **extra}


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

# ---------------------------------------------------------------- 新增资源
# 原则：required 只写「上游不可能不含」的字段。契约写得太满，
# 服务端加个字段就红一片，很快会被人用 --no-verify 绕过，
# 那时它就什么也拦不住了。真正的漂移风险集中在类型上
# （id 从 int 变 str、时间从字符串变时间戳），所以类型约束一条都不让。
LABEL = {
    "type": "object",
    "required": ["id", "name", "color"],
    "properties": {
        "id": {"type": "integer"},
        "name": {"type": "string", "minLength": 1},
        # 形如 #0033cc。UI 直接拿它当背景色，一旦少了 # 就是大面积白块
        "color": {"type": "string", "pattern": r"^#?[0-9a-fA-F]{6}$"},
        # 可空。未填 description 时 GitLab 返回 null 而不是 ""——
        # 写成 {"type":"string"} 会把「没填描述」误判成契约违约
        "description": _nullable_text(),
    },
}

MILESTONE = {
    "type": "object",
    "required": ["id", "iid", "title", "state"],
    "properties": {
        "id": {"type": "integer"},
        "iid": {"type": "integer"},
        "title": {"type": "string", "minLength": 1},
        "state": {"type": "string", "enum": ["opened", "closed", "active"]},
        "description": _nullable_text(),
        "due_date": {"type": ["string", "null"]},
        "start_date": {"type": ["string", "null"]},
    },
}

NOTE = {
    "type": "object",
    "required": ["id", "body"],
    "properties": {
        "id": {"type": "integer"},
        "body": {"type": "string"},
        "author": {"type": "object"},
        "created_at": {"type": "string"},
        # 系统评论（关闭议题、改标签）与普通评论混在同一列表里，
        # 前端靠这个字段区分展示，类型从 bool 变字符串会让筛选静默失效
        "system": {"type": "boolean"},
        "noteable_id": {"type": "integer"},
        "noteable_iid": {"type": "integer"},
        "noteable_type": {"type": "string"},
    },
}

MERGE_REQUEST = {
    "type": "object",
    "required": ["id", "iid", "title", "state"],
    "properties": {
        "id": {"type": "integer"},
        "iid": {"type": "integer"},
        "title": {"type": "string", "minLength": 1},
        "state": {"type": "string", "enum": ["opened", "closed", "merged", "locked"]},
        "source_branch": {"type": "string"},
        "target_branch": {"type": "string"},
        "merge_status": {"type": "string"},
        "description": _nullable_text(),
        "web_url": {"type": "string"},
    },
}

MEMBER = {
    "type": "object",
    "required": ["id", "access_level"],
    "properties": {
        # 用户 id。成员关系的主键是用户 id，不是成员记录 id
        "id": {"type": "integer"},
        "username": {"type": "string"},
        "name": {"type": "string"},
        # 10 Guest / 20 Reporter / 30 Developer / 40 Maintainer / 50 Owner
        "access_level": {"type": "integer", "minimum": 0},
        "state": {"type": "string"},
        "expires_at": {"type": ["string", "null"]},
    },
}

COMMIT = {
    "type": "object",
    "required": ["id", "message"],
    "properties": {
        "id": {"type": "string", "minLength": 7},
        "short_id": {"type": "string"},
        "title": {"type": "string"},
        "message": {"type": "string"},
        "author_name": {"type": "string"},
        "author_email": {"type": "string"},
        "created_at": {"type": "string"},
        "web_url": {"type": "string"},
    },
}

BRANCH = {
    "type": "object",
    "required": ["name"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "merged": {"type": "boolean"},
        "protected": {"type": "boolean"},
        "default": {"type": "boolean"},
        "commit": {"type": "object"},
    },
}

FILE = {
    "type": "object",
    "required": ["file_path", "content"],
    "properties": {
        "file_path": {"type": "string"},
        "branch": {"type": "string"},
        # 内容是 base64。哪天变成明文，所有文件内容断言都会「看起来通过」
        # 而实际比对的是乱码——所以 encoding 必须一起约束
        "encoding": {"type": "string", "enum": ["base64", "text"]},
        "content": {"type": "string"},
        "last_commit_id": {"type": "string"},
    },
}

TAG = {
    "type": "object",
    "required": ["name", "target"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "target": {"type": "string"},
        "message": {"type": ["string", "null"]},
        "commit": {"type": "object"},
    },
}

TREE_ITEM = {
    "type": "object",
    "required": ["name", "type"],
    "properties": {
        "name": {"type": "string"},
        "path": {"type": "string"},
        "type": {"type": "string", "enum": ["blob", "tree", "commit"]},
        "mode": {"type": "string"},
        "id": {"type": "string"},
    },
}

NAMESPACE = {
    "type": "object",
    "required": ["id", "name", "path"],
    "properties": {
        "id": {"type": "integer"},
        "name": {"type": "string"},
        "path": {"type": "string"},
        "kind": {"type": "string", "enum": ["user", "group"]},
        "full_path": {"type": "string"},
    },
}

SNIPPET = {
    "type": "object",
    "required": ["id", "title", "file_name"],
    "properties": {
        "id": {"type": "integer"},
        "title": {"type": "string"},
        "file_name": {"type": "string"},
        "description": {"type": ["string", "null"]},
        "visibility": {"type": "string"},
        "web_url": {"type": "string"},
        "raw_url": {"type": "string"},
    },
}

WIKI_PAGE = {
    "type": "object",
    "required": ["slug", "title"],
    "properties": {
        "slug": {"type": "string"},
        "title": {"type": "string"},
        "format": {"type": "string"},
        "encoding": {"type": "string"},
        "content": {"type": "string"},
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
