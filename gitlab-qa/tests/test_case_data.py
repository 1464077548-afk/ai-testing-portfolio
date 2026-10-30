"""YAML 用例数据的自检：不需要 GitLab 实例，秒级完成，适合进门禁。

这组用例不测被测系统，测的是**我们自己写的测试数据**。
理由和被测系统一样：数据写坏不会报错，只会让某条用例"没跑"或"白跑"，
报告上看不出来。所以把最容易写错的几种情况都钉死：

1. 期望值自欺——`expect: [200, 400]`，两种结果都算通过，等于没断言；
2. GET 用例带 body——请求体会被忽略，用例实际测的是另一回事；
3. id 重复——参数化 id 撞车，红的时候分不清是哪条；
4. 缺 desc——几个月后没人记得这条矩阵想守什么。

这些检查跑在收集/执行早期，且不需要网络，改完 YAML 几秒内就能知道对错。
"""

from __future__ import annotations

import pytest

from gqa.data.case_loader import iter_case_files, load_case_file

FILES = iter_case_files()


def _all_cases():
    return [(path, case) for path in FILES for case in load_case_file(path)]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_case_file_is_loadable(path):
    """加载器会在字段拼错 / 缺必填 / id 重复时直接抛，这里兜住这些情况。"""
    cases = load_case_file(path)
    assert cases, f"{path.name} 里没有任何用例"
    assert len(cases) >= 5, (
        f"{path.name} 只有 {len(cases)} 条用例——矩阵太小，"
        "单独写成一条用例可能比放进 YAML 更清楚")


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_expectations_are_not_self_defeating(path):
    """`expect: [200, 400]` 是最常见的自欺写法：两种结果都通过，等于没测。

    校验规则：一个期望集合里不能既有 2xx 又有 4xx/5xx。
    确实存在"两种实现都说得通"的情况（比如 per_page 超限可以 400 也可以
    归一到 100），那种情况应当写两条用例、各自只认一种结果，
    而不是写一条"怎样都行"的。
    """
    for case in load_case_file(path):
        ok = [s for s in case.expect if 200 <= s < 300]
        bad = [s for s in case.expect if s >= 400]
        assert not (ok and bad), (
            f"{path.name}::{case.id} 的期望同时含成功码 {ok} 与失败码 {bad}——"
            "两种结果都算通过，这条用例等于没断言")


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_case_declares_both_success_and_failure_message(path):
    """expect_msg 只在失败码用例上有意义。

    给"期望 200"的用例配 expect_msg 是自相矛盾的——成功响应里
    不会有错误消息，子串匹配永远失败，或者更糟：匹配上了别的内容。
    """
    for case in load_case_file(path):
        if case.expect_msg and all(200 <= s < 300 for s in case.expect):
            pytest.fail(
                f"{path.name}::{case.id} 期望成功（{list(case.expect)}），"
                f"却又要求错误体包含 {case.expect_msg!r}")


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_get_cases_carry_no_body(path):
    """GET 用例带 body 是个安静的错误：请求体被忽略，用例在测别的东西。

    这类用例会绿——因为它真能发出去、也真能拿到 200——
    只是它验证的那条规则从未被执行过。
    """
    for case in load_case_file(path):
        if case.method == "GET":
            assert case.body is None, (
                f"{path.name}::{case.id} 是 GET 却带了 body；"
                "要传查询条件请用 params")


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_case_is_documented(path):
    """desc 是矩阵里唯一的人类可读说明，缺了它这条用例就无法评审。"""
    for case in load_case_file(path):
        assert len(case.desc.strip()) >= 6, (
            f"{path.name}::{case.id} 的 desc 太短（{case.desc!r}）——"
            "写清楚这条矩阵要守住什么")


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_paths_are_absolute_and_placeholders_are_known(path):
    """path 必须以 / 开头，且占位符只能是 {pid} / {uniq}（后者加载时已换掉）。

    少写一个 / 会让请求打到相对路径上，拿到一个"看起来合理的 200"。
    """
    import re

    for case in load_case_file(path):
        assert case.path.startswith("/"), (
            f"{path.name}::{case.id} 的 path 必须以 / 开头：{case.path}")
        left = set(re.findall(r"\{(\w+)\}", case.path))
        assert left <= {"pid"}, (
            f"{path.name}::{case.id} 的 path 含未知占位符 {left - {'pid'}}；"
            "目前只有 {pid} 会在运行时绑定")


# ==========================================================================
# 加载器自己的安全网
# ==========================================================================
_BAD_FILES = {
    "unknown-field": """
cases:
  - id: x
    desc: 字段名拼错
    method: GET
    path: /projects
    expected: [200]
""",
    "missing-required": """
cases:
  - id: y
    desc: 少了 path
    method: GET
    expect: [200]
""",
    "duplicate-id": """
cases:
  - {id: dup, desc: 第一条, method: GET, path: /projects, expect: [200]}
  - {id: dup, desc: 第二条, method: GET, path: /projects, expect: [200]}
""",
    "empty-expect": """
cases:
  - id: z
    desc: expect 是空列表
    method: GET
    path: /projects
    expect: []
""",
    "broken-yaml": """
cases:
  - id: broken
   desc: 缩进错了
""",
    "no-cases-key": """
cases_list:
  - id: nope
""",
}


@pytest.mark.parametrize("name,text", sorted(_BAD_FILES.items()))
def test_broken_data_files_are_rejected(tmp_path, name, text):
    """写坏的 YAML 必须让这轮跑不起来，而不是被静默跳过。

    上面几种是最容易发生的错误，尤其是字段拼错——`expect` 写成 `expected`
    之后那条用例会带着空期望继续跑，断言 `status in ()` 永远不成立……
    不，更糟：加载器若不校验，expect 为空会让断言变成"任意状态码都行"吗？
    不会，但会退化成一条意义不明的用例。总之：错了就要响。
    """
    from gqa.data.case_loader import CaseDataError, load_case_file

    bad = tmp_path / f"{name}.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(CaseDataError):
        load_case_file(bad)


def test_directory_is_not_empty():
    """目录为空时上面所有 parametrize 都会"通过"（没有参数就不跑），
    表现为一片绿，实际一条自检都没执行——必须单独拦一下。"""
    assert FILES, (
        f"tests/data/api/ 下没有 YAML 用例文件（查找目录："
        f"{FILES[0].parent if FILES else '?'}）——所有数据自检会被空参数化跳过")
