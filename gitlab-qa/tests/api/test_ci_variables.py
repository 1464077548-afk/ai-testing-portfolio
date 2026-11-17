"""CI/CD 变量（project variables）：机密类资源的读写与掩码规则。

全部期望值来自本实例实测（GitLab 19.0.1）：
  · POST {key,value} → 201，variable_type=env_var、environment_scope=*
  · 重复 key → 400 '(KEY) has already been taken'（注意：不是 409）
  · GET 列表 200 / GET 单查 200 / PUT 更新 200
  · masked=true 要求值"可掩码"（Base32 风格、≥8 位）：
      短值 "v3" → 400 'value is invalid'
      "8af3d51c9e7f4b26a4c9d2e" → 200 且 masked=true
  · **PUT 只传 key（不带 value）→ 200 且变量原样不变**：key 不能
    通过 PUT 改名，缺省 value 不会被清空（用例复跑纠正了探针误判）
  · DELETE → 204，重复删 404 'Variable Not Found'
  · 缺 key → 400 'key is missing'；key 含空格 → 400
    "can contain only letters, digits and '_'"
  · 项目不存在 → 404 'Project Not Found'
覆盖缺口来源：ci_variables 模块 15 个操作 QA 覆盖为 0。
"""

from __future__ import annotations

import allure
import pytest

from gqa.data.factory import DataFactory

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]

BAD_KEYS = [
    "with space",
    "dash-key",
    "ключ",          # 非 ASCII 字母同样只允许 [A-Za-z0-9_]
]
MASKABLE = "8af3d51c9e7f4b26a4c9d2e"   # 实测可通过掩码校验的值


def _attach(text: str, name: str) -> None:
    try:
        allure.attach(text, name=name, attachment_type=allure.attachment_type.TEXT)
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(scope="module")
def var_scope(gl, settings) -> dict:
    """共用项目。每条用例用自己独立的变量 key，互不干扰。"""
    factory = DataFactory(gl, prefix=f"{settings.resource_prefix}-civ")
    try:
        proj = factory.project()
        yield {"pid": proj["id"], "factory": factory, "seq": 0}
    finally:
        factory.cleanup()
        _attach(factory.ledger.report(), "数据清理记录")


def _fresh_key(var_scope) -> str:
    """每个用例拿一个不重复的 key，避免上一条用例的残留让下一条 400。"""
    var_scope["seq"] += 1
    return f"QA_VAR_{var_scope['seq']}"


# ==========================================================================
# 1. 正向 CRUD 全链路
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("CI/CD 变量")
@allure.story("CRUD 全链路")
@allure.severity(allure.severity_level.BLOCKER)
def test_variable_crud_roundtrip(var_scope, gl):
    key = _fresh_key(var_scope)
    pid = var_scope["pid"]
    created = gl.client.post(
        f"/projects/{pid}/variables",
        json_body={"key": key, "value": "v1"}, expect=201).json_data
    assert created["key"] == key
    assert created["value"] == "v1", "掩码变量之外的普通值必须明文回显（用于断言与对账）"

    one = gl.client.get(f"/projects/{pid}/variables/{key}", expect=200).json_data
    assert one["value"] == "v1", "创建回显与单查不一致——写入路径上有改写"

    updated = gl.client.put(f"/projects/{pid}/variables/{key}",
                            json_body={"value": "v2"}, expect=200).json_data
    assert updated["value"] == "v2", "PUT 200 但值没变——更新被静默丢弃"

    gl.client.delete(f"/projects/{pid}/variables/{key}", expect=204)
    gl.client.get(f"/projects/{pid}/variables/{key}", expect=404)


# ==========================================================================
# 2. 唯一性：重复 key
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("CI/CD 变量")
@allure.story("重复 key 拒绝")
@allure.severity(allure.severity_level.CRITICAL)
def test_duplicate_key_is_400(var_scope, gl):
    """同一 key 二次创建 → 400 'has already been taken'（实测，不是 409）。

    变量按 key 寻址，重复创建如果被静默接受，旧值会被覆盖——
    CI 流水线在两个版本间拿到的值不同，且没有任何提示。
    """
    pid = var_scope["pid"]
    key = _fresh_key(var_scope)
    gl.client.post(f"/projects/{pid}/variables",
                   json_body={"key": key, "value": "first"}, expect=201)
    try:
        resp = gl.client.post(f"/projects/{pid}/variables",
                              json_body={"key": key, "value": "second"},
                              expect=400)
        msg = str(resp.json_data)
        assert "already been taken" in msg, (
            f"400 但原因不是重复 key：{msg[:200]}")
        # 守住"没被静默覆盖"：原值必须还在
        one = gl.client.get(f"/projects/{pid}/variables/{key}",
                            expect=200).json_data
        assert one["value"] == "first", "重复创建被拒，但原值被覆盖了——拒绝得不彻底"
    finally:
        gl.client.delete(f"/projects/{pid}/variables/{key}", expect=(204, 404))


# ==========================================================================
# 3. 掩码规则
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("CI/CD 变量")
@allure.story("masked 值可掩码校验")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.parametrize("value,ok", [("v3", False), (MASKABLE, True)],
                         ids=["short", "maskable"])
def test_masked_value_must_be_maskable(var_scope, gl, value, ok):
    """masked=true 时值必须可掩码。

    实测边界：短值 "v3" → 400；24 位 Base32 风格 → 200 且 masked=true。
    为什么这是 CRITICAL：masked 是机密标记，规则过松 = 日志里泄漏
    凭据；规则过严 = 用户以为掩了其实没掩上。
    """
    pid = var_scope["pid"]
    key = _fresh_key(var_scope)
    # 先按已实测的路径建出变量（POST → 201），再 PUT 改 masked——
    # PUT 对不存在 key 的行为没有实测过，不猜
    gl.client.post(f"/projects/{pid}/variables",
                   json_body={"key": key, "value": "v0"}, expect=201)
    if ok:
        resp = gl.client.put(f"/projects/{pid}/variables/{key}",
                             json_body={"value": value, "masked": True},
                             expect=200)
        assert resp.json_data["masked"] is True, "更新成功但 masked 标志没有落库"
    else:
        resp = gl.client.put(f"/projects/{pid}/variables/{key}",
                             json_body={"value": value, "masked": True},
                             expect=400)
        assert "invalid" in str(resp.json_data), (
            f"400 但原因不是值不可掩码：{str(resp.json_data)[:200]}")
    gl.client.delete(f"/projects/{pid}/variables/{key}", expect=(204, 404))


# ==========================================================================
# 4. 字段校验
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("CI/CD 变量")
@allure.story("必填 key")
@allure.severity(allure.severity_level.CRITICAL)
def test_missing_key_is_400(var_scope, gl):
    """缺 key → 400 'key is missing'。key 是变量的主键，必填没有商量。"""
    resp = gl.client.post(f"/projects/{var_scope['pid']}/variables",
                          json_body={"value": "x"}, expect=400)
    assert "key is missing" in str(resp.json_data)


@allure.epic("GitLab API")
@allure.feature("CI/CD 变量")
@allure.story("key 字符集")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("key", BAD_KEYS)
def test_key_charset_is_rejected(var_scope, gl, key):
    """key 只允许字母/数字/下划线：CI 语法里变量名要能直接被引用。

    空格、连字符、非 ASCII 字母都 400，实测消息
    "can contain only letters, digits and '_'"。
    """
    resp = gl.client.post(f"/projects/{var_scope['pid']}/variables",
                          json_body={"key": key, "value": "x"}, expect=400)
    assert "letters" in str(resp.json_data), (
        f"400 但原因不是 key 字符集：{str(resp.json_data)[:200]}")


@allure.epic("GitLab API")
@allure.feature("CI/CD 变量")
@allure.story("PUT 改 key 被忽略")
@allure.severity(allure.severity_level.NORMAL)
def test_update_key_field_is_ignored(var_scope, gl):
    """实测：PUT 只传 key（不带 value）→ 200，变量原样不变。

    即：key 不能通过 PUT 改名，缺省的 value 也不会被清空——
    PUT 在这里表现为"忽略未知操作的无操作"。
    （探针阶段曾误判为 400：那次 PUT 附带了 value "x"，
    而变量残留着上一轮的 masked=true，"x" 不可掩码才被拒——
    被这条用例复跑纠了出来，这也是"期望值必须实测"的注脚。）
    """
    pid = var_scope["pid"]
    key = _fresh_key(var_scope)
    gl.client.post(f"/projects/{pid}/variables",
                   json_body={"key": key, "value": "v1"}, expect=201)
    try:
        resp = gl.client.put(f"/projects/{pid}/variables/{key}",
                             json_body={"key": f"{key}_NEW"},
                             expect=200)
        one = resp.json_data
        assert one["key"] == key, (
            f"PUT 后 key 变成了 {one['key']!r}——变量按 key 寻址，"
            "允许改名等于旧引用全部悄悄指向不存在的变量")
        assert one["value"] == "v1", "没传 value 却把值改了——PUT 不是整覆盖语义"
    finally:
        gl.client.delete(f"/projects/{pid}/variables/{key}", expect=(204, 404))


# ==========================================================================
# 5. 幂等 + 越界
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("CI/CD 变量")
@allure.story("重复删除幂等")
@allure.severity(allure.severity_level.CRITICAL)
def test_variable_double_delete_is_404(var_scope, gl):
    pid = var_scope["pid"]
    key = _fresh_key(var_scope)
    gl.client.post(f"/projects/{pid}/variables",
                   json_body={"key": key, "value": "v"}, expect=201)
    gl.client.delete(f"/projects/{pid}/variables/{key}", expect=204)
    gl.client.delete(f"/projects/{pid}/variables/{key}", expect=404)


@allure.epic("GitLab API")
@allure.feature("CI/CD 变量")
@allure.story("404 防枚举语义")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_variables_on_missing_project_is_404(gl, method):
    if method == "GET":
        resp = gl.client.get("/projects/99999999/variables", expect=404)
    else:
        resp = gl.client.post("/projects/99999999/variables",
                              json_body={"key": "K", "value": "v"}, expect=404)
    assert "Project Not Found" in str(resp.json_data)
