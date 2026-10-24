# -*- coding: utf-8 -*-
"""回归：真实 PushFrame 里的 WebcastChatMessage 必须能走到界面。

复现历史 bug：``douyin.py`` 用了 ``METHOD_MAP`` 但没从 ``douyin_protocol``
导入它，于是每条已映射消息都抛 ``NameError`` 被兜底 except 吞成
「处理抖音消息异常: name 'METHOD_MAP' is not defined」，弹幕一条都进不去。
"""

from __future__ import annotations

from screenkey.danmaku.douyin import DouyinFetcher
from screenkey.danmaku.douyin_protocol import MSG_METHOD, MSG_PAYLOAD
from screenkey.danmaku.protobuf_lite import (
    encode_field_bytes,
    encode_field_string,
)


def _chat_envelope(method: str, content: str, nickname: str) -> bytes:
    """拼一个 ``Message{ method=1, payload=2 }`` 信封，payload 是弹幕正文。"""
    # ChatMessage{ user=2 { nickname=3 }, content=3 }
    user = encode_field_string(3, nickname)
    chat = encode_field_bytes(2, user) + encode_field_string(3, content)
    return encode_field_string(MSG_METHOD[0], method) + encode_field_bytes(
        MSG_PAYLOAD[0], chat
    )


def test_chat_envelope_reaches_ui() -> None:
    fetcher = DouyinFetcher()
    received = []
    fetcher.on_message(received.append)

    fetcher._handle_message_envelope(
        _chat_envelope("WebcastChatMessage", "666", "观众甲")
    )

    assert received, "弹幕没有到达上层：METHOD_MAP 分派链断了"
    message = received[0]
    assert message.content == "666"
    assert message.nickname == "观众甲"
    assert fetcher._got_any_message is True


def test_unknown_method_is_ignored_not_crashed() -> None:
    fetcher = DouyinFetcher()
    received = []
    fetcher.on_message(received.append)
    # 未映射的方法只应记 DEBUG，不能抛异常
    fetcher._handle_message_envelope(
        encode_field_string(MSG_METHOD[0], "WebcastInRoomBannerMessage")
        + encode_field_bytes(MSG_PAYLOAD[0], b"\x08\x01")
    )
    assert received == []
