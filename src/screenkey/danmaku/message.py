# -*- coding: utf-8 -*-
"""统一的弹幕消息模型。

参考项目里两个平台各自定义了一份 ``DanmakuMessage``（``core/danmaku/bili.py``
与 ``core/danmaku/douyin.py``），字段完全相同却必须保持"两个独立的类"，
甚至用注释强调"``core.DanmakuMessage`` 必须是 B 站版"。这让下游的筛选器、
匹配器、界面都要写平台分支。

本模块用一个 ``DanmakuMessage`` 覆盖所有平台，平台差异放进 ``platform``
字段与 ``extra`` 字典里。所有消息都携带 ``platform`` / ``room_id``，
便于筛选规则按来源区分。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Mapping

from ..utils.helpers import timestamp_to_datetime


class MessageType(str, Enum):
    """弹幕消息类型。

    用 ``str`` 混入是为了让 ``MessageType.GIFT == "gift"`` 成立，
    JSON 序列化与筛选规则里的字符串比较都无需额外转换。
    """

    DANMAKU = "danmaku"          # 普通聊天弹幕
    GIFT = "gift"                # 礼物
    GUARD = "guard"              # 上舰 / 开通贵族
    SUPER_CHAT = "super_chat"    # 醒目留言（SC）
    ENTER = "enter"              # 进入直播间
    FOLLOW = "follow"            # 关注
    LIKE = "like"                # 点赞
    STATS = "stats"              # 房间统计（在线人数等）
    SYSTEM = "system"            # 系统消息 / 连接状态

    @classmethod
    def coerce(cls, value: Any) -> "MessageType":
        """把任意输入（枚举、字符串、None）安全地转成 ``MessageType``。

        未知值回落到 ``DANMAKU`` 而不是抛异常 —— 平台协议新增消息类型时，
        旧版本应当继续把未知消息当普通弹幕处理，而不是崩溃。
        """
        if isinstance(value, cls):
            return value
        if value is None:
            return cls.DANMAKU
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return cls.DANMAKU


@dataclass(frozen=True)
class DanmakuMessage:
    """一条弹幕消息（不可变）。

    不可变是刻意的：消息会在抓取线程、筛选流水线、界面之间传递，
    参考项目里这些对象被多处就地改写（``message.content = ...``），
    出现过"界面显示的内容与触发匹配的内容不一致"的问题。
    需要派生新消息时用 :meth:`evolved`。
    """

    content: str
    nickname: str = ""
    user_id: str = ""
    platform: str = ""
    room_id: str = ""
    msg_type: MessageType = MessageType.DANMAKU
    timestamp: datetime = field(default_factory=datetime.now)
    level: int = 0
    medal_name: str = ""
    medal_level: int = 0
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # frozen dataclass 仍需 object.__setattr__ 才能规整字段
        object.__setattr__(self, "content", self.content if isinstance(self.content, str) else str(self.content))
        object.__setattr__(self, "nickname", self.nickname or "")
        object.__setattr__(self, "user_id", str(self.user_id) if self.user_id not in (None, "") else "")
        object.__setattr__(self, "platform", (self.platform or "").lower())
        object.__setattr__(self, "room_id", str(self.room_id) if self.room_id not in (None, "") else "")
        object.__setattr__(self, "msg_type", MessageType.coerce(self.msg_type))
        object.__setattr__(self, "extra", dict(self.extra or {}))
        # ``timestamp`` 一律规整成 ``datetime``。声明类型是 ``datetime``，但两个平台
        # 抓到的原始值分别是秒级/毫秒级整数或 ISO 字符串，构造时很容易直接塞进来；
        # 若不在这里统一，下游拿到"有时是 float 有时是 datetime"的字段就开始各自猜，
        # 界面曾因此对 datetime 调 ``datetime.fromtimestamp()`` 而抛 TypeError。
        object.__setattr__(self, "timestamp", timestamp_to_datetime(self.timestamp))

    # ------------------------------------------------------------------
    # 派生
    # ------------------------------------------------------------------

    def evolved(self, **changes: Any) -> "DanmakuMessage":
        """返回替换了若干字段的新消息（原对象不变）。"""
        return replace(self, **changes)

    def with_content(self, content: str) -> "DanmakuMessage":
        """返回内容被替换的新消息（筛选器剥前缀时使用）。"""
        return self.evolved(content=content)

    # ------------------------------------------------------------------
    # 便捷查询
    # ------------------------------------------------------------------

    @property
    def is_chat(self) -> bool:
        """是否为用户手打的聊天弹幕。"""
        return self.msg_type is MessageType.DANMAKU

    @property
    def normalized(self) -> str:
        """用于匹配的规整文本：去首尾空白并转小写。"""
        return self.content.strip().lower()

    def get(self, key: str, default: Any = None) -> Any:
        """按顺序在 ``extra`` 与常见字段里取值。

        让筛选规则可以写 ``rule pattern = "gift_name"`` 这类字段引用，
        而不用为每种消息类型写一个分支。
        """
        if key in self.extra:
            return self.extra[key]
        return getattr(self, key, default)

    @property
    def fingerprint(self) -> str:
        """内容指纹，用于去重（同一直播间同一用户同一内容）。"""
        raw = f"{self.platform}|{self.room_id}|{self.user_id}|{self.msg_type.value}|{self.content}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """转成可 JSON 序列化的 dict。"""
        return {
            "content": self.content,
            "nickname": self.nickname,
            "user_id": self.user_id,
            "platform": self.platform,
            "room_id": self.room_id,
            "msg_type": self.msg_type.value,
            "timestamp": self.timestamp.isoformat(),
            "level": self.level,
            "medal_name": self.medal_name,
            "medal_level": self.medal_level,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DanmakuMessage":
        """从 :meth:`to_dict` 的输出还原；未知键忽略，缺失键用默认值。"""
        if not isinstance(data, Mapping):
            raise TypeError(f"弹幕数据必须是 mapping，实际为 {type(data).__name__}")

        # 走统一的 :func:`timestamp_to_datetime`。这里曾经是自己手写的三分支
        # （datetime / ISO 字符串 / 其他一律 ``datetime.now()``），结果是**数字
        # 时间戳被静默丢弃**、换成"此刻"——平台原始数据里最常见的就是毫秒整数。
        timestamp = timestamp_to_datetime(data.get("timestamp"))

        return cls(
            content=str(data.get("content", "")),
            nickname=str(data.get("nickname", "") or ""),
            user_id=str(data.get("user_id", "") or ""),
            platform=str(data.get("platform", "") or ""),
            room_id=str(data.get("room_id", "") or ""),
            msg_type=MessageType.coerce(data.get("msg_type")),
            timestamp=timestamp,
            level=int(data.get("level", 0) or 0),
            medal_name=str(data.get("medal_name", "") or ""),
            medal_level=int(data.get("medal_level", 0) or 0),
            extra=dict(data.get("extra") or {}),
        )

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志可读性
        return f"[{self.platform}] {self.nickname}: {self.content}"


def make_system_message(content: str, platform: str = "", room_id: str = "") -> DanmakuMessage:
    """构造一条系统消息（连接状态提示等）。"""
    return DanmakuMessage(
        content=content,
        nickname="系统",
        platform=platform,
        room_id=room_id,
        msg_type=MessageType.SYSTEM,
    )


__all__ = ["DanmakuMessage", "MessageType", "make_system_message"]
