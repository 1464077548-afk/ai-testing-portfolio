"""安全测试支撑层。

分成两块，职责不混：

    guard    信任边界：这个目标允许被安全用例打吗（不允许就 skip）
    scanner  断言工具：响应里有没有敏感字段、有没有堆栈指纹、注入载荷库

这里刻意不实现漏洞扫描器——那是 ZAP / nuclei 的活。
本模块只做能写进 pytest、能反复跑、失败时能指出**具体哪个键**的断言。
一次扫描的结果不是资产，能进 CI 反复跑的断言才是。
"""

from gqa.security.guard import (
    ENV_KEY,
    allowed_hosts,
    describe,
    host_of,
    is_allowed,
)
from gqa.security.raw import RawResponse, raw_get
from gqa.security.scanner import (
    PATH_TRAVERSAL_PAYLOADS,
    SQLI_PAYLOADS,
    TEMPLATE_PAYLOADS,
    XSS_PAYLOADS,
    mask,
    scan_fingerprints,
    scan_internal_paths,
    scan_secret_keys,
)

__all__ = [
    "ENV_KEY",
    "allowed_hosts",
    "describe",
    "host_of",
    "is_allowed",
    "PATH_TRAVERSAL_PAYLOADS",
    "SQLI_PAYLOADS",
    "TEMPLATE_PAYLOADS",
    "XSS_PAYLOADS",
    "RawResponse",
    "raw_get",
    "mask",
    "scan_fingerprints",
    "scan_internal_paths",
    "scan_secret_keys",
]
