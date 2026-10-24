"""界面层冒烟测试：**真的**把主窗口建起来、跑一小段事件循环、再干净退出。

为什么需要这一层：其余测试全部用 ``dry_run`` 引擎或纯数据类，**没有任何一条会
构造 `MainWindow`**。而 `MainWindow.__init__` 的签名是
``(config: Optional[AppConfig], config_manager: Optional[ConfigManager])``——
两个参数类型相近、很容易传错；一旦传错，`engine.py` 会去读
``config.input.game_mode`` 并抛 ``AttributeError: 'ConfigManager' object has no
attribute 'input'``，而所有离线测试依然是绿的。这里就是补这个盲区。

全部离屏（``QT_QPA_PLATFORM=offscreen``）、不联网、不注册热键、不发真实按键。
无法导入 PyQt5 时整组跳过。
"""

from __future__ import annotations

import os
import re
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
    """指向临时目录的 ConfigManager，避免污染用户真实配置。"""
    from danmaku_kb.config.manager import ConfigManager

    manager = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
    manager.load()
    return manager


class TestMainWindowConstruction:
    def test_main_window_builds_with_both_arguments(self, qt, isolated_manager):
        """按 ``main.py`` 的真实调用方式构造主窗口。

        这条曾经真的失败过：把 ``ConfigManager`` 当成 `config` 传进去，
        ``DanmakuKeyboardEngine.__init__`` 会在 ``self.config.input.game_mode``
        上抛 ``AttributeError``。所以这里连参数**顺序**也一并锁住。
        """
        from danmaku_kb.ui.main_window import MainWindow

        window = MainWindow(config=isolated_manager.config, config_manager=isolated_manager)
        try:
            assert window.engine is not None
            # 引擎拿到的必须是 AppConfig，而不是 ConfigManager
            assert window.engine.config is isolated_manager.config
            assert hasattr(window.engine.config, "input")
            assert window.manager is isolated_manager
            # 映射表应当从 manager 带过来
            assert len(window.engine.key_mappings) == len(isolated_manager.key_mappings)
            assert len(window.engine.mouse_mappings) == len(isolated_manager.mouse_mappings)
        finally:
            window.close()

    def test_main_window_works_with_no_arguments(self, qt, tmp_path, monkeypatch):
        """不传参时也要能建起来（会自己 new 一个 ConfigManager）。

        用 ``DANMAKU_KB_CONFIG_DIR`` 把配置目录引到 tmp，避免写用户目录
        （受限环境里写 ``~/.danmaku_kb`` 会 ``[WinError 5] 拒绝访问``）。
        """
        monkeypatch.setenv("DANMAKU_KB_CONFIG_DIR", str(tmp_path / "auto"))

        from danmaku_kb.ui.main_window import MainWindow

        window = MainWindow()
        try:
            assert window.engine is not None
            assert hasattr(window.engine.config, "input")
        finally:
            window.close()

    def test_engine_is_dry_run_safe_by_default(self, qt, isolated_manager):
        """界面构造时绝不能让引擎立刻真的发按键。"""
        from danmaku_kb.ui.main_window import MainWindow

        window = MainWindow(config=isolated_manager.config, config_manager=isolated_manager)
        try:
            # 只有真正发起连接/收到弹幕才可能按键；刚建好时引擎不应在运行
            assert window.engine.running is False
        finally:
            window.close()


class TestMappingTableRendering:
    """映射表格里鼠标动作的显示。

    ``_refresh_mapping_table`` 曾经拿 ``m.action.value`` 跟**大写**字符串比
    （``== "MOVE"`` / ``.startswith("SCROLL")``），而 ``MouseAction`` 的值是
    小写的 ``"move"`` / ``"scroll_up"`` —— 两个分支永远不成立，
    移动与滚轮映射在表格里一直显示原始英文枚举值而不是 ``(x,y)`` / ``滚轮 xN``。
    """

    def _cell(self, window, row, col):
        return window.mapping_table.item(row, col).text()

    def _mouse_outputs(self, window):
        """只取"类型"列为"鼠标"的那些行的"输出"列（键盘行排在前面）。"""
        out = []
        for r in range(window.mapping_table.rowCount()):
            if self._cell(window, r, 0) == "鼠标":
                out.append(self._cell(window, r, 2))
        return out

    def test_move_and_scroll_are_rendered(self, qt, isolated_manager):
        from danmaku_kb.input.mappings import (
            MouseAction,
            MouseButton,
            MouseMapping,
        )
        from danmaku_kb.ui.main_window import MainWindow

        # 键盘映射清空，让断言只面对鼠标行（否则键盘行排在前面）
        isolated_manager.key_mappings = []
        isolated_manager.mouse_mappings = [
            MouseMapping(
                trigger="移动", button=MouseButton.LEFT,
                action=MouseAction.MOVE, move_x=5, move_y=-3,
            ),
            MouseMapping(
                trigger="上滚", button=MouseButton.MIDDLE,
                action=MouseAction.SCROLL_UP, scroll_amount=2,
            ),
            MouseMapping(
                trigger="下滚", button=MouseButton.MIDDLE,
                action=MouseAction.SCROLL_DOWN, scroll_amount=3,
            ),
        ]
        window = MainWindow(config=isolated_manager.config, config_manager=isolated_manager)
        try:
            window.manager.key_mappings = []
            window.manager.mouse_mappings = list(isolated_manager.mouse_mappings)
            window._refresh_mapping_table()
            assert self._mouse_outputs(window) == ["(5,-3)", "滚轮 x2", "滚轮 x3"]
        finally:
            window.close()

    def test_plain_click_shows_button_name(self, qt, isolated_manager):
        from danmaku_kb.input.mappings import (
            MouseAction,
            MouseButton,
            MouseMapping,
        )
        from danmaku_kb.ui.main_window import MainWindow

        isolated_manager.key_mappings = []
        isolated_manager.mouse_mappings = [
            MouseMapping(trigger="左键", button=MouseButton.LEFT, action=MouseAction.CLICK),
        ]
        window = MainWindow(config=isolated_manager.config, config_manager=isolated_manager)
        try:
            window.manager.key_mappings = []
            window.manager.mouse_mappings = list(isolated_manager.mouse_mappings)
            window._refresh_mapping_table()
            assert self._mouse_outputs(window) == ["left"]
        finally:
            window.close()

    def test_long_press_shows_hold_duration(self, qt, isolated_manager):
        """「动作」列对长按要带出按住时长，否则用户看不出自己设的值存没存下来。"""
        from danmaku_kb.input.mappings import KeyAction, KeyMapping
        from danmaku_kb.ui.main_window import MainWindow

        isolated_manager.key_mappings = [
            KeyMapping(trigger="q", key="q", action=KeyAction.LONG_PRESS,
                       long_press_duration=1.25),
        ]
        isolated_manager.mouse_mappings = []
        window = MainWindow(config=isolated_manager.config, config_manager=isolated_manager)
        try:
            window.manager.key_mappings = list(isolated_manager.key_mappings)
            window.manager.mouse_mappings = []
            window._refresh_mapping_table()
            assert self._cell(window, 0, 3) == "长按 1.25s"
        finally:
            window.close()

    def test_other_actions_do_not_show_hold_duration(self, qt, isolated_manager):
        """非长按动作不显示时长：那一列不该被无关数字占满。"""
        from danmaku_kb.input.mappings import KeyAction, KeyMapping
        from danmaku_kb.ui.main_window import MainWindow

        isolated_manager.key_mappings = [
            KeyMapping(trigger="w", key="up", action=KeyAction.PRESS_AND_RELEASE,
                       long_press_duration=2.0),
        ]
        isolated_manager.mouse_mappings = []
        window = MainWindow(config=isolated_manager.config, config_manager=isolated_manager)
        try:
            window.manager.key_mappings = list(isolated_manager.key_mappings)
            window.manager.mouse_mappings = []
            window._refresh_mapping_table()
            assert self._cell(window, 0, 3) == "按下并松开（点按）"
        finally:
            window.close()


    def test_action_column_is_chinese_for_all_rows(self, qt, isolated_manager):
        """用户反馈：不进编辑器时，映射页「动作」列显示的是英文枚举值。

        以前直接写 ``m.action.value``（``press_and_release`` / ``click`` /
        ``scroll_up``），中文界面里很突兀。现在统一走
        ``ui/mapping_editor.py`` 的 ``action_label()``，与编辑器下拉同一份文案。
        """

        from danmaku_kb.ui.main_window import MainWindow

        window = MainWindow(config=isolated_manager.config, config_manager=isolated_manager)
        try:
            assert window.mapping_table.rowCount() > 0
            for r in range(window.mapping_table.rowCount()):
                text = self._cell(window, r, 3)
                assert text, f"第 {r} 行的动作是空的"
                assert not re.search(r"[a-z_]{4,}", text), f"第 {r} 行动作仍是英文：{text!r}"
        finally:
            window.close()

    def test_action_column_matches_editor_labels(self, qt, isolated_manager):
        import danmaku_kb.input.mappings as mappings_mod
        from danmaku_kb.ui.main_window import MainWindow

        window = MainWindow(config=isolated_manager.config, config_manager=isolated_manager)
        try:
            # 键盘默认动作是「按下并松开」，鼠标默认是「单击」
            key_rows = [r for r in range(window.mapping_table.rowCount())
                        if self._cell(window, r, 0) == "键盘"]
            mouse_rows = [r for r in range(window.mapping_table.rowCount())
                          if self._cell(window, r, 0) == "鼠标"]
            assert key_rows and mouse_rows
            first_key = isolated_manager.key_mappings[0]
            assert self._cell(window, key_rows[0], 3) == (
                "长按" if first_key.action is mappings_mod.KeyAction.LONG_PRESS else "按下并松开（点按）"
            )
            assert self._cell(window, mouse_rows[0], 3) == "单击"
        finally:
            window.close()


class TestLoginDialogConstruction:
    """登录对话框必须能被构造出来。

    这里曾经有一个**必崩**的缺陷：``LoginDialog.__init__`` 里写的是
    ``self.biliUpdate.connect(self._apply_bili_update)``，而真实方法名是
    ``_apply_bili_status``。PyQt5 的 ``pyqtSignal.connect()`` 在连接时就
    解析槽函数，找不到就抛 ``AttributeError`` —— 于是对话框构造必然失败，
    而 ``main_window._open_login`` 没有 try/except，用户点一下「获取凭证」
    整个进程直接 abort。

    之前没有任何一条测试构造过 ``LoginDialog``（``test_login_qr.py`` 只测了
    纯逻辑层），所以这个缺陷长期存活。
    """

    def test_bilibili_tab_dialog_constructs(self, qt):
        from danmaku_kb.ui.login_dialog import LoginDialog

        dialog = LoginDialog(platform="bilibili")
        try:
            assert dialog.tabs.count() == 2
            assert dialog.tabs.currentIndex() == 0
        finally:
            dialog.close()

    def test_douyin_tab_dialog_constructs(self, qt):
        from danmaku_kb.ui.login_dialog import LoginDialog

        dialog = LoginDialog(platform="douyin")
        try:
            assert dialog.tabs.currentIndex() == 1, "抖音平台应默认切到 Cookie 页"
        finally:
            dialog.close()

    def test_update_signal_has_a_real_slot(self, qt):
        """``biliUpdate`` 必须连到一个**真的存在**的方法上。

        直接连一个不存在的方法在 PyQt5 里就会抛 AttributeError，所以这条
        其实被上一条覆盖了；这里额外锁住"信号发出后状态栏真的会更新"，
        防止有人把 connect 删掉而测试还是绿的。
        """
        from danmaku_kb.login.bilibili_qr import LoginResult, QRState
        from danmaku_kb.ui.login_dialog import LoginDialog

        dialog = LoginDialog(platform="bilibili")
        try:
            dialog.biliUpdate.emit(
                LoginResult(state=QRState.SCANNED, message="已扫码")
            )
            assert "已扫码" in dialog.bili_status.text()
        finally:
            dialog.close()

    def test_cancel_button_does_not_pass_checked_as_keep_text(self, qt):
        """``clicked`` 会把 ``checked`` 当第一个位置参数传进槽函数。

        ``bili_cancel_btn.clicked.connect(self._stop_bili_login)`` 恰好因为
        ``checked=False`` 而"看起来能用"，但那是巧合：按钮一旦变成 checkable，
        ``keep_text`` 就会变成 True，状态栏文案再也不刷新。这里改成显式 lambda。
        """
        from danmaku_kb.ui.login_dialog import LoginDialog

        dialog = LoginDialog(platform="bilibili")
        try:
            # 按钮初始是禁用的（还没开始轮询），必须先启用才能真的点。
            dialog.bili_cancel_btn.setEnabled(True)
            dialog.bili_status.setText("原状态")
            dialog.bili_cancel_btn.click()
            assert dialog.bili_status.text() == "已停止轮询", (
                "点停止轮询后状态栏应刷新；若被 checked 参数污染就不会变"
            )
        finally:
            dialog.close()


class TestEventLoopRuns:
    def test_event_loop_processes_timers_and_exits(self, qt, isolated_manager):
        """跑一小段真实事件循环，确认 update 路径不会抛异常。

        用 QTimer 自己安排退出，不依赖 ``app.exec_()`` 被人打断；
        能走到 ``SMOKE_OK`` 就说明 ``_refresh_stats`` 之类的定时器回调是安全的。
        """
        from PyQt5.QtCore import QTimer

        from danmaku_kb.ui.main_window import MainWindow

        window = MainWindow(config=isolated_manager.config, config_manager=isolated_manager)
        window.show()

        ticks = {"n": 0}

        def tick():
            ticks["n"] += 1
            if ticks["n"] >= 6:
                window.close()
                qt.quit()

        timer = QTimer()
        timer.timeout.connect(tick)
        timer.start(50)
        qt.exec_()

        assert ticks["n"] >= 6, f"事件循环没跑够：{ticks['n']}"

    def test_stats_timer_stops_on_close(self, qt, isolated_manager):
        """关窗后统计定时器必须停掉，否则对象销毁后回调会悬空。"""
        from danmaku_kb.ui.main_window import MainWindow

        window = MainWindow(config=isolated_manager.config, config_manager=isolated_manager)
        window.close()

        timer = getattr(window, "_stats_timer", None)
        if timer is not None:
            assert not timer.isActive(), "关窗后 _stats_timer 仍在跑"
