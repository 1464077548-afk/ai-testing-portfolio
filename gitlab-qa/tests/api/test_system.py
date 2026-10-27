"""系统级端点：版本、元信息、实例设置、统计。

放在接口套件里是刻意的：这几个端点不需要鉴权或只需要管理员，
以前只有冒烟/安全套件会碰它们，于是「跑一次接口套件」永远看不到
完整覆盖率。把系统端点收进接口套件，覆盖率这个数字才对得上一次运行。

写操作（PUT 实例设置）的做法值得说明：先读当前值，再把它原样写回。
这样既验证了写通道可用，又不会把实例配置改掉——
测试改配置是最难排查的一类副作用，因为它影响的是之后所有的用例。
"""

from __future__ import annotations

import allure
import pytest

from gqa import contracts

pytestmark = [pytest.mark.api, pytest.mark.regression]


@allure.epic("GitLab API")
@allure.feature("系统")
@allure.story("版本与元信息")
@allure.severity(allure.severity_level.CRITICAL)
def test_version_and_metadata(gl):
    version = gl.system.version()
    assert version.status == 200
    errors = contracts.collect_errors(version.json_data, contracts.VERSION)
    assert errors == [], f"不符合契约：{errors}"

    metadata = gl.system.metadata()
    assert metadata.status == 200
    errors = contracts.collect_errors(metadata.json_data, contracts.VERSION)
    assert errors == [], f"不符合契约：{errors}"

    with allure.step("两处版本号应一致"):
        assert version.json_data["version"] == metadata.json_data["version"], (
            "版本端点与元信息端点返回的版本号不一致")


@allure.epic("GitLab API")
@allure.feature("系统")
@allure.story("实例统计")
@allure.severity(allure.severity_level.NORMAL)
def test_statistics_require_admin(gl):
    resp = gl.system.statistics()
    assert resp.status in (200, 403), f"统计端点异常：{resp.status}"
    if resp.status == 200:
        assert isinstance(resp.json_data, dict)


@allure.epic("GitLab API")
@allure.feature("系统")
@allure.story("实例设置可读")
@allure.severity(allure.severity_level.NORMAL)
def test_application_settings_readable(gl):
    resp = gl.client.get("/application/settings", expect=(200, 401, 403))
    assert resp.status in (200, 401, 403), f"设置端点异常：{resp.status}"
    if resp.status == 200:
        assert isinstance(resp.json_data, dict)
        assert "signup_enabled" in resp.json_data, "设置里没有 signup_enabled 字段"


@allure.epic("GitLab API")
@allure.feature("系统")
@allure.story("实例设置写回原值不改变配置")
@allure.severity(allure.severity_level.NORMAL)
def test_application_settings_write_is_idempotent(gl):
    got = gl.client.get("/application/settings", expect=(200, 401, 403))
    if got.status != 200:
        pytest.skip(f"当前身份读不到实例设置（{got.status}），跳过写回验证")

    current = got.json_data.get("signup_enabled")
    updated = gl.client.put("/application/settings",
                            json_body={"signup_enabled": current},
                            expect=(200, 400, 403))
    if updated.status == 403:
        pytest.skip("当前 Token 不是管理员，跳过设置写回")
    assert updated.status == 200, f"写回设置失败：{updated.json_data}"

    after = gl.client.get("/application/settings", expect=200)
    assert after.json_data.get("signup_enabled") == current, "写回原值后配置变了"
