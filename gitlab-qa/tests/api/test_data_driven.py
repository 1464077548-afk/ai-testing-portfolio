"""数据驱动用例：输入矩阵全部来自 tests/data/api/*.yaml。

这组用例本身只包含「怎么发请求、怎么判状态码」这段通用逻辑，
具体测哪些输入、期望什么结果，全部写在 YAML 里。

分工是这样的：

    tests/data/api/validation.yaml    创建接口的必填字段与非法值
    tests/data/api/naming.yaml        分支名 / 标签名的合法性规则
    tests/data/api/query_params.yaml  查询参数边界（分页 / 排序 / 过滤 / 搜索）

加一条边界用例的流程：往对应 YAML 里加一段即可，这个文件和 conftest
都不用动。反之，如果某条矩阵需要改代码才能表达，说明它已经不是
"数据"，应该写成独立用例。

三个文件的期望值都是实测得来的，注释里标了 GitLab 版本。
"""

from __future__ import annotations

from urllib.parse import quote

import allure
import pytest

from gqa.data.factory import DataFactory

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]


@pytest.fixture(scope="module")
def matrix_project(gl, settings) -> int:
    """三组矩阵共用一个项目。

    用 function 级的 a_project 会让每条矩阵都新建一次带 README 的项目，
    60 条矩阵光造数就要两分钟，而它们压根不需要"全新的项目"。

    共享是安全的，因为这三组矩阵不依赖项目是干净的：
    校验类全是应被拒绝的请求（不产生数据），命名类用 {uniq} 命名且用完即删，
    查询参数类全是 GET。彼此之间唯一的共享状态就是项目 id 本身。
    """
    factory = DataFactory(gl, prefix=f"{settings.resource_prefix}-mtx")
    proj = factory.project(initialize_with_readme=True)
    yield proj["id"]
    factory.cleanup()
    _attach(factory.ledger.report(), "矩阵数据清理记录")


def _attach(text: str, name: str) -> None:
    try:
        allure.attach(text, name=name, attachment_type=allure.attachment_type.TEXT)
    except Exception:  # noqa: BLE001
        pass


def _message_of(body) -> str:
    """错误体可能是字符串，也可能是 {field: [...]} 形式的对象，统一转字符串。"""
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or body)
    return str(body)


def _call(gl, case):
    """按 case 发一次请求。expect 交给调用方自己判，不用 client 的 expect=

    因为断言失败时要给出「实际状态码 + 期望集合 + 响应原文」三件套，
    让 client 抛 GitLabError 只能拿到一句格式化好的话，看不到完整响应体。
    """
    if case.method == "GET":
        return gl.client.get(case.path, params=case.params)
    return gl.client.request(case.method, case.path, params=case.params,
                             json_body=case.body)


def _cleanup(gl, pid: int, case) -> None:
    """YAML 里标了 cleanup 的，创建成功后要把东西删掉。

    不删的话每跑一轮就在实例上多 3 个分支、3 个标签，
    几十轮之后仓库页面会慢到超时——而那时没人会想到是"用例没清理"。
    """
    if not case.cleanup:
        return
    try:
        if case.cleanup == "branch":
            name = (case.params or {}).get("branch")
            if name:
                gl.repository.delete_branch(pid, name)
        elif case.cleanup == "tag":
            name = (case.body or {}).get("tag_name")
            if name:
                gl.client.delete(
                    f"/projects/{pid}/repository/tags/{quote(str(name), safe='')}",
                    expect=(200, 202, 204, 404))
    except Exception as exc:  # noqa: BLE001
        # 清理失败不推翻用例结论，但要留痕：静默吞掉的残留最难查
        _attach(f"[残留] {case.cleanup} {case.id}: {exc}", "清理异常")


def _run_matrix(gl, pid: int, case) -> None:
    """三段断言：状态码在期望集合内、错误原因对得上、该回收的回收掉。"""
    if case.skip:
        pytest.skip(case.skip)

    bound = case.bind(pid=pid)
    try:
        resp = _call(gl, bound)

        _attach(
            f"{bound.describe()}\n实际：HTTP {resp.status}\n"
            f"响应：{str(resp.json_data)[:400]}",
            f"{case.source}::{case.id}")

        assert resp.status in case.expect, (
            f"[{case.id}] {case.desc}\n"
            f"  期望状态码 {list(case.expect)}，实际 {resp.status}\n"
            f"  请求：{bound.describe()}\n"
            f"  响应：{str(resp.json_data)[:300]}")

        # 状态码对了还不够：400 的原因必须和这条用例要验的东西有关。
        # 不查这个的话，路径写错、Token 失效导致的 400 也会"通过"。
        if case.expect_msg:
            msg = _message_of(resp.json_data)
            assert case.expect_msg.lower() in msg.lower(), (
                f"[{case.id}] 状态码 {resp.status} 符合预期，但错误原因对不上：\n"
                f"  期望包含：{case.expect_msg}\n  实际消息：{msg[:300]}")
    finally:
        if case.cleanup:
            _cleanup(gl, pid, case)


# ==========================================================================
# 三组矩阵
# ==========================================================================
@allure.epic("GitLab API")
@allure.feature("数据驱动矩阵")
@allure.story("创建接口的字段校验")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.cases("validation.yaml")
def test_validation_matrix(gl, matrix_project, case):
    """缺必填字段、字段取非法值 → 一律 400，且错误原因说得清楚。

    状态码只说明"拒绝了"，expect_msg 才说明"拒绝得对不对"。
    少了后者，一条把 path 写错、拿到 404/400 的用例也会是绿的。
    """
    _run_matrix(gl, matrix_project, case)


@allure.epic("GitLab API")
@allure.feature("数据驱动矩阵")
@allure.story("分支名与标签名的合法性")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.cases("naming.yaml")
def test_naming_matrix(gl, matrix_project, case):
    """命名规则是纯业务约束，最适合放在 YAML 里——规则变了改数据即可。

    合法命名的用例会真的创建成功（201），所以带 {uniq} 防重名，
    并用 cleanup 回收。这里断言的是"该收的收、该放的放"，
    两边任一方向写反都会红。
    """
    _run_matrix(gl, matrix_project, case)


@allure.epic("GitLab API")
@allure.feature("数据驱动矩阵")
@allure.story("查询参数边界")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.cases("query_params.yaml")
def test_query_params_matrix(gl, matrix_project, case):
    """把服务端对边界值的真实态度固定下来。

    这里最有价值的是几处"不一致"：per_page=0 报 400 但 page=0 收 200；
    per_page=-1 静默回落成默认页宽。这些没法靠推理得出，只能实测记录。
    将来若版本变更，红的原因就写在对应 YAML 条目的 desc 里。
    """
    _run_matrix(gl, matrix_project, case)
