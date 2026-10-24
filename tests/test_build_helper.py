# -*- coding: utf-8 -*-
"""``tools/build_helper.py`` 的回归测试。

这个模块此前**一条测试都没有**，而它做着一件危险的事：在项目目录旁边
建/删一个目录联接（junction）。删错东西的后果是把真实项目删掉，
所以这里既测行为，也把「删除必须用 ``os.rmdir``」这条安全属性钉死。

背景（为什么需要它）：PyQt5 5.15.x 的 ``QLibraryInfo`` 处理不了非 ASCII
安装路径，把「幕键」变成 ``????``，导致 PyInstaller 的 Qt hook 报
``Qt plugin directory '.../????V2/...' does not exist!``。解法是建一个纯
ASCII 的 junction 再从那里构建。
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import itertools
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = PROJECT_ROOT / "tools" / "build_helper.py"

# 本文件几乎每个测试都需要一个**纯 ASCII** 的临时目录——这正是被测对象
# 关心的性质。不能用 pytest 的 ``tmp_path``：它落在
# ``E:\DSHwork\幕键\.test_tmp\...`` 里，而项目目录名「幕键」
# 本身就含中文，用它测「ASCII 路径直接构建」会永远走进非 ASCII 分支。
# 所以自己造一个 ASCII 根（``E:\DSHwork`` 是 ASCII，``.tmp`` 也是）。
_ASCII_TMP_BASE = PROJECT_ROOT.parent / ".tmp" / "build_helper_tests"
_counter = itertools.count()


def _load_helper():
    """按路径加载 build_helper（``tools/`` 不是包，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location("build_helper_under_test", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def helper():
    return _load_helper()


def _remove_junctions_under(root: Path) -> None:
    """先摘掉 ``root`` 里的 junction，再让调用方删目录。

    **顺序很重要**：``shutil.rmtree`` 会跟随 junction 进到目标里删东西。
    这里用 ``os.rmdir``（只摘重解析点）逐个清掉。
    """
    if not root.is_dir():
        return
    for entry in list(root.iterdir()):
        try:
            if entry.is_dir() and entry.is_junction():
                os.rmdir(str(entry))
        except (OSError, AttributeError):
            pass


@pytest.fixture
def ascii_tmp():
    """纯 ASCII 的临时目录；夹具自己保证这一点。"""
    base = _ASCII_TMP_BASE / f"t{next(_counter)}"
    base.mkdir(parents=True, exist_ok=True)
    assert all(ord(ch) < 128 for ch in str(base)), f"夹具自身必须给出 ASCII 路径，实际 {base}"
    try:
        yield base
    finally:
        _remove_junctions_under(base)
        shutil.rmtree(base, ignore_errors=True)



# --------------------------------------------------------------------------
# 纯函数
# --------------------------------------------------------------------------


class TestAsciiDetection:
    def test_pure_ascii_path(self, helper):
        assert helper.is_ascii_path(Path(r"E:\DSHwork\screenkey")) is True

    def test_chinese_path_is_not_ascii(self, helper):
        assert helper.is_ascii_path(Path(r"E:\DSHwork\幕键")) is False

    def test_ascii_boundary(self, helper):
        """ord(ch) == 127 仍算 ASCII，128 开始不算。"""
        assert helper.is_ascii_path(Path("a\x7f")) is True
        assert helper.is_ascii_path(Path("a\x80")) is False

    def test_describe_lists_the_bad_chars(self, helper):
        """列出的是**去重且排序**的非 ASCII 字符，用于告诉用户是哪个目录名。"""
        got = helper.describe_non_ascii(Path(r"E:\DSHwork\幕键"))
        # 幕 U+5E55 < 键 U+952E，去重后按码点排序
        assert got == "幕键"

    def test_describe_empty_for_ascii(self, helper):
        assert helper.describe_non_ascii(Path(r"E:\DSHwork\dmkb2")) == ""

    def test_describe_deduplicates(self, helper):
        assert helper.describe_non_ascii(Path("弹弹弹")) == "弹"


# --------------------------------------------------------------------------
# 安全属性：删 junction 绝不能跟随链接
# --------------------------------------------------------------------------


class TestRemoveLinkIsSafe:
    """``_remove_link`` 是整个模块唯一会破坏文件系统的操作。"""

    def test_uses_os_rmdir_not_shutil_rmtree(self, helper):
        """检查**实际代码**，不是 docstring——注释里提到 rmtree 是在讲为什么不用它。"""
        tree = ast.parse(inspect.getsource(helper._remove_link))
        func = tree.body[0]
        # 丢掉 docstring，只看真正会执行的语句
        body = [
            node for node in func.body
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str))
        ]
        code = "\n".join(ast.unparse(node) for node in body)
        assert "os.rmdir" in code, f"必须用 os.rmdir，实际代码：{code}"
        assert "rmtree" not in code, (
            "shutil.rmtree 会跟随 junction 进去把**真实项目**删掉。"
            "junction 必须用 os.rmdir 移除（它只摘掉重解析点）。"
        )

    def test_module_never_imports_shutil(self):
        """整个模块都不该引入 shutil——留着它就是在给 rmtree 开门。"""
        tree = ast.parse(HELPER_PATH.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        assert not any("shutil" in name for name in imported), (
            f"build_helper 不应导入 shutil，实际导入了：{imported}"
        )

    def test_rmdir_removes_junction_without_touching_target(self, helper, ascii_tmp):
        """真建一个 junction，确认删除后**目标目录里的文件还在**。"""
        target = ascii_tmp / "real_project"
        target.mkdir()
        marker = target / "main.spec"
        marker.write_text("keep me", encoding="utf-8")

        link = ascii_tmp / "ascii_link"
        if not helper._make_link(link, target):
            pytest.skip("本环境无法创建 junction")

        try:
            assert (link / "main.spec").exists(), "junction 应能穿透到目标"
            assert helper._remove_link(link) is True
            assert not helper._link_exists(link), "junction 本身应已消失"
            # 关键断言：目标目录毫发无损
            assert target.is_dir(), "os.rmdir 不该删掉目标目录"
            assert marker.read_text(encoding="utf-8") == "keep me"
        finally:
            if helper._link_exists(link):
                os.rmdir(str(link))


# --------------------------------------------------------------------------
# 悬空 junction（目标已被删除）
# --------------------------------------------------------------------------


class TestDanglingJunction:
    """目标目录被删掉后，junction 还在——``Path.exists()`` 会撒谎说它不在。

    这是一个**真实踩到的生产缺陷**：``build.bat --check`` 曾永久失败并报
    「当文件已存在时，无法创建该文件」，因为 ``link.exists()`` 跟随重解析点、
    对悬空 junction 返回 False，于是调用方以为"没有链接"就直接 ``mklink``。
    实测三者对照：

        link.exists()      -> False   （跟随重解析点，被骗）
        os.path.lexists()  -> True    （只看名字，正确）
        link.is_junction() -> True
    """

    @pytest.fixture
    def dangling(self, helper, ascii_tmp):
        """造一个悬空 junction：建好链接后把目标目录删掉。"""
        target = ascii_tmp / "will_vanish"
        target.mkdir()
        link = ascii_tmp / "dangling_link"
        if not helper._make_link(link, target):
            pytest.skip("本环境无法创建 junction")
        # 删掉目标，只留链接
        target.rmdir()
        assert not link.exists(), "前提：悬空 junction 的 exists() 应为 False"
        assert os.path.lexists(str(link)), "前提：名字其实还在"
        try:
            yield link, target
        finally:
            if os.path.lexists(str(link)):
                os.rmdir(str(link))

    def test_exists_lies_but_lexists_does_not(self, helper, dangling):
        """把这个反直觉的事实钉住，将来有人改回 ``exists()`` 会立刻失败。"""
        link, _ = dangling
        assert link.exists() is False, "Path.exists() 会跟随重解析点，对悬空链接返回 False"
        assert os.path.lexists(str(link)) is True

    def test_link_exists_detects_dangling(self, helper, dangling):
        """``_link_exists`` 必须认出悬空的 junction，否则会去 mklink 然后失败。"""
        link, _ = dangling
        assert helper._link_exists(link) is True

    def test_link_exists_false_for_absent(self, helper, ascii_tmp):
        assert helper._link_exists(ascii_tmp / "definitely_not_here") is False

    def test_remove_link_works_on_dangling(self, helper, dangling):
        """``os.rmdir`` 不跟随链接，所以对悬空 junction 同样有效。"""
        link, _ = dangling
        assert helper._remove_link(link) is True
        assert not os.path.lexists(str(link))

    def test_ensure_rebuilds_over_dangling(self, helper, ascii_tmp, monkeypatch, capsys):
        """端到端：悬空 junction 必须被删掉重建，而不是让 mklink 报"已存在"。"""
        project = ascii_tmp / "幕键"
        project.mkdir()
        (project / "main.spec").write_text("# spec", encoding="utf-8")

        link_home = ascii_tmp / "ascii_home"
        link_home.mkdir()
        monkeypatch.setenv("SCREENKEY_BUILD_LINK", str(link_home))

        # 先在目标位置造一个悬空 junction
        gone = ascii_tmp / "gone"
        gone.mkdir()
        stale = link_home / helper.LINK_NAME
        if not helper._make_link(stale, gone):
            pytest.skip("本环境无法创建 junction")
        gone.rmdir()
        assert os.path.lexists(str(stale))

        got = helper.ensure_ascii_build_root(project, create=True)
        try:
            assert got is not None, (
                "悬空 junction 应被识别并重建；修好前这里会返回 None，"
                "并打印「mklink 返回 1：当文件已存在时，无法创建该文件」"
            )
            assert helper._link_points_at(got, project)
            assert (got / "main.spec").exists()
            assert "先删除再重建" in capsys.readouterr().err
        finally:
            if got is not None and os.path.lexists(str(got)):
                os.rmdir(str(got))


# --------------------------------------------------------------------------
# 链接判定
# --------------------------------------------------------------------------


class TestLinkPointsAt:
    def test_missing_link_is_false(self, helper, ascii_tmp):
        assert helper._link_points_at(ascii_tmp / "nope", ascii_tmp) is False

    def test_real_link_is_recognised(self, helper, ascii_tmp):
        target = ascii_tmp / "target"
        target.mkdir()
        link = ascii_tmp / "lnk"
        if not helper._make_link(link, target):
            pytest.skip("本环境无法创建 junction")
        try:
            assert helper._link_points_at(link, target) is True
            assert helper._link_points_at(link, ascii_tmp / "other") is False
        finally:
            if link.exists():
                os.rmdir(str(link))


# --------------------------------------------------------------------------
# ensure_ascii_build_root
# --------------------------------------------------------------------------


class TestEnsureAsciiBuildRoot:
    def test_ascii_project_root_is_returned_unchanged(self, helper, ascii_tmp):
        """纯 ASCII 路径不需要任何处理，也不能建出多余的东西。"""
        project = ascii_tmp / "ascii_proj"
        project.mkdir()
        (project / "main.spec").write_text("# spec", encoding="utf-8")

        before = {p.name for p in ascii_tmp.iterdir()}
        got = helper.ensure_ascii_build_root(project)
        assert got == project.resolve()
        assert {p.name for p in ascii_tmp.iterdir()} == before, "ASCII 路径下不该创建任何东西"

    def test_check_mode_creates_nothing_for_non_ascii(self, helper, ascii_tmp, capsys):
        """``--check`` 的契约：只报告，绝不创建。"""
        project = ascii_tmp / "幕键"
        project.mkdir()
        (project / "main.spec").write_text("# spec", encoding="utf-8")

        got = helper.ensure_ascii_build_root(project, create=False)
        assert got is None
        err = capsys.readouterr().err
        assert "非 ASCII" in err
        assert "仅检查模式" in err
        # 没有任何东西被创建
        assert {p.name for p in ascii_tmp.iterdir()} == {"幕键"}

    def test_check_mode_reports_the_offending_chars(self, helper, ascii_tmp, capsys):
        project = ascii_tmp / "幕键"
        project.mkdir()
        helper.ensure_ascii_build_root(project, create=False)
        assert "幕键" in capsys.readouterr().err

    def test_creates_junction_and_returns_it(self, helper, ascii_tmp, monkeypatch):
        """非 ASCII 路径 + create=True → 建 junction 并返回它。"""
        project = ascii_tmp / "幕键"
        project.mkdir()
        (project / "main.spec").write_text("# spec", encoding="utf-8")

        link_home = ascii_tmp / "ascii_home"
        link_home.mkdir()
        monkeypatch.setenv("SCREENKEY_BUILD_LINK", str(link_home))

        got = helper.ensure_ascii_build_root(project, create=True)
        assert got is not None, "应能建出 junction"
        assert helper.is_ascii_path(got), f"返回的构建根必须是纯 ASCII，实际 {got}"

        try:
            assert (got / "main.spec").exists()
        finally:
            if got.exists():
                os.rmdir(str(got))

    def test_reuses_existing_correct_junction(self, helper, ascii_tmp, monkeypatch):
        """幂等：已存在且指向正确时复用，不删不重建。"""
        project = ascii_tmp / "幕键"
        project.mkdir()
        (project / "main.spec").write_text("# spec", encoding="utf-8")

        link_home = ascii_tmp / "ascii_home"
        link_home.mkdir()
        monkeypatch.setenv("SCREENKEY_BUILD_LINK", str(link_home))

        first = helper.ensure_ascii_build_root(project, create=True)
        assert first is not None
        try:
            # 在链接里放个记号；重建的话它就没了
            marker = first / "reuse_marker.txt"
            marker.write_text("survived", encoding="utf-8")

            second = helper.ensure_ascii_build_root(project, create=True)
            assert second == first
            assert marker.read_text(encoding="utf-8") == "survived", (
                "复用分支不该删除并重建 junction"
            )
        finally:
            if first.exists():
                os.rmdir(str(first))

    def test_rebuilds_junction_pointing_elsewhere(self, helper, ascii_tmp, monkeypatch):
        """已存在但指向别处 → 删掉重建，而不是将就用错的。"""
        project = ascii_tmp / "幕键"
        project.mkdir()
        (project / "main.spec").write_text("# spec", encoding="utf-8")

        link_home = ascii_tmp / "ascii_home"
        link_home.mkdir()
        monkeypatch.setenv("SCREENKEY_BUILD_LINK", str(link_home))

        # 先建一个指向别处的 junction
        wrong = ascii_tmp / "wrong_target"
        wrong.mkdir()
        stale = link_home / helper.LINK_NAME
        if not helper._make_link(stale, wrong):
            pytest.skip("本环境无法创建 junction")

        got = helper.ensure_ascii_build_root(project, create=True)
        assert got is not None
        try:
            assert helper._link_points_at(got, project), "应已改指向真实项目"
            assert not helper._link_points_at(got, wrong)
        finally:
            if got.exists():
                os.rmdir(str(got))


# --------------------------------------------------------------------------
# 可写位置探测
# --------------------------------------------------------------------------


class TestPickLinkPath:
    def test_env_var_wins(self, helper, ascii_tmp, monkeypatch):
        monkeypatch.setenv("SCREENKEY_BUILD_LINK", str(ascii_tmp))
        got = helper._pick_link_path()
        assert got is not None
        assert got.parent == ascii_tmp.resolve()
        assert got.name == helper.LINK_NAME

    def test_non_ascii_candidate_is_skipped(self, helper, ascii_tmp, monkeypatch):
        """候选位置自己含非 ASCII 就没意义，必须跳过。"""
        bad = ascii_tmp / "中文目录"
        bad.mkdir()
        monkeypatch.setenv("SCREENKEY_BUILD_LINK", str(bad))
        got = helper._pick_link_path()
        assert got is None or helper.is_ascii_path(got), (
            "含非 ASCII 的候选位置不该被选中"
        )

    def test_empty_env_var_is_skipped(self, helper, monkeypatch):
        monkeypatch.setenv("SCREENKEY_BUILD_LINK", "")
        got = helper._pick_link_path()
        # 会回落到 %TEMP% 等候选；只要不崩且结果（若有）是 ASCII 即可
        assert got is None or helper.is_ascii_path(got)


# --------------------------------------------------------------------------
# CLI 契约
# --------------------------------------------------------------------------


class TestCliContract:
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        # **必须显式指定子进程的输出编码**：``build_helper.py`` 往 stderr 写中文
        # 提示，字节取决于子进程自己的 PYTHONIOENCODING / 控制台代码页。本机
        # Python 默认是 GBK（``sys.stderr.encoding == 'gbk'``），而这里按 UTF-8
        # 解码，于是 ``"非 ASCII" in stderr`` 会随机失败 —— 测试一旦依赖环境
        # 编码就不再是确定性的。实测：不设该变量时 stderr 是
        # ``b'[\xbc\xec\xb2\xe9] ...'``（GBK），设了之后才是 UTF-8 字节。
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, str(HELPER_PATH), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

    def test_ascii_project_check_exits_zero(self, ascii_tmp):
        project = ascii_tmp / "ascii_proj"
        project.mkdir()
        proc = self._run("--project-root", str(project), "--check")
        assert proc.returncode == 0
        assert proc.stdout.strip() == str(project.resolve())

    def test_stdout_is_exactly_one_line(self, ascii_tmp):
        """调用方按「stdout 只有一行路径」来取值，多一行就取错了。"""
        project = ascii_tmp / "ascii_proj"
        project.mkdir()
        proc = self._run("--project-root", str(project), "--check")
        assert proc.stdout.strip().count("\n") == 0

    def test_out_file_has_no_bom(self, ascii_tmp):
        """batch 用 set /p 读它；带 BOM 的话路径开头会多出三个字节。"""
        project = ascii_tmp / "ascii_proj"
        project.mkdir()
        out = ascii_tmp / "root.txt"
        proc = self._run("--project-root", str(project), "--check", "--out", str(out))
        assert proc.returncode == 0
        raw = out.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), "不应有 UTF-8 BOM"
        assert raw.decode("utf-8") == str(project.resolve())

    def test_check_mode_fails_for_non_ascii_project(self, ascii_tmp):
        """非 ASCII 且 --check → 退出码 1（调用方据此报错停下）。"""
        project = ascii_tmp / "幕键"
        project.mkdir()
        proc = self._run("--project-root", str(project), "--check")
        assert proc.returncode == 1
        assert proc.stdout.strip() == ""
        assert "非 ASCII" in proc.stderr
