# -*- coding: utf-8 -*-
"""弹幕窗口「设置」：按消息类型开关 + 显示项的回归测试。

背景（2026-10-23，B 站实测后用户提出）：弹幕列表一直没有"哪些消息要显示"
的开关，进场/点赞刷屏时真正的聊天弹幕会被冲走；旧版程序（``抖音加B站幕键``）
的「筛选」对话框有 8 种类型的开关，本套件锁定新实现的关键行为：

* 被关闭的类型不进待渲染队列、不进历史（导出时不夹带）；
* 未知类型（以后新增的）默认仍然显示，旧配置不会把它们静音；
* 对话框的取值能原样应用回 ``DisplayConfig``，且「自动滚动」两处入口同步；
* 关闭的设置要能写回 ``from_dict`` 往返，不因为一次往返被悄悄复位。

全部离屏、不联网。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------- 配置层
def test_show_types_default_all_on():
    from screenkey.config.models import DANMAKU_TYPE_ORDER, DisplayConfig

    cfg = DisplayConfig()
    assert cfg.show_types == {key: True for key in DANMAKU_TYPE_ORDER}
    assert cfg.type_enabled("danmaku") is True
    assert cfg.type_enabled("like") is True


def test_show_types_partial_dict_keeps_other_defaults():
    """只写想关掉的那一项时，其余类型必须保持默认的"开"。"""
    from screenkey.config.models import DisplayConfig

    cfg = DisplayConfig.from_dict({"show_types": {"gift": False}})
    assert cfg.show_types["gift"] is False
    assert cfg.show_types["danmaku"] is True
    assert cfg.show_types["enter"] is True


def test_show_types_string_values_are_coerced():
    """手写 ``"off"`` / ``"false"`` 不能因为非空字符串被判为真。"""
    from screenkey.config.models import DisplayConfig

    cfg = DisplayConfig.from_dict({"show_types": {"enter": "off", "like": "false", "gift": "1"}})
    assert cfg.show_types["enter"] is False
    assert cfg.show_types["like"] is False
    assert cfg.show_types["gift"] is True


def test_unknown_type_still_enabled():
    from screenkey.config.models import DisplayConfig

    cfg = DisplayConfig.from_dict({"show_types": {"danmaku": False}})
    assert cfg.type_enabled("brand_new_type") is True
    assert cfg.type_enabled(None) is True


def test_show_types_survive_from_dict_round_trip():
    from screenkey.config.models import DisplayConfig

    cfg = DisplayConfig()
    cfg.show_types["gift"] = False
    cfg.show_types["enter"] = False
    again = DisplayConfig.from_dict(cfg.__dict__)
    assert again.show_types["gift"] is False
    assert again.show_types["enter"] is False
    assert again.show_types["danmaku"] is True


# ---------------------------------------------------------------- 渲染层
def test_danmaku_html_labels_non_chat_types():
    from screenkey.ui.theme import danmaku_html

    plain = danmaku_html("你好", nickname="甲", timestamp="12:00:00", kind="danmaku")
    assert "〔" not in plain
    gift = danmaku_html("送出小心心", nickname="乙", kind="gift")
    assert "〔礼物〕" in gift
    enter = danmaku_html("进入了直播间", kind="enter")
    assert "〔进场〕" in enter


# ---------------------------------------------------------------- 界面层
@pytest.fixture(scope="module")
def qt():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt5", reason="需要 PyQt5 才能测界面")
    from screenkey.ui.qt_env import PYQT5_AVAILABLE, configure_qt_environment

    assert PYQT5_AVAILABLE
    configure_qt_environment()
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _msg(kind, text="x"):
    from screenkey.danmaku.message import DanmakuMessage, MessageType

    return DanmakuMessage(content=text, nickname="测试", platform="bilibili",
                          msg_type=MessageType.coerce(kind))


def test_window_filters_disabled_types(qt):
    from screenkey.config.models import DisplayConfig
    from screenkey.ui.danmaku_window import DanmakuWindow

    cfg = DisplayConfig.from_dict({"show_types": {"enter": False, "like": False}})
    win = DanmakuWindow(cfg)
    try:
        win.append(_msg("danmaku", "真正的弹幕"))
        win.append(_msg("enter", "某某 进入了直播间"))
        win.append(_msg("like", "点赞了"))
        assert win._type_filtered == 2
        assert len(win._history) == 1
        assert len(win._pending) == 1
        assert "过滤 2 条" in win.status.text()
    finally:
        win.deleteLater()


def test_window_apply_settings_updates_config_and_toolbar(qt):
    from screenkey.config.models import DisplayConfig
    from screenkey.ui.danmaku_window import DanmakuWindow

    win = DanmakuWindow(DisplayConfig())
    received = []
    win.configChanged.connect(received.append)
    try:
        win.apply_settings({
            "show_types": {"gift": False},
            "show_timestamp": False,
            "show_nickname": False,
            "show_platform": True,
            "auto_scroll": False,
            "font_size": 22,
            "max_lines": 300,
        })
        assert win.config.show_types["gift"] is False
        assert win.config.type_enabled("danmaku") is True
        assert win.config.show_timestamp is False
        assert win.config.font_size == 22
        assert win.config.max_lines == 300
        # 工具栏「自动滚动」必须同步到同一份值
        assert win.auto_scroll.isChecked() is False
        assert win.view.document().maximumBlockCount() == 300
        assert received, "apply_settings 应发出 configChanged 以便主窗口落盘"
    finally:
        win.deleteLater()


def test_dialog_values_round_trip(qt):
    from screenkey.config.models import DisplayConfig
    from screenkey.ui.danmaku_window import DisplaySettingsDialog

    cfg = DisplayConfig()
    cfg.show_types["super_chat"] = False
    dlg = DisplaySettingsDialog(cfg)
    try:
        assert dlg._type_boxes["super_chat"].isChecked() is False
        assert dlg._type_boxes["danmaku"].isChecked() is True
        dlg._type_boxes["super_chat"].setChecked(True)
        dlg._type_boxes["follow"].setChecked(False)
        dlg.font_size.setValue(30)
        values = dlg.values()
        assert values["show_types"]["super_chat"] is True
        assert values["show_types"]["follow"] is False
        assert values["font_size"] == 30
    finally:
        dlg.deleteLater()


# ---------------------------------------------------------------- 窗口置顶
def test_always_on_top_default_and_coercion():
    """置顶默认开；手写字符串/缺字段都按 ``_as_bool`` 规则走。"""
    from screenkey.config.models import DisplayConfig

    assert DisplayConfig().always_on_top is True
    assert DisplayConfig.from_dict({}).always_on_top is True
    assert DisplayConfig.from_dict({"always_on_top": "off"}).always_on_top is False
    # 往返之后不能被悄悄复位
    cfg = DisplayConfig.from_dict({"always_on_top": False})
    assert DisplayConfig.from_dict(cfg.__dict__).always_on_top is False


def test_window_applies_topmost_flag_on_construction(qt):
    from PyQt5.QtCore import Qt

    from screenkey.config.models import DisplayConfig
    from screenkey.ui.danmaku_window import DanmakuWindow

    on = DanmakuWindow(DisplayConfig.from_dict({"always_on_top": True}))
    off = DanmakuWindow(DisplayConfig.from_dict({"always_on_top": False}))
    try:
        assert on.windowFlags() & Qt.WindowStaysOnTopHint
        assert not (off.windowFlags() & Qt.WindowStaysOnTopHint)
        assert on.pin_btn.isChecked() is True
        assert off.pin_btn.isChecked() is False
    finally:
        on.deleteLater()
        off.deleteLater()


def test_pin_button_toggles_flag_and_persists_config(qt):
    """工具栏「置顶」：改 flags、写回配置、发 configChanged 供主窗口落盘。"""
    from PyQt5.QtCore import Qt

    from screenkey.config.models import DisplayConfig
    from screenkey.ui.danmaku_window import DanmakuWindow

    win = DanmakuWindow(DisplayConfig.from_dict({"always_on_top": True}))
    received = []
    win.configChanged.connect(received.append)
    try:
        win.pin_btn.setChecked(False)
        assert not (win.windowFlags() & Qt.WindowStaysOnTopHint)
        assert win.config.always_on_top is False
        assert received

        win.pin_btn.setChecked(True)
        assert win.windowFlags() & Qt.WindowStaysOnTopHint
        assert win.config.always_on_top is True
    finally:
        win.deleteLater()


def test_toggling_pin_on_visible_window_keeps_it_shown(qt):
    """已显示的窗口改 flags 后必须重新 show()，否则 Windows 上会直接消失。"""
    from PyQt5.QtCore import Qt

    from screenkey.config.models import DisplayConfig
    from screenkey.ui.danmaku_window import DanmakuWindow

    win = DanmakuWindow(DisplayConfig.from_dict({"always_on_top": True}))
    try:
        win.show()
        assert win.isVisible()
        geo = win.geometry()
        win.pin_btn.setChecked(False)
        assert win.isVisible(), "取消置顶后窗口必须仍然可见"
        assert not (win.windowFlags() & Qt.WindowStaysOnTopHint)
        # 几何不能被 setWindowFlags 重置（Windows 上会变 0,0 或默认尺寸）
        assert win.geometry().size() == geo.size()
    finally:
        win.deleteLater()


def test_apply_config_syncs_pin_state(qt):
    from PyQt5.QtCore import Qt

    from screenkey.config.models import DisplayConfig
    from screenkey.ui.danmaku_window import DanmakuWindow

    win = DanmakuWindow(DisplayConfig.from_dict({"always_on_top": False}))
    try:
        assert not (win.windowFlags() & Qt.WindowStaysOnTopHint)
        win.apply_config(DisplayConfig.from_dict({"always_on_top": True}))
        assert win.windowFlags() & Qt.WindowStaysOnTopHint
        assert win.pin_btn.isChecked() is True
        assert win.config.always_on_top is True
    finally:
        win.deleteLater()


def test_dialog_exposes_always_on_top(qt):
    from screenkey.config.models import DisplayConfig
    from screenkey.ui.danmaku_window import DisplaySettingsDialog

    dlg = DisplaySettingsDialog(DisplayConfig.from_dict({"always_on_top": True}))
    try:
        assert dlg.always_on_top.isChecked() is True
        dlg.always_on_top.setChecked(False)
        assert dlg.values()["always_on_top"] is False
    finally:
        dlg.deleteLater()
