"""Allure 元信息写入（environment / executor / categories）。

这里处理一个容易踩的坑：**Allure 按 Java properties 规范读 environment.properties，
也就是 ISO-8859-1，不是 UTF-8**。直接写中文进去，报告首页会显示成
`æªéç½®` 这种乱码，而且不报错——只有肉眼看报告才会发现。

Java properties 支持 \\uXXXX 转义，把非 ASCII 字符转成这种形式即可，
读取端会自动还原。下面的 escape 函数就是干这个的。
"""

from __future__ import annotations

from pathlib import Path


def escape_properties(text: str) -> str:
    """把非 ISO-8859-1 可表示的字符转成 \\uXXXX 形式。

    同时转义反斜杠与换行，保证生成的是合法的 properties 文件。
    """
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif code < 0x80:
            out.append(ch)
        else:
            out.append(f"\\u{code:04x}")
    return "".join(out)


def write_properties(path: Path, props: dict[str, str]) -> None:
    """写 properties 文件，非 ASCII 自动转义。"""
    lines = [f"{escape_properties(k)}={escape_properties(v)}" for k, v in props.items()]
    path.write_text("\n".join(lines), encoding="ascii")


# Java properties 的读法：\uXXXX 会被还原成对应字符
def unescape_properties(text: str) -> str:
    """反向还原，仅用于测试与自检。"""
    import re

    def _sub(m: "re.Match[str]") -> str:
        return chr(int(m.group(1), 16))

    text = re.sub(r"\\u([0-9a-fA-F]{4})", _sub, text)
    text = text.replace("\\n", "\n").replace("\\r", "\r").replace("\\\\", "\\")
    return text
