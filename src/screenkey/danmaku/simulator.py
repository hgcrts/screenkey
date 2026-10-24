# -*- coding: utf-8 -*-
"""模拟弹幕源。

用于在**没有真实直播间**的情况下测试整套流程（筛选 → 匹配 → 按键），
也是打包演示、写自动化测试的抓手。

参考项目的模拟器只能"随机吐预置句子"，测不到筛选与匹配的组合行为。
这里增强为：

- 支持**脚本化序列**（按顺序发指定内容），便于测试断言
- 支持**随机模式**（在预置语料里随机挑）
- 支持注入"重复字符"弹幕（``666666``）以覆盖感应匹配
- 可指定发送间隔与总条数，测试可把间隔设为 0 快速跑完
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, List, Optional, Sequence

from ..utils.logging_setup import get_logger
from .base import DanmakuFetcher
from .message import DanmakuMessage, MessageType

logger = get_logger("danmaku.simulator")

#: 模拟用户昵称
SIMULATOR_NICKNAMES: List[str] = [
    "用户A",
    "用户B",
    "测试用户",
    "游客",
    "粉丝123",
    "路人甲",
    "老粉",
    "舰长",
    "大佬",
    "新来的",
]

#: 模拟弹幕语料。前 20 条与参考项目逐字一致（保持兼容直觉），
#: 后面追加的是覆盖各匹配路径的用例。
SIMULATOR_MESSAGES: List[str] = [
    # --- 参考项目原有 20 条 ---
    "你好",
    "测试弹幕",
    "666",
    "主播好厉害",
    "关注了",
    "点赞",
    "w",
    "s",
    "a",
    "d",
    "空格",
    "回车",
    "1",
    "2",
    "3",
    "q",
    "e",
    "r",
    "测试成功",
    "支持主播",
    # --- 覆盖更多路径 ---
    "666666",
    "wwwwww",
    "上",
    "下",
    "左",
    "右",
    "4",
    "5",
    "f",
    "左键",
    "右键",
    "上滚",
    "下滚",
    "  带空格的内容  ",
    "这是一条很长很长的弹幕内容用来测试长度限制规则是否真的生效",
]


class SimulatorFetcher(DanmakuFetcher):
    """模拟弹幕源。

    :param script: 指定要发送的内容序列（按顺序循环）；为空则随机挑
    :param interval: 每条之间的间隔（秒）
    :param total: 总共发送多少条；``0`` 表示无限
    :param platform: 消息上标注的平台名
    """

    supports_realtime = True
    platform = "simulator"

    def __init__(
        self,
        script: Optional[Sequence[str]] = None,
        interval: float = 0.8,
        total: int = 0,
        platform: str = "simulator",
        nicknames: Optional[Sequence[str]] = None,
    ) -> None:
        super().__init__()
        self.script: List[str] = list(script or [])
        self.interval = max(0.0, float(interval))
        self.total = max(0, int(total))
        self.platform = platform
        self.nicknames = list(nicknames or SIMULATOR_NICKNAMES)

        self._sent = 0
        self._task: Optional[asyncio.Task] = None
        self._user_seq = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def connect(self, room: str = "simulator", **kwargs: Any) -> bool:
        """启动模拟循环。

        **立刻返回**，模拟循环作为后台任务继续跑 —— 这是基类
        :meth:`DanmakuFetcher.connect` 的契约：它应当尽快返回，
        而不是把整个抓取生命周期都堵在这里。
        """
        self._room_id = str(room or "simulator")
        self._sent = 0
        self._notify_status(True, "模拟弹幕已启动")
        self.emit_system("模拟弹幕已启动")
        logger.info(
            "模拟器启动: %d 条脚本, 间隔 %.2fs, 总数 %s",
            len(self.script),
            self.interval,
            self.total or "无限",
        )
        self._task = asyncio.ensure_future(self._guarded_loop())
        return True

    async def _guarded_loop(self) -> None:
        """包一层异常处理，避免后台任务里的异常静默丢失。"""
        try:
            await self._emit_loop()
        except asyncio.CancelledError:
            logger.info("模拟器任务被取消")
            raise
        except Exception as exc:
            logger.error("模拟器异常: %s", exc)
            self._notify_error(f"模拟器异常: {exc}")

    async def disconnect(self) -> None:
        """停止模拟循环。"""
        if self._disconnecting:
            return
        self._disconnecting = True
        try:
            task = self._task
            self._task = None
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            logger.info("模拟器已停止，共发送 %d 条", self._sent)
        finally:
            self._disconnecting = False

    async def _emit_loop(self) -> None:
        """主循环。

        .. note::
           方法名**刻意不叫** ``_loop``。基类
           :class:`~screenkey.danmaku.base.DanmakuFetcher` 曾把事件循环存进
           实例属性 ``self._loop``，而实例属性会遮蔽同名方法 ——
           子类的 ``_loop()`` 从此永远取不到，只要走真实
           :meth:`~screenkey.danmaku.base.DanmakuFetcher.start` 路径
           就会抛 ``TypeError: 'ProactorEventLoop' object is not callable``
           并被 ``_guarded_loop`` 吞成一条"模拟器异常"。
           现在基类属性已改名 ``_event_loop``，这里再用描述性名字双保险。

        总数发完后**必须**把 ``_stopping`` 置位并派发"未连接"状态：
        以前只 ``break``，基类 ``_thread_main`` 仍在
        ``while not self._stopping: sleep(0.1)`` 里空转，界面永远显示
        "运行中/已连接"，且 ``_running`` 一直是 ``True``，用户下次点
        "连接"只会得到"启动弹幕接收线程失败"。
        """
        while not self._stopping:
            if self.total and self._sent >= self.total:
                self.emit_system(f"模拟弹幕发送完毕（{self._sent} 条）")
                logger.info("模拟弹幕发送完毕（%d 条），结束会话", self._sent)
                self._stopping = True
                self._notify_status(False, f"模拟弹幕发送完毕（{self._sent} 条）")
                break
            message = self.make_message()
            self._emit_message(message)
            self._sent += 1
            if self.interval > 0:
                await asyncio.sleep(self.interval)
            else:
                # 让出控制权，避免饿死事件循环
                await asyncio.sleep(0)

    # ------------------------------------------------------------------
    # 消息构造
    # ------------------------------------------------------------------

    def make_message(self, content: Optional[str] = None) -> DanmakuMessage:
        """构造一条模拟弹幕。"""
        if content is None:
            if self.script:
                content = self.script[self._sent % len(self.script)]
            else:
                content = random.choice(SIMULATOR_MESSAGES)

        self._user_seq += 1
        nickname = self.nicknames[(self._user_seq - 1) % len(self.nicknames)]
        return DanmakuMessage(
            content=content,
            nickname=nickname,
            user_id=str(10000 + (self._user_seq % 90000)),
            platform=self.platform,
            room_id=self._room_id,
            msg_type=MessageType.DANMAKU,
        )

    # ------------------------------------------------------------------
    # 一次性发送（不需要后台线程）
    # ------------------------------------------------------------------

    def emit_once(self, content: str, nickname: str = "测试用户", user_id: str = "1") -> None:
        """直接发一条弹幕（同步，供测试用）。

        走 :meth:`_emit_message` 而不是 :meth:`_notify`：基类给
        "正在停止时不要再往外抛消息"这个共同关切留了 ``_stopping`` 检查，
        绕过去就会出现"点了停止还在往界面推弹幕"。
        """
        self._emit_message(
            DanmakuMessage(
                content=content,
                nickname=nickname,
                user_id=user_id,
                platform=self.platform,
                room_id=self._room_id or "simulator",
                msg_type=MessageType.DANMAKU,
            )
        )

    def emit_sequence(self, contents: Sequence[str]) -> int:
        """直接发一串弹幕；返回发送条数。"""
        for content in contents:
            self.emit_once(content)
        return len(contents)


__all__ = ["SIMULATOR_MESSAGES", "SIMULATOR_NICKNAMES", "SimulatorFetcher"]
