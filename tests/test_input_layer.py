# -*- coding: utf-8 -*-
"""输入层测试：键码表、键盘/鼠标控制器、全局热键。

这一层是整个程序真正"**映射到本地键盘**"的地方，此前**一条测试都没有**
（`test_mappings.py` 只测映射数据的序列化，`test_matcher.py` 只测"该触发哪条"，
真正把动作发出去的 `keyboard.py` / `mouse.py` / `keycodes.py` / `hotkey.py`
从未被执行过）。补这组测试时立刻逼出了一个真实缺陷：
`KeyboardController.set_game_mode()` 是空操作，界面上的「游戏模式」复选框完全无效
（详见 `TestGameModeSwitching`）。

**离线原则**：测试绝不真的发按键。做法是在 `keycodes` 模块上打桩——
`keyboard.py` / `mouse.py` 都是 `from . import keycodes` 后按属性调用，
所以替换模块属性就能拦住所有真实事件（`import` 的是模块对象，不是函数引用）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from danmaku_kb.input import hotkey as hotkey_mod
from danmaku_kb.input import keycodes
from danmaku_kb.input.hotkey import GlobalHotkey, normalize_hotkey
from danmaku_kb.input.keyboard import KeyboardController
from danmaku_kb.input.mappings import (
    KeyAction,
    KeyMapping,
    MouseAction,
    MouseMapping,
)
from danmaku_kb.input.mouse import WHEEL_DELTA, MouseController

SRC = Path(__file__).resolve().parents[1] / "src"
KEYCODES_PATH = SRC / "danmaku_kb" / "input" / "keycodes.py"


# --------------------------------------------------------------------------
# 打桩：拦住所有真实的按键/鼠标事件
# --------------------------------------------------------------------------


class _Spy:
    """替换 ``keycodes`` 里的发送函数，记录调用而不真的发事件。"""

    SEND_FUNCS = (
        "send_key_sendinput",
        "send_key_directx",
        "send_key_scancode",
        "send_key_keybd_event",
        "send_mouse_button",
        "send_mouse_move",
        "send_mouse_wheel",
        "send_mouse_event",
    )

    def __init__(self, result: bool = True) -> None:
        self.calls: list = []
        self.result = result
        self._saved: dict = {}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "_Spy":
        for name in self.SEND_FUNCS:
            self._saved[name] = getattr(keycodes, name)
            monkeypatch.setattr(keycodes, name, self._make(name))
        return self

    def _make(self, name: str):
        def stub(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self.result

        return stub

    # -- 查询辅助 --------------------------------------------------------

    @property
    def names(self) -> list:
        """被调用的发送函数名序列。"""
        return [c[0] for c in self.calls]

    def count(self, name: str) -> int:
        return sum(1 for n in self.names if n == name)

    def first_args(self, name: str) -> tuple:
        for n, args, _ in self.calls:
            if n == name:
                return args
        raise AssertionError(f"未调用 {name}；实际调用: {self.names}")


@pytest.fixture
def spy(monkeypatch):
    """默认成功、可查询调用的发送桩。"""
    return _Spy(True).install(monkeypatch)


@pytest.fixture
def failing_spy(monkeypatch):
    """所有发送都失败，用来验证失败分支。"""
    return _Spy(False).install(monkeypatch)


@pytest.fixture
def kb(spy):
    """普通模式的键盘控制器（发送已被打桩）。"""
    return KeyboardController()


@pytest.fixture
def mouse(spy):
    """鼠标控制器（发送已被打桩）。"""
    return MouseController()


# --------------------------------------------------------------------------
# 键码表
# --------------------------------------------------------------------------


class TestKeycodeTables:
    """键码表的完整性与自洽性。"""

    def test_letter_and_digit_vk_codes(self):
        assert keycodes.resolve_key("a") == 0x41
        assert keycodes.resolve_key("z") == 0x5A
        assert keycodes.resolve_key("0") == 0x30
        assert keycodes.resolve_key("9") == 0x39

    def test_named_keys(self):
        assert keycodes.resolve_key("enter") == 0x0D
        assert keycodes.resolve_key("space") == 0x20
        assert keycodes.resolve_key("escape") == 0x1B
        assert keycodes.resolve_key("tab") == 0x09
        assert keycodes.resolve_key("backspace") == 0x08
        assert keycodes.resolve_key("shift") == 0x10
        assert keycodes.resolve_key("ctrl") == 0x11
        assert keycodes.resolve_key("alt") == 0x12

    def test_function_keys(self):
        assert keycodes.resolve_key("f1") == 0x70
        assert keycodes.resolve_key("f12") == 0x7B

    def test_arrow_keys(self):
        assert keycodes.resolve_key("up") == 0x26
        assert keycodes.resolve_key("down") == 0x28
        assert keycodes.resolve_key("left") == 0x25
        assert keycodes.resolve_key("right") == 0x27

    def test_case_and_whitespace_insensitive(self):
        assert keycodes.resolve_key("A") == keycodes.resolve_key("a")
        assert keycodes.resolve_key("  Enter  ") == keycodes.resolve_key("enter")

    def test_unknown_key_returns_none(self):
        assert keycodes.resolve_key("这不是键") is None
        assert keycodes.resolve_key("") is None
        assert keycodes.resolve_key(None) is None

    def test_single_alpha_fallback(self):
        """单字母走 ``ord(name.upper())`` 兜底，即使不在 VK_CODES 里。"""
        assert keycodes.resolve_key("a") == ord("A")

    def test_resolve_scan(self):
        assert keycodes.resolve_scan("a") == 0x1E
        assert keycodes.resolve_scan("enter") == 0x1C
        assert keycodes.resolve_scan("space") == 0x39
        assert keycodes.resolve_scan("不存在") is None

    def test_dik_codes_follow_dinput_convention(self):
        """DIK 码遵循 ``dinput.h``：普通键 == 扫描码，扩展键 == 扫描码 | 0x80。

        这是 DIK 的真实约定（``DIK_A`` 就是 ``0x1E``，不是 ``0x9E``），
        早期测试误以为**所有**键都带 ``0x80`` 高位，是错的。
        """
        for key, scan in keycodes._BASE_SCAN_CODES.items():
            assert keycodes.DIK_CODES[key] == scan, f"{key} 是普通键，DIK 应等于扫描码"
        for key, scan in keycodes._EXTENDED_SCAN_CODES.items():
            assert keycodes.DIK_CODES[key] == (scan | 0x80), f"{key} 是扩展键，DIK 应带 0x80 高位"

    def test_dik_matches_known_dinput_constants(self):
        """对照 ``dinput.h`` 里的知名常量，确认表本身没抄错。"""
        for key, expected in (
            ("a", 0x1E),
            ("escape", 0x01),
            ("up", 0xC8),
            ("home", 0xC7),
            ("left", 0xCB),
            ("right", 0xCD),
            ("down", 0xD0),
            ("delete", 0xD3),
            ("insert", 0xD2),
            ("end", 0xCF),
        ):
            assert keycodes.DIK_CODES[key] == expected, (
                f"DIK_{key.upper()} 应为 {hex(expected)}，实际 {hex(keycodes.DIK_CODES[key])}"
            )

    def test_scan_and_dik_cover_the_same_keys(self):
        assert set(keycodes.SCAN_CODES) == set(keycodes.DIK_CODES)
        assert set(keycodes.SCAN_CODES) == set(keycodes._BASE_SCAN_CODES) | set(
            keycodes._EXTENDED_SCAN_CODES
        )

    def test_extended_keys(self):
        for key in ("up", "down", "left", "right", "insert", "delete", "home", "end"):
            assert keycodes.is_extended_key(key), f"{key} 应是扩展键"
        assert not keycodes.is_extended_key("a")
        assert not keycodes.is_extended_key("")

    def test_extended_keys_match_scan_table(self):
        """``EXTENDED_KEYS`` 必须与 ``_EXTENDED_SCAN_CODES`` 的键集完全一致。

        两张手工维护的表必然漂移，而漂移的后果是**静默发错键**：
        ``is_extended_key()`` 返回 False 时，按下/抬起少发
        ``KEYEVENTF_EXTENDEDKEY``，Windows 会把该键解释成另一个键。
        历史缺陷：``EXTENDED_KEYS`` 曾漏掉 ``lwin`` 与 ``divide``。
        """
        assert set(keycodes.EXTENDED_KEYS) == set(keycodes._EXTENDED_SCAN_CODES), (
            f"两表不一致：仅在 EXTENDED_KEYS 里 "
            f"{sorted(set(keycodes.EXTENDED_KEYS) - set(keycodes._EXTENDED_SCAN_CODES))}；"
            f"仅在 _EXTENDED_SCAN_CODES 里 "
            f"{sorted(set(keycodes._EXTENDED_SCAN_CODES) - set(keycodes.EXTENDED_KEYS))}"
        )

    def test_left_windows_key_is_extended(self):
        """``lwin`` 是真实的扩展键，漏掉它会让左 Win 键发成错误的键。"""
        assert keycodes.is_extended_key("lwin") is True
        assert keycodes.is_extended_key("divide") is True

    def test_extended_keys_are_actually_in_scan_table(self):
        """扩展键必须在扫描码表里有对应项，否则发出来的是错的键。"""
        for key in keycodes.EXTENDED_KEYS:
            assert key in keycodes.SCAN_CODES, f"{key} 在 EXTENDED_KEYS 里却不在 SCAN_CODES 里"

    def test_send_methods_registry(self):
        assert set(keycodes.SEND_METHODS) == {
            "sendinput",
            "directx",
            "scancode",
            "keybd_event",
        }


class TestKeycodeSafety:
    """所有发送函数在非 Windows / 未知键下都必须安全返回 False，不抛异常。"""

    @pytest.mark.parametrize(
        "func_name",
        [
            "send_key_sendinput",
            "send_key_directx",
            "send_key_scancode",
            "send_key_keybd_event",
        ],
    )
    def test_unknown_key_returns_false(self, func_name):
        func = getattr(keycodes, func_name)
        assert func("这个键不存在", True) is False

    def test_send_unicode_char_rejects_empty(self):
        assert keycodes.send_unicode_char("") is False

    def test_send_mouse_button_rejects_unknown(self):
        assert keycodes.send_mouse_button("第4个键", True) is False

    def test_get_screen_size_returns_pair(self):
        size = keycodes.get_screen_size()
        assert isinstance(size, tuple) and len(size) == 2
        assert all(isinstance(v, int) for v in size)

    def test_get_cursor_position_returns_pair(self):
        pos = keycodes.get_cursor_position()
        assert isinstance(pos, tuple) and len(pos) == 2

    def test_mouse_flags_cover_three_buttons(self):
        for button in ("left", "right", "middle"):
            assert button in keycodes.MOUSE_FLAGS
            down, up = keycodes.MOUSE_FLAGS[button]
            assert down != up

    def test_module_is_importable_without_windows_api(self):
        """非 Windows 上整个模块仍须可导入（常量占位保证）。"""
        assert hasattr(keycodes, "WINDOWS_API")
        assert hasattr(keycodes, "VK_CODES")


# --------------------------------------------------------------------------
# 键盘控制器
# --------------------------------------------------------------------------


class TestKeyboardController:
    def test_press_release_track_state(self, kb, spy):
        assert kb.press("w") is True
        assert "w" in kb.pressed_keys
        assert kb.release("w") is True
        assert "w" not in kb.pressed_keys

    def test_press_unknown_key_returns_false_without_sending(self, kb, spy):
        assert kb.press("不存在") is False
        assert spy.calls == [], "未知键名不该发出任何事件"

    def test_tap_presses_then_releases(self, kb, spy):
        assert kb.tap("space", duration=0) is True
        assert spy.count("send_key_sendinput") == 2
        # 第一次按下、第二次抬起
        assert spy.calls[0][1][1] is True
        assert spy.calls[1][1][1] is False
        assert kb.pressed_keys == set(), "tap 结束后不该留下按住的键"

    def test_tap_releases_even_when_sleep_interrupted(self, kb, spy, monkeypatch):
        """``tap`` 的 ``finally`` 保证异常时也抬起，不留卡住的键。"""

        def boom(_seconds):
            raise KeyboardInterrupt("模拟中断")

        monkeypatch.setattr("danmaku_kb.input.keyboard.time.sleep", boom)
        with pytest.raises(KeyboardInterrupt):
            kb.tap("w", duration=1.0)
        assert kb.pressed_keys == set(), "异常也必须把键抬起来"
        assert spy.calls[-1][1][1] is False

    def test_release_all_clears_everything(self, kb, spy):
        kb.press("w")
        kb.press("a")
        assert len(kb.pressed_keys) == 2
        kb.release_all()
        assert kb.pressed_keys == set()

    def test_stop_releases_keys(self, kb, spy):
        kb.press("w")
        kb.stop()
        assert kb.pressed_keys == set()

    def test_unknown_key_not_recorded_as_pressed(self, kb, spy):
        kb.press("不存在")
        assert kb.pressed_keys == set()

    def test_execute_skips_disabled_mapping(self, kb, spy):
        mapping = KeyMapping(trigger="w", key="up", enabled=False)
        assert kb.execute(mapping) is False
        assert spy.calls == []

    def test_execute_press_action(self, kb, spy):
        mapping = KeyMapping(trigger="w", key="up", action=KeyAction.PRESS)
        assert kb.execute(mapping) is True
        assert "up" in kb.pressed_keys

    def test_execute_release_action(self, kb, spy):
        mapping = KeyMapping(trigger="w", key="up", action=KeyAction.RELEASE)
        assert kb.execute(mapping) is True

    def test_execute_long_press_uses_mapping_duration(self, kb, spy, monkeypatch):
        slept: list = []
        monkeypatch.setattr("danmaku_kb.input.keyboard.time.sleep", lambda s: slept.append(s))
        mapping = KeyMapping(
            trigger="w", key="up", action=KeyAction.LONG_PRESS, long_press_duration=0.75
        )
        assert kb.execute(mapping) is True
        assert slept == [0.75], f"应睡 long_press_duration，实际 {slept}"

    def test_execute_is_long_press_flag_overrides_action(self, kb, spy, monkeypatch):
        """匹配器判为长按时，即使映射写的是普通动作也走长按。"""
        slept: list = []
        monkeypatch.setattr("danmaku_kb.input.keyboard.time.sleep", lambda s: slept.append(s))
        mapping = KeyMapping(
            trigger="w", key="up", action=KeyAction.PRESS_AND_RELEASE,
            long_press_duration=0.6,
        )
        assert kb.execute(mapping, is_long_press=True) is True
        assert slept == [0.6]

    def test_execute_duration_override(self, kb, spy, monkeypatch):
        slept: list = []
        monkeypatch.setattr("danmaku_kb.input.keyboard.time.sleep", lambda s: slept.append(s))
        mapping = KeyMapping(trigger="w", key="up", action=KeyAction.LONG_PRESS)
        kb.execute(mapping, duration=1.25)
        assert slept == [1.25]

    def test_long_press_has_a_floor(self, kb, spy, monkeypatch):
        """``long_press`` 至少按 0.01 秒，避免 duration=0 时按下即抬起。"""
        slept: list = []
        monkeypatch.setattr("danmaku_kb.input.keyboard.time.sleep", lambda s: slept.append(s))
        kb.long_press("w", duration=0)
        assert slept == [0.01]

    def test_execute_sequence_presses_and_releases_in_reverse(self, kb, spy):
        assert kb.execute_sequence(["ctrl", "shift", "q"], interval=0) is True
        pressed = [c[1][0] for c in spy.calls if c[1][1] is True]
        released = [c[1][0] for c in spy.calls if c[1][1] is False]
        assert pressed == ["ctrl", "shift", "q"]
        assert released == ["q", "shift", "ctrl"], "组合键必须逆序抬起"
        assert kb.pressed_keys == set()

    def test_execute_sequence_empty_is_false(self, kb, spy):
        assert kb.execute_sequence([]) is False

    def test_failed_send_does_not_record_pressed(self, monkeypatch):
        """发送失败时不能把键记成"按住了"，否则 release_all 会去抬一个没按下的键。"""
        _Spy(False).install(monkeypatch)
        controller = KeyboardController()
        assert controller.press("w") is False
        assert controller.pressed_keys == set()

    def test_send_method_dispatch(self, kb, spy):
        """四种后端各自路由到对应的 keycodes 函数。"""
        for method, func in (
            ("sendinput", "send_key_sendinput"),
            ("directx", "send_key_directx"),
            ("scancode", "send_key_scancode"),
            ("keybd_event", "send_key_keybd_event"),
        ):
            kb.set_send_method(method)
            before = len(spy.calls)
            kb.press("w")
            assert spy.names[before] == func, f"后端 {method} 应调 {func}"

    def test_available_matches_platform(self, kb):
        assert kb.available == keycodes.WINDOWS_API


class TestGameModeSwitching:
    """游戏模式必须真的切换发送后端。

    这是**一个真实缺陷**：修复前 ``set_game_mode`` 里写的是
    ``if not self.send_method:``，而 ``__init__`` 早已把 ``send_method``
    填成非空默认值（``"sendinput"``），条件永远为假 —— 于是切换游戏模式
    是**空操作**，界面上的「游戏模式（用扫描码发送，兼容 DirectInput 游戏）」
    复选框完全无效，游戏里依然收不到按键。

    这正是参考项目那个"切回普通模式控制器不重建"缺陷的翻版：
    看起来修了，其实修的是死代码。
    """

    def test_switching_to_game_mode_changes_backend(self, spy):
        kb = KeyboardController()
        assert kb.send_method == "sendinput"
        kb.set_game_mode(True)
        assert kb.game_mode is True
        assert kb.send_method == "directx", "切到游戏模式必须改用扫描码后端"

    def test_switching_back_restores_sendinput(self, spy):
        kb = KeyboardController(game_mode=True)
        assert kb.send_method == "directx"
        kb.set_game_mode(False)
        assert kb.game_mode is False
        assert kb.send_method == "sendinput", "切回普通模式必须改回 SendInput"

    def test_game_mode_actually_routes_to_directx(self, spy):
        """端到端：切模式后真的调用 directx 后端，而不是只改了个字段。"""
        kb = KeyboardController()
        kb.set_game_mode(True)
        kb.press("w")
        assert spy.names == ["send_key_directx"], f"实际调用了 {spy.names}"

    def test_normal_mode_actually_routes_to_sendinput(self, spy):
        kb = KeyboardController(game_mode=True)
        kb.set_game_mode(False)
        kb.press("w")
        assert spy.names == ["send_key_sendinput"]

    def test_round_trip_is_stable(self, spy):
        """来回切多次，后端始终跟 game_mode 一致。"""
        kb = KeyboardController()
        for _ in range(3):
            kb.set_game_mode(True)
            assert kb.send_method == "directx"
            kb.set_game_mode(False)
            assert kb.send_method == "sendinput"

    def test_explicit_send_method_survives_mode_switch(self, spy):
        """显式指定后端时，模式切换不该覆盖用户的选择。"""
        kb = KeyboardController(send_method="keybd_event")
        kb.set_game_mode(True)
        assert kb.send_method == "keybd_event"
        kb.set_game_mode(False)
        assert kb.send_method == "keybd_event"

    def test_set_send_method_marks_it_explicit(self, spy):
        kb = KeyboardController()
        kb.set_send_method("scancode")
        kb.set_game_mode(True)
        assert kb.send_method == "scancode", "set_send_method 之后模式切换不该覆盖"

    def test_same_value_is_a_noop(self, spy):
        kb = KeyboardController()
        kb.set_game_mode(False)
        assert kb.send_method == "sendinput"

    def test_no_dead_condition_in_source(self):
        """源码级守卫：不许再用 ``if not self.send_method`` 判断是否覆盖后端。

        这个条件永远为假（``send_method`` 在 ``__init__`` 里就被填满了），
        是整个缺陷的成因。用 AST 剥掉 docstring 后检查，避免被注释骗过。
        """
        tree = ast.parse(KEYCODES_PATH.parent.joinpath("keyboard.py").read_text(encoding="utf-8"))
        func = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "set_game_mode"
        )
        # 剥掉 docstring
        body = [n for n in func.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
        source = ast.unparse(ast.Module(body=body, type_ignores=[]))
        assert "not self.send_method" not in source, (
            "set_game_mode 里出现了 `if not self.send_method` —— 这个条件永远为假，"
            "会让游戏模式切换变成空操作（历史缺陷）。应该用 _explicit_send_method。"
        )
        assert "_explicit_send_method" in source


# --------------------------------------------------------------------------
# 鼠标控制器
# --------------------------------------------------------------------------


class TestMouseController:
    def test_click_presses_then_releases(self, mouse, spy):
        assert mouse.click("left") is True
        assert spy.count("send_mouse_button") == 2
        assert spy.calls[0][1] == ("left", True)
        assert spy.calls[1][1] == ("left", False)

    def test_double_click_is_two_clicks(self, mouse, spy):
        assert mouse.double_click("right", interval=0.02) is True
        assert spy.count("send_mouse_button") == 4

    def test_press_release_track_state(self, mouse, spy):
        mouse.press("left")
        assert "left" in mouse.pressed_buttons
        mouse.release("left")
        assert "left" not in mouse.pressed_buttons

    def test_long_press_releases_in_finally(self, mouse, spy, monkeypatch):
        monkeypatch.setattr("danmaku_kb.input.mouse.time.sleep", lambda s: None)
        assert mouse.long_press("left", duration=0.5) is True
        assert mouse.pressed_buttons == set()

    def test_long_press_has_a_floor(self, mouse, spy, monkeypatch):
        slept: list = []
        monkeypatch.setattr("danmaku_kb.input.mouse.time.sleep", lambda s: slept.append(s))
        mouse.long_press("left", duration=0)
        assert slept == [0.01]

    def test_move_relative(self, mouse, spy):
        mouse.move(10, -20)
        assert spy.first_args("send_mouse_move") == (10, -20)

    def test_move_absolute_normalizes_to_65535(self, mouse, spy, monkeypatch):
        monkeypatch.setattr(keycodes, "get_screen_size", lambda: (1920, 1080))
        mouse.move(960, 540, absolute=True)
        name, args, kwargs = spy.calls[-1]
        assert name == "send_mouse_move"
        assert args[0] == pytest.approx(32767, abs=2)
        assert args[1] == pytest.approx(32767, abs=2)
        assert kwargs.get("absolute") is True

    def test_move_absolute_clamps_out_of_range(self, mouse, spy, monkeypatch):
        monkeypatch.setattr(keycodes, "get_screen_size", lambda: (1920, 1080))
        mouse.move(-500, 99999, absolute=True)
        args = spy.first_args("send_mouse_move")
        assert args[0] == 0, "负坐标应夹到 0"
        assert args[1] == 65535, "超界坐标应夹到 65535"

    def test_scroll_multiplies_by_wheel_delta(self, mouse, spy):
        mouse.scroll(3)
        assert spy.first_args("send_mouse_wheel") == (3 * WHEEL_DELTA,)
        assert WHEEL_DELTA == 120

    def test_scroll_up_is_positive(self, mouse, spy):
        mouse.scroll_up(2)
        assert spy.first_args("send_mouse_wheel") == (2 * WHEEL_DELTA,)

    def test_scroll_down_is_negative(self, mouse, spy):
        mouse.scroll_down(2)
        assert spy.first_args("send_mouse_wheel") == (-2 * WHEEL_DELTA,)

    def test_scroll_amount_zero_becomes_one(self, mouse, spy):
        """滚 0 格没意义，应兜底成 1 格（否则弹幕触发了却什么都不发生）。"""
        mouse.scroll_up(0)
        assert spy.first_args("send_mouse_wheel") == (WHEEL_DELTA,)

    def test_execute_skips_disabled(self, mouse, spy):
        mapping = MouseMapping(trigger="左键", action=MouseAction.CLICK, enabled=False)
        assert mouse.execute(mapping) is False
        assert spy.calls == []

    @pytest.mark.parametrize(
        "action,expected",
        [
            (MouseAction.CLICK, "send_mouse_button"),
            (MouseAction.DOUBLE_CLICK, "send_mouse_button"),
            (MouseAction.PRESS, "send_mouse_button"),
            (MouseAction.RELEASE, "send_mouse_button"),
            (MouseAction.MOVE, "send_mouse_move"),
            (MouseAction.SCROLL_UP, "send_mouse_wheel"),
            (MouseAction.SCROLL_DOWN, "send_mouse_wheel"),
        ],
    )
    def test_execute_routes_each_action(self, mouse, spy, action, expected):
        mapping = MouseMapping(trigger="t", action=action, scroll_amount=1)
        assert mouse.execute(mapping) is True
        assert expected in spy.names, f"{action} 应调 {expected}，实际 {spy.names}"

    def test_execute_long_press(self, mouse, spy, monkeypatch):
        monkeypatch.setattr("danmaku_kb.input.mouse.time.sleep", lambda s: None)
        mapping = MouseMapping(
            trigger="t", action=MouseAction.LONG_PRESS, long_press_duration=0.4
        )
        assert mouse.execute(mapping) is True

    def test_release_all(self, mouse, spy):
        mouse.press("left")
        mouse.press("right")
        assert len(mouse.pressed_buttons) == 2
        mouse.release_all()
        assert mouse.pressed_buttons == set()

    def test_stop_releases(self, mouse, spy):
        mouse.press("left")
        mouse.stop()
        assert mouse.pressed_buttons == set()

    def test_failed_send_not_recorded(self, monkeypatch):
        _Spy(False).install(monkeypatch)
        controller = MouseController()
        assert controller.press("left") is False
        assert controller.pressed_buttons == set()

    def test_available_matches_platform(self, mouse):
        assert mouse.available == keycodes.WINDOWS_API


# --------------------------------------------------------------------------
# 全局热键
# --------------------------------------------------------------------------


class TestHotkeyNormalization:
    """热键规范化的核心契约：**顺序无关**。

    参考项目的缺陷正是这里：注册时按书写序存、查询时按字母序查，
    ``ctrl+shift+q`` 永远匹配不上。
    """

    def test_order_does_not_matter(self):
        assert normalize_hotkey("ctrl+shift+q") == normalize_hotkey("shift+ctrl+q")
        assert normalize_hotkey("Ctrl+Shift+Q") == normalize_hotkey("q+shift+ctrl")

    def test_case_insensitive(self):
        assert normalize_hotkey("CTRL+Q") == normalize_hotkey("ctrl+q")

    def test_result_is_sorted(self):
        assert normalize_hotkey("shift+ctrl+q") == "ctrl+q+shift"

    def test_aliases_unified(self):
        assert normalize_hotkey("control+q") == normalize_hotkey("ctrl+q")
        assert normalize_hotkey("return+q") == normalize_hotkey("enter+q")
        assert normalize_hotkey("esc+q") == normalize_hotkey("escape+q")

    def test_duplicate_parts_collapse(self):
        assert normalize_hotkey("ctrl+ctrl+q") == normalize_hotkey("ctrl+q")

    def test_empty_returns_empty(self):
        assert normalize_hotkey("") == ""
        assert normalize_hotkey("   ") == ""

    def test_single_key(self):
        assert normalize_hotkey("q") == "q"


class TestGlobalHotkey:
    def test_register_and_list(self):
        hk = GlobalHotkey()
        assert hk.register("ctrl+shift+q") is True
        assert hk.hotkeys == ["ctrl+shift+q"]

    def test_register_rejects_empty(self):
        hk = GlobalHotkey()
        assert hk.register("") is False
        assert hk.hotkeys == []

    def test_register_is_order_insensitive(self):
        hk = GlobalHotkey()
        hk.register("ctrl+shift+q")
        # 同一组合换个写法再注册，应该覆盖同一条而不是新增
        hk.register("shift+ctrl+q")
        assert len(hk.hotkeys) == 1

    def test_unregister(self):
        hk = GlobalHotkey()
        hk.register("ctrl+q")
        assert hk.unregister("q+ctrl") is True, "注销也必须走规范化"
        assert hk.hotkeys == []
        assert hk.unregister("ctrl+q") is False

    def test_clear(self):
        hk = GlobalHotkey()
        hk.register("ctrl+q")
        hk.register("ctrl+w")
        hk.clear()
        assert hk.hotkeys == []

    def test_matches_ignores_order(self):
        hk = GlobalHotkey()
        assert hk.matches({"shift", "ctrl", "q"}, "ctrl+shift+q") is True
        assert hk.matches({"q", "ctrl", "shift"}, "shift+q+ctrl") is True

    def test_matches_rejects_partial(self):
        hk = GlobalHotkey()
        assert hk.matches({"ctrl", "q"}, "ctrl+shift+q") is False
        assert hk.matches({"ctrl", "shift", "q", "alt"}, "ctrl+shift+q") is False

    def test_matches_rejects_empty(self):
        hk = GlobalHotkey()
        assert hk.matches(set(), "ctrl+q") is False

    def test_matches_applies_aliases(self):
        """pynput 报的是 ``ctrl_l``，得能对上用户写的 ``ctrl``。"""
        hk = GlobalHotkey()
        assert hk.matches({"ctrl_l", "shift_r", "q"}, "ctrl+shift+q") is True

    def test_press_triggers_callback(self):
        fired: list = []
        hk = GlobalHotkey(on_trigger=fired.append)
        hk.register("ctrl+q")
        hk._handle_press(_FakeKey("ctrl"))
        hk._handle_press(_FakeKey("q"))
        assert fired == ["ctrl+q"]

    def test_release_then_press_triggers_again(self):
        """松开再按应能再次触发（把防重复间隔关掉，单测状态机）。"""
        fired: list = []
        hk = GlobalHotkey(on_trigger=fired.append)
        hk.repeat_interval = 0
        hk.register("ctrl+q")
        hk._handle_press(_FakeKey("ctrl"))
        hk._handle_press(_FakeKey("q"))
        hk._handle_release(_FakeKey("q"))
        hk._handle_release(_FakeKey("ctrl"))
        hk._handle_press(_FakeKey("ctrl"))
        hk._handle_press(_FakeKey("q"))
        assert len(fired) == 2

    def test_repeat_interval_blocks_immediate_retrigger(self):
        """防重复间隔内的再次触发被抑制（这是刻意的设计，不是缺陷）。"""
        fired: list = []
        hk = GlobalHotkey(on_trigger=fired.append)
        hk.repeat_interval = 10.0
        hk.register("ctrl+q")
        hk._handle_press(_FakeKey("ctrl"))
        hk._handle_press(_FakeKey("q"))
        hk._handle_release(_FakeKey("q"))
        hk._handle_release(_FakeKey("ctrl"))
        hk._handle_press(_FakeKey("ctrl"))
        hk._handle_press(_FakeKey("q"))
        assert len(fired) == 1, "间隔未过，不该重复触发"

    def test_repeat_interval_allows_retrigger_after_elapse(self):
        """间隔过去之后必须能再次触发，否则热键按一次就永久失效。"""
        fired: list = []
        hk = GlobalHotkey(on_trigger=fired.append)
        hk.repeat_interval = 0.5
        hk.register("ctrl+q")
        clock = {"t": 1000.0}
        # hotkey 内部是 `import time` 后调 time.monotonic()，打桩同一个模块对象即可
        import time as _time_mod

        original_monotonic = _time_mod.monotonic
        _time_mod.monotonic = lambda: clock["t"]
        try:
            hk._handle_press(_FakeKey("ctrl"))
            hk._handle_press(_FakeKey("q"))
            hk._handle_release(_FakeKey("q"))
            hk._handle_release(_FakeKey("ctrl"))
            clock["t"] += 1.0  # 超过 0.5 秒
            hk._handle_press(_FakeKey("ctrl"))
            hk._handle_press(_FakeKey("q"))
        finally:
            _time_mod.monotonic = original_monotonic
        assert len(fired) == 2, f"间隔过后应能再触发，实际 {fired}"

    def test_repeat_interval_suppresses_autorepeat(self):
        """按住不放时 Windows 会狂发 press，不能每次都触发。"""
        fired: list = []
        hk = GlobalHotkey(on_trigger=fired.append)
        hk.register("ctrl+q")
        hk._handle_press(_FakeKey("ctrl"))
        hk._handle_press(_FakeKey("q"))
        for _ in range(5):
            hk._handle_press(_FakeKey("q"))
        assert len(fired) == 1, f"防重复失效，触发了 {len(fired)} 次"

    def test_callback_exception_is_isolated(self):
        """回调抛异常不能把监听线程带崩，也不能阻止后续触发。"""
        fired: list = []

        def bad(combo):
            fired.append(combo)
            raise RuntimeError("故意炸")

        hk = GlobalHotkey(on_trigger=bad)
        hk.register("ctrl+q")
        hk._handle_press(_FakeKey("ctrl"))
        hk._handle_press(_FakeKey("q"))  # 不该抛出来
        assert fired == ["ctrl+q"]

    def test_unregistered_combo_does_not_fire(self):
        fired: list = []
        hk = GlobalHotkey(on_trigger=fired.append)
        hk.register("ctrl+q")
        hk._handle_press(_FakeKey("ctrl"))
        hk._handle_press(_FakeKey("w"))
        assert fired == []

    def test_no_callback_does_not_crash(self):
        hk = GlobalHotkey()
        hk.register("ctrl+q")
        hk._handle_press(_FakeKey("ctrl"))
        hk._handle_press(_FakeKey("q"))

    def test_running_is_false_before_start(self):
        hk = GlobalHotkey()
        assert hk.running is False

    def test_stop_is_safe_when_never_started(self):
        hk = GlobalHotkey()
        hk.stop()  # 不该抛
        assert hk.running is False

    def test_start_reports_pynput_availability(self):
        hk = GlobalHotkey()
        hk.register("ctrl+q")
        result = hk.start()
        try:
            assert result == hotkey_mod.PYNPUT_AVAILABLE
        finally:
            hk.stop()

    def test_key_name_handles_various_pynput_shapes(self):
        assert GlobalHotkey._key_name(_FakeKey("q")) == "q"
        assert GlobalHotkey._key_name(_FakeKey("ctrl_l")) == "ctrl"
        assert GlobalHotkey._key_name(_FakeKey("shift_r")) == "shift"
        assert GlobalHotkey._key_name(_FakeKey("cmd")) == "win"

    def test_key_name_handles_string_form(self):
        """pynput 有时给出 ``Key.ctrl_l`` 这样的字符串。"""
        assert GlobalHotkey._key_name("Key.ctrl_l") == "ctrl"
        assert GlobalHotkey._key_name("Key.enter") == "enter"


class _FakeKey:
    """模拟 pynput 的 ``Key`` / ``KeyCode``。

    pynput 的 ``Key.ctrl_l`` 有 ``name`` 属性且无 ``char``；
    ``KeyCode.from_char("q")`` 有 ``char``。这里用同一个类覆盖两种情况。
    """

    def __init__(self, name: str) -> None:
        if len(name) == 1:
            self.char = name
            self.name = None
        else:
            self.char = None
            self.name = name

    def __str__(self) -> str:
        return f"Key.{self.name}" if self.name else str(self.char)
