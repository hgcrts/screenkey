"""界面层缺陷回归测试（第二轮 UI 审计的修复）。

这里每一条都对应一个**曾经真实存在**的缺陷，不是"为了覆盖率"补的：

* 弹幕窗口的「只看触发按键的」/「自动滚动」两个复选框以前不写回配置，
  改一次字号就把勾选状态静默回滚；
* 暂停期间待渲染队列无上限（存的是已渲染 HTML），内存单向增长；
* 主窗口统计直接读无锁的 ``engine.stats``，抓取线程正在锁内改它；
* ``EngineBridge.statsUpdated`` 全项目 0 处 connect（死信号）；
* 「发送测试序列」在 GUI 线程同步跑完 12 条弹幕，期间窗口无响应；
* 登录对话框重复点「生成二维码」会让两条轮询线程同时写状态，
  1 秒倒计时会把"登录成功！"顶掉。

全部离屏（``QT_QPA_PLATFORM=offscreen``）、不联网、不注册热键、不发真实按键。
无法导入 PyQt5 时整组跳过。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

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
    from danmaku_kb.config.manager import ConfigManager

    manager = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
    manager.load()
    return manager


@pytest.fixture
def window(qt, isolated_manager):
    from danmaku_kb.ui.main_window import MainWindow

    win = MainWindow(config=isolated_manager.config, config_manager=isolated_manager)
    yield win
    win.close()


# ===================================================================== 弹幕窗口
class TestDanmakuWindowDisplayOptions:
    def test_toggle_writes_back_to_config_and_emits(self, qt):
        from danmaku_kb.config.models import DisplayConfig
        from danmaku_kb.ui.danmaku_window import DanmakuWindow

        win = DanmakuWindow(DisplayConfig(only_triggered=False, auto_scroll=True))
        try:
            got: list = []
            win.configChanged.connect(got.append)
            win.only_triggered.setChecked(True)
            assert got, "勾选「只看触发按键的」必须通知外部，否则重启就丢了"
            assert win.config.only_triggered is True
            got.clear()
            win.auto_scroll.setChecked(False)
            assert got, "取消「自动滚动」同样必须通知外部"
            assert win.config.auto_scroll is False
        finally:
            win.close()

    def test_apply_config_does_not_emit(self, qt):
        """``apply_config`` 更新复选框时必须屏蔽信号。

        否则用户改字号 → ``apply_config`` → ``setChecked`` → ``toggled`` →
        ``configChanged``，等于自己触发一次"用户改了选项"。
        """
        from danmaku_kb.config.models import DisplayConfig
        from danmaku_kb.ui.danmaku_window import DanmakuWindow

        win = DanmakuWindow(DisplayConfig(only_triggered=True, auto_scroll=True))
        try:
            got: list = []
            win.configChanged.connect(got.append)
            win.apply_config(DisplayConfig(only_triggered=False, auto_scroll=False))
            assert got == [], "apply_config 不该被当成用户操作再抛一次 configChanged"
            assert win.only_triggered.isChecked() is False
            assert win.auto_scroll.isChecked() is False
        finally:
            win.close()

    def test_pending_queue_is_bounded(self, qt):
        """暂停期间队列必须有上限，否则一小时不下就是单向内存增长。"""
        from danmaku_kb.config.models import DisplayConfig
        from danmaku_kb.ui.danmaku_window import DanmakuWindow
        from danmaku_kb.danmaku.message import make_system_message

        win = DanmakuWindow(DisplayConfig())
        try:
            win._paused = True  # 模拟点「暂停」
            limit = DanmakuWindow.PENDING_LIMIT
            assert limit > 0
            msg = make_system_message("测试")
            for _ in range(limit + 200):
                win.append(msg)
            assert len(win._pending) == limit, "待渲染队列必须在 PENDING_LIMIT 处封顶"
        finally:
            win.close()


class TestMainWindowDisplaySync:
    def test_display_config_change_is_persisted(self, window):
        """弹幕窗口改选项 → 主配置跟着改（并且会触发防抖保存）。"""
        window.danmaku_window = None
        window._show_danmaku_window()
        win = window.danmaku_window
        assert win is not None
        try:
            win.only_triggered.setChecked(True)
            win.auto_scroll.setChecked(False)
            assert window.config.display.only_triggered is True
            assert window.config.display.auto_scroll is False
        finally:
            window.danmaku_window = None


    def test_closing_danmaku_window_drops_reference(self, window):
        """手动关掉弹幕窗口后主窗口必须丢掉引用。

        否则那个隐藏窗口会继续被 ``append()``：``_flush`` 把每条弹幕
        ``insertHtml`` 进一个看不见的文档，纯烧 CPU。
        """
        window.danmaku_window = None
        window._show_danmaku_window()
        win = window.danmaku_window
        assert win is not None
        win.close()
        assert window.danmaku_window is None
        # 再点一次应当重新建一个可用的窗口
        window._show_danmaku_window()
        assert window.danmaku_window is not None
        assert window.danmaku_window is not win
        window.danmaku_window = None


# ===================================================================== 统计链路
class TestStatsPipeline:
    def test_bridge_refresh_stats_emits_dict_snapshot(self, qt):
        from danmaku_kb.engine import DanmakuKeyboardEngine
        from danmaku_kb.ui.worker import EngineBridge

        engine = DanmakuKeyboardEngine()
        engine.dry_run = True
        bridge = EngineBridge(engine)
        got: list = []
        bridge.statsUpdated.connect(got.append)
        bridge.refresh_stats()
        assert len(got) == 1
        assert isinstance(got[0], dict), "必须发加锁的 stats_snapshot() 字典，不是活的 stats 对象"
        assert "total_messages" in got[0] and "elapsed" in got[0]

    def test_stats_label_is_filled_from_snapshot(self, window):
        """``statsUpdated`` 以前是死信号；现在主窗口真的消费它。"""
        snapshot = {
            "elapsed": 60.0,
            "total_messages": 120,
            "displayed_messages": 100,
            "dropped_messages": 20,
            "total_actions": 30,
            "key_actions": 20,
            "mouse_actions": 10,
            "suppressed_actions": 3,
            "errors": 1,
        }
        window._on_stats_updated(snapshot)
        assert "弹幕 120" in window.stats_label.text()
        text = window.stat_view.text()
        assert "120（120.0/分）" in text
        assert "鼠标 10" in text and "错误 1" in text

    def test_stats_refresh_tolerates_garbage(self, window):
        window._on_stats_updated(None)
        window._on_stats_updated("不是字典")
        window._on_stats_updated({})

    def test_refresh_stats_goes_through_bridge(self, window):
        """界面定时器必须走 EngineBridge.refresh_stats，而不是直接读 engine.stats。"""
        import inspect

        lines = [
            line.split("#", 1)[0]
            for line in inspect.getsource(window._refresh_stats).splitlines()
        ]
        code = "\n".join(lines)
        assert "refresh_stats" in code
        assert "self.engine.stats" not in code, "无锁直读 stats 会拿到撕裂的计数器"


# ===================================================================== 模拟序列
class TestSimulationSequence:
    def test_sequence_does_not_block_the_gui_thread(self, window):
        """「发送测试序列」必须异步逐条投递。

        以前 ``fetcher.emit_sequence(seq)`` 在 GUI 线程里同步跑完 12 条：每条
        都要走筛选→匹配→按键，而按键执行里有 ``time.sleep``，期间窗口完全
        无响应（Windows 上直接显示"未响应"）。
        """
        window.sim_check.setChecked(True)
        window._on_connect()
        try:
            window._sim_sequence()
            # 立刻返回：只投递了第 1 条，其余 11 条留在队列里等事件循环。
            assert window._sim_sent == 1, "第一条之后必须让出事件循环"
            assert len(window._sim_queue) == 11
            assert window._sim_queue[0] == "w"
        finally:
            window._on_stop()

    def test_pump_finishes_and_logs(self, window):
        window.sim_check.setChecked(True)
        window._on_connect()
        try:
            window._sim_queue = ["666666"]
            window._sim_sent = 0
            window._pump_sim_queue()
            assert window._sim_queue == []
            assert window._sim_sent == 1
            # 再抽一次应当走进"结束"分支（队列空），只写日志、不抛异常。
            window._pump_sim_queue()
        finally:
            window._on_stop()


# ===================================================================== 登录对话框
class TestLoginDialogFixes:
    @pytest.fixture
    def dialog(self, qt):
        from danmaku_kb.ui.login_dialog import LoginDialog

        dlg = LoginDialog(platform="bilibili")
        yield dlg
        dlg.close()

    def test_stale_generation_results_are_dropped(self, dialog):
        """旧轮询线程的结果不能覆盖新二维码的状态。"""
        from danmaku_kb.login.bilibili_qr import LoginResult, QRState

        got: list = []
        dialog.biliUpdate.connect(got.append)
        dialog._bili_generation = 5
        stale = LoginResult(state=QRState.EXPIRED, message="旧二维码过期")
        dialog._on_bili_update(stale, 4)
        assert got == [], "上一代的轮询结果必须丢弃"
        fresh = LoginResult(state=QRState.WAITING, message="等待扫码")
        dialog._on_bili_update(fresh, 5)
        assert got == [fresh]

    def test_countdown_does_not_overwrite_final_state(self, dialog):
        """终态锁定后，1 秒倒计时不能再改状态文本。"""
        dialog.bili_status.setText("登录成功！已获取 SESSDATA（32 字符）")
        dialog._status_locked = True
        dialog._update_countdown()
        assert dialog.bili_status.text() == "登录成功！已获取 SESSDATA（32 字符）"

    def test_success_locks_status_and_stops_polling(self, dialog):
        from danmaku_kb.login.bilibili_qr import LoginResult, QRState

        result = LoginResult(state=QRState.SUCCESS, sessdata="abc123", message="登录成功")
        dialog._apply_bili_status(result)
        assert dialog._status_locked is True
        assert dialog.sessdata_edit.text() == "abc123"
        assert dialog.qr_btn.isEnabled(), "结束后必须能重新生成二维码"
        assert dialog._login is None

    def test_late_waiting_result_cannot_override_success(self, dialog):
        from danmaku_kb.login.bilibili_qr import LoginResult, QRState

        dialog._apply_bili_status(
            LoginResult(state=QRState.SUCCESS, sessdata="abc123", message="登录成功")
        )
        text = dialog.bili_status.text()
        dialog._apply_bili_status(LoginResult(state=QRState.WAITING, message="等待扫码"))
        assert dialog.bili_status.text() == text, "迟到的中间态不能顶掉终态文案"

    def test_restarting_login_bumps_generation(self, dialog, monkeypatch):
        """重复点「生成二维码」必须先停旧轮询并换一代。

        以前直接覆盖 ``self._login``：旧线程还活着，两条轮询同时往同一个对话框
        写状态，二维码倒计时也会被旧会话的时间覆盖。
        """
        stopped: list = []

        class FakeSession:
            url = "https://example.invalid/qr"

        class FakeLogin:
            session = None

            def __init__(self, tag: int) -> None:
                self.tag = tag

            def generate(self):
                return FakeSession()

            def start_polling(self, on_update=None, interval=2.0, max_seconds=180.0):
                return True

            def stop_polling(self, timeout=None):
                stopped.append(self.tag)

        counter = {"n": 0}

        def make_login(timeout=None):
            counter["n"] += 1
            return FakeLogin(counter["n"])

        import danmaku_kb.ui.login_dialog as mod

        monkeypatch.setattr(mod, "BilibiliQRLogin", make_login)
        monkeypatch.setattr(mod, "make_qr_png", lambda *a, **k: b"")
        dialog._login = None
        before = dialog._bili_generation
        dialog._start_bili_login()
        assert dialog._bili_generation == before + 1
        first = dialog._login
        assert first is not None

        dialog._start_bili_login()
        assert dialog._bili_generation == before + 2
        assert dialog._login is not first
        assert stopped == [1], "重新生成二维码前必须先停掉上一次的轮询"

    def test_stop_polling_failure_does_not_escape(self, dialog):
        """``_stop_bili_login`` 在 closeEvent 里被调用，异常必须自己吞掉。

        PyQt5 遇到 Qt 事件处理里的未捕获异常会直接 ``abort()``
        （退出码 0xC0000409），连 Python traceback 都看不到。
        """

        class BrokenLogin:
            def stop_polling(self, timeout=None):
                raise RuntimeError("模拟轮询线程状态异常")

        dialog._login = BrokenLogin()
        dialog._stop_bili_login()
        assert dialog._login is None


# ===================================================================== 端到端
class TestEndToEndSimulatedRun:
    """真的连一次模拟弹幕，跑一段事件循环，再干净退出。

    这一层补的是"各部件单独测都绿、拼在一起却不工作"的盲区：抓取线程 →
    引擎回调 → EngineBridge 信号 → GUI 槽 → 弹幕窗口 + 统计，全链路都过一遍。
    """

    def test_simulated_session_processes_danmaku_and_closes_cleanly(self, qt, isolated_manager):
        from PyQt5.QtWidgets import QApplication

        from danmaku_kb.ui.main_window import MainWindow

        window = MainWindow(config=isolated_manager.config, config_manager=isolated_manager)
        # 关键：绝不在跑测试的机器上真的发按键
        window.engine.dry_run = True
        try:
            window.global_cd.setValue(0)
            window.user_cd.setValue(0)
            window.dedupe.setValue(0)
            window.sim_check.setChecked(True)
            window.sim_interval.setValue(0.1)
            window._on_connect()
            assert window.engine.running is True, "模拟器应当已经启动"
            assert window.danmaku_window is not None, "连接后应当自动弹出弹幕窗口"

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                QApplication.processEvents()
                time.sleep(0.01)

            snapshot = window.engine.stats_snapshot()
            assert snapshot["total_messages"] > 0, "2 秒内必须收到模拟弹幕"
            assert snapshot["total_actions"] > 0, "「666666」等默认映射应当触发动作（dry_run）"
            assert window.stats_label.text().startswith("弹幕 "), "统计标签应当被刷新"

            window._on_stop()
            assert window.engine.running is False
        finally:
            window.close()

    def test_stop_then_reconnect_succeeds(self, qt, isolated_manager):
        """点「停止」后立刻点「连接」不能失败。

        ``BaseFetcher.stop()`` 只置停止标志（非阻塞），``_running`` 要等抓取
        线程退出才复位；这段时间里 ``start()`` 会命中 ``if self._running: return
        False`` —— 用户看到的是"点了连接没反应，得再点一次"。
        """
        from PyQt5.QtWidgets import QApplication

        from danmaku_kb.ui.main_window import MainWindow

        window = MainWindow(config=isolated_manager.config, config_manager=isolated_manager)
        window.engine.dry_run = True
        try:
            window.sim_check.setChecked(True)
            window.sim_interval.setValue(0.1)
            window._on_connect()
            assert window.engine.running is True
            window._on_stop()
            window._on_connect()
            assert window.engine.running is True, "停止后必须能立刻重新连接"
            window._on_stop()
            for _ in range(20):
                QApplication.processEvents()
                time.sleep(0.01)
        finally:
            window.close()


# ===================================================================== Qt 环境
class TestQtEnvRetry:
    def test_missing_plugin_root_does_not_lock_state(self, qt, monkeypatch):
        """定位不到插件目录时不能置 ``_configured``，否则永远没有重试机会。"""
        import danmaku_kb.ui.qt_env as qt_env

        monkeypatch.setattr(qt_env, "_configured", False)
        monkeypatch.setattr(qt_env, "qt_plugin_path", None)
        monkeypatch.setattr(qt_env, "PYQT5_AVAILABLE", False)
        assert qt_env.configure_qt_environment() is None
        assert qt_env._configured is False, "PyQt5 暂时不可用时不该锁死状态"

        monkeypatch.setattr(qt_env, "PYQT5_AVAILABLE", True)
        monkeypatch.setattr(qt_env, "_find_plugin_root", lambda: None)
        assert qt_env.configure_qt_environment() is None
        assert qt_env._configured is False

        monkeypatch.setattr(qt_env, "_find_plugin_root", lambda: Path("C:/fake/plugins"))
        got = qt_env.configure_qt_environment()
        assert got == Path("C:/fake/plugins")
        assert qt_env._configured is True
        # 成功后再调用直接返回缓存值，不再重复探测
        monkeypatch.setattr(qt_env, "_find_plugin_root", lambda: None)
        assert qt_env.configure_qt_environment() == Path("C:/fake/plugins")
        monkeypatch.setattr(qt_env, "_configured", False)
        monkeypatch.setattr(qt_env, "qt_plugin_path", None)
