"""实例级检索：命名空间、全局搜索、代码片段、Wiki。

这几个端点共同的特点是「跨资源」，一旦出问题影响的是整个产品而不是
某个功能，所以断言重点放在形状与错误语义上，不做过深的内容校验。
"""

from __future__ import annotations

import allure
import pytest

from gqa import contracts

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]


@allure.epic("GitLab API")
@allure.feature("命名空间")
@allure.story("命名空间列表结构")
@allure.severity(allure.severity_level.NORMAL)
def test_namespaces_are_listed(gl):
    rows = gl.namespaces.list()
    assert isinstance(rows, list), "命名空间列表应为数组"
    for row in rows:
        errors = contracts.collect_errors(row, contracts.NAMESPACE)
        assert errors == [], f"不符合契约：{errors}"
    if rows:
        assert {"id", "name", "path"} <= set(rows[0])


@allure.epic("GitLab API")
@allure.feature("全局搜索")
@allure.story("能搜到刚创建的项目")
@allure.severity(allure.severity_level.NORMAL)
def test_search_finds_created_project(gl, data_factory):
    from gqa.utils.wait import wait_until

    project = data_factory.project()
    term = project["path"]

    # 搜索有索引延迟。固定 sleep 在快机器上浪费时间，在慢机器上假失败，
    # 所以这里一律轮询
    def found() -> bool:
        resp = gl.search.run("projects", term)
        return resp.status == 200 and any(
            p["id"] == project["id"] for p in resp.json_data)

    assert wait_until(found, timeout=20), f"项目 {term} 创建后搜不到"


@allure.epic("GitLab API")
@allure.feature("全局搜索")
@allure.story("缺少 scope 应 400")
@allure.severity(allure.severity_level.MINOR)
def test_search_without_scope_is_rejected(gl):
    resp = gl.search.run("", "anything")
    assert resp.status == 400, f"缺少 scope 应 400，实际 {resp.status}"


@allure.epic("GitLab API")
@allure.feature("代码片段")
@allure.story("创建片段并读回")
@allure.severity(allure.severity_level.NORMAL)
def test_snippet_lifecycle(gl, data_factory):
    snippet = data_factory.snippet()

    errors = contracts.collect_errors(snippet, contracts.SNIPPET)
    assert errors == [], f"不符合契约：{errors}"

    fetched = gl.snippets.get(snippet["id"])
    assert fetched.status == 200
    assert fetched.json_data["title"] == snippet["title"]

    ids = [s["id"] for s in gl.snippets.list()]
    assert snippet["id"] in ids, "片段没出现在列表里"

    with allure.step("删除后应查不到"):
        deleted = gl.client.delete(f"/snippets/{snippet['id']}",
                                   expect=(200, 202, 204, 404))
        assert deleted.status in (200, 202, 204, 404)
        assert gl.snippets.get(snippet["id"]).status == 404


@allure.epic("GitLab API")
@allure.feature("代码片段")
@allure.story("缺少必填字段应 400")
@allure.severity(allure.severity_level.MINOR)
def test_snippet_requires_title_and_file(gl):
    resp = gl.snippets.create("", "qa.txt", "content")
    assert resp.status == 400, f"缺 title 应 400，实际 {resp.status}"


@allure.epic("GitLab API")
@allure.feature("Wiki")
@allure.story("Wiki 页面列表")
@allure.severity(allure.severity_level.MINOR)
def test_wiki_pages_are_listed(gl, a_project):
    resp = gl.wikis.list(a_project.id)
    assert resp.status in (200, 404), f"Wiki 列表异常：{resp.status}"
    if resp.status == 200:
        for page in resp.json_data:
            errors = contracts.collect_errors(page, contracts.WIKI_PAGE)
            assert errors == [], f"不符合契约：{errors}"
