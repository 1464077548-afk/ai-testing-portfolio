"""仓库：文件 / 提交 / 分支 / 标签。

这组是覆盖率提升的大头，也是最容易写出「只在靶机上绿」的用例的地方：
文件路径和分支名都可能带斜杠，写死 main 在默认分支不同的实例上直接 404，
base64 编码假设错了则断言看起来通过了、比对的实际是乱码。
"""

from __future__ import annotations

import base64

import allure
import pytest

from gqa import contracts
from gqa.api.client import GitLabError

pytestmark = [pytest.mark.api, pytest.mark.regression,
              pytest.mark.destructive, pytest.mark.requires_token]


def _default_branch(project: dict) -> str:
    """默认分支名由实例决定，写死 main 在部分实例上会 404。"""
    return project.get("default_branch") or "main"


def _decode_file(resp) -> str:
    body = resp.json_data
    if body.get("encoding") == "base64":
        return base64.b64decode(body["content"]).decode("utf-8")
    return str(body.get("content") or "")


@allure.epic("GitLab API")
@allure.feature("仓库 · 文件")
@allure.story("文件增删改查")
@allure.severity(allure.severity_level.CRITICAL)
def test_file_lifecycle(gl, data_factory):
    project = data_factory.project()
    pid = project["id"]
    branch = _default_branch(project)

    with allure.step("新建文件"):
        created = gl.repository.create_file(pid, "qa-file.txt", branch,
                                            "第一行内容\n", "qa: add file")
        assert created.status == 201, f"创建文件失败：{created.status}"

    with allure.step("读回内容与创建时一致"):
        fetched = gl.repository.file(pid, "qa-file.txt", ref=branch)
        assert fetched.status == 200
        errors = contracts.collect_errors(fetched.json_data, contracts.FILE)
        assert errors == [], f"不符合契约：{errors}"
        assert _decode_file(fetched) == "第一行内容\n"

    with allure.step("更新文件"):
        updated = gl.repository.update_file(pid, "qa-file.txt", branch,
                                            "改后的内容\n", "qa: update file")
        assert updated.status == 200
        assert _decode_file(gl.repository.file(pid, "qa-file.txt", ref=branch)) == "改后的内容\n"

    with allure.step("删除文件后应 404"):
        gl.repository.delete_file(pid, "qa-file.txt", branch, "qa: remove file")
        assert gl.repository.file(pid, "qa-file.txt", ref=branch).status == 404


@allure.epic("GitLab API")
@allure.feature("仓库 · 文件")
@allure.story("不存在的分支上写文件应被拒绝")
@allure.severity(allure.severity_level.NORMAL)
def test_write_to_missing_branch_is_rejected(gl, data_factory):
    project = data_factory.project()
    with pytest.raises(GitLabError) as exc:
        gl.repository.create_file(project["id"], "x.txt", "no-such-branch",
                                  "x", "qa: bad branch")
    assert exc.value.status in (400, 404), f"不存在的分支应被拒绝，实际 {exc.value.status}"


@allure.epic("GitLab API")
@allure.feature("仓库 · 文件")
@allure.story("目录树能列出文件")
@allure.severity(allure.severity_level.NORMAL)
def test_tree_lists_files(gl, data_factory):
    project = data_factory.project()
    pid, branch = project["id"], _default_branch(project)
    gl.repository.create_file(pid, "qa-tree.txt", branch, "hi\n", "qa: add")

    resp = gl.repository.tree(pid, ref=branch)
    assert resp.status == 200
    rows = resp.json_data
    assert any(r["name"] == "qa-tree.txt" for r in rows), "新建文件没出现在目录树里"
    for row in rows:
        errors = contracts.collect_errors(row, contracts.TREE_ITEM)
        assert errors == [], f"目录项不符合契约：{errors}"


@allure.epic("GitLab API")
@allure.feature("仓库 · 提交")
@allure.story("提交后可查详情")
@allure.severity(allure.severity_level.NORMAL)
def test_commit_can_be_created_and_fetched(gl, data_factory):
    project = data_factory.project()
    pid, branch = project["id"], _default_branch(project)

    resp = gl.repository.create_commit(
        pid, branch, "qa: commit via api",
        [{"action": "create", "file_path": "qa-commit.txt", "content": "from api\n"}])
    assert resp.status == 201, f"创建提交失败：{resp.status}"
    sha = resp.json_data["id"]
    errors = contracts.collect_errors(resp.json_data, contracts.COMMIT)
    assert errors == [], f"不符合契约：{errors}"

    with allure.step("提交列表里应能查到"):
        listing = gl.repository.commits(pid)
        assert listing.status == 200
        assert any(c["id"] == sha for c in listing.json_data), "新提交没出现在列表里"

    with allure.step("按 sha 取详情"):
        detail = gl.repository.commit(pid, sha)
        assert detail.status == 200
        assert detail.json_data["message"] == "qa: commit via api"


@allure.epic("GitLab API")
@allure.feature("仓库 · 分支")
@allure.story("分支创建、保护、删除")
@allure.severity(allure.severity_level.CRITICAL)
def test_branch_lifecycle(gl, data_factory):
    project = data_factory.project()
    pid = project["id"]
    base = _default_branch(project)
    name = "qa/feature-branch"   # 故意带斜杠：验证路径编码没写错

    with allure.step("创建分支"):
        created = gl.repository.create_branch(pid, name, base)
        assert created.status == 201, f"创建分支失败：{created.status}"
        errors = contracts.collect_errors(created.json_data, contracts.BRANCH)
        assert errors == [], f"不符合契约：{errors}"

    with allure.step("读回与列表"):
        assert gl.repository.branch(pid, name).status == 200
        names = [b["name"] for b in gl.repository.branches(pid).json_data]
        assert name in names

    with allure.step("设为保护分支"):
        protected = gl.repository.protect_branch(pid, name)
        assert protected.status == 200
        assert protected.json_data.get("protected") is True, "保护标记没生效"

    with allure.step("删除后应查不到"):
        gl.repository.delete_branch(pid, name)
        assert gl.repository.branch(pid, name).status == 404


@allure.epic("GitLab API")
@allure.feature("仓库 · 分支")
@allure.story("默认分支不允许删除")
@allure.severity(allure.severity_level.NORMAL)
def test_default_branch_cannot_be_deleted(gl, data_factory):
    """这是保护而不是缺陷：把默认分支删掉，仓库就没法访问了。"""
    project = data_factory.project()
    base = _default_branch(project)
    with pytest.raises(GitLabError) as exc:
        gl.repository.delete_branch(project["id"], base)
    assert exc.value.status in (400, 403, 405), (
        f"删除默认分支应被拒绝，实际 {exc.value.status}")


@allure.epic("GitLab API")
@allure.feature("仓库 · 标签")
@allure.story("创建标签并出现在列表")
@allure.severity(allure.severity_level.NORMAL)
def test_tag_lifecycle(gl, data_factory):
    project = data_factory.project()
    pid, base = project["id"], _default_branch(project)

    created = gl.repository.create_tag(pid, "v-qa-1.0.0", base, "qa release")
    assert created.status == 201, f"创建标签失败：{created.status}"
    errors = contracts.collect_errors(created.json_data, contracts.TAG)
    assert errors == [], f"不符合契约：{errors}"

    names = [t["name"] for t in gl.repository.tags(pid)]
    assert "v-qa-1.0.0" in names, "标签没出现在列表里"

    with allure.step("重复创建同名标签应被拒绝"):
        with pytest.raises(GitLabError) as exc:
            gl.repository.create_tag(pid, "v-qa-1.0.0", base)
        assert exc.value.status in (400, 409)


@allure.epic("GitLab API")
@allure.feature("仓库")
@allure.story("比较与语言占比")
@allure.severity(allure.severity_level.MINOR)
def test_compare_and_languages(gl, data_factory):
    """比较接口要能真正比出差异。

    两个坑，都是实测踩出来的：
    1. `compare_same_ref` 比的是**解析后的 commit**，不是分支名。
       从 main 切一个分支、什么都不改，比较结果仍是 same_ref=true——
       所以「建了分支就断言 same_ref=false」必然假红。要断言差异，
       就得先在新分支上真的提交一次。
    2. 不存在的 ref 是 404 而不是空结果，别把「参数写错」当成「没有差异」。
    """
    project = data_factory.project()
    pid, base = project["id"], _default_branch(project)

    with allure.step("从默认分支切出一条并制造真实差异"):
        created = gl.repository.create_branch(pid, "qa-compare", base)
        assert created.status == 201, f"建分支失败：{created.status} {created.json_data}"
        committed = gl.repository.create_commit(
            pid, "qa-compare", "qa: compare target",
            [{"action": "create", "file_path": "qa-compare.txt", "content": "diff\n"}])
        assert committed.status == 201, f"提交失败：{committed.status} {committed.json_data}"

    with allure.step("有差异：same_ref=false 且能列出提交"):
        compared = gl.repository.compare(pid, base, "qa-compare")
        assert compared.status == 200, f"比较失败：{compared.status}"
        data = compared.json_data
        allure.attach(f"commits={len(data.get('commits') or [])} diffs="
                      f"{len(data.get('diffs') or [])} same_ref={data.get('compare_same_ref')}",
                      name="比较结果", attachment_type=allure.attachment_type.TEXT)
        assert data.get("compare_same_ref") is False, "两个不同 commit 却被判为 same_ref"
        assert data.get("commits"), "有差异却没列出任何提交"

    with allure.step("同 ref：same_ref=true"):
        same = gl.repository.compare(pid, base, base)
        assert same.json_data.get("compare_same_ref") is True, "同 ref 比较应标记 same_ref"

    with allure.step("不存在的 ref 应 404 而非空结果"):
        missing = gl.repository.compare(pid, base, "qa-no-such-ref")
        assert missing.status == 404, f"不存在的 ref 应 404，实际 {missing.status}"

    languages = gl.projects.languages(pid)
    assert languages.status in (200, 404)


@allure.epic("GitLab API")
@allure.feature("仓库")
@allure.story("项目不存在时仓库接口 404")
@allure.severity(allure.severity_level.MINOR)
def test_repository_of_missing_project_is_404(gl):
    assert gl.repository.tree(99999999).status == 404
    assert gl.repository.branches(99999999).status == 404
    assert gl.repository.commits(99999999).status == 404
