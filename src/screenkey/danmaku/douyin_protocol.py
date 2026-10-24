# -*- coding: utf-8 -*-
"""抖音 IM 协议的纯数据层。

这里只有三件事：字段号表、帧/响应解析、直播间 HTML 提取。刻意**不依赖**
``DouyinFetcher`` —— 输入字节、输出 ``dict``，没有连接、没有状态、没有重连。
平台改版时要改的东西全在这个文件里；``douyin.py`` 只负责 Cookie 暖场、
WebSocket 收发、ack 与心跳节奏。

关于字段号
----------
每个字段都给**多候选**号（如 ``USER_NICKNAME = (3, 2)``），按顺序取第一个
存在的。抖音的 proto 在不同版本/灰度里挪过号，写死单号会在部分房间解析成空。
这些号来自实测抓包，不要凭 proto 文件猜——``Message`` 的 payload 曾被误写成
``(6, 8)``（实为 ``needWrdsStore`` / ``wrdsSubKey``），结果连接正常、收帧正常、
永远 0 条弹幕。
"""

from __future__ import annotations

import json
import re
import zlib
from typing import Any, Dict, List, Optional, Tuple

from .message import MessageType
from .protobuf_lite import (
    ProtobufError,
    encode_field_bytes,
    encode_field_string,
    encode_field_varint,
    get_int,
    get_message,
    get_str,
    parse_message,
)
from ..utils.logging_setup import get_logger

#: 与 ``douyin.py`` 共用同一个 logger 名：解析告警看起来仍属于"抖音后端"
logger = get_logger("danmaku.douyin")


# ----------------------------------------------------------------------
# protobuf 字段号（多候选：平台改版时逐个试）
# ----------------------------------------------------------------------

# PushFrame
PF_PAYLOAD_ENCODING = (6,)
PF_PAYLOAD_TYPE = (7,)
PF_PAYLOAD = (8,)
PF_LOG_ID = (2,)
PF_HEADERS = (5,)

# HeadersList（PushFrame.headersList 的每一项）
HEADER_KEY = (1,)
HEADER_VALUE = (2,)

#: headersList 里表示压缩方式的键。**实测**：真实下行帧的 ``payloadEncoding``
#: (field 6) 是**空的**，压缩方式只在 headersList 的 ``compress_type`` 里声明，
#: 所以不能只看 field 6 —— 否则 gzip 数据会被当成裸 protobuf 解析，一个字段都
#: 解不出来（表现为连上了、连接稳定，但永远 0 条弹幕）。
HEADER_COMPRESS_TYPE = "compress_type"

#: headersList 里携带 internal_ext 的键，ack 要用它回。
HEADER_INTERNAL_EXT = "im-internal_ext"

# Response
RESP_CURSOR = (2,)
RESP_INTERNAL_EXT = (5,)
RESP_NEED_ACK = (9,)
RESP_HEARTBEAT = (8,)
RESP_PUSH_SERVER = (10,)

# Message（信封）
#: **实测**（真实抓包）：``Message`` 的字段是
#: ``1=method(string)`` / ``2=payload(bytes)`` / ``3=msgId(int)``。
#: 早期版本把 payload 写成 ``(6, 8)`` —— 那两号分别是 ``needWrdsStore``(bool)
#: 与 ``wrdsSubKey``(string)，于是**每一条消息都被静默丢弃**（连接正常、
#: 收帧正常、永远 0 条弹幕）。字段号来自实测，不要凭 proto 文件猜。
MSG_METHOD = (1,)
MSG_MSG_ID = (3,)
MSG_PAYLOAD = (2,)

# ChatMessage
CHAT_USER = (2,)
CHAT_CONTENT = (3,)

# User
USER_ID = (1,)
USER_NICKNAME = (3, 2)
USER_LEVEL = (6,)

# GiftMessage
GIFT_USER = (7, 2)
GIFT_GIFT = (15,)
GIFT_COUNT = (5, 6)
GIFT_STRUCT_NAME = (2, 16, 1)

# MemberMessage
MEMBER_USER = (2,)

# LikeMessage
LIKE_COUNT = (2,)
LIKE_TOTAL = (3,)
LIKE_USER = (5,)

# SocialMessage
SOCIAL_USER = (2,)
SOCIAL_ACTION = (4,)

# RoomUserSeqMessage
SEQ_TOTAL = (3, 7)

# ControlMessage
CONTROL_STATUS = (1,)

#: 我们关心的方法名 → 内部消息类型
METHOD_MAP: Dict[str, MessageType] = {
    "WebcastChatMessage": MessageType.DANMAKU,
    "WebcastEmojiChatMessage": MessageType.DANMAKU,
    "WebcastGiftMessage": MessageType.GIFT,
    "WebcastMemberMessage": MessageType.ENTER,
    "WebcastLikeMessage": MessageType.LIKE,
    "WebcastSocialMessage": MessageType.FOLLOW,
    "WebcastRoomUserSeqMessage": MessageType.STATS,
    "WebcastRoomStatsMessage": MessageType.STATS,
    "WebcastControlMessage": MessageType.SYSTEM,
}

#: 这些方法纯噪音，直接丢弃（解析都不做）
IGNORED_METHODS = {
    "WebcastRoomMessage",
    "WebcastRoomRankMessage",
    "WebcastHotRoomMessage",
    "WebcastScreenChatMessage",
    "WebcastRoomStreamAdaptationMessage",
    "WebcastImDeleteMessage",
    "WebcastRanklistHourEntranceMessage",
}


# ----------------------------------------------------------------------
# HTML 提取
# ----------------------------------------------------------------------


def _pick(mapping: Dict[int, List[Any]], numbers: Tuple[int, ...], default: Any = None) -> Any:
    """从多候选字段号里取第一个存在的值。"""
    for number in numbers:
        if number in mapping and mapping[number]:
            return mapping[number][0]
    return default


def _pick_int(mapping: Dict[int, List[Any]], numbers: Tuple[int, ...], default: int = 0) -> int:
    """多候选取整型。"""
    for number in numbers:
        value = get_int(mapping, number, default=0)
        if value:
            return value
    return default


def _pick_str(mapping: Dict[int, List[Any]], numbers: Tuple[int, ...], default: str = "") -> str:
    """多候选取字符串。"""
    for number in numbers:
        value = get_str(mapping, number, default="")
        if value:
            return value
    return default


def _pick_message(
    mapping: Dict[int, List[Any]], numbers: Tuple[int, ...]
) -> Optional[Dict[int, List[Any]]]:
    """多候选取嵌套消息。"""
    for number in numbers:
        nested = get_message(mapping, number)
        if nested:
            return nested
    return None


_INIT_PROPS_PATTERNS = (
    re.compile(r"window\.__INIT_PROPS__\s*=\s*(\{.*?\});?\s*</script>", re.S),
    re.compile(r"window\.__INIT_PROPS__\s*=\s*(\{.*\})\s*;?\s*$", re.S | re.M),
)

# 注意：抖音页面里的内联 JSON 是**被转义过的**，原文形如
# ``\"user_unique_id\":\"7694971917535479339\"``，所以每条正则都要
# 用 ``\\?"`` 同时容忍"有反斜杠"和"没有反斜杠"两种写法。
# 只写 ``"key"\s*:\s*"`` 会一条都匹配不到（实测 roomId 能匹配、其余全空）。
_ROOM_ID_PATTERNS = (
    re.compile(r'"roomId"\s*:\s*"(\d{5,})"'),
    re.compile(r'"room_id"\s*:\s*"?(\d{5,})"?'),
    re.compile(r'roomId\\?"\s*:\s*\\?"(\d{5,})'),
)

_USER_UNIQUE_ID_PATTERNS = (
    re.compile(r'user_unique_id\\?"\s*:\s*\\?"(\d{5,})'),
    re.compile(r'userUniqueId\\?"\s*:\s*\\?"(\d{5,})'),
)

_CURSOR_PATTERNS = (
    re.compile(r'cursor\\?"\s*:\s*\\?"([^"\\]{1,2000})'),
    re.compile(r'"cursor"\s*:\s*"([^"]{0,2000})"'),
)

_INTERNAL_EXT_PATTERNS = (
    re.compile(r'internalExt\\?"\s*:\s*\\?"([^"\\]{1,4000})'),
    re.compile(r'"internalExt"\s*:\s*"([^"]{0,4000})"'),
    re.compile(r'internal_ext\\?"\s*:\s*\\?"([^"\\]{1,4000})'),
    re.compile(r'"internal_ext"\s*:\s*"([^"]{0,4000})"'),
)


def _search_first(patterns: Tuple[re.Pattern, ...], text: str, default: str = "") -> str:
    """按顺序试多个正则，返回第一个命中。"""
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return default


def _walk_json_for_key(node: Any, key: str, depth: int = 0) -> Optional[Any]:
    """在 JSON 树里按 key 名深度优先找值（INIT_PROPS 结构随版本变）。"""
    if depth > 12:
        return None
    if isinstance(node, dict):
        if key in node and node[key] not in (None, "", 0):
            return node[key]
        for value in node.values():
            found = _walk_json_for_key(value, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _walk_json_for_key(item, key, depth + 1)
            if found is not None:
                return found
    return None


def extract_room_info(html: str) -> Dict[str, Any]:
    """从直播间 HTML 里抠出连接所需的信息。

    返回 ``{"room_id", "user_unique_id", "cursor", "internal_ext"}``；
    抠不到的项为空串。**不抛异常** —— 调用方决定缺哪项算致命。
    """
    info: Dict[str, Any] = {
        "room_id": "",
        "user_unique_id": "",
        "cursor": "",
        "internal_ext": "",
    }

    # 优先解析 __INIT_PROPS__（结构最稳，且能拿到 cursor/internalExt）
    for pattern in _INIT_PROPS_PATTERNS:
        match = pattern.search(html)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        room_id = _walk_json_for_key(data, "roomId") or _walk_json_for_key(data, "room_id")
        if room_id:
            info["room_id"] = str(room_id)
        uid = _walk_json_for_key(data, "user_unique_id") or _walk_json_for_key(
            data, "userUniqueId"
        )
        if uid:
            info["user_unique_id"] = str(uid)
        cursor = _walk_json_for_key(data, "cursor")
        if cursor:
            info["cursor"] = str(cursor)
        internal_ext = _walk_json_for_key(data, "internalExt") or _walk_json_for_key(
            data, "internal_ext"
        )
        if internal_ext:
            info["internal_ext"] = str(internal_ext)
        break

    # 正则兜底
    if not info["room_id"]:
        info["room_id"] = _search_first(_ROOM_ID_PATTERNS, html)
    if not info["user_unique_id"]:
        info["user_unique_id"] = _search_first(_USER_UNIQUE_ID_PATTERNS, html)
    if not info["cursor"]:
        info["cursor"] = _search_first(_CURSOR_PATTERNS, html)
    if not info["internal_ext"]:
        info["internal_ext"] = _search_first(_INTERNAL_EXT_PATTERNS, html)

    return info


# ----------------------------------------------------------------------
# 帧解析
# ----------------------------------------------------------------------


def parse_headers_list(fields: Dict[int, List[Any]]) -> Dict[str, str]:
    """把 ``PushFrame.headersList`` 解析成普通字典。

    这一层极易被忽略，但它**必不可少**：实测真实下行帧的
    ``payloadEncoding``（field 6）是**空的**，压缩方式只在 headersList 的
    ``compress_type`` 里声明；ack 要回发的 internal_ext 也在它的
    ``im-internal_ext`` 里。只看 field 6 的后果是——连接稳定、却永远 0 条弹幕。
    """
    headers: Dict[str, str] = {}
    for raw in fields.get(PF_HEADERS[0], []):
        if not isinstance(raw, (bytes, bytearray)):
            continue
        try:
            item = parse_message(bytes(raw))
        except ProtobufError:
            continue
        key = _pick_str(item, HEADER_KEY, "")
        if key:
            headers[key] = _pick_str(item, HEADER_VALUE, "")
    return headers


def parse_push_frame(data: bytes) -> Optional[Dict[str, Any]]:
    """解析一个 ``PushFrame``。

    返回 ``{"encoding", "type", "payload", "log_id", "headers"}``；
    解析失败返回 ``None``。
    """
    try:
        fields = parse_message(data)
    except ProtobufError as exc:
        logger.debug("PushFrame 解析失败: %s", exc)
        return None

    payload = _pick(fields, PF_PAYLOAD, b"")
    if not isinstance(payload, bytes):
        payload = b""

    return {
        "encoding": _pick_str(fields, PF_PAYLOAD_ENCODING, "none"),
        "type": _pick_str(fields, PF_PAYLOAD_TYPE, ""),
        "payload": payload,
        "log_id": _pick_int(fields, PF_LOG_ID, 0),
        "headers": parse_headers_list(fields),
    }


MAX_DECOMPRESSED_PAYLOAD = 32 * 1024 * 1024
"""单帧解压后的上限（32MB）。

帧本身的 ``max_size`` 是 8MB，但 gzip 压缩比可以极高：一个 8MB 的压缩帧能
展开成 GB 级，直接 ``gzip.decompress`` 会抛 ``MemoryError``（不在
``(OSError, EOFError, zlib.error)`` 里），把整条接收链打断。这里用
``zlib.decompressobj`` 带上限解压，超限按解压失败处理。
"""


def decode_payload_ex(
    payload: bytes,
    encoding: str = "",
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[bytes, bool]:
    """解压 payload，并额外返回"是否解压成功"。

    :func:`decode_payload` 的语义是"失败也返回原始字节"，调用方无法区分
    "这帧本来没压缩" 和 "这帧是坏 gzip"。本函数把判定结果显式返回，供
    收包循环在解压失败时也计入 ``_parse_errors``（否则最常见的"解不出弹幕"
    场景会绕过连续失败计数，表现为连接稳定、永远 0 条弹幕、永不重连）。

    返回 ``(数据, 是否可信)``：未压缩时 ``(payload, True)``；解压成功
    ``(明文, True)``；声明/魔数表明该压缩但解不开时 ``(payload, False)``。
    """
    if not payload:
        return b"", True

    declared = ""
    if headers:
        declared = (headers.get(HEADER_COMPRESS_TYPE) or "").strip().lower()
    if not declared:
        declared = (encoding or "").strip().lower()

    is_gzip = declared in ("gzip", "gz") or payload[:2] == b"\x1f\x8b"
    if not is_gzip:
        return payload, True

    try:
        # 16 + MAX_WBITS 表示按 gzip 头解析；max_length 限制输出大小。
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        data = decompressor.decompress(payload, MAX_DECOMPRESSED_PAYLOAD)
        if decompressor.unconsumed_tail:
            logger.warning(
                "gzip 解压超过 %d 字节上限，按解压失败处理", MAX_DECOMPRESSED_PAYLOAD
            )
            return payload, False
        data += decompressor.flush()
        return data, True
    except (OSError, EOFError, zlib.error) as exc:
        logger.debug("gzip 解压失败，按未压缩处理: %s", exc)
        return payload, False


def decode_payload(
    payload: bytes,
    encoding: str = "",
    headers: Optional[Dict[str, str]] = None,
) -> bytes:
    """解压 payload（支持 gzip；其它原样返回）。

    压缩方式的判定顺序（**三者取或，任一成立就 gunzip**）：

    1. ``headersList.compress_type == "gzip"`` —— 实测真实帧走的是这一条，
       而 ``payloadEncoding`` 是空的；
    2. ``payloadEncoding == "gzip"`` —— 旧版本/文档里的位置，保留兼容；
    3. payload 前两字节是 ``1f 8b``（gzip 魔数）—— 兜底的硬证据，即使两个
       声明字段都缺失也能识别。

    解压失败时**返回原始 payload 而不是空字节**：服务端并非每一帧都压缩，
    声明 ``compress=gzip`` 只是说"我可能会压"。返回空会让调用方以为收到空帧，
    而原样返回还能让上层试着当 protobuf 解析，最多是解析失败。

    需要区分"没压缩"与"压缩但解不开"时用 :func:`decode_payload_ex`。
    """
    return decode_payload_ex(payload, encoding, headers)[0]



def parse_response(data: bytes) -> Dict[str, Any]:
    """解析 ``Response``：消息列表 + 游标 + internal_ext + 心跳间隔。"""
    try:
        fields = parse_message(data)
    except ProtobufError as exc:
        logger.debug("Response 解析失败: %s", exc)
        return {"messages": [], "cursor": "", "internal_ext": "", "heartbeat": 0, "need_ack": False}

    messages: List[bytes] = []
    for raw in fields.get(1, []):
        if isinstance(raw, bytes):
            messages.append(raw)

    heartbeat = _pick_int(fields, RESP_HEARTBEAT, 0)
    if heartbeat <= 0:
        # 有些版本心跳放在 fetchInterval
        heartbeat = get_int(fields, 3, 0)

    return {
        "messages": messages,
        "cursor": _pick_str(fields, RESP_CURSOR, ""),
        "internal_ext": _pick_str(fields, RESP_INTERNAL_EXT, ""),
        "heartbeat": heartbeat,
        "need_ack": bool(_pick_int(fields, RESP_NEED_ACK, 0)),
        "push_server": _pick_str(fields, RESP_PUSH_SERVER, ""),
    }


def build_ack(internal_ext: str, log_id: int = 0) -> bytes:
    """构造 ack 帧。

    ``PushFrame{ logId, payloadType="ack", payload=internalExt 的原始 UTF-8 字节 }``

    **注意 payload 是 internalExt 原文字节，不是包了一层的 Response 消息。**
    实测抓到的服务端 ack 帧 hex 为::

        10 <logId varint> 3a 03 "ack" 42 <len> "internal_src:pushserver|..."

    其中 ``42`` 是 ``(8<<3)|2``（field 8, length-delimited），后面直接就是
    internalExt 字符串；并没有 ``2a``（field 5）这一层。若多包一层，
    服务端认不出 ack，连接会在约 21 秒后被判超时断开。
    """
    frame = b""
    if log_id:
        frame += encode_field_varint(2, log_id)
    frame += encode_field_string(7, "ack")
    if internal_ext:
        frame += encode_field_bytes(8, internal_ext.encode("utf-8"))
    return frame
