# -*- coding: utf-8 -*-
"""键盘动作执行器。

把 :class:`~screenkey.input.mappings.KeyMapping` 变成真实按键。
支持四种动作与四种发送后端（见 :mod:`screenkey.input.keycodes`）。

设计要点：

- **线程安全**：动作可能来自弹幕线程，也可能来自界面"测试按键"按钮，
  内部用一把锁串行化，避免长按的"按下"与"抬起"交错
- **长按可中断**：长按期间若收到 :meth:`stop`，立即抬起所有按键，
  否则会留下"卡住的键"（参考项目曾出现按住 W 不松的问题）
- **失败可见**：非 Windows 或未知键名返回 ``False``，并记 debug 日志，
  不抛异常打断弹幕处理流水线
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional, Set

from ..utils.logging_setup import get_logger
from . import keycodes
from .mappings import KeyAction, KeyMapping

logger = get_logger("input.keyboard")


class KeyboardController:
    """按键模拟控制器。"""

    def __init__(self, game_mode: bool = False, send_method: str = "") -> None:
        """
        :param game_mode: 为 True 时默认走扫描码（兼容 DirectInput 游戏）
        :param send_method: 显式指定后端；空字符串表示按 ``game_mode`` 自动选
        """
        self._lock = threading.RLock()
        self._pressed: Set[str] = set()
        self._stopping = False
        self.game_mode = bool(game_mode)
        #: 调用方是否显式指定过后端。显式指定时模式切换不覆盖它。
        self._explicit_send_method = bool(send_method)
        self.send_method = send_method or ("directx" if self.game_mode else "sendinput")

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def set_game_mode(self, enabled: bool) -> None:
        """切换游戏模式。

        参考项目踩过的坑：切回普通模式时没有重建控制器，仍然发扫描码。
        这里把后端一并重置。

        **注意**：判断"是否要改后端"用的是 ``_explicit_send_method``
        而不是 ``self.send_method``。后者在 ``__init__`` 里就已经被填上了
        非空默认值，拿它做条件会永远为假，切换模式就变成空操作——
        游戏模式复选框会彻底失效（这个缺陷真实存在过）。
        """
        with self._lock:
            if self.game_mode == bool(enabled):
                return
            self.game_mode = bool(enabled)
            if not self._explicit_send_method:
                self.send_method = "directx" if self.game_mode else "sendinput"
            logger.info("键盘控制器模式切换: game_mode=%s, 后端=%s", self.game_mode, self.send_method)

    def set_send_method(self, method: str) -> None:
        """设置发送后端。

        显式设置后，:meth:`set_game_mode` 不再覆盖它。
        """
        with self._lock:
            self.send_method = method or "sendinput"
            self._explicit_send_method = bool(method)

    # ------------------------------------------------------------------
    # 单次按键
    # ------------------------------------------------------------------

    def _send(self, key: str, pressed: bool, force: bool = False) -> bool:
        """按当前后端发送一次按下/抬起。

        :param force: 为 True 时忽略 :attr:`_stopping`（只给 release_all 用，
            停止时必须仍能把已经按住的键松开）。

        ``_stopping`` 以前全项目只写不读 —— :meth:`stop` 把它置 True 之后，
        正在引擎线程里执行的 ``execute``/``tap`` 照样把键按下去。程序退出时
        ``release_all()`` 与 ``press()`` 会互相竞态，结果在系统里留下一批
        物理按住的键（实测到的"卡键"）。

        只有**按下**会被拦截；抬起永远放行 —— 拦抬起等于把键永久留在按下状态。
        """
        if pressed and self._stopping and not force:
            logger.debug("控制器已停止，忽略按下: %r", key)
            return False
        method = self.send_method
        if method == "scancode":
            return keycodes.send_key_scancode(key, pressed)
        if method == "directx":
            return keycodes.send_key_directx(key, pressed)
        if method == "keybd_event":
            return keycodes.send_key_keybd_event(key, pressed)
        return keycodes.send_key_sendinput(key, pressed)

    def press(self, key: str) -> bool:
        """按下按键（不松）。"""
        with self._lock:
            if keycodes.resolve_key(key) is None:
                logger.debug("未知按键名，忽略: %r", key)
                return False
            ok = self._send(key, True)
            if ok:
                self._pressed.add(str(key).strip().lower())
            return ok

    def release(self, key: str) -> bool:
        """松开按键。"""
        with self._lock:
            name = str(key).strip().lower()
            ok = self._send(key, False)
            self._pressed.discard(name)
            return ok

    def tap(self, key: str, duration: float = 0.02) -> bool:
        """按一下再松开。"""
        with self._lock:
            if not self.press(key):
                return False
            try:
                if duration > 0:
                    time.sleep(duration)
            finally:
                self.release(key)
            return True

    def long_press(self, key: str, duration: float = 0.5) -> bool:
        """按住指定时长后松开。"""
        return self.tap(key, duration=max(0.01, float(duration)))

    # ------------------------------------------------------------------
    # 执行映射
    # ------------------------------------------------------------------

    def execute(
        self,
        mapping: KeyMapping,
        *,
        is_long_press: bool = False,
        duration: Optional[float] = None,
    ) -> bool:
        """执行一条键盘映射。

        :param is_long_press: 由匹配器判定为长按时为 True
        :param duration: 覆盖长按/按键时长
        """
        if not mapping.enabled:
            return False

        key = mapping.key
        action = mapping.action

        if is_long_press or action is KeyAction.LONG_PRESS:
            hold = float(duration if duration is not None else mapping.long_press_duration)
            return self.long_press(key, hold)

        if action is KeyAction.PRESS:
            return self.press(key)
        if action is KeyAction.RELEASE:
            return self.release(key)

        # 默认：按一下
        return self.tap(key, duration=float(duration) if duration is not None else 0.02)

    def execute_sequence(self, keys: List[str], interval: float = 0.02) -> bool:
        """依次按下一组键（组合键场景，如 ctrl+shift+q）。"""
        if not keys:
            return False
        ok = True
        with self._lock:
            for key in keys:
                if not self.press(key):
                    ok = False
                if interval > 0:
                    time.sleep(interval)
            for key in reversed(keys):
                self.release(key)
        return ok

    # ------------------------------------------------------------------
    # 安全
    # ------------------------------------------------------------------

    def release_all(self) -> None:
        """松开所有被按住的键。

        程序退出、暂停、切换模式时都应调用，否则会留下卡住的键。
        """
        with self._lock:
            for name in list(self._pressed):
                self._send(name, False, force=True)
            self._pressed.clear()

    def stop(self) -> None:
        """停止并释放所有按键。

        先置 ``_stopping`` 再释放：顺序反过来的话，正在另一个线程里跑到一半的
        ``press()`` 会在 :meth:`release_all` 之后才真正按下，键就留在系统里了。
        """
        with self._lock:
            self._stopping = True
        self.release_all()

    def start(self) -> None:
        """恢复可用。"""
        self._stopping = False

    @property
    def stopping(self) -> bool:
        """是否已停止（不再接受新的按键动作）。"""
        return self._stopping

    @property
    def pressed_keys(self) -> Set[str]:
        """当前被按住的键集合（只读副本）。"""
        with self._lock:
            return set(self._pressed)

    @property
    def available(self) -> bool:
        """当前平台是否支持按键模拟。"""
        return keycodes.WINDOWS_API


__all__ = ["KeyboardController"]
