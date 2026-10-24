# -*- coding: utf-8 -*-
"""全局热键。

用于"暂停/恢复按键模拟"这类需要在游戏里也能生效的快捷键。

参考项目在这里踩了一个经典坑：注册热键时按键按**书写顺序**存入集合，
查询时用**字母序**排序后查表，导致 ``ctrl+shift+q`` 永远匹配不上
（存的是 ``("ctrl","shift","q")``，查的是 ``("ctrl","q","shift")``）。
修法是统一走 ``normalize_hotkey``（本模块的做法：排序后拼字符串）。

实现基于 ``pynput``，它能在 Windows 上全局监听键盘。
非 Windows 或 pynput 缺失时，本模块静默降级为"不可用"。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional, Set

from ..utils.helpers import normalize_key_name, parse_key_combo
from ..utils.logging_setup import get_logger

logger = get_logger("input.hotkey")

try:  # pragma: no cover - 依赖环境
    from pynput import keyboard as _pynput_keyboard

    PYNPUT_AVAILABLE = True
except Exception:  # pragma: no cover
    _pynput_keyboard = None  # type: ignore[assignment]
    PYNPUT_AVAILABLE = False


#: pynput 的按键名与我们的键名之间的映射
_KEY_ALIASES: Dict[str, str] = {
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "ctrl": "ctrl",
    "shift_l": "shift",
    "shift_r": "shift",
    "shift": "shift",
    "alt_l": "alt",
    "alt_r": "alt",
    "alt_gr": "alt",
    "alt": "alt",
    "cmd": "win",
    "cmd_l": "win",
    "cmd_r": "win",
    "enter": "enter",
    "return": "enter",
    "space": "space",
    "esc": "escape",
    "tab": "tab",
    "backspace": "backspace",
}


def normalize_hotkey(combo: str) -> str:
    """把热键组合规整成可比较的规范形式。

    ``"Ctrl+Shift+Q"`` 与 ``"shift+ctrl+q"`` 都变成 ``"ctrl+shift+q"``
    （按键名排序后用 ``+`` 连接）。**所有**存取热键的地方都必须过这个函数，
    否则就会出现参考项目那种"注册了却永不触发"的 bug。

    重复的键会被合并：``"ctrl+control+q"`` → ``"ctrl+q"``。
    否则会注册出一个**永远不可能命中**的热键——触发判定用的是集合
    （``_handle_press`` 里 ``"+".join(sorted(self._pressed))``），
    集合里 ``ctrl`` 只会出现一次，而规范形式却含两个 ``ctrl``。
    """
    parts = parse_key_combo(combo)
    canonical = sorted({_KEY_ALIASES.get(p, normalize_key_name(p)) for p in parts})
    return "+".join(p for p in canonical if p)


class GlobalHotkey:
    """全局热键监听器。

    :param on_trigger: 热键触发时的回调（在 pynput 的监听线程里调用）
    """

    def __init__(self, on_trigger: Optional[Callable[[str], None]] = None) -> None:
        self._on_trigger = on_trigger
        self._hotkeys: Dict[str, str] = {}   # 规范形式 → 原始写法
        self._pressed: Set[str] = set()
        self._listener: Any = None
        self._lock = threading.RLock()
        self._last_fired: Dict[str, float] = {}
        #: 同一热键的最小重复触发间隔（秒），防止长按重复触发
        self.repeat_interval = 0.5

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register(self, combo: str) -> bool:
        """注册一个热键；返回是否成功（格式非法时 False）。"""
        canonical = normalize_hotkey(combo)
        if not canonical:
            logger.warning("热键格式非法，忽略: %r", combo)
            return False
        with self._lock:
            self._hotkeys[canonical] = combo
        logger.info("已注册热键: %s (规范形式 %s)", combo, canonical)
        return True

    def unregister(self, combo: str) -> bool:
        """注销热键。"""
        canonical = normalize_hotkey(combo)
        with self._lock:
            return self._hotkeys.pop(canonical, None) is not None

    def clear(self) -> None:
        """清空全部热键。"""
        with self._lock:
            self._hotkeys.clear()
            self._pressed.clear()

    @property
    def hotkeys(self) -> List[str]:
        """已注册的热键（原始写法）。"""
        with self._lock:
            return list(self._hotkeys.values())

    # ------------------------------------------------------------------
    # 启动/停止
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """开始监听。"""
        if not PYNPUT_AVAILABLE:
            logger.warning("pynput 不可用，全局热键功能关闭")
            return False
        with self._lock:
            if self._listener is not None:
                return True
            try:
                self._listener = _pynput_keyboard.Listener(
                    on_press=self._handle_press,
                    on_release=self._handle_release,
                )
                self._listener.daemon = True
                self._listener.start()
                logger.info("全局热键监听已启动，共 %d 个热键", len(self._hotkeys))
                return True
            except Exception as exc:
                logger.error("启动热键监听失败: %s", exc)
                self._listener = None
                return False

    def stop(self) -> None:
        """停止监听。"""
        with self._lock:
            listener = self._listener
            self._listener = None
            self._pressed.clear()
        if listener is not None:
            try:
                listener.stop()
            except Exception as exc:  # pragma: no cover
                logger.debug("停止热键监听时出错: %s", exc)

    @property
    def running(self) -> bool:
        """是否在监听。"""
        return self._listener is not None

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    @staticmethod
    def _key_name(key: Any) -> str:
        """把 pynput 的 Key/KeyCode 转成我们的键名。"""
        try:
            if hasattr(key, "char") and key.char:
                return str(key.char).lower()
            name = getattr(key, "name", None)
            if name:
                return _KEY_ALIASES.get(str(name).lower(), str(name).lower())
            # 形如 "Key.ctrl_l"
            text = str(key)
            if text.startswith("Key."):
                raw = text[4:].lower()
                return _KEY_ALIASES.get(raw, raw)
            return text.lower()
        except Exception:  # pragma: no cover
            return ""

    def _handle_press(self, key: Any) -> None:
        """按键按下。"""
        name = self._key_name(key)
        if not name:
            return
        with self._lock:
            self._pressed.add(name)
            # 规范形式同样是排序后的集合
            current = "+".join(sorted(self._pressed))
            combo = self._hotkeys.get(current)
        if combo is None:
            return

        # 防重复：长按不重复触发
        import time

        now = time.monotonic()
        if now - self._last_fired.get(current, 0.0) < self.repeat_interval:
            return
        self._last_fired[current] = now

        logger.info("热键触发: %s", combo)
        if self._on_trigger is not None:
            try:
                self._on_trigger(combo)
            except Exception as exc:  # pragma: no cover
                logger.exception("热键回调异常（已隔离）: %s", exc)

    def _handle_release(self, key: Any) -> None:
        """按键抬起。"""
        name = self._key_name(key)
        if not name:
            return
        with self._lock:
            self._pressed.discard(name)

    # ------------------------------------------------------------------
    # 自检
    # ------------------------------------------------------------------

    def matches(self, pressed_keys: Set[str], combo: str) -> bool:
        """给定当前按下的键，判断是否满足某热键。

        这个方法把"集合比较"的逻辑集中在一处，测试可以直接调用，
        不需要真的模拟键盘事件。
        """
        expected = set(normalize_hotkey(combo).split("+")) - {""}
        actual = {_KEY_ALIASES.get(k.lower(), normalize_key_name(k)) for k in pressed_keys}
        return expected == actual and bool(expected)


__all__ = ["GlobalHotkey", "PYNPUT_AVAILABLE", "normalize_hotkey"]
