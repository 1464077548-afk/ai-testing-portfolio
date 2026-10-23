"""注入与转义：测的是"用户输入会不会被当成代码执行"。

接口层的注入测试有个容易走偏的方向：想直接证明"注入成功"。
实际上能自动断言的只有三件事：

1. **载荷被原样存取**（应用没把用户的字改掉，也没崩）；
2. **服务端没有把载荷当模板求值**（{{7*7}} 存进去还是 {{7*7}}，不是 49）；
3. **错误响应不吐内部信息**（注入失败时不能把 SQL 报错、堆栈带出来）。

真正"浏览器里会不会执行"——那是 UI 层的 XSS 验证，接口层最多断到
`Content-Type: application/json`（JSON 不会被当 HTML 渲染）。
把这条边界说清楚，比写一个"看起来在测 XSS"的假断言有价值。
"""

from __future__ import annotations

import allure
import pytest

from gqa.security import (
    PATH_TRAVERSAL_PAYLOADS,
    SQLI_PAYLOADS,
    TEMPLATE_PAYLOADS,
    XSS_PAYLOADS,
    raw_get,
    scan_fingerprints,
)

pytestmark = [
    pytest.mark.security,
    pytest.mark.requires_token,
    pytest.mark.destructive,
]


@allure.epic("GitLab 安全")
@allure.feature("注入与转义")
@allure.story("XSS 载荷被原样存储，不被求值")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.parametrize("payload", XSS_PAYLOADS,
                         ids=["script_tag", "img_onerror", "js_uri"])
def test_xss_payload_is_stored_literally(target, gl, data_factory, payload):
    project = data_factory.project()
    pid = project["id"]

    with allure.step("把载荷写进 issue 描述"):
        issue = data_factory.issue(pid, title=f"XSS 探针 {payload[:12]}",
                                   description=payload)

    with allure.step("读回：内容应原样保留，不被截断或改写"):
        fetched = gl.issues.get(pid, issue["iid"])
        assert fetched.status == 200
        assert fetched.json_data["description"] == payload, (
            f"写入与读回不一致。写入 {payload!r}，读回 "
            f"{fetched.json_data.get('description')!r}"
        )

    with allure.step("响应必须是 JSON——JSON 不会被浏览器当 HTML 渲染"):
        ctype = fetched.headers.get("Content-Type", "")
        assert ctype.startswith("application/json"), (
            f"issue 接口返回 Content-Type={ctype!r}，"
            f"若被浏览器按 HTML 渲染，存储型 XSS 就可能成立"
        )


@allure.epic("GitLab 安全")
@allure.feature("注入与转义")
@allure.story("模板语法不被求值")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.parametrize("payload", TEMPLATE_PAYLOADS,
                         ids=["jinja", "erb", "es_template"])
def test_template_payload_is_not_evaluated(target, gl, data_factory, payload):
    """{{ 7 * 7 }} 存进去就该是 {{ 7 * 7 }}。

    如果读回变成 "49"，说明服务端把用户输入送进了模板引擎求值——
    这是 SSTI（服务端模板注入），危害远大于反射型 XSS。
    """
    project = data_factory.project()
    pid = project["id"]
    content = f"模板探针 {payload} 结束"

    issue = data_factory.issue(pid, title="SSTI 探针", description=content)
    fetched = gl.issues.get(pid, issue["iid"])

    raw = str(fetched.json_data.get("description") or "")
    assert payload in raw, (
        f"载荷没有原样保留：期望包含 {payload!r}，实际 {raw!r}"
    )
    # 49 = 7*7。只有真被求值才会出现，且原载荷里不含 49
    assert "49" not in raw, (
        f"模板被求值了（出现 49，疑似 SSTI）：{raw!r}"
    )


@allure.epic("GitLab 安全")
@allure.feature("注入与转义")
@allure.story("搜索参数注入不生效且不泄露 SQL 报错")
@allure.severity(allure.severity_level.BLOCKER)
@pytest.mark.parametrize("payload", SQLI_PAYLOADS,
                         ids=["tautology", "drop_table", "union_select"])
def test_sqli_in_search_does_not_leak_or_break(target, gl, data_factory, payload):
    data_factory.project()  # 保证搜索空间非空

    with allure.step(f"用注入串搜索：{payload!r}"):
        resp = gl.client.get("/projects", params={"search": payload},
                             expect=(200, 400, 403, 422))

    if resp.status != 200:
        # 直接拒绝也算安全（严格校验），只要不是 500
        assert resp.status != 500
        allure.attach(f"注入串被拒绝：HTTP {resp.status}", name="说明",
                      attachment_type=allure.attachment_type.TEXT)
        return

    with allure.step("响应正文不得出现 SQL 报错/堆栈指纹"):
        hits = scan_fingerprints(str(resp.json_data), include_sql=True)
        assert hits == [], f"注入串触发了服务端错误信息泄露：{hits}"

    with allure.step("结果集不应因 OR 1=1 而异常放大"):
        # 正常的项目列表不会因为一个搜索词就返回全量；这里只做上界断言，
        # 避免依赖具体实例的数据量
        items = resp.json_data if isinstance(resp.json_data, list) else []
        assert len(items) <= 100, (
            f"注入串返回 {len(items)} 条，疑似条件恒真导致全表返回"
        )


@allure.epic("GitLab 安全")
@allure.feature("注入与转义")
@allure.story("路径穿越读不到系统文件")
@allure.severity(allure.severity_level.BLOCKER)
@pytest.mark.parametrize("payload", PATH_TRAVERSAL_PAYLOADS,
                         ids=["plain", "encoded", "encoded_dots"])
def test_path_traversal_is_rejected(target, payload):
    """必须用原始请求，否则测的是客户端不是服务端。

    requests / urllib3 会把 `../` 先归一化掉，恶意路径根本到不了服务端——
    用例"通过"只是因为载荷没发出去。实测就是这么被坑的：
    三个载荷全部返回 text/html 登录页，看起来像"被拦了"，其实请求早被改写。

    判定口径（重要，避免误报）：
      - 4xx        → 明确拒绝
      - 2xx 且正文无 /etc/passwd 特征 → 反代把路径归一化到了别的页面，不算漏洞
      - 正文含 root:x:0:0 之类            → 真读到系统文件，安全缺陷
    """
    with allure.step(f"原始请求（不做路径归一化）：{payload!r}"):
        resp = raw_get(
            target.base_url,
            f"/api/v4/projects/{payload}",
            headers={"PRIVATE-TOKEN": target.token, "Accept": "application/json"},
        )

    with allure.step("核心断言：不能读到系统文件"):
        leaked = resp.has_file_content()
        assert leaked == [], (
            f"路径穿越读到了系统文件：命中特征 {leaked}，"
            f"HTTP {resp.status}，正文片段 {resp.body[:200]!r}"
        )

    if resp.status >= 400:
        allure.attach(f"HTTP {resp.status}（已拒绝）", name="结果",
                      attachment_type=allure.attachment_type.TEXT)
        return

    # 2xx 但没读到文件内容：说明请求在反代层被归一化到别的路由，
    # 这属于"路径没生效"，不是"穿越成功"。记录下来，别当成失败。
    allure.attach(
        f"HTTP {resp.status}\nContent-Type: {resp.content_type}\n"
        f"正文前 300 字：\n{resp.body[:300]}",
        name="未拒绝但未读到文件",
        attachment_type=allure.attachment_type.TEXT,
    )
