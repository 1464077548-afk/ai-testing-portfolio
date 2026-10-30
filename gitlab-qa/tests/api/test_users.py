"""用户相关接口。

「创建用户」是管理员端点：普通 Token 会得到 403。这条边界不写清楚，
用例换到非管理员身份上跑就会红，而看起来像是功能坏了——
实际是身份不够，属于用例设计问题。所以这里显式区分并说明。
"""

from __future__ import annotations

import random
import secrets
import string
import time

import allure
import pytest

from gqa import contracts
from gqa.utils.wait import wait_until

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]


def _strong_password() -> str:
    """每次随机生成，避开 GitLab 19 的密码策略。

    踩过的坑：写死一个"看起来很强"的口令（如 Qa!Auto-9f3k2m）会被判
    「must not contain commonly used combinations of words and letters」——
    策略认得出 Auto/Qa 这类常见词根。固定口令还有第二个问题：
    它一旦进仓库就永久泄露。随机生成两弊皆消。
    """
    alphabet = string.ascii_letters + string.digits
    return "Qz7#" + "".join(secrets.choice(alphabet) for _ in range(12))


def _uniq(prefix: str) -> str:
    return f"{prefix}-{int(time.time()) % 10**7}-{random.randint(1000, 9999)}"


@allure.epic("GitLab API")
@allure.feature("用户")
@allure.story("当前用户")
@allure.severity(allure.severity_level.CRITICAL)
def test_current_user_matches_contract(gl):
    resp = gl.users.current()
    assert resp.status == 200, f"取当前用户失败：{resp.status}"
    errors = contracts.collect_errors(resp.json_data, contracts.USER)
    assert errors == [], f"不符合契约：{errors}"


@allure.epic("GitLab API")
@allure.feature("用户")
@allure.story("用户列表与详情")
@allure.severity(allure.severity_level.NORMAL)
def test_user_list_and_detail(gl):
    rows = gl.users.list()
    assert rows, "用户列表为空，无法继续校验"
    for row in rows[:5]:
        errors = contracts.collect_errors(row, contracts.USER)
        assert errors == [], f"不符合契约：{errors}"

    uid = rows[0]["id"]
    detail = gl.users.get(uid)
    assert detail.status == 200
    assert detail.json_data["id"] == uid


@allure.epic("GitLab API")
@allure.feature("用户")
@allure.story("不存在的用户 404")
@allure.severity(allure.severity_level.MINOR)
def test_unknown_user_is_404(gl):
    assert gl.users.get(987654321).status == 404, "不存在的用户应 404"


@allure.epic("GitLab API")
@allure.feature("用户")
@allure.story("SSH 密钥列表")
@allure.severity(allure.severity_level.MINOR)
def test_user_keys_are_listed(gl):
    resp = gl.users.keys()
    assert resp.status in (200, 403), f"密钥列表异常：{resp.status}"
    if resp.status == 200:
        assert isinstance(resp.json_data, list)


@allure.epic("GitLab API")
@allure.feature("用户")
@allure.story("用户可见项目")
@allure.severity(allure.severity_level.MINOR)
def test_user_projects_are_listed(gl):
    uid = gl.users.current().json_data["id"]
    rows = gl.users.projects(uid)
    assert isinstance(rows, list), "用户项目列表应为数组"
    for row in rows[:5]:
        errors = contracts.collect_errors(row, contracts.PROJECT)
        assert errors == [], f"不符合契约：{errors}"


@allure.epic("GitLab API")
@allure.feature("用户")
@allure.story("项目成员候选用户")
@allure.severity(allure.severity_level.MINOR)
def test_project_candidate_users(gl, a_project):
    rows = gl.projects.users(a_project.id)
    assert isinstance(rows, list)
    for row in rows[:5]:
        errors = contracts.collect_errors(row, contracts.USER)
        assert errors == [], f"不符合契约：{errors}"


@allure.epic("GitLab API")
@allure.feature("用户")
@allure.story("管理员可删除用户")
@allure.severity(allure.severity_level.MINOR)
def test_admin_can_delete_user(gl):
    """创建 → 删除 → 查不到。删不干净的用户会留在实例上，
    下一轮再建同名用户时 409，而报错指向的是创建，根因却在上一轮。

    删除是**两阶段**的（GitLab 19.0.1 实测）：DELETE 立刻回 204，
    用户 state 由 active 变 blocked，真正的物理清除由后台任务完成——
    实测 60 秒后 GET 仍是 200。所以「删完立刻断言 404」必然假红。
    这里认两个信号中的任意一个：对象消失，或已被停用（blocked）。
    两者都证明删除生效了；都等不到才算失败。
    """
    username = _uniq("qa-del-user")
    created = gl.users.create(username, f"{username}@example.com", _strong_password())
    if created.status == 403:
        pytest.skip("当前 Token 不是管理员，跳过删除用户用例")
    assert created.status == 201, f"创建用户失败：{created.json_data}"
    uid = created.json_data["id"]

    deleted = gl.client.delete(f"/users/{uid}", expect=(200, 202, 204, 404, 409))
    assert deleted.status in (200, 202, 204), f"删除用户失败：{deleted.status}"

    def _taken_effect() -> bool:
        resp = gl.users.get(uid)
        if resp.status == 404:
            return True
        return (resp.json_data or {}).get("state") in ("blocked", "deleted")

    # 轮询窗口给 240s：删除是后台异步任务（Sidekiq），全量并发跑时
    # 实例负载高、队列积压——实测轻载 2s；CI 上四路并发 + 反复造数据后
    # Sidekiq default 队列积压到 400+ 条、延迟 65s 以上（实测 2026-10-30），
    # 120s 窗口会周期性不够。窗口只在收敛变慢时才烧满，
    # 正常路径一翻转立刻返回，不会平白拖慢套件。
    # 间隔 2s：满载时每秒一发只会加重实例负担，反而拖慢收敛。
    settled = wait_until(_taken_effect, timeout=240.0, interval=2.0,
                         description=f"用户 {uid} 进入已删除/停用态")
    last = gl.users.get(uid)
    allure.attach(
        f"DELETE → {deleted.status}\n"
        f"轮询后 GET → {last.status}，state={(last.json_data or {}).get('state')}",
        name="删除收敛", attachment_type=allure.attachment_type.TEXT)
    assert settled, (
        f"删除后用户既未消失也未被停用（HTTP {last.status}，"
        f"state={(last.json_data or {}).get('state')}）")


@allure.epic("GitLab API")
@allure.feature("用户")
@allure.story("管理员可创建用户")
@allure.severity(allure.severity_level.NORMAL)
def test_admin_can_create_user(gl):
    """创建用户需要管理员。非管理员拿到 403 时按设计 skip——
    把「身份不够」报成「功能有缺陷」是最容易误导人的一类假红。
    """
    # 用户名全局唯一：重复会 409，而 409 与「创建成功」在报告上都叫「没崩」，
    # 不唯一的话这条用例会在第二次运行时开始假绿
    username = _uniq("qa-auto-user")
    resp = gl.users.create(username, f"{username}@example.com", _strong_password())
    if resp.status == 403:
        pytest.skip("当前 Token 不是管理员，跳过创建用户用例")

    assert resp.status == 201, f"创建用户失败（{resp.status}）：{resp.json_data}"
    uid = resp.json_data["id"]
    try:
        assert gl.users.get(uid).status == 200, "新建用户查不到"
    finally:
        gl.client.delete(f"/users/{uid}", expect=(200, 202, 204, 404, 409))
