# -*- coding: utf-8 -*-
"""统一弹幕模型与各平台抓取器。

:class:`DanmakuFetcher` 是抓取器基类；
:func:`create_fetcher` 按平台名构造对应实现。
"""

from __future__ import annotations

from typing import Any

from .base import DanmakuFetcher
from .message import DanmakuMessage, MessageType, make_system_message
from .simulator import SIMULATOR_MESSAGES, SIMULATOR_NICKNAMES, SimulatorFetcher

__all__ = [
    "DanmakuFetcher",
    "DanmakuMessage",
    "MessageType",
    "SIMULATOR_MESSAGES",
    "SIMULATOR_NICKNAMES",
    "SimulatorFetcher",
    "create_fetcher",
    "make_system_message",
]


#: 视为"模拟器"的平台名（``simulator`` 本身 + 中文/简写）
SIMULATOR_NAMES = frozenset({"simulator", "sim", "模拟", "模拟器"})

#: 平台别名 → 规范平台名。**不做**未知平台回落，见 :func:`create_fetcher`。
PLATFORM_ALIASES = {
    "bilibili": "bilibili",
    "bili": "bilibili",
    "b站": "bilibili",
    "哔哩哔哩": "bilibili",
    "douyin": "douyin",
    "dy": "douyin",
    "抖音": "douyin",
}


class UnknownPlatformError(ValueError):
    """平台名无法识别。

    以前未知平台会被**静默**替换成 :class:`SimulatorFetcher`：用户把
    ``"bilibili"`` 拼成 ``"bilibli"``，界面照样显示"已连接"，实际收到的是
    假弹幕。现在显式报错，让调用方把问题送到界面。
    """


def normalize_platform(platform: str) -> str:
    """把平台别名归一成 ``bilibili`` / ``douyin`` / ``simulator``。

    :raises UnknownPlatformError: 无法识别的平台名
    """
    name = (platform or "").strip().lower()
    if name in SIMULATOR_NAMES:
        return "simulator"
    if name in PLATFORM_ALIASES:
        return PLATFORM_ALIASES[name]
    raise UnknownPlatformError(
        f"未知平台 {platform!r}，支持 bilibili / douyin / simulator"
    )


def create_fetcher(platform: str, **kwargs: Any) -> DanmakuFetcher:
    """按平台名创建抓取器。

    :param platform: ``"bilibili"`` / ``"douyin"`` / ``"simulator"``（含别名）
    :param kwargs: 传给具体抓取器的参数（如 ``sessdata``、``cookie``、
        ``signature``、``signature_provider``、``proxy``、``interval``）
    :raises UnknownPlatformError: 平台名无法识别

    依赖缺失时**不抛异常**，而是返回一个会在 ``connect`` 时报错的实例，
    这样界面可以先正常起来、连的时候再提示装依赖。
    """
    name = normalize_platform(platform)

    if name == "bilibili":
        from .bilibili import BilibiliFetcher

        return BilibiliFetcher(sessdata=kwargs.get("sessdata", ""))

    if name == "douyin":
        from .douyin import DouyinFetcher

        # 这几个参数以前被工厂**全部吞掉**：``signature`` / ``proxy`` /
        # ``device_id`` 传进来也到不了抓取器，于是抖音永远带着空签名去连，
        # 实测 155ms 内被服务端 1006 断开，而界面只显示"连接中断"。
        return DouyinFetcher(
            cookie=kwargs.get("cookie", ""),
            mode=kwargs.get("mode", "websocket"),
            signature=kwargs.get("signature", ""),
            signature_provider=kwargs.get("signature_provider"),
            proxy=kwargs.get("proxy", ""),
            device_id=kwargs.get("device_id", ""),
        )

    return SimulatorFetcher(
        script=kwargs.get("script"),
        interval=kwargs.get("interval", 0.8),
        total=kwargs.get("total", 0),
        platform=kwargs.get("sim_platform", "simulator"),
    )
