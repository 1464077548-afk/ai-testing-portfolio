"""安全断言用的扫描器与载荷库。

两条刻意的取舍，决定了这个模块长什么样：

1. **只扫键名，不扫值**
   扫值的话误报极高：URL、分支名、issue 描述里出现 "token" 都很正常。
   而"响应里多出一个叫 access_token 的字段"几乎一定是问题，且能精确指路。

2. **一次列全部，不报第一个**
   改一个跑一次的循环最耗时间。所有扫描函数都返回列表。

另外这个模块只做"断言"，不做"攻击"：载荷都是无害字符串
（<script>alert(1)</script> 存进去看是否被原样返回，而不是真去执行它）。
"""

from __future__ import annotations

from typing import Any, Iterator

# ---------------------------------------------------------------- 载荷库
# 集中一处，用例只引用不硬编码——加一个新载荷不用改用例
XSS_PAYLOADS = [
    # ==================== 基础经典 Payload ====================
    # 1. 基础脚本注入（最经典的XSS测试）
    '<script>alert("xss")</script>',
    '<script>alert(document.cookie)</script>',  # 窃取Cookie测试
    # ==================== 事件型 XSS（绕过 script 过滤）====================
    # 2. 图片标签错误触发（绕过简单script过滤），利用图片加载失败触发onerror事件，绕过禁止<script>标签的简单过滤。
    '<img src=x onerror=alert("xss")>',
    '<img src=x onmouseover=alert("xss")>',
    '<body onload=alert("xss")>',   # 页面加载事件
    '<div onmouseover=alert("xss")>test</div>',
    '<input onfocus=alert("xss") autofocus>',
    '<video src=x onerror=alert("xss")>',
    '<audio src=x onerror=alert("xss")>',
    # ==================== 伪协议 XSS====================
    # 3. JavaScript伪协议（常用于href/src属性注入）伪协议类型 XSS，专门测试链接 / 超链接属性（如<a href="输入内容">）。
    'javascript:alert("xss")',  
    'javascript:alert(document.cookie)',
    'vbscript:msgbox("xss")',
    'data:text/html;base64,PHNjc==',
    # ==================== SVG / 特殊标签绕过 ====================
    # 4. SVG标签加载触发（绕过部分标签黑名单），SVG 标签自带加载事件，绕过常见 HTML 标签过滤规则。
    '<svg onload=alert("xss")>',
    '<svg/onload=alert("xss")>',
    '<iframe onload=alert("xss")></iframe>',# 框架标签注入
    '<iframe src="javascript:alert(1)"></iframe>',
            
    # ==================== 标签闭合绕过（极强绕过能力）====================
    '"><script>alert("xss")</script>',
    '"><img src=x onerror=alert("xss")>',
    '"></div><script>alert("xss")</script>',
    '"><svg onload=alert("xss")>',
    "\"><img src=x onerror=alert(1)>",
    '\'><script>alert("xss")</script>',         # 闭合标签绕过
    # ==================== 编码混淆绕过（大小写/变形）====================
    '<Script>alert("xss")</Script>',
    '<sCrIpT>alert("xss")</sCrIpT>',
    '<IMG SRC=X ONERROR=alert("xss")>',

    # ==================== 无括号绕过（过滤括号时可用）====================
    '<script>alert`xss`</script>',
    '<img src=x onerror=alert`xss`>',

    # ==================== 超长字符绕过（部分WAF可绕过）====================
    '<a href="javascript:alert(1)">click me</a>',
    '<marquee onstart=alert("xss")>',
    
    # ==================== 富文本专用 XSS ====================
    '<p style="color:red;x:expression(alert(1))">test</p>',
]

SQLI_PAYLOADS = [
    "' OR '1'='1",
    "'; DROP TABLE projects; --",
    "1' UNION SELECT 1,version(),3--",
    "1; SELECT * FROM projects",
]

TEMPLATE_PAYLOADS = [
    "{{ 7 * 7 }}",
    "<%= 7 * 7 %>",
    "${7*7}",
]

PATH_TRAVERSAL_PAYLOADS = [
    "../../../../etc/passwd",
    '....//....//etc/passwd',
    "..%2F..%2F..%2F..%2Fetc%2Fpasswd",
    "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    '..\\..\\..\\windows\\system32',

]


# ---------------------------------------------------------------- 键名黑名单
# 精确匹配：出现即报（这些键名在任何 API 响应里出现都不正常）
SECRET_KEYS = frozenset({
    "token", "private_token", "access_token", "refresh_token",
    "impersonation_token", "personal_access_token",
    "runners_token", "registration_token", "health_check_token",
    "secret", "client_secret", "webhook_secret", "shared_secret",
    "otp_secret", "otp_secret_key", "otp_backup_codes",
    "password", "encrypted_password", "password_hash", "password_digest",
    "private_key", "api_key", "access_key", "secret_key_base", "db_key_base",
    "credential", "credentials", "authorization", "cookie", "session_id",
})

# 子串匹配：键名里含这些词就可疑（覆盖 xxx_token / secret_xxx 这类变体）
SECRET_TOKENS = (
    "token", "secret", "password", "passwd", "private_key", "privatekey",
    "api_key", "apikey", "credential", "session_id", "authorization",
)

# 白名单：这些键名带敏感词但是布尔/时间/统计，不是凭据。
# 漏在这里的后果是误报红——宁可先放行，也不要让安全用例天天假红没人看。
ALLOW_KEYS = frozenset({
    "password_expires_at", "password_automatically_set", "password_expired",
    "token_expires_at", "tokens_expires_at",
    "two_factor_enabled", "otp_required_for_login", "session_timeout",
    "runners_token_expiration_interval",
    # 实测误报：这两个是 CI 的配置项，名字带 token 但不是凭据
    "ci_id_token_sub_claim_components",      # 数组：ID token 里要带哪些 claim
    "ci_push_repository_for_job_token_allowed",  # 布尔开关
})

ALLOW_SUFFIXES = (
    "_expires_at", "_expiration", "_enabled", "_required", "_allowed",
    "_type", "_url", "_prefix", "_count", "_id", "_path", "_interval", "_timeout",
)


def _looks_secret(key: str) -> bool:
    k = (key or "").lower()
    if not k or k in ALLOW_KEYS:
        return False
    if any(k.endswith(s) for s in ALLOW_SUFFIXES):
        return False
    return any(t in k for t in SECRET_TOKENS)


def iter_nodes(node: Any, path: str = "$") -> Iterator[tuple[str, str, Any]]:
    """深度遍历，产出 (jsonpath 风格路径, 键名, 值)。列表元素键名为空串。"""
    if isinstance(node, dict):
        for key, value in node.items():
            p = f"{path}.{key}"
            yield p, str(key), value
            yield from iter_nodes(value, p)
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            p = f"{path}[{idx}]"
            yield p, "", value
            yield from iter_nodes(value, p)


def mask(value: Any, keep: int = 3) -> str:
    """报告里展示可疑字段时脱敏——证据要能看，但别把凭据再复制一份进报告。"""
    if value is None:
        return "null"
    text = str(value)
    if not text:
        return '""'
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}…{text[-keep:]} (长度 {len(text)})"


def scan_secret_keys(
    payload: Any,
    *,
    allow: tuple[str, ...] = (),
    extra_deny: tuple[str, ...] = (),
) -> list[str]:
    """扫响应里的敏感字段键名，返回人类可读的命中列表（空列表 = 干净）。"""
    deny = set(SECRET_KEYS) | set(extra_deny)
    allow = set(ALLOW_KEYS) | set(allow)

    hits: list[str] = []
    for path, key, value in iter_nodes(payload):
        if not key or key in allow:
            continue
        if key in deny or _looks_secret(key):
            hits.append(f"{path} → {key} = {mask(value)}")
    return hits


# ---------------------------------------------------------------- 指纹扫描
# 正文里出现这些，说明服务端把内部信息吐出来了（堆栈、SQL 报错、绝对路径）
STACKTRACE_PATTERNS = (
    "PG::", "ActiveRecord::", "SQLite3::", "Mysql2::", "Sequel::",
    "NoMethodError", "NameError", "SyntaxError", "TypeError at",
    "ActionView::", "Grape::", "Rack::",
    "backtrace", "exception_class",
    "/opt/gitlab/", "/var/opt/gitlab", "lib/ruby/",
    "gems/", ".rb:", ".erb:",
)

SQL_PATTERNS = (
    "syntax error at or near", "UNION SELECT", "SELECT * FROM",
    "PG::SyntaxError", "relation \"", "column \"",
)

INTERNAL_PATH_PATTERNS = (
    "/opt/gitlab/", "/var/opt/gitlab", "/home/git/", "/app/", "C:\\",
)


def scan_fingerprints(text: str, *, include_sql: bool = False) -> list[str]:
    """扫正文里的技术栈指纹 / 堆栈 / 内部路径。

    include_sql=True 时额外扫 SQL 报错特征——只在错误响应上开，
    正常业务响应里出现 "SELECT" 之类太常见，会淹没真信号。
    """
    if not text:
        return []
    patterns = list(STACKTRACE_PATTERNS)
    if include_sql:
        patterns += list(SQL_PATTERNS)
    return sorted({p for p in patterns if p in text})


def scan_internal_paths(text: str) -> list[str]:
    """只扫内部绝对路径——用于「版本号接口不该暴露部署路径」这类断言。"""
    if not text:
        return []
    return sorted({p for p in INTERNAL_PATH_PATTERNS if p in text})
