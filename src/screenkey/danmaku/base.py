# -*- coding: utf-8 -*-
"""弹幕抓取器基类。

三个平台实现（B 站、抖音、模拟器）共享同一套生命周期与回调协议。
参考项目把回调逻辑放在 ``CallbackRegistry`` 混入类里，并把
"后台线程跑独立 event loop"的样板代码复制到每个抓取器里
（``run_async_stop`` 被两个模块各写一遍）。这里统一到基类。

回调协议（与参考项目一致的约定，因为它是踩过坑得出的）：

1. **回调异常绝不向外传播** —— 一个回调抛异常不能中断其他回调，
   更不能杀掉抓取线程
2. **回调在抓取线程里被调用** —— 界面层必须自己切回主线程
3. **``stop()`` 同步且非阻塞** —— 界面点"断开"要立刻返回，
   真正的清理在后台线程完成
4. **``disconnect()`` 可重入保护** —— 用 ``_disconnecting`` 标志，
   避免重复清理；但**不能**用 ``_stopping`` 短路，否则断开永远不可达
   （参考项目的 bug：``stop()`` 设了 ``_stopping``，``disconnect()``
   第一行就 ``if self._stopping: return``，导致 socket 永不关闭）
"""

from __future__ import annotations

import asyncio
import logging
import threading
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from ..utils.logging_setup import get_logger, get_raw_danmaku_logger
from .message import DanmakuMessage, make_system_message

logger = get_logger("danmaku.base")
raw_logger = get_raw_danmaku_logger()

#: 回调类型
MessageCallback = Callable[[DanmakuMessage], None]
ErrorCallback = Callable[[str], None]
StatusCallback = Callable[[bool], None]


class DanmakuFetcher(ABC):
    """弹幕抓取器基类。

    子类需要实现 :meth:`connect` 与 :meth:`disconnect`，
    并在收到弹幕时调用 :meth:`_emit`。
    """

    #: 该实现是否支持真正的实时弹幕。
    #: 参考项目用这个属性让界面区分"真的连上了"和"假装连上了"——
    #: 抖音模块当年 ``listen()`` 是空转循环却仍报"已连接"，
    #: 加了这个属性后界面才能如实显示"模拟/不支持"。
    supports_realtime: bool = False

    #: 平台标识（``"bilibili"`` / ``"douyin"`` / ``"simulator"``）
    platform: str = ""

    def __init__(self) -> None:
        self._message_callbacks: List[MessageCallback] = []
        self._error_callbacks: List[ErrorCallback] = []
        self._status_callbacks: List[StatusCallback] = []

        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False
        self._stopping = False       # 用户要求停止
        self._disconnecting = False  # 正在清理，防重入
        self._connected = False
        self._room_id = ""
        self._message_count = 0

        #: 连接相关配置（由 :meth:`apply_connection_config` 注入）。
        #:
        #: 以前 ``ConnectionConfig`` 里的 ``auto_reconnect`` /
        #: ``reconnect_delay`` / ``max_reconnect_attempts`` /
        #: ``connection_timeout`` / ``heartbeat_interval`` 五项**全项目没有
        #: 任何读取点**，用户在选项页怎么改都不生效，重连次数/退避/超时
        #: 全是硬编码。现在由引擎在 ``start()`` 时统一注入，各抓取器按需消费。
        self.connection_options: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 回调注册
    # ------------------------------------------------------------------

    def on_message(self, callback: MessageCallback) -> None:
        """注册弹幕回调。"""
        if callable(callback):
            self._message_callbacks.append(callback)

    def on_error(self, callback: ErrorCallback) -> None:
        """注册错误回调。"""
        if callable(callback):
            self._error_callbacks.append(callback)

    def on_status(self, callback: StatusCallback) -> None:
        """注册连接状态回调（True=已连接）。"""
        if callable(callback):
            self._status_callbacks.append(callback)

    def clear_callbacks(self) -> None:
        """清空所有回调（界面销毁时调用，避免回调打到已销毁的控件上）。"""
        self._message_callbacks.clear()
        self._error_callbacks.clear()
        self._status_callbacks.clear()

    def apply_connection_config(self, options: Dict[str, Any]) -> None:
        """注入连接配置（``engine.start`` 调用）。

        只保留认识的键，其余忽略 —— 引擎会把整组 ``ConnectionConfig``
        字段都传下来，但并不是每个抓取器都用得上。子类可覆写以做自己的
        类型强转。
        """
        if not isinstance(options, dict):
            return
        for key in (
            "auto_reconnect",
            "reconnect_delay",
            "max_reconnect_attempts",
            "connection_timeout",
            "heartbeat_interval",
        ):
            if key in options and options[key] is not None:
                self.connection_options[key] = options[key]

    def _connection_option(self, key: str, default: Any) -> Any:
        """读一个连接配置项（带默认值）。"""
        value = self.connection_options.get(key, default)
        return default if value is None else value

    # ------------------------------------------------------------------
    # 通知（异常隔离）
    # ------------------------------------------------------------------

    def _notify(self, message: DanmakuMessage) -> None:
        """把消息派发给所有回调。

        每个回调单独 try/except：一个坏回调不能影响其余回调，
        也不能把异常抛回抓取线程。
        """
        if message is None:
            return
        self._message_count += 1

        if logger.isEnabledFor(logging.DEBUG):
            raw_logger.debug(
                "[%s] %s: %s", message.platform or self.platform, message.nickname, message.content
            )

        for callback in list(self._message_callbacks):
            try:
                callback(message)
            except Exception as exc:  # pragma: no cover - 防御性
                logger.exception("弹幕回调异常（已隔离）: %s", exc)

    def _emit_message(self, message: DanmakuMessage) -> None:
        """子类回调里转发消息（带 ``_stopping`` 检查）。

        放在基类而不是各子类，是因为"正在停止时不要再往外抛消息"是
        所有抓取器共同的关切；参考项目把它写在 B 站模块里，
        抖音模块调用同名方法时就变成了 ``AttributeError``。
        """
        if message is None or self._stopping:
            return
        self._notify(message)

    def _notify_error(self, error: str) -> None:
        """派发错误。"""
        logger.error("[%s] %s", self.platform, error)
        for callback in list(self._error_callbacks):
            try:
                callback(str(error))
            except Exception as exc:  # pragma: no cover
                logger.exception("错误回调异常（已隔离）: %s", exc)

    def _notify_status(self, connected: bool, text: str = "") -> None:
        """派发连接状态。

        ``text`` 是可选的说明（例如"连接中断，正在重试(2/5)"）。
        回调既可以只接一个 ``connected`` 参数，也可以再接一个 ``text``；
        两种签名都支持，避免界面被迫改签名。
        """
        self._connected = bool(connected)
        for callback in list(self._status_callbacks):
            try:
                try:
                    callback(bool(connected), text)  # type: ignore[call-arg]
                except TypeError:
                    callback(bool(connected))  # type: ignore[call-arg]
            except Exception as exc:  # pragma: no cover
                logger.exception("状态回调异常（已隔离）: %s", exc)

    def emit_system(self, content: str) -> None:
        """发送一条系统消息到界面。"""
        self._notify(make_system_message(content, platform=self.platform, room_id=self._room_id))

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @abstractmethod
    async def connect(self, room: str, **kwargs: Any) -> bool:
        """连接直播间。返回是否成功。"""

    @abstractmethod
    async def disconnect(self) -> None:
        """清理资源。实现必须自行处理重入。"""

    def start(self, room: str, **kwargs: Any) -> bool:
        """在后台线程启动抓取。

        在后台线程里新建一个 event loop 跑 :meth:`connect`，
        然后保持运行直到 :meth:`stop` 被调用。
        """
        # 刚点过「停止」又立刻点「连接」时，``_running`` 还是 True：``stop()``
        # 只置 ``_stopping``，真正的复位发生在抓取线程的 finally 里。以前这里
        # 直接返回 False，界面就报"启动失败"，用户得再点一次 —— 实测大约
        # 0.6 秒的窗口期。先在超时内等一下正在退出的旧线程，再决定会不会冲突。
        if self._running and self._stopping:
            self.join(timeout=2.0)

        with self._lock:
            if self._running:
                logger.warning("[%s] 已在运行中，忽略重复启动", self.platform)
                return False
            self._stopping = False
            self._disconnecting = False
            self._running = True
            self._message_count = 0

        # 引擎会把 ConnectionConfig 的五个连接字段一并塞进 kwargs。
        # 在起线程前消费掉，子类在 connect 里就能读到（而不是每个子类
        # 各自从 kwargs 里再捞一遍）。
        self.apply_connection_config(kwargs)

        self._thread = threading.Thread(
            target=self._thread_main,
            args=(room, kwargs),
            name=f"danmaku-{self.platform or 'fetcher'}",
            daemon=True,
        )
        self._thread.start()
        return True

    def _thread_main(self, room: str, kwargs: Dict[str, Any]) -> None:
        """后台线程入口：建 loop、连接、保持运行。"""
        loop = asyncio.new_event_loop()
        self._event_loop = loop
        asyncio.set_event_loop(loop)
        try:
            connected = loop.run_until_complete(self.connect(room, **kwargs))
            if not connected:
                logger.warning("[%s] 连接未成功建立", self.platform)
                self._notify_status(False)
                return
            # 连接成功后保持 loop 运行，让子类注册的任务继续跑
            while not self._stopping:
                loop.run_until_complete(asyncio.sleep(0.1))
        except Exception as exc:
            logger.exception("[%s] 抓取线程异常退出: %s", self.platform, exc)
            self._notify_error(f"连接异常: {exc}")
        finally:
            try:
                loop.run_until_complete(self.disconnect())
            except Exception as exc:  # pragma: no cover
                logger.error("[%s] 清理失败: %s", self.platform, exc)
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:  # pragma: no cover
                pass
            try:
                loop.close()
            except Exception:  # pragma: no cover
                pass
            with self._lock:
                self._running = False
                self._event_loop = None
            self._notify_status(False)
            logger.info("[%s] 抓取线程已退出", self.platform)

    def stop(self, timeout: float = 3.0) -> None:
        """请求停止（同步、非阻塞）。

        **不等待线程结束** —— 界面点断开要立刻有反馈。
        想等线程真的退出，用 :meth:`join`。
        """
        with self._lock:
            if not self._running:
                return
            self._stopping = True
        logger.info("[%s] 收到停止请求", self.platform)
        if timeout > 0:
            # 后台起一个守护线程兜底：超时后强行置位，防止卡死
            threading.Thread(
                target=self._force_stop_after, args=(timeout,), daemon=True
            ).start()

    def _force_stop_after(self, timeout: float) -> None:
        """超时兜底。"""
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("[%s] 停止超时 %.1fs，线程仍在运行", self.platform, timeout)

    def join(self, timeout: Optional[float] = None) -> bool:
        """等待抓取线程结束；返回是否已结束。"""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        """是否正在运行。"""
        with self._lock:
            return self._running

    @property
    def connected(self) -> bool:
        """是否已连接。"""
        return self._connected

    @property
    def room_id(self) -> str:
        """当前房间号。"""
        return self._room_id

    @property
    def message_count(self) -> int:
        """已接收消息数。"""
        return self._message_count

    def __enter__(self) -> "DanmakuFetcher":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()


__all__ = ["DanmakuFetcher"]
