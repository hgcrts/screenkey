# -*- coding: utf-8 -*-
"""鼠标动作执行器。

把 :class:`~screenkey.input.mappings.MouseMapping` 变成真实鼠标事件。
与 :class:`~screenkey.input.keyboard.KeyboardController` 对称：
同样有锁、同样支持中断释放、同样在非 Windows 上安全降级。
"""

from __future__ import annotations

import threading
import time
from typing import Optional, Set

from ..utils.logging_setup import get_logger
from . import keycodes
from .mappings import MouseAction, MouseMapping

logger = get_logger("input.mouse")

#: 一格滚轮的标准单位
WHEEL_DELTA = 120


class MouseController:
    """鼠标模拟控制器。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pressed: Set[str] = set()
        self._stopping = False

    # ------------------------------------------------------------------
    # 基础动作
    # ------------------------------------------------------------------

    def click(self, button: str = "left") -> bool:
        """单击。"""
        with self._lock:
            if not self.press(button):
                return False
            time.sleep(0.02)
            return self.release(button)

    def double_click(self, button: str = "left", interval: float = 0.08) -> bool:
        """双击。"""
        with self._lock:
            if not self.click(button):
                return False
            time.sleep(max(0.02, interval))
            return self.click(button)

    def press(self, button: str = "left") -> bool:
        """按下。

        ``_stopping`` 以前只写不读：:meth:`stop` 之后另一个线程里的
        ``click``/``execute`` 照样把左键按下去，退出后鼠标左键就卡在按下状态。
        """
        with self._lock:
            if self._stopping:
                logger.debug("控制器已停止，忽略按下: %r", button)
                return False
            ok = keycodes.send_mouse_button(button, True)
            if ok:
                self._pressed.add(str(button).strip().lower())
            return ok

    def release(self, button: str = "left") -> bool:
        """抬起。"""
        with self._lock:
            ok = keycodes.send_mouse_button(button, False)
            self._pressed.discard(str(button).strip().lower())
            return ok

    def long_press(self, button: str = "left", duration: float = 0.5) -> bool:
        """长按。"""
        with self._lock:
            if not self.press(button):
                return False
            try:
                time.sleep(max(0.01, float(duration)))
            finally:
                self.release(button)
            return True

    def move(self, dx: int = 0, dy: int = 0, absolute: bool = False) -> bool:
        """移动鼠标。

        ``absolute=False``（默认）时 ``dx``/``dy`` 是相对位移；
        ``True`` 时按屏幕尺寸归一化成 0-65535。
        """
        if absolute:
            screen_w, screen_h = keycodes.get_screen_size()
            abs_x = int(max(0, min(65535, (dx / max(1, screen_w)) * 65535)))
            abs_y = int(max(0, min(65535, (dy / max(1, screen_h)) * 65535)))
            return keycodes.send_mouse_move(abs_x, abs_y, absolute=True)
        return keycodes.send_mouse_move(int(dx), int(dy))

    def move_relative(self, dx: int, dy: int) -> bool:
        """相对移动。"""
        return self.move(dx, dy, absolute=False)

    def scroll(self, amount: int) -> bool:
        """滚动；正数向上，负数向下（1 = 一格）。"""
        return keycodes.send_mouse_wheel(int(amount) * WHEEL_DELTA)

    def scroll_up(self, amount: int = 1) -> bool:
        """向上滚。"""
        return self.scroll(abs(int(amount)) or 1)

    def scroll_down(self, amount: int = 1) -> bool:
        """向下滚。"""
        return self.scroll(-(abs(int(amount)) or 1))

    # ------------------------------------------------------------------
    # 执行映射
    # ------------------------------------------------------------------

    def execute(
        self,
        mapping: MouseMapping,
        *,
        is_long_press: bool = False,
        duration: Optional[float] = None,
    ) -> bool:
        """执行一条鼠标映射。"""
        if not mapping.enabled:
            return False

        button = mapping.button.value
        action = mapping.action

        if is_long_press or action is MouseAction.LONG_PRESS:
            hold = float(duration if duration is not None else mapping.long_press_duration)
            return self.long_press(button, hold)

        if action is MouseAction.CLICK:
            return self.click(button)
        if action is MouseAction.DOUBLE_CLICK:
            return self.double_click(button)
        if action is MouseAction.PRESS:
            return self.press(button)
        if action is MouseAction.RELEASE:
            return self.release(button)
        if action is MouseAction.MOVE:
            return self.move_relative(int(mapping.move_x), int(mapping.move_y))
        if action is MouseAction.SCROLL_UP:
            return self.scroll_up(int(mapping.scroll_amount))
        if action is MouseAction.SCROLL_DOWN:
            return self.scroll_down(int(mapping.scroll_amount))

        logger.debug("未处理的鼠标动作: %s", action)
        return False

    # ------------------------------------------------------------------
    # 安全
    # ------------------------------------------------------------------

    def release_all(self) -> None:
        """抬起所有按住的鼠标键。

        抬起**不受** ``_stopping`` 影响（:meth:`press` 才拦），否则停止后
        已经按下的键永远松不开。
        """
        with self._lock:
            for name in list(self._pressed):
                keycodes.send_mouse_button(name, False)
            self._pressed.clear()

    def stop(self) -> None:
        """停止并释放。

        先置 ``_stopping`` 再释放：反过来的话另一个线程里跑到一半的
        ``press()`` 会在 release_all 之后才按下，鼠标键就卡住了。
        """
        with self._lock:
            self._stopping = True
        self.release_all()

    def start(self) -> None:
        """恢复。"""
        self._stopping = False

    @property
    def stopping(self) -> bool:
        """是否已停止（不再接受新的鼠标动作）。"""
        return self._stopping

    @property
    def pressed_buttons(self) -> Set[str]:
        """当前按住的鼠标键。"""
        with self._lock:
            return set(self._pressed)

    @property
    def available(self) -> bool:
        """当前平台是否支持鼠标模拟。"""
        return keycodes.WINDOWS_API


__all__ = ["MouseController", "WHEEL_DELTA"]
