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
    '''
    把非 ISO-8859-1 可表示的字符转成 \\uXXXX 形式。
    同时转义反斜杠与换行，保证生成的是合法的 properties 文件。
    Java Properties 文件的字符转义函数，确保任意 Unicode 文本都能安全写入 .properties 文件。

    为什么？Java 的 .properties 文件默认使用 ISO-8859-1 编码。如果直接写入中文，读取时会乱码。标准做法是用 \\uXXXX 转义非 ASCII 字符，Java 的 Properties.load() 会自动还原。
    '''
    out: list[str] = []   #用列表收集转义后的字符片段，最后再 join，比字符串拼接高效
    for ch in text:
        code = ord(ch)   #ord(ch) 获取字符的 Unicode 码点
        if ch == "\\":
            out.append("\\\\")   #反斜杠转义为 \\。这是必须的，因为 \ 在 properties 文件中是转义符本身。
        elif ch == "\n":
            out.append("\\n")  #换行符和回车符分别转义为 \n 和 \r，保证 properties 文件不会意外断行。
        elif ch == "\r":
            out.append("\\r")
        elif code < 0x80:  #ASCII 范围内的字符（0x00–0x7F）直接保留，不做转义。这覆盖了英文字母、数字、常见标点
            out.append(ch)
        else:
            out.append(f"\\u{code:04x}")  #非 ASCII 字符（如中文、emoji）转义为 \uXXXX 形式。04x 表示用 4 位十六进制小写表示，不足补零。
    return "".join(out)  #拼接所有片段，返回最终结果。


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
