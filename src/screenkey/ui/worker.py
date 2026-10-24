"""引擎 → Qt 信号桥。

**为什么需要它**
----------------
引擎的回调在**抓取线程**里执行（这是刻意的：筛选/匹配/按键都在抓取线程完成，
见 ``engine.py`` 的线程模型说明）。Qt 控件只能在主线程操作，所以必须有一个
对象把跨线程回调转成 Qt 信号——``pyqtSignal.emit`` 是 Qt 保证线程安全的。

参考项目直接在抓取线程里 ``ui.append(...)``，靠"碰巧没崩"运行；一旦弹幕量上来
就会随机出现 ``QObject: Cannot create children for a parent that is in a different thread``。
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import QObject, pyqtSignal

from ..engine import ScreenKeyEngine, EngineEvent
from ..utils.logging_setup import get_logger

logger = get_logger(__name__)

__all__ = ["EngineBridge"]


class EngineBridge(QObject):
    """把 ``ScreenKeyEngine`` 的事件转发成 Qt 信号。

    所有信号都在主线程投递（``QueuedConnection`` 语义由 Qt 自动处理）。
    """

    #: 一条弹幕（不论是否触发按键）
    messageReceived = pyqtSignal(object, bool, str)   # DanmakuMessage, actionable, matched
    #: 触发了按键
    actionTriggered = pyqtSignal(object, list)        # DanmakuMessage, [MatchResult]
    #: 连接状态变化
    statusChanged = pyqtSignal(bool, str)             # connected, text
    #: 出错
    errorOccurred = pyqtSignal(str)
    #: 警告（不算错误：例如"注入环境受限，按键会被系统静默丢弃"）
    warningOccurred = pyqtSignal(str)
    #: 统计刷新（定时器驱动，不是每条弹幕都发）
    statsUpdated = pyqtSignal(object)                 # dict 快照（engine.stats_snapshot()）

    def __init__(self, engine: ScreenKeyEngine, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.engine.on_event = self._on_event

    # ------------------------------------------------------------------
    def refresh_stats(self) -> None:
        """由界面定时器调用：取一份加锁快照并发 ``statsUpdated``。

        以前界面直接读 ``engine.stats``，而抓取线程在锁内改这些计数器 ——
        GUI 线程无锁读会拿到撕裂的值；引擎早就提供了加锁的
        ``stats_snapshot()``，却没人用，``statsUpdated`` 信号也 0 处 connect。
        """

        try:
            snapshot = self.engine.stats_snapshot()
        except Exception as exc:  # pragma: no cover - 统计失败不该影响界面
            logger.error("读取统计快照失败：%s", exc, exc_info=True)
            return
        self.statsUpdated.emit(snapshot)

    def _on_event(self, event: EngineEvent) -> None:
        """引擎回调（抓取线程）。这里**只做 emit**，不碰任何控件。"""

        try:
            kind = event.kind
            if kind == "action":
                if event.message is not None:
                    self.messageReceived.emit(event.message, True, event.text)
                self.actionTriggered.emit(event.message, list(event.matches or []))
            elif kind == "message":
                if event.message is not None:
                    self.messageReceived.emit(event.message, False, "")
            elif kind == "status":
                # 引擎把连接标志放在 extra["connected"]，不是 actionable。
                connected = bool((event.extra or {}).get("connected", False))
                self.statusChanged.emit(connected, event.text)
            elif kind == "error":
                self.errorOccurred.emit(event.text or "未知错误")
            elif kind == "warning":
                self.warningOccurred.emit(event.text or "未知警告")
            elif kind == "stats":
                # 引擎目前不发 kind="stats"（统计由 refresh_stats 主动拉），
                # 这条分支保留是为了兼容外部直接投递 EngineStats 的用法。
                if event.message is not None and hasattr(event.message, "total_messages"):
                    self.statsUpdated.emit(event.message)
        except Exception as exc:  # pragma: no cover - 绝不让异常回到引擎
            logger.error("事件转发失败：%s", exc, exc_info=True)

    def detach(self) -> None:
        """断开与引擎的连接（窗口关闭时调用）。"""

        if self.engine is not None:
            self.engine.on_event = None
