"""契约测试：确认「服务端实际返回的」和「我们声明期望的」一致。

这组用例的价值不在当下通过，而在未来某天上游改了字段类型时立刻变红。
没有契约测试，这类变更要等到下游解析崩溃才被发现。
"""

from __future__ import annotations

import allure
import pytest

from gqa import contracts

pytestmark = [pytest.mark.api, pytest.mark.contract]


@allure.epic("契约")
@allure.feature("项目结构")
@allure.story("列表每一项都符合契约")
@allure.severity(allure.severity_level.CRITICAL)
def test_project_list_matches_contract(anon_client):
    resp = anon_client.client.get("/projects", params={"per_page": 10}, expect=(200, 401, 403))
    if resp.status != 200:
        pytest.skip(f"匿名无权查看项目列表（HTTP {resp.status}）")

    items = resp.json_data
    assert items, "公开项目列表为空，无法做契约校验"

    problems: list[str] = []
    for item in items[:10]:
        errs = contracts.collect_errors(item, contracts.PROJECT)
        if errs:
            problems.append(f"{item.get('id')}: {errs}")

    assert not problems, "项目结构不符合契约：\n" + "\n".join(problems)
    allure.attach(f"校验 {len(items[:10])} 条，全部通过", name="契约校验结果",
                  attachment_type=allure.attachment_type.TEXT)


@allure.epic("契约")
@allure.feature("项目结构")
@allure.story("关键字段类型不可漂移")
@allure.severity(allure.severity_level.BLOCKER)
def test_id_field_is_integer(anon_client):
    """id 必须是整数。

    这条单独列出来，是因为「id 从 int 变 str」是最致命的一类漂移：
    所有按 id 拼 URL 的用例都会错位，而且不会报错，只是查到别的东西。
    """
    resp = anon_client.client.get("/projects", params={"per_page": 5}, expect=(200, 401, 403))
    if resp.status != 200 or not resp.json_data:
        pytest.skip("无法获取项目列表")

    for item in resp.json_data[:5]:
        assert isinstance(item.get("id"), int), (
            f"项目 id 应为整数，实际 {type(item.get('id')).__name__}: {item.get('id')!r}"
        )
        assert isinstance(item.get("name"), str), "项目 name 应为字符串"


@allure.epic("契约")
@allure.feature("错误结构")
@allure.story("错误响应形状统一")
@allure.severity(allure.severity_level.NORMAL)
def test_error_body_shape(anon_client):
    """所有错误响应都该带 message，前端要靠它做提示。"""
    resp = anon_client.client.get("/user", expect=(200, 401))
    assert resp.status == 401
    body = resp.json_data
    assert isinstance(body, dict), f"错误响应应为对象，实际 {type(body)}"
    assert "message" in body or "error" in body, (
        f"错误响应缺少 message/error 字段：{list(body)[:5]}"
    )


@allure.epic("契约")
@allure.feature("议题结构")
@allure.story("议题响应符合契约")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.requires_token
def test_issue_matches_contract(gl, a_project):
    from gqa.data.factory import DataFactory
    from gqa.settings import Settings

    # 这里用 data_factory 会与 fixture 冲突，直接内联创建并自行删除
    factory = DataFactory(gl, prefix="contract")
    try:
        raw = factory.issue(a_project.id, description="契约校验")
        errs = contracts.collect_errors(raw, contracts.ISSUE)
        assert errs == [], f"议题结构不符合契约：{errs}"
    finally:
        factory.cleanup()
