"""边界输入的裁决规则。

为什么不把「超长标题应该 400」写死：

    服务端选择接受还是拒绝，是它的实现决定，不是测试能规定的东西。
    写死一边，版本升级换个策略就得到一条无意义的红，然后被人改成
    `assert resp.status in (200, 400)`——这条断言从此什么都不验。

    真正该守住的是两条与实现选择无关的规则：

        接受 → 存下来的值必须**等于**输入的（不得静默截断、取整、改写）
        拒绝 → 必须是 4xx，且错误消息指向相关字段（不得 500，不得含糊）

    这两条能抓到比状态码错严重得多的缺陷：「2000 字的标题被悄悄截成
    255 字」不会报错、不会红，用户的数据就这么没了。只断言状态码的
    用例对这类问题完全失明。

    同一套裁决也用在「非法枚举值 / 越界数值 / 坏日期」上：
    服务端可以接受也可以拒绝，但不许既不拒绝又改写。
"""

from __future__ import annotations

from typing import Any

# 「等价」的宽松判定：空串与 null 都表示"没值"，服务端回显哪个都算忠实
_BLANK = ("", None)


def message_of(body: Any) -> str:
    """错误体归一化成一句话。

    GitLab 的 message 可能是字符串、也可能是 {字段: [原因]} 的对象，
    两种都要能取到文本，否则 expect_msg 这类断言会时通时不通。
    """
    if isinstance(body, dict):
        msg = body.get("message") or body.get("error") or body
        if not isinstance(msg, str):
            import json
            msg = json.dumps(msg, ensure_ascii=False)
        return msg
    return str(body)


def equivalent(sent: Any, stored: Any) -> bool:
    """输入与回显是否一致。

    空串和 None 互认：`due_date: ""` 被服务端存成 null 属于忠实表达，
    不能算改写。除此之外必须逐字相等。
    """
    if sent in _BLANK and stored in _BLANK:
        return True
    return sent == stored


def verdict(resp, *, field: str, sent: Any, stored: Any,
            aliases: tuple[str, ...] = ()) -> str:
    """判一次写入，返回 "accepted" 或 "rejected(<code>)"。

    违反规则时抛 AssertionError，消息里带齐三件套：
    发出去的值、拿回来的值、服务端的原话。

    aliases 是实测逼出来的：GitLab 的错误消息并不总用入参名做键。
    标签超长报的是 `title`（内部模型复用了 title），项目名超长报的是
    `path`/`project_namespace`。不认这些别名，用例会因为"消息里没有 name"
    而红——那不是被测系统的缺陷，是断言挂错了字段。
    """
    if 200 <= resp.status < 300:
        if not equivalent(sent, stored):
            raise AssertionError(
                f"状态码 {resp.status} 表示接受，但存下来的值被改写了：\n"
                f"  字段：{field}\n"
                f"  送出：{_clip(sent)}\n"
                f"  存下：{_clip(stored)}\n"
                "  静默改写比直接拒绝更危险——调用方拿不到任何提示。"
            )
        return "accepted"

    if 400 <= resp.status < 500:
        msg = message_of(resp.json_data)
        keys = (field, *aliases)
        if not any(k.lower() in msg.lower() for k in keys):
            raise AssertionError(
                f"状态码 {resp.status} 是一次拒绝，但错误消息没指向 {field}：\n"
                f"  实际消息：{msg[:300]}\n"
                f"  可接受的键：{list(keys)}\n"
                "  消息对不上说明拒绝的原因和本条用例要验的东西无关，"
                "通过它等于没测（路径写错、Token 失效也会 4xx）。"
            )
        return f"rejected({resp.status})"

    raise AssertionError(
        f"状态码 {resp.status} 既不是接受也不是可解释的拒绝：\n"
        f"  响应：{str(resp.json_data)[:300]}\n"
        "  边界输入只允许两种结局：忠实保存，或带原因地拒绝。"
    )


def _clip(value: Any, limit: int = 120) -> str:
    text = repr(value)
    return text if len(text) <= limit else f"{text[:limit]}…（共 {len(text)} 字符）"
