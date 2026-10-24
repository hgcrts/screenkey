"""注入环境诊断 + 配置目录回退的回归测试。

**对应一次真实故障**：用户报告"记事本和游戏都收不到按键"，而程序日志里每条
弹幕都显示按键已发出（`ok=True`，没有 `[发送失败]` 标记）。探针实测表明：
程序跑在受限环境（沙箱/受限令牌）里时，``SendInput`` 依旧返回成功、
``GetLastError()==0``，但 Windows **静默丢弃**了输入——目标窗口收不到，程序
也检测不到。

这带来两类必须锁住的缺陷：

1. **完全无声**：环境不对时程序什么也不说，用户只能怀疑程序坏了。现在要求
   引擎和界面都能把"注入会被系统丢弃"说出来，并且**每次运行只说一次**。
2. **配置永远存不下来**：``~/.danmaku_kb`` 建不出来时旧实现每次 ``save()``
   都报一次错，用户的设置全部丢失。现在要求自动回落到一个真能写的目录。

全部离线：不联网、不开界面（离屏）、不发真实按键。需要 PyQt5 的用例在缺失
时自动跳过。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from danmaku_kb import engine as engine_mod
from danmaku_kb.config import manager as manager_mod
from danmaku_kb.config.manager import ConfigManager, writable_config_dir
from danmaku_kb.engine import DanmakuKeyboardEngine
from danmaku_kb.input import keycodes
from danmaku_kb.input.window import WindowInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"


@pytest.fixture(scope="module")
def qt():
    """离屏 QApplication；没有 PyQt5 就跳过整组。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt5", reason="需要 PyQt5 才能测界面")

    import sys

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))

    from danmaku_kb.ui.qt_env import PYQT5_AVAILABLE, configure_qt_environment

    assert PYQT5_AVAILABLE, "PyQt5 已装但探测为不可用，qt_env 的探测逻辑有问题"
    configure_qt_environment()

    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def isolated_manager(qt, tmp_path):
    manager = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
    manager.load()
    return manager


def _engine(**kwargs) -> DanmakuKeyboardEngine:
    eng = DanmakuKeyboardEngine(**kwargs)
    eng.dry_run = False
    return eng


# ===================================================================== 判据本身
class TestIntegrityProbe:
    def test_returns_str_without_crashing(self):
        """判据函数在任何环境都必须能调——它是启动路径的一部分。"""
        assert isinstance(keycodes.injection_environment_problem(), str)
        assert isinstance(keycodes.target_blocks_injection(0), str)

    @pytest.mark.skipif(not keycodes.WINDOWS_API, reason="仅 Windows 有完整性级别")
    def test_unreadable_token_is_not_reported(self, monkeypatch):
        """读不到自己的令牌**不能**当成"受限环境"报警。

        这是踩过的坑：本模块曾漏声明 ``restype``，导致 ``GetCurrentProcess()``
        返回的伪句柄 ``(HANDLE)-1`` 被截成 32 位，``OpenProcessToken`` 在**任何**
        环境都失败 —— 于是"读不到令牌"被当成受限环境，正常桌面用户也会收到
        假的"受限环境"警告（作者本人的正常桌面运行就中过一次）。
        真伪由 :func:`keycodes.injection_loopback` 的实测回答，不是这里猜。
        """
        monkeypatch.setattr(keycodes, "current_integrity_level", lambda: None)
        assert keycodes.injection_environment_problem() == ""

    @pytest.mark.skipif(not keycodes.WINDOWS_API, reason="仅 Windows 有完整性级别")
    def test_integrity_probe_actually_reads_the_token(self):
        """回归：本进程的完整性级别必须真的读得出来。

        读不出来几乎总是声明问题（见上一条），而不是环境问题。这条断言把
        "探测本身是好的"钉住：同一个进程用两条不同路径取级别，必须一致。
        """
        own = keycodes.current_integrity_level()
        assert own is not None, "正常 Windows 上读不到自己的令牌 = 探测已坏"
        assert own >= keycodes._INTEGRITY_MEDIUM, f"普通测试进程不该低于中完整性：{own:#x}"
        assert keycodes.process_integrity_level(os.getpid()) == own

    @pytest.mark.skipif(not keycodes.WINDOWS_API, reason="仅 Windows 有完整性级别")
    def test_low_integrity_is_reported(self, monkeypatch):
        monkeypatch.setattr(keycodes, "current_integrity_level", lambda: 0x0100)
        text = keycodes.injection_environment_problem()
        assert "完整性级别偏低" in text
        assert "受限" in text

    @pytest.mark.skipif(not keycodes.WINDOWS_API, reason="仅 Windows 有完整性级别")
    def test_normal_integrity_is_silent(self, monkeypatch):
        """普通权限（中完整性）是**正常**的，不能乱报警告。"""
        monkeypatch.setattr(keycodes, "current_integrity_level", lambda: 0x2000)
        assert keycodes.injection_environment_problem() == ""

    @pytest.mark.skipif(not keycodes.WINDOWS_API, reason="仅 Windows 有完整性级别")
    def test_higher_target_is_reported(self, monkeypatch):
        monkeypatch.setattr(keycodes, "current_integrity_level", lambda: 0x1000)
        monkeypatch.setattr(keycodes, "process_integrity_level", lambda pid: 0x2000)
        text = keycodes.target_blocks_injection(4321)
        assert "以管理员身份运行" in text

    @pytest.mark.skipif(not keycodes.WINDOWS_API, reason="仅 Windows 有完整性级别")
    def test_same_integrity_target_is_silent(self, monkeypatch):
        monkeypatch.setattr(keycodes, "current_integrity_level", lambda: 0x1000)
        monkeypatch.setattr(keycodes, "process_integrity_level", lambda pid: 0x1000)
        assert keycodes.target_blocks_injection(4321) == ""

    def test_missing_pid_is_silent(self):
        """拿不到 pid 时不能瞎报——宁可不说，也不要把正常环境说成有问题。"""
        assert keycodes.target_blocks_injection(0) == ""


# ===================================================================== 引擎警告
class TestEngineEnvironmentWarning:
    def test_emits_warning_event(self, monkeypatch):
        monkeypatch.setattr(
            engine_mod, "injection_environment_problem", lambda: "本进程跑在受限环境里"
        )
        eng = _engine()
        seen: list = []
        eng.on_event = seen.append
        eng._warn_injection_environment()

        assert [e.kind for e in seen] == ["warning"], "环境受限必须发 warning 事件"
        assert "受限环境" in seen[0].text

    def test_warning_is_not_counted_as_error(self, monkeypatch):
        """警告不能进 errors：否则统计里的"错误"会被环境问题污染。"""
        monkeypatch.setattr(
            engine_mod, "injection_environment_problem", lambda: "受限环境"
        )
        eng = _engine()
        eng.on_event = lambda event: None
        eng._warn_injection_environment()
        assert eng.stats_snapshot()["errors"] == 0

    def test_warns_only_once_per_run(self, monkeypatch):
        """每次连接都刷一遍同样的警告只会把日志淹掉。"""
        monkeypatch.setattr(
            engine_mod, "injection_environment_problem", lambda: "受限环境"
        )
        eng = _engine()
        seen: list = []
        eng.on_event = seen.append
        eng._warn_injection_environment()
        eng._warn_injection_environment()
        assert len(seen) == 1

    def test_silent_when_environment_is_fine(self, monkeypatch):
        monkeypatch.setattr(engine_mod, "injection_environment_problem", lambda: "")
        monkeypatch.setattr(engine_mod, "restricted_host_markers", lambda: "")
        eng = _engine()
        seen: list = []
        eng.on_event = seen.append
        eng._warn_injection_environment()
        assert seen == []

    def test_host_markers_are_reported(self, monkeypatch):
        """令牌看起来正常、但跑在已知受限宿主里时，靠宿主标记识别。

        这类宿主（沙箱/自动化宿主）里完整性级别往往就是普通用户，只有环境
        变量能认出来 —— 而注入照样会被静默丢弃。
        """
        monkeypatch.setattr(engine_mod, "injection_environment_problem", lambda: "")
        monkeypatch.setattr(
            engine_mod, "restricted_host_markers", lambda: "DSH_SESSION_ID（DSH 会话宿主）"
        )
        eng = _engine()
        seen: list = []
        eng.on_event = seen.append
        eng._warn_injection_environment()
        assert [e.kind for e in seen] == ["warning"]
        assert "DSH_SESSION_ID" in seen[0].text
        assert "启动.bat" in seen[0].text

    def test_environment_problem_wins_over_host_markers(self, monkeypatch):
        """两条判据都命中时只报一条，别把同一件事说两遍。"""
        monkeypatch.setattr(engine_mod, "injection_environment_problem", lambda: "级别偏低")
        monkeypatch.setattr(engine_mod, "restricted_host_markers", lambda: "DSH_SHELL（x）")
        eng = _engine()
        seen: list = []
        eng.on_event = seen.append
        eng._warn_injection_environment()
        assert len(seen) == 1
        assert "级别偏低" in seen[0].text

    def test_dry_run_never_warns(self, monkeypatch):
        """dry_run 是自检/空跑，根本不注入，不该报环境问题。"""
        monkeypatch.setattr(
            engine_mod, "injection_environment_problem", lambda: "受限环境"
        )
        eng = DanmakuKeyboardEngine()
        eng.dry_run = True
        seen: list = []
        eng.on_event = seen.append
        eng._warn_injection_environment()
        assert seen == []

    def test_probe_exception_does_not_break_startup(self, monkeypatch):
        """自检本身抛异常不能影响启动——它只是"多说一句"，不是前置条件。"""

        def boom():
            raise OSError("探测失败")

        monkeypatch.setattr(engine_mod, "injection_environment_problem", boom)
        monkeypatch.setattr(engine_mod, "restricted_host_markers", lambda: "")
        eng = _engine()
        seen: list = []
        eng.on_event = seen.append
        eng._warn_injection_environment()  # 不该抛
        assert seen == []

    def test_host_marker_probe_exception_is_swallowed(self, monkeypatch):
        """第二条判据（宿主标记）抛异常时同样只降级，不影响启动。"""
        monkeypatch.setattr(engine_mod, "injection_environment_problem", lambda: "")

        def boom():
            raise OSError("读环境变量失败")

        monkeypatch.setattr(engine_mod, "restricted_host_markers", boom)
        eng = _engine()
        seen: list = []
        eng.on_event = seen.append
        eng._warn_injection_environment()
        assert seen == []


class TestEngineForegroundIntegrityCheck:
    @staticmethod
    def _fg(pid: int = 4321) -> WindowInfo:
        return WindowInfo(hwnd=1, title="某游戏", class_name="Game", process_name="game.exe", pid=pid)

    def test_warns_when_foreground_is_elevated(self, monkeypatch):
        monkeypatch.setattr(engine_mod, "get_foreground_window", lambda: self._fg())
        monkeypatch.setattr(
            engine_mod, "target_blocks_injection", lambda pid: "前台窗口以更高权限运行"
        )
        eng = _engine()
        seen: list = []
        eng.on_event = seen.append
        eng._check_foreground_integrity()
        assert [e.kind for e in seen] == ["warning"]
        assert "更高权限" in seen[0].text

    def test_same_pid_is_checked_only_once(self, monkeypatch):
        """切换窗口才重新检查；同一窗口不重复报。"""
        calls: list = []
        monkeypatch.setattr(engine_mod, "get_foreground_window", lambda: self._fg(77))
        monkeypatch.setattr(
            engine_mod,
            "target_blocks_injection",
            lambda pid: calls.append(pid) or "权限不足",
        )
        eng = _engine()
        eng.on_event = lambda event: None
        eng._check_foreground_integrity()
        eng._check_foreground_integrity()
        assert calls == [77]

    def test_no_foreground_window_is_silent(self, monkeypatch):
        monkeypatch.setattr(engine_mod, "get_foreground_window", lambda: None)
        monkeypatch.setattr(
            engine_mod,
            "target_blocks_injection",
            lambda pid: pytest.fail("拿不到前台窗口时不该去查权限"),
        )
        eng = _engine()
        seen: list = []
        eng.on_event = seen.append
        eng._check_foreground_integrity()
        assert seen == []

    def test_silent_when_target_allows_input(self, monkeypatch):
        monkeypatch.setattr(engine_mod, "get_foreground_window", lambda: self._fg())
        monkeypatch.setattr(engine_mod, "target_blocks_injection", lambda pid: "")
        eng = _engine()
        seen: list = []
        eng.on_event = seen.append
        eng._check_foreground_integrity()
        assert seen == []

    def test_dry_run_skips_check(self, monkeypatch):
        monkeypatch.setattr(
            engine_mod,
            "get_foreground_window",
            lambda: pytest.fail("dry_run 下不该去查前台窗口权限"),
        )
        eng = DanmakuKeyboardEngine()
        eng.dry_run = True
        eng.on_event = lambda event: None
        eng._check_foreground_integrity()

    def test_probe_failure_is_swallowed(self, monkeypatch):
        def boom():
            raise OSError("取前台窗口失败")

        monkeypatch.setattr(engine_mod, "get_foreground_window", boom)
        eng = _engine()
        eng.on_event = lambda event: None
        eng._check_foreground_integrity()  # 不该抛
        assert eng._checked_fg_pid == 0


# ===================================================================== 配置目录
class TestConfigDirFallback:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        """受测环境里 `~` 本来就不可写，测试必须显式控制这条分支。"""
        monkeypatch.delenv("DANMAKU_KB_CONFIG_DIR", raising=False)
        monkeypatch.setattr(manager_mod, "_fallback_warned", set())

    def test_uses_preferred_when_writable(self, monkeypatch, tmp_path):
        preferred = tmp_path / "preferred"
        monkeypatch.setattr(manager_mod, "default_config_dir", lambda: preferred)
        monkeypatch.setattr(manager_mod, "_probe_writable", lambda path: True)
        assert writable_config_dir() == preferred

    def test_falls_back_to_portable_when_preferred_unwritable(self, monkeypatch, tmp_path):
        preferred = tmp_path / "preferred"
        portable = tmp_path / "portable"
        monkeypatch.setattr(manager_mod, "default_config_dir", lambda: preferred)
        monkeypatch.setattr(manager_mod, "portable_config_dir", lambda: portable)
        monkeypatch.setattr(
            manager_mod, "_probe_writable", lambda path: Path(path) == portable
        )
        assert writable_config_dir() == portable, "默认目录写不进去必须换一个能写的"

    def test_falls_back_to_temp_when_portable_unwritable(self, monkeypatch, tmp_path):
        preferred = tmp_path / "preferred"
        portable = tmp_path / "portable"
        temp_dir = tmp_path / "temp"
        monkeypatch.setattr(manager_mod, "default_config_dir", lambda: preferred)
        monkeypatch.setattr(manager_mod, "portable_config_dir", lambda: portable)
        monkeypatch.setattr(manager_mod.tempfile, "gettempdir", lambda: str(temp_dir))
        monkeypatch.setattr(
            manager_mod, "_probe_writable", lambda path: Path(path) == temp_dir / manager_mod.DEFAULT_DIR_NAME
        )
        assert writable_config_dir() == temp_dir / manager_mod.DEFAULT_DIR_NAME

    def test_explicit_override_is_never_probed(self, monkeypatch, tmp_path):
        """用户显式指定的目录照用——写不进去要报错，而不是偷偷换地方。"""
        override = tmp_path / "explicit"
        monkeypatch.setenv("DANMAKU_KB_CONFIG_DIR", str(override))
        monkeypatch.setattr(
            manager_mod, "_probe_writable", lambda path: pytest.fail("显式指定不该探测")
        )
        assert writable_config_dir() == override

    def test_manager_uses_the_writable_dir(self, monkeypatch, tmp_path):
        preferred = tmp_path / "nope"
        portable = tmp_path / "portable"
        monkeypatch.setattr(manager_mod, "default_config_dir", lambda: preferred)
        monkeypatch.setattr(manager_mod, "portable_config_dir", lambda: portable)
        monkeypatch.setattr(
            manager_mod, "_probe_writable", lambda path: Path(path) == portable
        )
        assert ConfigManager(auto_save=False).config_dir == portable

    def test_fallback_warning_logged_once(self, monkeypatch, tmp_path, caplog):
        """同一目标只提醒一次，否则每次 save/构造都刷一条。"""
        preferred = tmp_path / "preferred"
        portable = tmp_path / "portable"
        monkeypatch.setattr(manager_mod, "default_config_dir", lambda: preferred)
        monkeypatch.setattr(manager_mod, "portable_config_dir", lambda: portable)
        monkeypatch.setattr(
            manager_mod, "_probe_writable", lambda path: Path(path) == portable
        )
        with caplog.at_level("WARNING"):
            writable_config_dir()
            writable_config_dir()
            writable_config_dir()
        assert caplog.text.count("已改用") == 1


# ===================================================================== 界面接线
class TestWarningSurfaces:
    def test_bridge_forwards_warning_signal(self, qt):
        from danmaku_kb.ui.worker import EngineBridge

        eng = _engine()
        bridge = EngineBridge(eng)
        seen: list = []
        bridge.warningOccurred.connect(seen.append)
        eng._emit_warning("注入会被系统丢弃")
        assert seen == ["注入会被系统丢弃"]

    def test_warning_is_not_routed_to_error_signal(self, qt):
        from danmaku_kb.ui.worker import EngineBridge

        eng = _engine()
        bridge = EngineBridge(eng)
        errors: list = []
        bridge.errorOccurred.connect(errors.append)
        eng._emit_warning("注入会被系统丢弃")
        assert errors == [], "警告走警告信号，不能混进错误信号"

    def test_main_window_shows_warning_in_log_and_status(self, qt, isolated_manager):
        from danmaku_kb.ui.main_window import MainWindow

        win = MainWindow(config=isolated_manager.config, config_manager=isolated_manager)
        try:
            win._on_warning("按键会被系统静默丢弃")
            assert "[警告] 按键会被系统静默丢弃" in win.log_view.toPlainText()
            assert "按键会被系统静默丢弃" in win.status_label.text()
        finally:
            win.close()

    def test_main_window_checks_environment_at_startup(self, qt, isolated_manager, monkeypatch):
        from danmaku_kb.ui import main_window as mw_mod

        monkeypatch.setattr(mw_mod, "injection_environment_problem", lambda: "沙箱环境受限")
        win = mw_mod.MainWindow(
            config=isolated_manager.config, config_manager=isolated_manager
        )
        try:
            assert "[警告] 沙箱环境受限" in win.log_view.toPlainText(), (
                "启动时就要把环境问题说出来，而不是等用户去猜"
            )
        finally:
            win.close()

    def test_main_window_silent_when_environment_ok(self, qt, isolated_manager, monkeypatch):
        from danmaku_kb.ui import main_window as mw_mod

        monkeypatch.setattr(mw_mod, "injection_environment_problem", lambda: "")
        win = mw_mod.MainWindow(
            config=isolated_manager.config, config_manager=isolated_manager
        )
        try:
            assert "[警告]" not in win.log_view.toPlainText()
        finally:
            win.close()


class TestInjectionLoopbackInterpretation:
    """回环判据的三种结论 —— 纯函数，所以能离线逐条验证。

    `injection_loopback()` 会真的把光标移动 1 像素，只在 `--check-env` 里调用；
    把判断抽成 `interpret_injection_loopback()` 就是为了让这段逻辑可测。
    """

    def test_sendinput_failure_is_reported_as_failure(self):
        ok, text = keycodes.interpret_injection_loopback(False, (10, 20), (10, 20), 5)
        assert ok is False
        assert "直接失败" in text and "5" in text
        assert "白名单" in text, "直接失败该建议安全软件白名单，而不是沙箱"

    def test_moved_cursor_means_injection_works(self):
        ok, text = keycodes.interpret_injection_loopback(True, (100, 200), (101, 201), 0)
        assert ok is True
        assert "注入有效" in text

    def test_unmoved_cursor_means_silently_dropped(self):
        """这就是用户那次故障的实测形态：SendInput 报告成功、光标纹丝不动。"""
        ok, text = keycodes.interpret_injection_loopback(
            True, (1275, 1322), (1275, 1322), 0
        )
        assert ok is False
        assert "静默丢弃" in text
        assert "启动.bat" in text, "要给出可执行的下一步，不能只说「失败了」"

    def test_error_code_survives_into_the_message(self):
        _ok, text = keycodes.interpret_injection_loopback(True, (1, 1), (1, 1), 87)
        assert "87" in text

    def test_accepts_lists_as_well_as_tuples(self):
        ok, _text = keycodes.interpret_injection_loopback(True, [3, 4], [3, 5], 0)
        assert ok is True


class TestRestrictedHostMarkers:
    """宿主标记是**提示性**判据：命中只说明"可能是受限宿主"，不等于注入失败。"""

    @staticmethod
    def _clear(monkeypatch):
        for name, _label in keycodes._RESTRICTED_HOST_MARKERS:
            monkeypatch.delenv(name, raising=False)

    def test_no_marker_returns_empty(self, monkeypatch):
        self._clear(monkeypatch)
        assert keycodes.restricted_host_markers() == ""

    def test_dsh_session_id_is_reported_with_its_label(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("DSH_SESSION_ID", "session-abc")
        text = keycodes.restricted_host_markers()
        assert "DSH_SESSION_ID" in text
        assert "DSH 会话宿主" in text

    def test_multiple_markers_are_joined(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("DSH_SHELL", "1")
        monkeypatch.setenv("SANDBOXIE", "1")
        text = keycodes.restricted_host_markers()
        assert "DSH_SHELL" in text and "Sandboxie" in text
        assert "、" in text

    def test_environ_failure_is_swallowed(self, monkeypatch):
        """探测自己不能抛：它只是"多说一句"，不该拦住任何东西。"""

        def boom(name, default=None):
            raise OSError("环境读不到")

        monkeypatch.setattr(keycodes.os.environ, "get", boom)
        assert keycodes.restricted_host_markers() == ""
