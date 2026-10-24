"""命令行入口的回归测试。

重点锁住两件事：

1. **退出码必须无歧义**。`argparse` 默认用 2 表示「参数错误」，而本项目用
   `EXIT_ENV = 2` 表示「缺少依赖」，两者撞车后调用方（`启动.bat`、用户脚本）
   没法只看退出码决定"重敲命令"还是"去装依赖"。所以 `main.py` 用
   `_ArgumentParser` 接管了 `error()`，把参数错误改成 `EXIT_ARGS`(1)。
2. **`--selftest` 必须真的跑通**。它是"环境没问题"的唯一离线判据，
   退出码非 0 就说明包导入、默认映射、匹配、配置读写、Qt 插件里至少一项坏了。

这些测试都只跑子进程，不联网、不开界面。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from screenkey import __version__ as PKG_VERSION

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
MAIN_PY = SRC / "screenkey" / "main.py"
LAUNCHER = PROJECT_ROOT / "启动.bat"


@pytest.fixture(scope="module")
def run_cli():
    """跑一次 `main.py <args>`，返回 ``(returncode, stdout, stderr)``。"""

    def _run(*args: str, timeout: float = 120.0):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, str(MAIN_PY), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr

    return _run


class TestExitCodes:
    def test_main_module_exists(self):
        assert MAIN_PY.is_file(), f"入口文件不存在：{MAIN_PY}"

    def test_bad_flag_exits_with_exit_args(self, run_cli):
        """参数写错必须是 1，不能是 argparse 默认的 2。

        这是本次修复的核心：``main.py`` 的 ``_ArgumentParser.error()``
        覆盖了 argparse 的默认退出码。
        """
        code, _out, err = run_cli("--definitely-not-a-flag")
        assert code == 1, f"参数错误应退出 1，实际 {code}；stderr={err!r}"
        assert "unrecognized arguments" in err
        # argparse 的默认值是 2，若回归到这里说明覆盖失效了
        assert code != 2, "退出码 2 被 EXIT_ENV（缺少依赖）占用，参数错误不能用它"

    def test_error_exit_code_differs_from_env_exit_code(self):
        """1 与 2 必须不同，否则"看退出码区分原因"这个设计就不成立。"""
        sys.path.insert(0, str(SRC))
        try:
            from screenkey.main import EXIT_ARGS, EXIT_ENV, EXIT_OK, EXIT_SELFTEST
        finally:
            sys.path.remove(str(SRC))

        codes = [EXIT_OK, EXIT_ARGS, EXIT_ENV, EXIT_SELFTEST]
        assert len(set(codes)) == len(codes), f"退出码有重复：{codes}"
        assert EXIT_ARGS == 1
        assert EXIT_ENV == 2
        assert EXIT_ARGS != EXIT_ENV

    def test_argparse_default_is_two_hence_the_override(self):
        """记录这个覆盖的必要性：标准 argparse 的参数错误码正是 2。"""
        parser = __import__("argparse").ArgumentParser(prog="x")
        parser.add_argument("--known", action="store_true")
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--unknown"])
        assert exc.value.code == 2, "argparse 的参数错误码若不是 2，本项注释需更新"

    def test_version_flag_exits_zero(self, run_cli):
        code, out, _err = run_cli("--version")
        assert code == 0
        assert PKG_VERSION in (out + _err), (
            f"--version 应打印包版本 {PKG_VERSION}，实际输出：{out + _err!r}"
        )

    def test_help_flag_exits_zero(self, run_cli):
        """`-h` 走的是显示帮助路径，必须是 0（不能跟着 error() 变成 1）。"""
        code, out, _err = run_cli("--help")
        assert code == 0, f"--help 应退出 0，实际 {code}"
        assert "usage: screenkey" in out


class TestBootstrapWithoutPythonPath:
    """`main.py` 必须能自己找到 `screenkey` 包，不能依赖外部 `PYTHONPATH`。

    这一组是用户实测缺陷（m03604「启动.bat 无法正常启动」）的回归测试。
    原先 `main.py` 把 `Path(__file__).parent`（也就是包目录 `src\\screenkey`）
    插进 `sys.path`，而 `from screenkey import ...` 需要的是它的**父目录**
    `src`，于是直接跑入口必然 `ModuleNotFoundError`。

    这个缺陷能长期存活，恰恰是因为**所有测试都替它设好了 PYTHONPATH**
    （见本文件 `run_cli` 夹具、`tests/conftest.py`、`tests/test_ui_smoke.py`）：
    测试把被测代码的缺陷兜住了，用户双击 `启动.bat` 才暴露出来。
    所以下面每一例都刻意把 `PYTHONPATH` 从环境里删掉。
    """

    @staticmethod
    def _env_without_pythonpath() -> dict:
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)  # 关键：不许外部替 main.py 兜底
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    @pytest.mark.parametrize(
        "cwd_name",
        ["项目根", "src 目录", "任意其它目录"],
    )
    def test_version_works_from_any_cwd_without_pythonpath(self, cwd_name, tmp_path):
        """三种工作目录都必须能跑起来（`启动.bat` 的 `start /D "%SRC%"` 就是第二种）。"""
        cwds = {
            "项目根": PROJECT_ROOT,
            "src 目录": SRC,
            "任意其它目录": tmp_path,
        }
        proc = subprocess.run(
            [sys.executable, str(MAIN_PY), "--version"],
            cwd=str(cwds[cwd_name]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._env_without_pythonpath(),
            timeout=120.0,
        )
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 0, (
            f"CWD={cwd_name} 时入口无法自举（退出码 {proc.returncode}）：\n{combined}"
        )
        assert "ModuleNotFoundError" not in combined, (
            f"CWD={cwd_name} 时出现 ModuleNotFoundError —— "
            f"`main.py` 的 sys.path 引导又插错层级了：\n{combined}"
        )
        assert PKG_VERSION in combined, (
            f"CWD={cwd_name} 时入口打印的版本号不对（应含 {PKG_VERSION}）：\n{combined}"
        )

    def test_module_form_still_works(self):
        """`python -m screenkey.main` 也必须仍然可用（两种入口都要活）。"""
        proc = subprocess.run(
            [sys.executable, "-m", "screenkey.main", "--version"],
            cwd=str(SRC),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._env_without_pythonpath(),
            timeout=120.0,
        )
        assert proc.returncode == 0, f"python -m 方式失败：\n{proc.stdout}{proc.stderr}"

    def test_selftest_works_without_pythonpath(self):
        """自检是最常用的排障入口，也必须不依赖外部 PYTHONPATH。"""
        proc = subprocess.run(
            [sys.executable, str(MAIN_PY), "--selftest"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._env_without_pythonpath(),
            timeout=180.0,
        )
        assert proc.returncode == 0, (
            f"无 PYTHONPATH 时自检失败（退出码 {proc.returncode}）：\n"
            f"{proc.stdout}{proc.stderr}"
        )
        assert "[FAIL]" not in proc.stdout
        # 项数写成字面量是刻意的：新增/删掉一个自检项时必须来这里确认一次，
        # 免得"自检少跑了一项"这种退化悄无声息地通过。
        assert proc.stdout.count("[OK]") == 11

    def test_bootstrap_inserts_src_not_package_dir(self):
        """直接锁住机制本身：插进 `sys.path` 的必须是 `src`，不是 `src/screenkey`。

        前几项是行为断言，这一项是**机制断言** —— 即使将来有人把目录结构改了
        而行为恰好还能通过，只要插入的层级不对就会在这里失败。
        """
        probe = (
            "import runpy, sys;"
            f"runpy.run_path(r'{MAIN_PY}', run_name='__probe__');"
            "print('|'.join(sys.path))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._env_without_pythonpath(),
            timeout=120.0,
        )
        assert proc.returncode == 0, f"探针失败：\n{proc.stdout}{proc.stderr}"
        entries = set(proc.stdout.strip().splitlines()[-1].split("|"))
        assert str(SRC) in entries, (
            f"sys.path 里没有 src（{SRC}）—— 入口无法导入 screenkey：\n{entries}"
        )
        assert str(SRC / "screenkey") not in entries, (
            "sys.path 里出现的是包目录本身而不是它的父目录，"
            "这正是 m03604 那个 ModuleNotFoundError 的根因"
        )


class TestSelftest:
    def test_selftest_passes(self, run_cli):
        """``--selftest`` 退出 0 且输出「全部通过」。"""
        code, out, _err = run_cli("--selftest")
        assert code == 0, f"自检失败（退出码 {code}）：\n{out}"
        assert "全部通过" in out

    def test_selftest_prints_eleven_checks(self, run_cli):
        """自检的 11 项检查全部 [OK]，且没有 [FAIL]。"""
        _code, out, _err = run_cli("--selftest")
        assert "[FAIL]" not in out, f"自检有失败项：\n{out}"
        assert out.count("[OK]") == 11, f"自检项数变了（期望 11）：\n{out}"

    def test_selftest_does_not_open_gui(self, run_cli):
        """自检不能真的拉起界面。

        用离屏平台跑，只要进程能干净退出就说明没有进入 Qt 事件循环
        （`app.exec_()` 会挂住直到超时）。
        """
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        proc = subprocess.run(
            [sys.executable, str(MAIN_PY), "--selftest"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=60.0,
        )
        assert proc.returncode == 0


class TestCheckEnv:
    """``--check-env``：排查"按键发不出去"的入口，必须永远可用。

    和 ``--selftest`` 的分工：自检验**代码链路**（离线、必过），这个查**本机
    当前环境**（是否受限令牌、目标是否提权、配置目录写不写得了）。它必须
    在环境不正常时也能跑完并给出解释，否则用户拿不到任何线索。
    """

    def test_parse_args_exposes_flag(self):
        sys.path.insert(0, str(SRC))
        try:
            from screenkey.main import parse_args

            assert parse_args(["--check-env"]).check_env is True
            assert parse_args([]).check_env is False
        finally:
            if str(SRC) in sys.path:
                sys.path.remove(str(SRC))

    def test_check_env_runs_and_exits_zero(self, run_cli, tmp_path):
        # 显式指定配置目录：这个检查会真的写一次配置，别污染开发机上的
        # `~/.screenkey`，也别在项目里留下 `.screenkey`。
        code, out, _err = run_cli("--check-env", "--config-dir", str(tmp_path / "cfg"))
        assert code == 0, f"--check-env 应始终退出 0（只报告不改动）：\n{out}"
        for token in ("注入环境检查", "Win32 注入 API", "管理员权限", "实际使用目录", "写入测试"):
            assert token in out, f"缺少检查项 {token!r}：\n{out}"

    def test_check_env_reports_problem_or_ok(self, run_cli, tmp_path):
        """无论环境好坏，都必须给出明确结论，不能只说"未知"。"""
        _code, out, _err = run_cli("--check-env", "--config-dir", str(tmp_path / "cfg"))
        assert ("注入环境        : 有问题" in out) or (
            "注入环境        : 看起来正常" in out
        ), f"注入环境没有给出结论：\n{out}"

    def test_check_env_reports_loopback_and_host_markers(self, run_cli, tmp_path):
        """回环实测与宿主标记是这次故障的核心判据，必须出现在报告里。

        令牌完整性级别只能说明"可能有问题"（实测中它读得到、级别也正常，
        注入照样被丢弃），所以判据必须是**回环实测**：注入一个必然可见的
        变化，再去读它。
        """
        _code, out, _err = run_cli("--check-env", "--config-dir", str(tmp_path / "cfg"))
        assert "注入回环" in out, f"缺少回环实测项：\n{out}"
        assert "受限宿主标记" in out, f"缺少宿主标记项：\n{out}"

    def test_check_env_does_not_touch_selftest_count(self, run_cli):
        """`--check-env` 不是第 12 个自检项。

        自检的 `[OK]` 条数被 ``test_selftest_prints_eleven_checks`` 钉死；
        把环境检查塞进自检列表会让离线自检依赖运行环境，也会打破那个计数。
        """
        _code, out, _err = run_cli("--selftest")
        assert out.count("[OK]") == 11


class TestLauncherBat:
    """`启动.bat` 是用户真正的入口，必须有一条端到端测试守着它。

    这是 m03604 的第二层教训：修好 `main.py` 的引导还不够 ——
    当时没有任何测试碰过 `启动.bat`，所以「默认分支与 `--console` 分支
    都不设 `PYTHONPATH`」这件事没人发现，用户双击才暴露。
    `TestBootstrapWithoutPythonPath` 守的是 `main.py` 自己找得到包，
    这一组守的是**批处理文件真的把程序拉起来了**。

    只测 `--selftest` 分支：它不开界面、不联网、会自己退出，
    适合放进自动化测试；默认分支会 `start` 一个 GUI 进程然后立刻返回，
    留在手工复验里做（见 docs/架构.md「七之四」的实测结果）。
    """

    @staticmethod
    def _decode(raw: bytes) -> str:
        """`启动.bat` 的输出是**混合编码**：批处理自身的提示是 GBK，
        它启动的 python 因 `PYTHONIOENCODING=utf-8` 输出 UTF-8。
        所以两种解码都要试 —— 只按一种断言就变成了对宿主代码页的断言。
        """
        return raw.decode("utf-8", "replace") + raw.decode("gbk", "replace")

    def test_launcher_exists_and_is_gbk_crlf(self):
        """文件本身必须存在，且是 GBK + CRLF。

        用 UTF-8 保存时中文区域下的 cmd.exe 会显示乱码；
        用 LF 保存时批处理的 `goto`/`if` 标签可能行为异常。
        """
        assert LAUNCHER.is_file(), f"找不到启动脚本：{LAUNCHER}"
        raw = LAUNCHER.read_bytes()
        assert b"\r\n" in raw, "启动.bat 必须是 CRLF 换行"
        # 纯 CRLF：不存在孤立的 LF（前面不是 CR 的那些）
        lone_lf = raw.replace(b"\r\n", b"").count(b"\n")
        assert lone_lf == 0, f"启动.bat 里有 {lone_lf} 个孤立 LF，应为纯 CRLF"
        assert not raw.startswith(b"\xef\xbb\xbf"), "启动.bat 不能带 UTF-8 BOM"
        text = raw.decode("gbk")
        assert "幕键" in text, "GBK 解码后看不到中文提示 —— 文件可能被存成了 UTF-8"

    def test_selftest_branch_runs_and_reports_success(self):
        """`启动.bat --selftest` 必须真的跑通并报告通过。

        这一条会在 `启动.bat` 的默认分支忘记设 `PYTHONPATH` 时失败 ——
        不过 `--selftest` 分支自己设了，所以真正锁住默认分支的是
        `test_all_branches_rely_on_self_bootstrap`。
        """
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)  # 不许外部替它兜底
        proc = subprocess.run(
            ["cmd", "/c", str(LAUNCHER), "--selftest"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            env=env,
            timeout=180.0,
        )
        out = self._decode(proc.stdout + proc.stderr)
        assert proc.returncode == 0, f"启动.bat --selftest 失败：\n{out}"
        assert "全部通过" in out, f"自检没有报告通过：\n{out}"
        assert "[FAIL]" not in out, f"自检有失败项：\n{out}"

    def test_all_branches_rely_on_self_bootstrap(self):
        """机制断言：`启动.bat` 的任何分支都不许再靠外部 `PYTHONPATH`。

        历史缺陷是默认分支（`start "" /D "%SRC%" ...`）与 `--console` 分支
        都没有设 `PYTHONPATH`，而 `--selftest` 分支设了 —— 一个分支恰好
        绕过缺陷，掩盖了另外两个分支全坏。现在 `main.py` 自己会引导，
        所以批处理里**不应该**再出现 `set PYTHONPATH`：出现了就说明
        有人又在用环境变量打补丁，而默认分支依然会漏。
        """
        text = LAUNCHER.read_bytes().decode("gbk")
        code_lines = [
            ln.strip()
            for ln in text.splitlines()
            if ln.strip() and not ln.strip().upper().startswith("REM")
        ]
        offenders = [ln for ln in code_lines if "PYTHONPATH" in ln.upper()]
        assert not offenders, (
            "启动.bat 里出现了 PYTHONPATH —— 入口自举已经修好，"
            "不该再用环境变量打补丁（这会让默认分支继续依赖它）：\n"
            + "\n".join(offenders)
        )
        # 同时确认两个会跑程序的入口都在（防止有人把分支删掉）
        assert "start " in text.lower(), "默认 GUI 分支不见了"
        assert "--console" in text, "--console 分支不见了"

    def test_restricted_host_warning_never_blocks_automation(self):
        """受限宿主预警：交互式分支停下来让人看见，自动化分支**不能** pause。

        一次真实教训：预警段里一句裸 `pause` 就会让 `--selftest` 与
        `--check-env` 在管道或无人值守环境里永远等一个不会到来的按键 ——
        这两个分支恰恰是自动化用的，最不该被挡住。
        """
        text = LAUNCHER.read_bytes().decode("gbk")
        assert "DSH_SESSION_ID" in text, "受限宿主预警不见了"
        # 注意按**标签行**切分：段内先出现的是 `goto :after_warn`，
        # 按第一次出现的 ":after_warn" 切会把 pause 之前的内容切没。
        section = text.split("REM ---------- 0.")[1].split("\n:after_warn")[0]
        code = [
            ln.strip()
            for ln in section.splitlines()
            if ln.strip() and not ln.strip().upper().startswith("REM")
        ]
        pause_lines = [ln for ln in code if "pause" in ln.lower()]
        assert pause_lines, "预警段应在交互式分支停下来，让用户看得见"
        for line in pause_lines:
            assert "%MODE%" in line, (
                f"预警段的 pause 必须按模式守卫，否则会卡住 --selftest：{line}"
            )


class TestConsoleEncoding:
    """控制台编不了的字符不能决定程序生死。

    中文 Windows 的控制台默认是 GBK。``--selftest`` 会打印"当前前台窗口标题"，
    ``--check-env`` 会打印房间号与进程路径 —— 这些都是用户输入。实测被一个带
    🔍 的浏览器标签页标题打中过一次：``print`` 抛 ``UnicodeEncodeError``，
    自检于是谎报"失败 3 项"（其实 10 项全过），排查的人会被带进沟里。

    所以 ``main()`` 一开始就把 stdout/stderr 的错误策略改成 ``replace``。
    """

    def test_emoji_in_output_does_not_crash(self):
        code = (
            "import sys; sys.path.insert(0, {src!r});"
            "from screenkey.main import _forgiving_std_streams as f;"
            "f(); print('窗口标题 🔍 测试')"
        ).format(src=str(SRC))
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "gbk"  # 模拟中文控制台
        env.pop("PYTHONUTF8", None)
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            env=env,
            timeout=120.0,
        )
        assert proc.returncode == 0, (
            "stdout 编不了 emoji 时程序崩了：\n" + proc.stderr.decode("gbk", "replace")
        )
        assert b"UnicodeEncodeError" not in proc.stderr

