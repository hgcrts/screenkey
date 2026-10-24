"""「只在指定窗口为前台时才注入按键」功能的测试。

覆盖三层：

* :mod:`danmaku_kb.input.window` 的纯逻辑（匹配、取值、fail-closed 正则）；
* 配置层：``focus_filter`` 能不能往返、手改 JSON 会不会把 ``"false"`` 当开启；
* 引擎闸门：前台窗口不匹配时**一个按键都不发**，并且**不消耗冷却**
  （否则用户切回游戏后发现还要再等一个冷却，体感就是"没修好"）。

全部离线：不联网、不发真实按键（``keyboard.execute`` 被替换成记录器，
``get_foreground_window`` 被替换成固定值）。没有任何窗口 API 调用。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List, Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from danmaku_kb.config.models import AppConfig, FocusFilterConfig, InputConfig  # noqa: E402
from danmaku_kb.danmaku.message import DanmakuMessage  # noqa: E402
from danmaku_kb.filters.rules import FilterEngine  # noqa: E402
from danmaku_kb.focus_gate import FocusGate  # noqa: E402
from danmaku_kb.input import window as window_mod  # noqa: E402
from danmaku_kb.input.mappings import KeyAction, KeyMapping  # noqa: E402
from danmaku_kb.input.window import (  # noqa: E402
    FocusFilter,
    WindowInfo,
    coerce_field,
    coerce_mode,
    window_matches,
)

# ===================================================================== 纯逻辑


def _info(
    title: str = "原神",
    process: str = "YuanShen.exe",
    class_name: str = "UnityWndClass",
) -> WindowInfo:
    return WindowInfo(hwnd=1, title=title, process_name=process, class_name=class_name)


class TestCoerce:
    """非法取值必须被收敛，而不是"拼错了就静默永远不匹配"。"""

    @pytest.mark.parametrize("value", ["PROCESS", " process ", "process"])
    def test_field_accepts_case_and_space(self, value: str) -> None:
        assert coerce_field(value) == "process"

    @pytest.mark.parametrize("value", ["Title", "TITLE", "title"])
    def test_field_accepts_title(self, value: str) -> None:
        assert coerce_field(value) == "title"

    @pytest.mark.parametrize("value", ["", None, "proc", "hwnd", 7, "窗口"])
    def test_field_falls_back_to_title(self, value: Any) -> None:
        assert coerce_field(value) == "title"

    @pytest.mark.parametrize("value", ["REGEX", " regex ", "Regex"])
    def test_mode_accepts_case_and_space(self, value: str) -> None:
        assert coerce_mode(value) == "regex"

    @pytest.mark.parametrize("value", ["", None, "glob", "equal", 3])
    def test_mode_falls_back_to_contains(self, value: Any) -> None:
        assert coerce_mode(value) == "contains"


class TestWindowMatches:
    def test_none_info_never_matches(self) -> None:
        assert window_matches(None, "原神") is False

    @pytest.mark.parametrize("pattern", ["", "   ", None])
    def test_empty_pattern_never_matches(self, pattern: Any) -> None:
        """空目标不能"匹配一切"：那等于启用了过滤却全部放行。"""
        assert window_matches(_info(), pattern) is False

    def test_contains_is_case_insensitive_by_default(self) -> None:
        info = _info(process="GenshinImpact.exe")
        assert window_matches(info, "genshinimpact", field="process") is True
        assert window_matches(info, "genshinimpact", field="process", case_sensitive=True) is False

    def test_contains_is_substring(self) -> None:
        assert window_matches(_info(title="原神 - 深渊"), "深渊") is True

    def test_contains_strips_pattern_whitespace(self) -> None:
        """从别处复制标题常带首尾空格，必须容忍。"""
        assert window_matches(_info(title="原神"), "  原神  ") is True

    def test_case_sensitive_contains(self) -> None:
        assert window_matches(_info(title="Game"), "game", case_sensitive=True) is False
        assert window_matches(_info(title="game"), "game", case_sensitive=True) is True

    def test_exact_does_not_substring(self) -> None:
        assert window_matches(_info(title="原神 1.5"), "原神", mode="exact") is False
        assert window_matches(_info(title="原神"), "原神", mode="exact") is True

    @pytest.mark.parametrize("pattern", ["genshinimpact", "GenshinImpact.exe", "GENSHINIMPACT.EXE"])
    def test_exact_on_process_ignores_exe(self, pattern: str) -> None:
        """填进程名时带不带 .exe 是同一个意思。"""
        assert window_matches(_info(process="GenshinImpact.exe"), pattern, field="process", mode="exact")

    def test_exact_on_title_keeps_exe(self) -> None:
        """同样的宽容**不能**泄漏到标题上：标题里的 .exe 是实打实的字符。"""
        assert window_matches(_info(title="a.exe"), "a", field="title", mode="exact") is False

    def test_field_selects_which_attribute(self) -> None:
        info = _info(title="原神", process="YuanShen.exe", class_name="UnityWndClass")
        assert window_matches(info, "YuanShen", field="process") is True
        assert window_matches(info, "YuanShen", field="title") is False
        assert window_matches(info, "UnityWnd", field="class") is True

    def test_regex_matches(self) -> None:
        info = _info(title="原神 - 深渊 12-3")
        assert window_matches(info, r"原神.*\d+-\d+", mode="regex") is True

    def test_invalid_regex_matches_nothing(self) -> None:
        """fail closed：正则写错就不匹配，而不是放行到任意窗口。"""
        assert window_matches(_info(title="原神"), "原神(", mode="regex") is False

    def test_regex_bad_pattern_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING"):
            window_matches(_info(), "([", mode="regex")
        assert any("正则" in record.getMessage() for record in caplog.records)

    def test_empty_field_value_does_not_match(self) -> None:
        assert window_matches(_info(process=""), "x", field="process") is False


class TestFocusFilter:
    def test_disabled_is_configured_false(self) -> None:
        f = FocusFilter(enabled=False, pattern="原神")
        assert f.configured is False
        assert f.should_inject() is True

    def test_enabled_without_pattern_is_not_configured(self) -> None:
        f = FocusFilter(enabled=True, pattern="   ")
        assert f.configured is False
        assert f.should_inject() is True
        assert f.status_text() == "已启用但还没填目标窗口：现在不会注入任何按键。"

    def test_from_config_none(self) -> None:
        f = FocusFilter.from_config(None)
        assert (f.enabled, f.pattern, f.field, f.mode) == (False, "", "title", "contains")

    def test_from_config_duck_typed(self) -> None:
        """``input`` 层不认识 config 层，只能靠 duck typing。"""

        class Fake:
            enabled = True
            pattern = "GenshinImpact.exe"
            field = "PROCESS"
            match_mode = "Exact"
            case_sensitive = True

        f = FocusFilter.from_config(Fake())
        assert f.field == "process"
        assert f.mode == "exact"
        assert f.case_sensitive is True

    def test_from_config_tolerates_missing_attributes(self) -> None:
        class Sparse:
            enabled = True
            pattern = "x"

        f = FocusFilter.from_config(Sparse())
        assert f.field == "title"
        assert f.mode == "contains"

    def test_should_inject_follows_foreground(self, monkeypatch: pytest.MonkeyPatch) -> None:
        f = FocusFilter(enabled=True, pattern="原神", field="title")
        monkeypatch.setattr(window_mod, "WINDOWS_API", True)
        monkeypatch.setattr(window_mod, "get_foreground_window", lambda: _info(title="原神"))
        assert f.should_inject() is True
        monkeypatch.setattr(window_mod, "get_foreground_window", lambda: _info(title="浏览器"))
        assert f.should_inject() is False

    def test_missing_foreground_window_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """读不到前台窗口时宁可不发，也不要打进不知道哪儿的窗口。"""
        f = FocusFilter(enabled=True, pattern="原神")
        monkeypatch.setattr(window_mod, "WINDOWS_API", True)
        monkeypatch.setattr(window_mod, "get_foreground_window", lambda: None)
        assert f.should_inject() is False
        assert "不会注入" in f.status_text()

    def test_non_windows_does_not_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """非 Windows 放行，让下层如实报"发不出去"，而不是静默什么都不做。"""
        f = FocusFilter(enabled=True, pattern="原神")
        monkeypatch.setattr(window_mod, "WINDOWS_API", False)
        assert f.should_inject() is True

    def test_status_text_when_disabled(self) -> None:
        assert "未启用" in FocusFilter(enabled=False).status_text()


# ===================================================================== 配置层


class TestFocusFilterConfig:
    def test_defaults_are_off(self) -> None:
        cfg = FocusFilterConfig()
        assert cfg.enabled is False
        assert cfg.pattern == ""
        assert cfg.field == "title"
        assert cfg.match_mode == "contains"

    @pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on", "是", "开", True, 1])
    def test_enabled_truthy_strings(self, value: Any) -> None:
        assert FocusFilterConfig.from_dict({"enabled": value}).enabled is True

    @pytest.mark.parametrize("value", ["false", "False", "0", "no", "off", "", "否", "关", "none", False, 0])
    def test_enabled_falsey_strings(self, value: Any) -> None:
        """手改 settings.json 写成字符串 ``"false"`` 时绝不能当成开启。"""
        assert FocusFilterConfig.from_dict({"enabled": value}).enabled is False

    def test_invalid_field_and_mode_fall_back(self) -> None:
        cfg = FocusFilterConfig.from_dict({"field": "hwnd", "match_mode": "glob"})
        assert cfg.field == "title"
        assert cfg.match_mode == "contains"

    def test_non_mapping_returns_default(self) -> None:
        for bad in (None, "x", 5, ["a"]):
            assert FocusFilterConfig.from_dict(bad) == FocusFilterConfig()

    def test_input_config_roundtrip(self) -> None:
        cfg = InputConfig.from_dict(
            {
                "enabled": True,
                "focus_filter": {
                    "enabled": "yes",
                    "pattern": "  GenshinImpact.exe  ",
                    "field": " PROCESS ",
                    "match_mode": "EXACT",
                    "case_sensitive": "0",
                }
            }
        )
        ff = cfg.focus_filter
        assert (ff.enabled, ff.pattern, ff.field, ff.match_mode, ff.case_sensitive) == (
            True,
            "  GenshinImpact.exe  ",
            "process",
            "exact",
            False,
        )

    def test_input_config_without_focus_key_gets_default(self) -> None:
        assert InputConfig.from_dict({"enabled": True}).focus_filter == FocusFilterConfig()

    def test_nested_focus_filter_survives_appconfig_roundtrip(self) -> None:
        from danmaku_kb.config.models import AppConfig

        cfg = AppConfig()
        cfg.input.focus_filter = FocusFilterConfig(
            enabled=True, pattern="原神", field="title", match_mode="contains", case_sensitive=True
        )
        payload = cfg.to_dict()
        assert payload["input"]["focus_filter"]["enabled"] is True
        assert payload["input"]["focus_filter"]["pattern"] == "原神"
        again = AppConfig.from_dict(payload)
        assert again.input.focus_filter == cfg.input.focus_filter


# ===================================================================== 引擎闸门


def _key_mapping() -> KeyMapping:
    return KeyMapping(trigger="w", key="w", action=KeyAction.PRESS_AND_RELEASE)


class _Recorder:
    """替掉 ``KeyboardController``，只记录"到底发没发"。"""

    def __init__(self) -> None:
        self.calls: List[Any] = []
        self.game_mode = False

    def execute(self, mapping: Any, is_long_press: bool = False) -> bool:
        self.calls.append((mapping.key, is_long_press))
        return True

    def set_game_mode(self, on: bool) -> None:
        self.game_mode = bool(on)


def _make_engine(*, focus: FocusFilterConfig, dry_run: bool = False):
    from danmaku_kb.engine import DanmakuKeyboardEngine

    config = InputConfig()
    config.focus_filter = focus
    from danmaku_kb.config.models import AppConfig

    app_config = AppConfig()
    app_config.input = config
    # 冷却/去重全部关掉，专心验证焦点闸门
    app_config.input.global_cooldown = 0.0
    app_config.input.per_user_cooldown = 0.0
    app_config.input.dedupe_window = 0.0
    app_config.input.max_actions_per_message = 1

    engine = DanmakuKeyboardEngine(
        config=app_config,
        key_mappings=[_key_mapping()],
        mouse_mappings=[],
        filters=FilterEngine.default(),
    )
    recorder = _Recorder()
    engine.keyboard = recorder  # type: ignore[assignment]
    engine.dry_run = dry_run
    return engine, recorder


def _danmaku(content: str = "w", user_id: int = 1) -> DanmakuMessage:
    return DanmakuMessage(content=content, nickname="观众", user_id=user_id, platform="simulator")


class TestEngineFocusGate:
    def _patch(self, monkeypatch: pytest.MonkeyPatch, title: Optional[str]) -> None:
        monkeypatch.setattr(window_mod, "WINDOWS_API", True)
        monkeypatch.setattr(
            window_mod,
            "get_foreground_window",
            (lambda: None) if title is None else (lambda: _info(title=title)),
        )

    def test_matching_window_injects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine, rec = _make_engine(focus=FocusFilterConfig(enabled=True, pattern="原神"))
        self._patch(monkeypatch, "原神")
        engine._handle_message(_danmaku())
        assert rec.calls == [("w", False)]
        assert engine.stats.total_actions == 1
        assert engine.stats.focus_suppressed == 0

    def test_non_matching_window_injects_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine, rec = _make_engine(focus=FocusFilterConfig(enabled=True, pattern="原神"))
        self._patch(monkeypatch, "Chrome")
        engine._handle_message(_danmaku())
        assert rec.calls == []
        assert engine.stats.total_actions == 0
        assert engine.stats.focus_suppressed == 1
        snapshot = engine.stats_snapshot()
        assert snapshot["focus_suppressed"] == 1

    def test_unreadable_foreground_injects_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine, rec = _make_engine(focus=FocusFilterConfig(enabled=True, pattern="原神"))
        self._patch(monkeypatch, None)
        engine._handle_message(_danmaku())
        assert rec.calls == []
        assert engine.stats.focus_suppressed == 1

    def test_disabled_filter_injects_anywhere(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine, rec = _make_engine(focus=FocusFilterConfig(enabled=False, pattern="原神"))
        self._patch(monkeypatch, "Chrome")
        engine._handle_message(_danmaku())
        assert rec.calls == [("w", False)]
        assert engine.stats.focus_suppressed == 0

    def test_enabled_but_blank_pattern_injects_anywhere(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """只是打开了开关还没填目标：按"没配好"处理，不断按键。"""
        engine, rec = _make_engine(focus=FocusFilterConfig(enabled=True, pattern="   "))
        self._patch(monkeypatch, "Chrome")
        engine._handle_message(_danmaku())
        assert rec.calls == [("w", False)]

    def test_dry_run_ignores_focus(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """空跑模式（自检/单测）结果不能依赖"跑的时候哪个窗口在前台"。"""
        engine, _ = _make_engine(focus=FocusFilterConfig(enabled=True, pattern="原神"), dry_run=True)
        self._patch(monkeypatch, "Chrome")
        assert engine.focus_allows_injection() is True
        assert engine.focus_status_text().startswith("空跑")

    def test_blocked_does_not_consume_cooldown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """被焦点拦下的那次不该吃掉冷却——切回游戏后必须立刻能触发。"""
        focus = FocusFilterConfig(enabled=True, pattern="原神")
        engine, rec = _make_engine(focus=focus)
        engine.config.input.global_cooldown = 30.0

        self._patch(monkeypatch, "Chrome")
        engine._handle_message(_danmaku())
        assert rec.calls == []

        self._patch(monkeypatch, "原神")
        engine._handle_message(_danmaku("w", user_id=2))
        assert rec.calls == [("w", False)]

    def test_focus_probe_error_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """探测炸了只能"不发按键"：这是最后一道闸，意外一律倒向不发。"""
        engine, rec = _make_engine(focus=FocusFilterConfig(enabled=True, pattern="原神"))
        monkeypatch.setattr(window_mod, "WINDOWS_API", True)

        def boom():
            raise OSError("query failed")

        monkeypatch.setattr(window_mod, "get_foreground_window", boom)
        engine._handle_message(_danmaku())
        assert rec.calls == []
        assert engine.stats.focus_suppressed == 1

    def test_status_text_survives_probe_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """状态文案在 GUI 定时器里被调用；异常逃出去就是 qFatal 闪退。"""
        f = FocusFilter(enabled=True, pattern="原神")
        monkeypatch.setattr(window_mod, "WINDOWS_API", True)

        def boom():
            raise OSError("query failed")

        monkeypatch.setattr(window_mod, "get_foreground_window", boom)
        text = f.status_text()
        assert isinstance(text, str)
        assert "出错" in text

    def test_live_config_change_takes_effect_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """界面上改完就生效：引擎每次判定读的都是当前配置对象。"""
        focus = FocusFilterConfig(enabled=True, pattern="原神")
        engine, rec = _make_engine(focus=focus)
        self._patch(monkeypatch, "Chrome")
        engine._handle_message(_danmaku())
        assert rec.calls == []

        # 用户把目标改成 Chrome（同一个配置对象上改字段）
        engine.config.input.focus_filter.pattern = "Chrome"
        engine._handle_message(_danmaku("w", user_id=2))
        assert rec.calls == [("w", False)]

    def test_turning_filter_off_takes_effect_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        focus = FocusFilterConfig(enabled=True, pattern="原神")
        engine, rec = _make_engine(focus=focus)
        self._patch(monkeypatch, "Chrome")
        engine.config.input.focus_filter.enabled = False
        engine._handle_message(_danmaku())
        assert rec.calls == [("w", False)]

    def test_old_config_without_focus_field_is_tolerated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """反序列化路径之外构造的旧配置对象没有 ``focus_filter``，不能炸。"""
        engine, rec = _make_engine(focus=FocusFilterConfig())
        del engine.config.input.focus_filter
        self._patch(monkeypatch, "Chrome")
        engine._handle_message(_danmaku())
        assert rec.calls == [("w", False)]
        assert engine.focus_allows_injection() is True


# ===================================================================== 界面层


@pytest.fixture(scope="module")
def qt():
    """离屏 QApplication；没装 PyQt5 就跳过本节。"""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt5", reason="需要 PyQt5 才能测界面")

    from danmaku_kb.ui.qt_env import PYQT5_AVAILABLE, configure_qt_environment

    assert PYQT5_AVAILABLE, "PyQt5 已装但探测为不可用，qt_env 的探测逻辑有问题"
    configure_qt_environment()

    from PyQt5.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def focused_window(qt, tmp_path):
    """真建一个主窗口；退出时关掉（会走 closeEvent 的落盘路径）。"""
    from danmaku_kb.config.manager import ConfigManager
    from danmaku_kb.ui.main_window import MainWindow

    manager = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
    manager.load()
    win = MainWindow(config=manager.config, config_manager=manager)
    yield win, manager
    win.close()


class TestMainWindowFocusUi:
    def test_group_starts_off_and_details_disabled(self, focused_window: Any) -> None:
        win, _ = focused_window
        assert win.focus_enable.isChecked() is False
        assert win.focus_pattern.isEnabled() is False
        assert "未启用" in win.focus_status.text()

    def test_turning_on_writes_config_and_enables_details(self, focused_window: Any) -> None:
        win, manager = focused_window
        win.focus_enable.setChecked(True)
        assert manager.config.input.focus_filter.enabled is True
        assert win.focus_pattern.isEnabled() is True

    def test_editing_pattern_strips_and_writes_config(self, focused_window: Any) -> None:
        win, manager = focused_window
        win.focus_enable.setChecked(True)
        win.focus_pattern.setText("  GenshinImpact.exe  ")
        win.focus_pattern.editingFinished.emit()
        assert manager.config.input.focus_filter.pattern == "GenshinImpact.exe"

    def test_field_and_mode_use_item_data(self, focused_window: Any) -> None:
        win, manager = focused_window
        win.focus_enable.setChecked(True)
        win.focus_field.setCurrentIndex(win.focus_field.findData("process"))
        win.focus_mode.setCurrentIndex(win.focus_mode.findData("regex"))
        win.focus_case.setChecked(True)
        ff = manager.config.input.focus_filter
        assert (ff.field, ff.match_mode, ff.case_sensitive) == ("process", "regex", True)

    def test_status_label_comes_from_engine(self, focused_window: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        win, _ = focused_window
        win.focus_enable.setChecked(True)
        monkeypatch.setattr(win.engine, "focus_status_text", lambda: "哨兵状态")
        win._refresh_focus_status()
        assert win.focus_status.text() == "哨兵状态"

    def test_refresh_stats_updates_focus_status_when_enabled(
        self, focused_window: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        win, _ = focused_window
        win.focus_enable.setChecked(True)
        monkeypatch.setattr(win.engine, "focus_status_text", lambda: "每秒刷新")
        win._refresh_stats()
        assert win.focus_status.text() == "每秒刷新"

    def test_pick_window_fills_pattern(self, focused_window: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from PyQt5.QtWidgets import QInputDialog

        from danmaku_kb.ui import main_window as mw

        win, manager = focused_window
        win.focus_enable.setChecked(True)
        monkeypatch.setattr(
            mw,
            "list_windows",
            lambda *a, **k: [_info(title="原神"), _info(title="Chrome", process="chrome.exe")],
        )
        monkeypatch.setattr(QInputDialog, "getItem", staticmethod(lambda *a, **k: ("Chrome", True)))
        win._pick_focus_window()
        assert win.focus_pattern.text() == "Chrome"
        assert manager.config.input.focus_filter.pattern == "Chrome"

    def test_pick_window_cancel_keeps_pattern(self, focused_window: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from PyQt5.QtWidgets import QInputDialog

        from danmaku_kb.ui import main_window as mw

        win, manager = focused_window
        win.focus_enable.setChecked(True)
        win.focus_pattern.setText("原神")
        win.focus_pattern.editingFinished.emit()
        monkeypatch.setattr(mw, "list_windows", lambda *a, **k: [_info(title="原神")])
        monkeypatch.setattr(QInputDialog, "getItem", staticmethod(lambda *a, **k: ("别的", False)))
        win._pick_focus_window()
        assert manager.config.input.focus_filter.pattern == "原神"

    def test_pick_window_without_windows_explains(self, focused_window: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from PyQt5.QtWidgets import QMessageBox

        from danmaku_kb.ui import main_window as mw

        win, _ = focused_window
        win.focus_enable.setChecked(True)
        monkeypatch.setattr(mw, "list_windows", lambda *a, **k: [])
        seen: List[str] = []
        monkeypatch.setattr(
            QMessageBox, "information", staticmethod(lambda *a, **k: seen.append(a[2]) or QMessageBox.Ok)
        )
        win._pick_focus_window()  # 不能抛
        assert seen, "没有窗口时应该给用户一句解释，而不是静默什么都不做"

    def test_pick_window_when_enumeration_raises(self, focused_window: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from PyQt5.QtWidgets import QMessageBox

        from danmaku_kb.ui import main_window as mw

        win, _ = focused_window
        win.focus_enable.setChecked(True)

        def boom(*a: Any, **k: Any):
            raise OSError("enum failed")

        monkeypatch.setattr(mw, "list_windows", boom)
        seen: List[str] = []
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: seen.append(a[2]) or QMessageBox.Ok))
        win._pick_focus_window()  # 不能抛：Qt 槽里逃出异常就是 qFatal
        assert seen

    def test_settings_survive_restart(self, qt: Any, tmp_path: Path) -> None:
        """关掉再打开：焦点设置必须还在（这是本功能最容易做漏的一半）。"""
        from danmaku_kb.config.manager import ConfigManager
        from danmaku_kb.ui.main_window import MainWindow

        config_dir = tmp_path / "cfg"

        manager = ConfigManager(config_dir=config_dir, auto_save=False)
        manager.load()
        win = MainWindow(config=manager.config, config_manager=manager)
        win.focus_enable.setChecked(True)
        win.focus_field.setCurrentIndex(win.focus_field.findData("process"))
        win.focus_pattern.setText("YuanShen.exe")
        win.focus_pattern.editingFinished.emit()
        assert manager.save() is True
        win.close()

        again = ConfigManager(config_dir=config_dir, auto_save=False)
        again.load()
        ff = again.config.input.focus_filter
        assert (ff.enabled, ff.pattern, ff.field) == (True, "YuanShen.exe", "process")

        reopened = MainWindow(config=again.config, config_manager=again)
        assert reopened.focus_enable.isChecked() is True
        assert reopened.focus_pattern.text() == "YuanShen.exe"
        assert reopened.focus_pattern.isEnabled() is True
        reopened.close()


class TestFocusGateStandalone:
    """焦点闸门脱离引擎单独可测 —— 这正是把它抽成 ``focus_gate`` 的理由。

    以前想验证"前台不匹配就不注入"，得先搭好键盘控制器、冷却、筛选引擎和
    抓取器；现在只需要一个配置对象 + 一个被替换掉的前台窗口。
    """

    def _patch(self, monkeypatch: pytest.MonkeyPatch, title: str) -> None:
        monkeypatch.setattr(window_mod, "WINDOWS_API", True)
        monkeypatch.setattr(
            window_mod, "get_foreground_window", lambda: _info(title=title)
        )

    def _gate(self, **kwargs: Any):
        config = AppConfig()
        config.input.focus_filter = FocusFilterConfig(**kwargs)
        return FocusGate(config)

    def test_blocks_when_other_window_in_front(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, "Chrome")
        gate = self._gate(enabled=True, pattern="原神")
        assert gate.blocks() is True
        assert gate.allows_injection() is False
        assert "不注入" in gate.status_text()

    def test_unconfigured_never_blocks(self) -> None:
        """默认配置（未启用）既不该拦，也不该说"读不到前台窗口"。"""
        gate = self._gate()
        assert gate.blocks() is False
        assert gate.allows_injection() is True

    def test_dry_run_always_allows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, "Chrome")
        gate = self._gate(enabled=True, pattern="原神")
        assert gate.blocks(dry_run=True) is False
        assert gate.allows_injection(dry_run=True) is True
        assert gate.status_text(dry_run=True).startswith("空跑")

    def test_reads_config_live_no_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """界面改了配置必须立刻生效：闸门只持配置引用，不持快照。"""
        self._patch(monkeypatch, "Chrome")
        gate = self._gate(enabled=True, pattern="原神")
        assert gate.blocks() is True

        gate._config.input.focus_filter.enabled = False  # 界面关掉开关
        assert gate.blocks() is False

        gate._config.input.focus_filter.enabled = True  # 重新打开，改目标
        gate._config.input.focus_filter.pattern = "Chrome"
        assert gate.blocks() is False
        assert "会注入" in gate.status_text()


