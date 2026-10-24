# -*- coding: utf-8 -*-
"""Windows 键盘/鼠标底层模拟。

这一层只做一件事：把"按一下 w"变成系统真的收到一次按键。它被
:mod:`screenkey.input.keyboard` 与 :mod:`screenkey.input.mouse` 使用。

为什么需要多种发送方式？因为不同程序接收按键的路径不同：

- ``send_key_sendinput``   —— SendInput，最通用，多数窗口程序有效
- ``send_key_keybd_event`` —— keybd_event（已弃用 API，但兼容老程序）
- ``send_key_scancode``    —— 只发扫描码，DirectInput 游戏常用
- ``send_key_directx``     —— 扫描码 + 虚拟键码，兼容性最好的一种游戏模式

参考项目把这套逻辑散在 ``core/keyboard_simulator.py`` 里，且
``SCAN_CODES`` 与 ``DIK_CODES`` 两张表逐行重复（改一处漏一处）。
这里用一张 ``_BASE_SCAN_CODES`` 派生两张表。

非 Windows 平台上，所有函数返回 ``False``、所有常量给占位值，
保证模块可以被导入（测试能在 Linux/macOS CI 上跑）。
"""

from __future__ import annotations

import os
import sys
import time
from typing import Dict, List, Optional

# ----------------------------------------------------------------------
# Windows API 探测
# ----------------------------------------------------------------------

WINDOWS_API = False
ctypes = None  # type: ignore[assignment]
wintypes = None  # type: ignore[assignment]

if sys.platform == "win32":  # pragma: no cover - 平台相关
    try:
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes

        ctypes = _ctypes
        wintypes = _wintypes
        WINDOWS_API = True
    except Exception:  # pragma: no cover
        WINDOWS_API = False

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_KEYDOWN = 0x0000
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000

MAPVK_VK_TO_VSC = 0
MAPVK_VSC_TO_VK = 1

#: 扩展键：这些键必须带 KEYEVENTF_EXTENDEDKEY 才能被正确识别
#: 扩展键（发送时需要 ``KEYEVENTF_EXTENDEDKEY``）。
#:
#: **不要手工维护这张表**——它在下面由 ``_EXTENDED_SCAN_CODES`` 直接派生
#: （见该表定义处）。原因：两张手工表必然漂移。历史缺陷就是本表曾漏掉
#: ``lwin`` 与 ``divide``（小键盘除号），而 ``_EXTENDED_SCAN_CODES`` 里两者都在；
#: 于是 ``is_extended_key()`` 对它们返回 False，按下/抬起时少发
#: ``KEYEVENTF_EXTENDEDKEY``，Windows 会把该键解释成**另一个键**。
#: ``tests/test_input_layer.py::test_extended_keys_match_scan_table`` 守住这条一致性。
EXTENDED_KEYS: List[str] = []

# ----------------------------------------------------------------------
# 虚拟键码表
# ----------------------------------------------------------------------

VK_CODES: Dict[str, int] = {}

# 字母 a-z → 0x41-0x5A
for _i, _ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    VK_CODES[_ch] = 0x41 + _i

# 数字 0-9 → 0x30-0x39
for _i, _ch in enumerate("0123456789"):
    VK_CODES[_ch] = 0x30 + _i

# 功能键 F1-F12 → 0x70-0x7B
for _i in range(1, 13):
    VK_CODES[f"f{_i}"] = 0x70 + (_i - 1)

VK_CODES.update(
    {
        "backspace": 0x08,
        "tab": 0x09,
        "clear": 0x0C,
        "enter": 0x0D,
        "return": 0x0D,
        "shift": 0x10,
        "ctrl": 0x11,
        "control": 0x11,
        "alt": 0x12,
        "menu": 0x12,
        "pause": 0x13,
        "capslock": 0x14,
        "escape": 0x1B,
        "esc": 0x1B,
        "space": 0x20,
        " ": 0x20,
        "pageup": 0x21,
        "page_up": 0x21,
        "pagedown": 0x22,
        "page_down": 0x22,
        "end": 0x23,
        "home": 0x24,
        "left": 0x25,
        "up": 0x26,
        "right": 0x27,
        "down": 0x28,
        "insert": 0x2D,
        "delete": 0x2E,
        "del": 0x2E,
        "win": 0x5B,
        "lwin": 0x5B,
        "rwin": 0x5C,
        "numpad0": 0x60,
        "numpad1": 0x61,
        "numpad2": 0x62,
        "numpad3": 0x63,
        "numpad4": 0x64,
        "numpad5": 0x65,
        "numpad6": 0x66,
        "numpad7": 0x67,
        "numpad8": 0x68,
        "numpad9": 0x69,
        "multiply": 0x6A,
        "add": 0x6B,
        "subtract": 0x6D,
        "decimal": 0x6E,
        "divide": 0x6F,
        "numlock": 0x90,
        "scrolllock": 0x91,
        "lshift": 0xA0,
        "rshift": 0xA1,
        "lctrl": 0xA2,
        "rctrl": 0xA3,
        "lalt": 0xA4,
        "ralt": 0xA5,
        "semicolon": 0xBA,
        "plus": 0xBB,
        "comma": 0xBC,
        "minus": 0xBD,
        "period": 0xBE,
        "slash": 0xBF,
        "tilde": 0xC0,
        "bracketleft": 0xDB,
        "backslash": 0xDC,
        "bracketright": 0xDD,
        "quote": 0xDE,
    }
)

# ----------------------------------------------------------------------
# 扫描码表
# ----------------------------------------------------------------------

#: 基准扫描码（PS/2 set 1）
_BASE_SCAN_CODES: Dict[str, int] = {
    "escape": 0x01,
    "esc": 0x01,
    "1": 0x02,
    "2": 0x03,
    "3": 0x04,
    "4": 0x05,
    "5": 0x06,
    "6": 0x07,
    "7": 0x08,
    "8": 0x09,
    "9": 0x0A,
    "0": 0x0B,
    "minus": 0x0C,
    "plus": 0x0D,
    "backspace": 0x0E,
    "tab": 0x0F,
    "q": 0x10,
    "w": 0x11,
    "e": 0x12,
    "r": 0x13,
    "t": 0x14,
    "y": 0x15,
    "u": 0x16,
    "i": 0x17,
    "o": 0x18,
    "p": 0x19,
    "bracketleft": 0x1A,
    "bracketright": 0x1B,
    "enter": 0x1C,
    "return": 0x1C,
    "ctrl": 0x1D,
    "control": 0x1D,
    "a": 0x1E,
    "s": 0x1F,
    "d": 0x20,
    "f": 0x21,
    "g": 0x22,
    "h": 0x23,
    "j": 0x24,
    "k": 0x25,
    "l": 0x26,
    "semicolon": 0x27,
    "quote": 0x28,
    "tilde": 0x29,
    "lshift": 0x2A,
    "shift": 0x2A,
    "backslash": 0x2B,
    "z": 0x2C,
    "x": 0x2D,
    "c": 0x2E,
    "v": 0x2F,
    "b": 0x30,
    "n": 0x31,
    "m": 0x32,
    "comma": 0x33,
    "period": 0x34,
    "slash": 0x35,
    "rshift": 0x36,
    "multiply": 0x37,
    "alt": 0x38,
    "space": 0x39,
    " ": 0x39,
    "capslock": 0x3A,
    "f1": 0x3B,
    "f2": 0x3C,
    "f3": 0x3D,
    "f4": 0x3E,
    "f5": 0x3F,
    "f6": 0x40,
    "f7": 0x41,
    "f8": 0x42,
    "f9": 0x43,
    "f10": 0x44,
    "numlock": 0x45,
    "scrolllock": 0x46,
    "f11": 0x57,
    "f12": 0x58,
}

#: 扩展键扫描码（需配合 KEYEVENTF_EXTENDEDKEY）
_EXTENDED_SCAN_CODES: Dict[str, int] = {
    "numpad_enter": 0x1C,
    "rctrl": 0x1D,
    "divide": 0x35,
    "ralt": 0x38,
    "home": 0x47,
    "up": 0x48,
    "pageup": 0x49,
    "left": 0x4B,
    "right": 0x4D,
    "end": 0x4F,
    "down": 0x50,
    "pagedown": 0x51,
    "insert": 0x52,
    "delete": 0x53,
    "win": 0x5B,
    "lwin": 0x5B,
    "rwin": 0x5C,
}

# 由 ``_EXTENDED_SCAN_CODES`` 派生，保证两张表永不失配（原因见顶部注释）。
# 注意 ``lwin`` 与 ``win`` 是同一个物理键的两个别名，两者都要在表里。
EXTENDED_KEYS = list(_EXTENDED_SCAN_CODES)

#: 完整扫描码表
SCAN_CODES: Dict[str, int] = dict(_BASE_SCAN_CODES)
SCAN_CODES.update(_EXTENDED_SCAN_CODES)

#: DirectInput 扫描码（set 1 + 0x80 高位标记，部分游戏引擎要求）
DIK_CODES: Dict[str, int] = {key: value for key, value in _BASE_SCAN_CODES.items()}
DIK_CODES.update(
    {
        "escape": 0x01,
        "esc": 0x01,
        "numpad_enter": 0x9C,
        "rctrl": 0x9D,
        "divide": 0xB5,
        "ralt": 0xB8,
        "home": 0xC7,
        "up": 0xC8,
        "pageup": 0xC9,
        "left": 0xCB,
        "right": 0xCD,
        "end": 0xCF,
        "down": 0xD0,
        "pagedown": 0xD1,
        "insert": 0xD2,
        "delete": 0xD3,
        "win": 0xDB,
        "lwin": 0xDB,
        "rwin": 0xDC,
    }
)

#: 鼠标按键名 → (按下标志, 抬起标志)
MOUSE_FLAGS: Dict[str, tuple] = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

# ----------------------------------------------------------------------
# ctypes 结构体
# ----------------------------------------------------------------------

if WINDOWS_API:  # pragma: no cover - 平台相关

    ULONG_PTR = wintypes.WPARAM

    class KEYBDINPUT(ctypes.Structure):
        """``INPUT`` 的键盘分支。"""

        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        """``INPUT`` 的鼠标分支。"""

        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        """``INPUT`` 的硬件分支（本模块不用，但结构体必须完整）。"""

        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("ki", KEYBDINPUT),
            ("mi", MOUSEINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        """``SendInput`` 的输入结构。"""

        _anonymous_ = ("union",)
        _fields_ = [
            ("type", wintypes.DWORD),
            ("union", _INPUT_UNION),
        ]

else:  # pragma: no cover
    KEYBDINPUT = MOUSEINPUT = HARDWAREINPUT = _INPUT_UNION = INPUT = None  # type: ignore


# ----------------------------------------------------------------------
# 键名解析
# ----------------------------------------------------------------------


def resolve_key(key: str) -> Optional[int]:
    """把键名转成虚拟键码；未知返回 ``None``。"""
    if not key:
        return None
    name = str(key).strip().lower()
    if name in VK_CODES:
        return VK_CODES[name]
    # 单个大写字母
    if len(name) == 1 and name.isalpha():
        return ord(name.upper())
    return None


def is_valid_key_name(name: str) -> bool:
    """``name`` 是不是 ``VK_CODES`` 里认得的键名。

    这是**映射保存时的校验口径**，与 :func:`resolve_key` 刻意不同：
    ``resolve_key`` 还会把任意单个字母按字符码当虚拟键码返回（发送路径要宽松），
    于是 ``"左"`` 这种单个汉字也能"解析成功"。但汉字不是键名，存进映射里
    永远发不出去，所以这里只认表里的名字。
    """

    text = str(name or "")
    if not text:
        return False
    if text.lower() in VK_CODES:
        return True  # 含 " "（单个空格）这个合法别名，不能先 strip 再判
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.lower() in VK_CODES:
        return True
    # 最后再按"空白不参与比较"的口径认一次：搜索框里能搜到 `page down`，
    # 用户照着填也该能存进映射（`page down` / `page_down` / `pagedown` 是同一个键）。
    # 注意这里**不能**把单空格也塌成空串——它在上面已经按合法别名放行了。
    return normalize_key_text(stripped) in VK_CODES


def resolve_scan(key: str) -> Optional[int]:
    """把键名转成扫描码；未知返回 ``None``。"""
    if not key:
        return None
    return SCAN_CODES.get(str(key).strip().lower())


def is_extended_key(key: str) -> bool:
    """该键是否为扩展键。"""
    return str(key or "").strip().lower() in EXTENDED_KEYS


# ----------------------------------------------------------------------
# 底层发送函数
# ----------------------------------------------------------------------


def MapVirtualKey(vk_code: int, map_type: int = MAPVK_VK_TO_VSC) -> int:
    """调用 ``MapVirtualKeyW`` 做键码转换；非 Windows 返回 0。"""
    if not WINDOWS_API:  # pragma: no cover
        return 0
    try:  # pragma: no cover
        return int(ctypes.windll.user32.MapVirtualKeyW(int(vk_code), int(map_type)))
    except Exception:
        return 0


def keybd_event(vk_code: int, scan_code: int, flags: int, extended: bool = False) -> bool:
    """调用 ``keybd_event`` 发送一次按键事件。

    :param vk_code: 虚拟键码（只发扫描码时传 0）
    :param scan_code: 扫描码
    :param flags: ``KEYEVENTF_*`` 组合
    :param extended: 是否追加 ``KEYEVENTF_EXTENDEDKEY``
    """
    if not WINDOWS_API:  # pragma: no cover
        return False
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    try:  # pragma: no cover
        ctypes.windll.user32.keybd_event(int(vk_code), int(scan_code), int(flags), 0)
        return True
    except Exception:
        return False


def _send_input(inp: "INPUT", count: int = 1) -> bool:
    """调用 ``SendInput``。"""
    if not WINDOWS_API:  # pragma: no cover
        return False
    try:  # pragma: no cover
        sent = ctypes.windll.user32.SendInput(
            int(count), ctypes.byref(inp), ctypes.sizeof(INPUT)
        )
        return int(sent) == int(count)
    except Exception:
        return False


def send_key_sendinput(key: str, pressed: bool = True) -> bool:
    """用 ``SendInput`` 发送按键（最通用）。"""
    vk = resolve_key(key)
    if vk is None:
        return False
    if not WINDOWS_API:  # pragma: no cover
        return False
    inp = INPUT()  # pragma: no cover
    inp.type = INPUT_KEYBOARD
    inp.ki.wVk = vk
    inp.ki.wScan = resolve_scan(key) or MapVirtualKey(vk, MAPVK_VK_TO_VSC)
    inp.ki.dwFlags = KEYEVENTF_KEYDOWN if pressed else KEYEVENTF_KEYUP
    inp.ki.time = 0
    inp.ki.dwExtraInfo = 0
    if is_extended_key(key):
        inp.ki.dwFlags |= KEYEVENTF_EXTENDEDKEY
    return _send_input(inp)


def send_key_keybd_event(key: str, pressed: bool = True) -> bool:
    """用 ``keybd_event`` 发送按键（兼容老程序）。"""
    vk = resolve_key(key)
    if vk is None:
        return False
    scan = resolve_scan(key) or MapVirtualKey(vk, MAPVK_VK_TO_VSC)
    flags = KEYEVENTF_KEYDOWN if pressed else KEYEVENTF_KEYUP
    return keybd_event(vk, scan, flags, is_extended_key(key))


def send_key_scancode(key: str, pressed: bool = True) -> bool:
    """只发扫描码（DirectInput 游戏）。

    参考项目用 ``DIK_CODES``（带 0x80 高位）配合
    ``KEYEVENTF_SCANCODE``，实测对多数 DirectInput 游戏有效。
    """
    scan = DIK_CODES.get(str(key or "").strip().lower())
    if scan is None:
        return False
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYDOWN if pressed else KEYEVENTF_KEYUP)
    return keybd_event(0, scan, flags, is_extended_key(key))


def send_key_directx(key: str, pressed: bool = True) -> bool:
    """扫描码 + 虚拟键码一起发（兼容性最好）。

    有些游戏只看扫描码，有些看虚拟键码；两个都填能覆盖更多情况。
    """
    vk = resolve_key(key)
    if vk is None:
        return False
    scan = DIK_CODES.get(str(key or "").strip().lower())
    if scan is None:
        scan = resolve_scan(key) or MapVirtualKey(vk, MAPVK_VK_TO_VSC)
    flags = KEYEVENTF_KEYDOWN if pressed else KEYEVENTF_KEYUP
    return keybd_event(vk, scan, flags, is_extended_key(key))


def send_unicode_char(char: str) -> bool:
    """用 ``KEYEVENTF_UNICODE`` 发送一个 Unicode 字符。

    用于输入中文或符号：这类字符没有虚拟键码，必须走 unicode 路径。
    """
    if not char or not WINDOWS_API:  # pragma: no cover
        return False
    code = ord(char[0])
    ok = True
    for pressed in (True, False):
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ki.wVk = 0
        inp.ki.wScan = code
        inp.ki.dwFlags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYDOWN if pressed else KEYEVENTF_KEYUP)
        inp.ki.time = 0
        inp.ki.dwExtraInfo = 0
        ok = _send_input(inp) and ok
    return ok


def send_mouse_event(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> bool:
    """发送鼠标事件。"""
    if not WINDOWS_API:  # pragma: no cover
        return False
    inp = INPUT()  # pragma: no cover
    inp.type = INPUT_MOUSE
    inp.mi.dx = int(dx)
    inp.mi.dy = int(dy)
    inp.mi.mouseData = int(data) & 0xFFFFFFFF
    inp.mi.dwFlags = int(flags)
    inp.mi.time = 0
    inp.mi.dwExtraInfo = 0
    return _send_input(inp)


def send_mouse_button(button: str, pressed: bool = True) -> bool:
    """按下/抬起鼠标按键。"""
    pair = MOUSE_FLAGS.get(str(button or "").strip().lower())
    if pair is None:
        return False
    return send_mouse_event(pair[0] if pressed else pair[1])


def send_mouse_move(dx: int, dy: int, absolute: bool = False) -> bool:
    """移动鼠标。

    ``absolute=True`` 时 ``dx``/``dy`` 是 0-65535 的归一化屏幕坐标；
    默认是相对位移。
    """
    flags = MOUSEEVENTF_MOVE
    if absolute:
        flags |= MOUSEEVENTF_ABSOLUTE
    return send_mouse_event(flags, dx=dx, dy=dy)


def send_mouse_wheel(amount: int) -> bool:
    """滚动滚轮。正数向上，负数向下（单位 120 = 一格）。"""
    return send_mouse_event(MOUSEEVENTF_WHEEL, data=int(amount))


def get_cursor_position() -> tuple:
    """取当前鼠标位置；失败返回 ``(0, 0)``。"""
    if not WINDOWS_API:  # pragma: no cover
        return (0, 0)
    try:  # pragma: no cover

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        pt = POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return (int(pt.x), int(pt.y))
    except Exception:
        return (0, 0)


def get_screen_size() -> tuple:
    """取主屏幕尺寸；失败返回 ``(1920, 1080)``。"""
    if not WINDOWS_API:  # pragma: no cover
        return (1920, 1080)
    try:  # pragma: no cover
        return (
            int(ctypes.windll.user32.GetSystemMetrics(0)),
            int(ctypes.windll.user32.GetSystemMetrics(1)),
        )
    except Exception:
        return (1920, 1080)


# ----------------------------------------------------------------------
# 注入环境自检
# ----------------------------------------------------------------------
#
# 为什么需要这一段：``SendInput`` 有**两种"报告成功但什么也没发生"**的情况，
# 用户看到的现象完全一样——"日志里明明发了按键，记事本和游戏都没反应"：
#
# 1. 本进程的令牌受限（沙箱、低完整性、被安全软件降权）。MSDN 对 SendInput
#    写得很清楚：被 UIPI 拦下时"GetLastError 与返回值都不会指示失败"。
# 2. 前台窗口跑在**更高**的完整性级别（游戏以管理员身份运行、本程序没提权）
#    ——同一条 UIPI 规则，方向反过来，同样是静默丢弃。
#
# 两种都只能靠比较两边的令牌完整性级别来发现，所以放在这里。

_INTEGRITY_MEDIUM = 0x1000
_INTEGRITY_HIGH = 0x2000
_INTEGRITY_SYSTEM = 0x4000

_TOKEN_QUERY = 0x0008
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_INTEGRITY_LEVEL = 25

# 完整性探测专用的库句柄与"已声明"标志，见 _ensure_integrity_prototypes()。
_integrity_prototypes_ready = False
_kernel32 = None  # type: ignore[var-annotated]
_advapi32 = None  # type: ignore[var-annotated]


def _ensure_integrity_prototypes() -> bool:
    """声明完整性相关 Win32 API 的参数/返回类型；可用返回 ``True``。

    **必须显式声明**，两个理由都真实踩过：

    1. 不声明 ``restype``，ctypes 默认按 ``c_int`` 处理返回值。``GetCurrentProcess()``
       返回的伪句柄是 ``(HANDLE)-1``，被截成 32 位后 ``OpenProcessToken`` 直接失败
       （``ERROR_INVALID_HANDLE``）——于是"读不到自己的令牌"，看起来像受限环境，
       其实是本文件漏了声明，会把完全正常的桌面用户吓一跳（真事：作者本人的正常
       桌面运行就因此收到过一次假的"受限环境"警告）。
    2. ``OpenProcess()`` 返回的句柄在 64 位下同理会被截断，后续 ``OpenProcessToken``
       必然失败。

    另外两点容易忽略：``ctypes.windll`` 与 ``ctypes.WinDLL(...)`` 是**不同的库对象**，
    函数缓存互不相通，所以"声明"与"调用"必须落在同一个对象上；``use_last_error=True``
    才拿得到本次调用的真实错误码（``ctypes.get_last_error()``）。
    """
    global _integrity_prototypes_ready, _kernel32, _advapi32
    if _integrity_prototypes_ready:
        return True
    if not WINDOWS_API:  # pragma: no cover - 平台相关
        return False
    try:  # pragma: no cover - 平台相关
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        adv = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]

        k32.GetCurrentProcess.argtypes = []
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        k32.OpenProcessToken.restype = wintypes.BOOL
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        adv.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        adv.GetTokenInformation.restype = wintypes.BOOL
        adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        adv.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    except Exception:  # pragma: no cover - 平台相关
        return False
    _kernel32 = k32
    _advapi32 = adv
    _integrity_prototypes_ready = True
    return True


if WINDOWS_API:  # pragma: no cover - 平台相关

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        """``SID_AND_ATTRIBUTES``。"""

        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TOKEN_MANDATORY_LABEL(ctypes.Structure):
        """``TOKEN_MANDATORY_LABEL``（完整性级别就装在它的 SID 里）。"""

        _fields_ = [("Label", SID_AND_ATTRIBUTES)]

else:  # pragma: no cover
    SID_AND_ATTRIBUTES = None  # type: ignore[assignment,misc]
    TOKEN_MANDATORY_LABEL = None  # type: ignore[assignment,misc]


def _integrity_from_token(token: int) -> Optional[int]:
    """从令牌句柄读完整性级别（SID 的最后一个 SubAuthority）。"""
    if not _ensure_integrity_prototypes():
        return None
    try:  # pragma: no cover - 平台相关
        size = wintypes.DWORD(0)
        _advapi32.GetTokenInformation(
            token, _TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size)
        )
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(int(size.value))
        if not _advapi32.GetTokenInformation(
            token,
            _TOKEN_INTEGRITY_LEVEL,
            ctypes.cast(buf, ctypes.c_void_p),
            int(size.value),
            ctypes.byref(size),
        ):
            return None
        label = ctypes.cast(buf, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
        sid = label.Label.Sid
        if not sid:
            return None
        count_ptr = _advapi32.GetSidSubAuthorityCount(sid)
        if not count_ptr:
            return None
        count = int(count_ptr.contents.value)
        if count <= 0:
            return None
        last = _advapi32.GetSidSubAuthority(sid, count - 1)
        if not last:
            return None
        return int(last.contents.value)
    except Exception:
        return None


def current_integrity_level() -> Optional[int]:
    """本进程的完整性级别 RID；读不到返回 ``None``。

    ``None`` 有两种来历：真读不到（受限环境），或本机 API 不可用。**不要把
    ``None`` 直接当成"受限"**——"按键到底送没送到"由 :func:`injection_loopback`
    实测回答，那才是权威判据。
    """
    if not _ensure_integrity_prototypes():
        return None
    token = wintypes.HANDLE()
    try:  # pragma: no cover - 平台相关
        opened = bool(
            _kernel32.OpenProcessToken(
                _kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
            )
        )
        if not opened:
            return None
        return _integrity_from_token(token)
    except Exception:
        return None
    finally:  # pragma: no cover - 平台相关
        if token:
            try:
                _kernel32.CloseHandle(token)
            except Exception:
                pass


def process_integrity_level(pid: int) -> Optional[int]:
    """指定进程的完整性级别 RID；读不到（提权进程、已退出）返回 ``None``。"""
    if not pid or not _ensure_integrity_prototypes():  # pragma: no cover
        return None
    handle = None
    token = wintypes.HANDLE()
    try:  # pragma: no cover - 平台相关
        handle = _kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            return None
        if not _kernel32.OpenProcessToken(handle, _TOKEN_QUERY, ctypes.byref(token)):
            return None
        return _integrity_from_token(token)
    except Exception:
        return None
    finally:  # pragma: no cover - 平台相关
        if token:
            try:
                _kernel32.CloseHandle(token)
            except Exception:
                pass
        if handle:
            try:
                _kernel32.CloseHandle(handle)
            except Exception:
                pass


def integrity_level_name(level: Optional[int]) -> str:
    """完整性级别的中文名。"""
    if level is None:
        return "未知"
    if level >= _INTEGRITY_SYSTEM:
        return "系统"
    if level >= _INTEGRITY_HIGH:
        return "高（管理员）"
    if level >= _INTEGRITY_MEDIUM:
        return "中（普通用户）"
    return "低（受限）"


def injection_environment_problem() -> str:
    """本进程自身是否处于"注入了也送不到"的环境；没问题返回 ``""``。

    非 Windows 返回 ``""``：那里的问题由 :data:`WINDOWS_API` 如实报告，
    不该盖成"焦点/权限不对"。

    **读不到完整性级别时返回 ``""``（不报警）**：``None`` 既可能是受限环境，
    也可能只是这台机器上某个 API 没按预期工作，把它当成"受限"会冤枉正常用户
    （真事：本模块曾漏声明 ``restype``，"读不到令牌"在**所有**环境都成立，于是
    正常桌面运行也会弹"受限环境"警告）。权威判据是 :func:`injection_loopback`
    的实测结果——它注入一个必然可见的变化再去读，报告成功却读不回来才算数。
    """
    if not WINDOWS_API:  # pragma: no cover
        return ""
    level = current_integrity_level()
    if level is None:
        return ""
    if level < _INTEGRITY_MEDIUM:
        return (
            f"本进程完整性级别偏低（{integrity_level_name(level)}），"
            "Windows 会静默丢弃注入的按键。请改用普通方式直接运行本程序。"
        )
    return ""


def target_blocks_injection(pid: int) -> str:
    """前台窗口（属于 ``pid``）是否会挡下注入；不会返回 ``""``。"""
    if not WINDOWS_API or not pid:  # pragma: no cover
        return ""
    own = current_integrity_level()
    target = process_integrity_level(pid)
    if own is None or target is None:
        return ""
    if target > own:
        return (
            f"前台窗口以更高权限运行（{integrity_level_name(target)} > 本程序 "
            f"{integrity_level_name(own)}）：Windows 不允许低权限进程向高权限"
            "窗口注入按键，而且不会报错。请右键「以管理员身份运行」本程序。"
        )
    return ""


# ----------------------------------------------------------------------
# 注入回环：不看令牌，直接看"注入的输入有没有真的生效"
# ----------------------------------------------------------------------
#
# 上面两项比的是令牌，能覆盖 UIPI 那一类；但还有一类别的原因会让注入被
# **静默丢弃**，而令牌看起来完全正常——受限宿主（沙箱/自动化宿主）的过滤层、
# 安全软件的输入防护、跑在另一个窗口站/桌面上的进程。它们都不会给调用方
# 任何错误码：SendInput 返回"成功"，GetLastError 是 0，目标窗口什么都收不到。
#
# 唯一的判据是**回环实测**：注入一个必然可见的变化，再去读那个变化。
# 这里用鼠标移动做判据（GetCursorPos 没有"桌面不活动就返回 0"的歧义），
# 键盘（VK_F24 + GetAsyncKeyState）作为辅助信号一起报出来。
#
# 为什么只在 --check-env 里调用：它会真的动一下光标（1 像素，随即复位），
# 属于有副作用的探测，不该在启动或每次连接时偷偷做。

#: 回环探测用的按键：F24 没有任何程序会绑定它，按一下不会打字、不触发快捷键。
_PROBE_VK = 0x87

#: 回环探测的鼠标位移（像素）。取 1 像素：即使被误当成"手抖"，用户也难以察觉。
_PROBE_MOVE = 1


def interpret_injection_loopback(
    send_ok: bool,
    cursor_before: tuple,
    cursor_after: tuple,
    send_error: int = 0,
) -> tuple:
    """把回环实测的原始结果翻译成 ``(是否有效, 说明)``（纯函数，便于单测）。

    :param send_ok: ``SendInput`` 的返回值是否"成功"（非 0）
    :param cursor_before: 注入前光标位置
    :param cursor_after: 注入后光标位置
    :param send_error: 注入后的 ``GetLastError()``
    """

    if not send_ok:
        return (
            False,
            f"SendInput 直接失败（错误码 {send_error}）：注入没有进入系统输入队列。"
            "这通常是被安全软件/输入防护挡下了，请把本程序加入白名单。",
        )
    if tuple(cursor_before) != tuple(cursor_after):
        return (
            True,
            f"注入有效：示意移动 {cursor_before} → {cursor_after}，已复位。"
            "输入确实进入了系统输入队列。",
        )
    return (
        False,
        f"SendInput 报告成功（错误码 {send_error}）、光标却停在 {cursor_before} 没动："
        "**注入被静默丢弃**。本进程运行在受限宿主里（沙箱、自动化宿主、"
        "被安全软件降权的进程），Windows 不会给任何错误提示。"
        "请改用「在 Windows 资源管理器里双击 启动.bat」或直接双击 exe 运行。",
    )


def injection_loopback() -> tuple:
    """真做一次注入回环，返回 ``(是否有效, 说明)``。

    只在 ``--check-env`` 里调用：会移动光标 1 像素并立即复位。非 Windows
    返回 ``(True, "非 Windows 平台，跳过回环探测")``——那里的"发不出去"
    由 :data:`WINDOWS_API` 如实报告，不该盖成环境问题。
    """

    if not WINDOWS_API:  # pragma: no cover
        return (True, "非 Windows 平台，跳过回环探测")

    before = get_cursor_position()
    result = (True, "回环探测无法完成，已跳过")
    try:  # pragma: no cover - 真实注入路径
        screen_w, screen_h = get_screen_size()
        # 朝屏幕内侧移动，避免光标贴边时被夹取导致"没动"的假阴性
        dx = -_PROBE_MOVE if before[0] >= screen_w - _PROBE_MOVE - 1 else _PROBE_MOVE
        dy = -_PROBE_MOVE if before[1] >= screen_h - _PROBE_MOVE - 1 else _PROBE_MOVE

        send_ok, send_error = _probe_send_mouse_move(dx, dy)
        time.sleep(0.05)
        after = get_cursor_position()
        keyboard_ok = _keyboard_loopback()

        result = interpret_injection_loopback(send_ok, before, after, send_error)
        if result[0] and not keyboard_ok:
            # 鼠标能动、键盘观察不到：仍然算可用，但要如实说出来
            result = (
                True,
                result[1] + "（注意：键盘回环没观察到按下状态，若按键仍无效请换一种发送方式）",
            )
    except Exception as exc:  # pragma: no cover - 探测自身不能抛
        result = (True, f"回环探测无法完成（{type(exc).__name__}: {exc}），已跳过")
    finally:  # pragma: no cover - 无论如何把光标送回原位
        try:
            if get_cursor_position() != before:
                send_mouse_move(before[0], before[1], absolute=True)
        except Exception:
            pass
    return result


def _probe_send_mouse_move(dx: int, dy: int) -> tuple:
    """发一次相对鼠标移动，返回 ``(是否成功, GetLastError)``。

    自己调 ``SendInput`` 而不是复用 :func:`send_mouse_move`：那个函数的
    ``windll`` 句柄没开 ``use_last_error``，拿不到本次调用的真实错误码。
    """

    try:  # pragma: no cover - 真实注入路径
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.mi.dx = int(dx)
        inp.mi.dy = int(dy)
        inp.mi.mouseData = 0
        inp.mi.dwFlags = MOUSEEVENTF_MOVE
        inp.mi.time = 0
        inp.mi.dwExtraInfo = 0
        ctypes.set_last_error(0)
        sent = int(user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)))
        return (sent == 1, int(ctypes.get_last_error()))
    except Exception:  # pragma: no cover
        return (False, -1)


def _keyboard_loopback() -> bool:
    """注入一次 VK_F24 按下，看 ``GetAsyncKeyState`` 能不能观察到；随后抬起。"""

    try:  # pragma: no cover - 真实注入路径
        user32 = ctypes.windll.user32
        down = INPUT()
        down.type = INPUT_KEYBOARD
        down.ki.wVk = _PROBE_VK
        down.ki.wScan = 0
        down.ki.dwFlags = KEYEVENTF_KEYDOWN
        down.ki.time = 0
        down.ki.dwExtraInfo = 0
        up = INPUT()
        up.type = INPUT_KEYBOARD
        up.ki.wVk = _PROBE_VK
        up.ki.wScan = 0
        up.ki.dwFlags = KEYEVENTF_KEYUP
        up.ki.time = 0
        up.ki.dwExtraInfo = 0

        user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
        time.sleep(0.02)
        state = int(user32.GetAsyncKeyState(_PROBE_VK)) & 0xFFFF
        user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
        return bool(state & 0x8000)
    except Exception:  # pragma: no cover
        return False


#: 已知会限制输入注入的宿主标记（环境变量名 → 给用户看的名字）。
#: 命中不代表一定失败，但配合回环结果就能把"为什么发不出去"说清楚。
_RESTRICTED_HOST_MARKERS = (
    ("DSH_SESSION_ID", "DSH 会话宿主"),
    ("DSH_SHELL", "DSH 宿主 shell"),
    ("SANDBOXIE", "Sandboxie"),
    ("SNAPCHAT_IN_SANDBOX", "Snapchat 沙箱"),
)


def restricted_host_markers() -> str:
    """返回命中的"受限宿主"标记描述；没有命中返回 ``""``。

    这是**提示性**判据：宿主进程可能被限制、也可能没有。真正的判据是
    :func:`injection_loopback`。
    """

    try:
        hits = [
            f"{name}（{label}）"
            for name, label in _RESTRICTED_HOST_MARKERS
            if os.environ.get(name)
        ]
    except Exception:  # pragma: no cover - os.environ 极不可能抛
        return ""
    return "、".join(hits)


# ----------------------------------------------------------------------
# 按键目录：给界面用的「全部可选按键 + 中文名 + 搜索」
# ----------------------------------------------------------------------
#
# 界面里填按键不该逼用户背下 ``VK_CODES`` 的键名，所以这里提供三个纯函数：
# 全部可选键、按键的中文说明、按内容搜键。它们**不依赖 Qt**，界面层只负责显示，
# 这样"搜索规则"能被离线测试逐条钉住（见 ``tests/test_key_selector.py``）。

#: 有中文说明的按键；字母/数字/功能键/小键盘数字由 :func:`key_label` 生成
_KEY_LABELS: Dict[str, str] = {
    " ": "空格",
    "add": "小键盘 +",
    "alt": "Alt",
    "backslash": "反斜杠 \\",
    "backspace": "退格",
    "bracketleft": "左方括号 [",
    "bracketright": "右方括号 ]",
    "capslock": "大写锁定 CapsLock",
    "clear": "清除 Clear",
    "comma": "逗号 ,",
    "control": "Ctrl（组合键写法）",
    "ctrl": "Ctrl",
    "decimal": "小键盘 .",
    "del": "Delete",
    "delete": "Delete",
    "divide": "小键盘 /",
    "down": "方向键 ↓",
    "end": "End（行尾）",
    "enter": "回车 Enter",
    "esc": "Esc",
    "escape": "Esc",
    "home": "Home（行首）",
    "insert": "Insert",
    "lalt": "左 Alt",
    "lctrl": "左 Ctrl",
    "left": "方向键 ←",
    "lshift": "左 Shift",
    "lwin": "左 Win",
    "menu": "菜单键（右键菜单）",
    "minus": "减号 -",
    "multiply": "小键盘 *",
    "numlock": "数字锁定 NumLock",
    "page_down": "下翻页 PageDown",
    "page_up": "上翻页 PageUp",
    "pagedown": "下翻页 PageDown",
    "pageup": "上翻页 PageUp",
    "pause": "暂停 Pause",
    "period": "句号 .",
    "plus": "加号 +",
    "quote": "引号 '",
    "ralt": "右 Alt",
    "rctrl": "右 Ctrl",
    "return": "回车 Enter",
    "right": "方向键 →",
    "rshift": "右 Shift",
    "rwin": "右 Win",
    "scrolllock": "滚动锁定 ScrollLock",
    "semicolon": "分号 ;",
    "shift": "Shift",
    "slash": "斜杠 /",
    "space": "空格",
    "subtract": "小键盘 -",
    "tab": "Tab 制表",
    "tilde": "波浪号 ~",
    "up": "方向键 ↑",
    "win": "Win 键",
}


def all_key_names() -> List[str]:
    """全部可作为映射目标的按键名（已排序）。

    不含 ``" "``（单个空格）这个别名：它是 ``space`` 的另一种写法，留在下拉里
    会显示成一个空白项，看起来像坏数据。手填 ``" "`` 仍然有效。
    """

    return sorted(name for name in VK_CODES if name.strip())


def key_label(name: str) -> str:
    """按键的中文说明；没有对应说明时返回空字符串。"""

    raw = str(name or "")
    text = raw.strip().lower()
    if not text:
        # 单个空格是空格键的合法别名（见 _KEY_LABELS）
        return "空格" if raw == " " else ""

    label = _KEY_LABELS.get(text)
    if label:
        return label
    if len(text) == 1 and text.isalpha():
        return f"字母 {text.upper()}"
    if len(text) == 1 and text.isdigit():
        return f"数字 {text}"
    if text.startswith("numpad") and text[6:].isdigit():
        return f"小键盘 {text[6:]}"
    if text.startswith("f") and text[1:].isdigit():
        return f"{text.upper()} 功能键"
    return ""


def key_display(name: str) -> str:
    """下拉列表里显示的文字：``space　空格（Space）`` 这种「键名 + 中文说明」。"""

    label = key_label(name)
    return f"{name}　{label}" if label else str(name)


def normalize_key_text(text: str) -> str:
    """搜索用的归一化：去掉所有空白并转小写。

    ``左 Shift`` 与 ``左Shift`` 要能搜到同一个键，所以空白一律不参与比较；
    只做这两步，不碰其它字符（中文、括号、``+`` 都原样保留）。
    """

    return "".join(str(text or "").split()).lower()


def search_key_names(query: str, limit: int = 0) -> List[str]:
    """按内容搜索按键名，返回按相关度排序的结果。

    匹配范围是**键名本身**和**中文说明**，大小写与空白不敏感；顺序是
    完全相等 → 前缀 → 键名包含 → 中文说明包含，同档内按名字排序。
    ``limit > 0`` 时只返回前 ``limit`` 条。
    """

    names = all_key_names()
    text = str(query or "").strip()
    if not text:
        return names[:limit] if limit > 0 else names

    needle = normalize_key_text(text)
    if not needle:  # 纯空白输入按"没输入"处理
        return names[:limit] if limit > 0 else names

    exact: List[str] = []
    prefix: List[str] = []
    in_name: List[str] = []
    in_label: List[str] = []
    for name in names:
        lowered = normalize_key_text(name)
        if lowered == needle:
            exact.append(name)
        elif lowered.startswith(needle):
            prefix.append(name)
        elif needle in lowered:
            in_name.append(name)
        elif needle in normalize_key_text(key_label(name)):
            in_label.append(name)

    ranked = exact + prefix + sorted(in_name) + sorted(in_label)
    return ranked[:limit] if limit > 0 else ranked


#: 可选的按键发送后端，供设置界面展示
SEND_METHODS = {
    "sendinput": "SendInput（推荐，通用）",
    "keybd_event": "keybd_event（兼容旧程序）",
    "scancode": "扫描码（DirectInput 游戏）",
    "directx": "扫描码+虚拟键码（游戏模式）",
}


__all__ = [
    "DIK_CODES",
    "EXTENDED_KEYS",
    "INPUT",
    "KEYBDINPUT",
    "MOUSEINPUT",
    "SEND_METHODS",
    "SCAN_CODES",
    "VK_CODES",
    "WINDOWS_API",
    "all_key_names",
    "current_integrity_level",
    "get_cursor_position",
    "get_screen_size",
    "injection_environment_problem",
    "injection_loopback",
    "integrity_level_name",
    "interpret_injection_loopback",
    "is_extended_key",
    "is_valid_key_name",
    "key_display",
    "key_label",
    "keybd_event",
    "normalize_key_text",
    "process_integrity_level",
    "resolve_key",
    "resolve_scan",
    "restricted_host_markers",
    "search_key_names",
    "send_key_directx",
    "send_key_keybd_event",
    "send_key_scancode",
    "send_key_sendinput",
    "send_mouse_button",
    "send_mouse_event",
    "send_mouse_move",
    "send_mouse_wheel",
    "send_unicode_char",
    "target_blocks_injection",
]
