"""敏感字段泄露：测的是"响应里有没有不该出现的东西"。

为什么只扫键名不扫值：值里出现 "token" 太常见（URL、分支名、issue 描述），
误报会让人把整个安全套件关掉——这比不测更糟。
而"响应里多出一个 access_token 字段"几乎一定是问题。

第二个重点是**错误响应**。正常响应大家都会检查，出错的路径反而最容易吐内部信息：
堆栈、SQL 报错、部署绝对路径。所以每个 4xx 都要单独扫一遍。
"""

from __future__ import annotations

import json

import allure
import pytest

from gqa.api.client import GitLabClient
from gqa.security import scan_fingerprints, scan_internal_paths, scan_secret_keys

pytestmark = [pytest.mark.security, pytest.mark.requires_token]


@allure.epic("GitLab 安全")
@allure.feature("信息泄露")
@allure.story("版本与元信息接口不含凭据字段")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.parametrize("path", ["/version", "/metadata"])
def test_version_and_metadata_have_no_secrets(target, gl, path):
    resp = gl.client.get(path, expect=(200, 401, 403))
    if resp.status != 200:
        pytest.skip(f"{path} 需要更高权限（HTTP {resp.status}），本环境跳过")

    hits = scan_secret_keys(resp.json_data)
    assert hits == [], f"{path} 响应里出现凭据类字段：{hits}"

    with allure.step("正文不得包含部署路径"):
        paths = scan_internal_paths(json.dumps(resp.json_data, ensure_ascii=False))
        assert paths == [], f"{path} 暴露了内部路径：{paths}"


@allure.epic("GitLab 安全")
@allure.feature("信息泄露")
@allure.story("当前用户响应不含凭据字段")
@allure.severity(allure.severity_level.CRITICAL)
def test_current_user_has_no_credentials(target, gl):
    resp = gl.users.current()
    assert resp.status == 200

    hits = scan_secret_keys(resp.json_data)
    assert hits == [], f"/user 响应里出现凭据类字段：{hits}"


@allure.epic("GitLab 安全")
@allure.feature("信息泄露")
@allure.story("项目列表不含凭据字段")
@allure.severity(allure.severity_level.CRITICAL)
def test_project_list_has_no_secrets(target, gl, member_client, data_factory):
    """列表接口是**批量暴露面**：一个字段泄露，一次就暴露 N 个项目。

    断言必须用**非管理员视角**，这一点是本项目实测修正的：

      管理员读 /projects，列表里每个项目都带 runners_token。
      这是权限模型内的行为（admin 对任何项目都有 maintainer 权限），
      报成失败属于误报；但它确实说明「列表接口会带凭据」，
      所以要看的是——**普通用户读列表时带不带**。

    普通用户看公开项目时若也能拿到 runners_token，任何登录用户都能把
    runner 注册到别人的项目上，直接截获 CI 任务。那才是缺陷。
    """
    data_factory.project(visibility="public")   # 让非成员能在列表里看到它

    with allure.step("管理员视角：记录（权限模型内，不判失败）"):
        admin_resp = gl.client.get("/projects", params={"per_page": 20},
                                   expect=(200,))
        admin_hits = scan_secret_keys(admin_resp.json_data)
        if admin_hits:
            allure.attach(
                "管理员列表带出凭据字段（GitLab 权限模型内行为，仅记录）：\n"
                + "\n".join(admin_hits[:5]),
                name="已知行为", attachment_type=allure.attachment_type.TEXT,
            )

    with allure.step("普通用户视角：列表不得出现凭据字段"):
        resp = member_client.client.get("/projects", params={"per_page": 20},
                                        expect=(200, 403))
        if resp.status != 200:
            allure.attach(f"普通用户读列表被拒（HTTP {resp.status}），更严格",
                          name="说明", attachment_type=allure.attachment_type.TEXT)
            return

        hits = scan_secret_keys(resp.json_data)
        assert hits == [], f"普通用户的项目列表里出现凭据类字段：{hits}"


@allure.epic("GitLab 安全")
@allure.feature("信息泄露")
@allure.story("非项目成员看不到 runner 注册令牌")
@allure.severity(allure.severity_level.BLOCKER)
def test_non_member_cannot_see_runners_token(target, gl, member_client,
                                             data_factory):
    """区分"谁能看到"比"有没有"更重要。

    - owner / maintainer 能看到 runners_token：产品设计，不是缺陷
    - 非成员看到：泄露。拿到这个值可以直接注册 runner，把 CI 任务
      导到自己的机器上执行——等于接管构建链路。

    所以先确认 owner 视角确实有该字段（否则这条用例失去意义），
    再断言非成员视角没有。
    """
    created = data_factory.project(visibility="public")
    pid = created["id"]

    with allure.step("前提：owner 视角确实能看到 runners_token"):
        owner_view = gl.projects.get(pid).json_data
        assert "runners_token" in owner_view, (
            "owner 视角都没有 runners_token，说明接口变了，本条用例需要重写"
        )

    with allure.step("非成员读同一个项目：不应出现 runners_token"):
        resp = member_client.client.get(f"/projects/{pid}", expect=(200, 403, 404))
        if resp.status != 200:
            allure.attach(f"非成员被端点级拒绝（HTTP {resp.status}），更严格",
                          name="说明", attachment_type=allure.attachment_type.TEXT)
            return

        hits = scan_secret_keys(resp.json_data)
        assert hits == [], f"非成员读到了凭据类字段：{hits}"


@allure.epic("GitLab 安全")
@allure.feature("信息泄露")
@allure.story("参数错误响应不吐堆栈")
@allure.severity(allure.severity_level.CRITICAL)
def test_bad_request_body_has_no_stacktrace(target, gl):
    with allure.step("用空项目名触发 400"):
        resp = gl.client.post("/projects", json_body={"name": ""},
                              expect=(400, 422))

    text = json.dumps(resp.json_data, ensure_ascii=False)
    hits = scan_fingerprints(text, include_sql=True)
    assert hits == [], f"400 响应暴露了内部信息：{hits}"

    paths = scan_internal_paths(text)
    assert paths == [], f"400 响应暴露了部署路径：{paths}"


@allure.epic("GitLab 安全")
@allure.feature("信息泄露")
@allure.story("未授权响应不吐堆栈")
@allure.severity(allure.severity_level.CRITICAL)
def test_unauthorized_body_has_no_stacktrace(target, settings):
    """拿一个格式合法但无效的 PAT 去打受保护端点。

    这类响应最容易被框架的异常兜底页处理，从而带出堆栈。
    """
    fake = GitLabClient(settings.base_url, token="glpat-00000000000000000000",
                        timeout=settings.timeout, retries=0)
    try:
        resp = fake.get("/user", expect=(200, 401, 403))
    finally:
        fake.close()

    assert resp.status in (401, 403), f"无效 Token 未被拒绝：HTTP {resp.status}"

    text = json.dumps(resp.json_data, ensure_ascii=False)
    hits = scan_fingerprints(text, include_sql=True)
    assert hits == [], f"401 响应暴露了内部信息：{hits}"


@allure.epic("GitLab 安全")
@allure.feature("信息泄露")
@allure.story("404 响应不吐堆栈")
@allure.severity(allure.severity_level.NORMAL)
def test_not_found_body_has_no_stacktrace(target, gl):
    resp = gl.client.get("/projects/999999999999", expect=(404, 403))
    text = json.dumps(resp.json_data, ensure_ascii=False)
    assert scan_fingerprints(text, include_sql=True) == [], (
        f"404 响应暴露了内部信息：{text[:300]}"
    )
