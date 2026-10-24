# -*- coding: utf-8 -*-
"""极简 protobuf wire-format 解析器（零依赖）。

为什么不直接用 ``protobuf`` / ``betterproto``？

- 抖音的弹幕协议是一堆嵌套的 ``Webcast*Message``，字段多但**我们只需要几个**。
  用完整编译器要求用户装 protobuf 编译器或维护 ``.proto`` 文件，
  而这些字段号会随平台改版漂移 —— 一旦对不上，编译期就失败。
- 手写 wire-format 解析是**容错**的：未知字段号直接跳过，
  平台新增字段不会让程序崩溃。这正是抓弹幕场景需要的性质。

wire format 速查（本模块实现的全部内容）::

    tag = (field_number << 3) | wire_type

    wire_type 0 = varint
    wire_type 1 = 64-bit（fixed64 / double）
    wire_type 2 = length-delimited（string / bytes / 嵌套消息 / packed repeated）
    wire_type 5 = 32-bit（fixed32 / float）

只依赖标准库。
"""

from __future__ import annotations

import struct
from typing import Any, Dict, Iterator, List, Optional, Tuple

# wire type 常量
WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LENGTH = 2
WIRE_START_GROUP = 3
WIRE_END_GROUP = 4
WIRE_FIXED32 = 5


class ProtobufError(ValueError):
    """protobuf 解析错误。"""


def read_varint(data: bytes, pos: int) -> Tuple[int, int]:
    """读一个 varint，返回 ``(值, 新位置)``。

    .. note::
       边界检查必须发生在**移位之前**。以前写成"先 ``result |= ... << shift``
       再 ``shift += 7`` 再判 ``shift > 63``"，于是 ``shift == 63`` 那一次
       （第 10 个字节）仍然会把 7 个 bit 左移 63 位塞进结果，实测
       ``b"\\xff" * 9 + b"\\x7f"`` 会返回一个 70 位整数。这个值会经
       ``_pick_int`` 一路流进 ``build_ack`` 编出非法 varint，
       让服务端直接断开连接。
    """
    result = 0
    shift = 0
    length = len(data)
    while pos < length:
        byte = data[pos]
        pos += 1
        if shift >= 64:
            raise ProtobufError("varint 超过 64 位")
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7
    raise ProtobufError("varint 在数据结束前未终止")


def encode_varint(value: int) -> bytes:
    """编码一个 varint（用于构造请求，如 ack 帧）。"""
    if value < 0:
        # 负数按补码处理成 64 位
        value &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def iter_fields(data: bytes) -> Iterator[Tuple[int, int, Any]]:
    """遍历顶层字段，产出 ``(字段号, wire_type, 值)``。

    - varint       → ``int``
    - fixed64      → ``bytes``（8 字节）
    - length-delim → ``bytes``（调用方自己决定当字符串还是嵌套消息解析）
    - fixed32      → ``bytes``（4 字节）

    遇到无法识别的 wire type 会抛 :class:`ProtobufError`；
    遇到截断的数据同样抛错。调用方应当 catch 后放弃这一帧，
    而不是让整个接收循环挂掉。
    """
    pos = 0
    length = len(data)
    while pos < length:
        tag, pos = read_varint(data, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07

        if wire_type == WIRE_VARINT:
            value, pos = read_varint(data, pos)
        elif wire_type == WIRE_FIXED64:
            if pos + 8 > length:
                raise ProtobufError("fixed64 数据不足")
            value = data[pos : pos + 8]
            pos += 8
        elif wire_type == WIRE_LENGTH:
            size, pos = read_varint(data, pos)
            if pos + size > length:
                raise ProtobufError("length-delimited 数据不足")
            value = data[pos : pos + size]
            pos += size
        elif wire_type == WIRE_FIXED32:
            if pos + 4 > length:
                raise ProtobufError("fixed32 数据不足")
            value = data[pos : pos + 4]
            pos += 4
        elif wire_type in (WIRE_START_GROUP, WIRE_END_GROUP):
            # 组（已弃用，抖音协议不用）。**必须抛错而不是 continue**：
            # continue 只是不 yield，却仍把组内的字节当顶层字段继续解析，
            # 结果是静默产出一堆垃圾字段，比直接失败更难查。
            raise ProtobufError(f"不支持 group wire type: {wire_type}")
        else:
            raise ProtobufError(f"未知 wire type: {wire_type}")

        yield (field_number, wire_type, value)


def parse_message(data: bytes) -> Dict[int, List[Any]]:
    """把消息解析成 ``{字段号: [值, ...]}``。

    同一字段号出现多次时全部收集（repeated 字段就是这样编码的）。
    """
    result: Dict[int, List[Any]] = {}
    for field_number, _wire, value in iter_fields(data):
        result.setdefault(field_number, []).append(value)
    return result


# ----------------------------------------------------------------------
# 取值辅助（宽容：类型不对就返回默认值）
# ----------------------------------------------------------------------


def get_int(fields: Dict[int, List[Any]], number: int, default: int = 0) -> int:
    """取一个整型字段（只接受真正的 varint）。

    .. note::
       以前对 ``bytes`` 值走 ``int.from_bytes(value, "little")``，等于把
       任意 fixed64 / length-delimited 内容硬解释成整数：字段是字符串
       ``"12345"`` 时返回 ``228509037105``，是嵌套消息时返回
       ``118074714752026``。抖音侧的 ``_pick_int`` 是"第一个非 0 值即胜出"，
       于是一个垃圾大整数会把后面真正正确的候选字段全部挡掉。
       这里只认 ``int``，其余返回 default，让调用方继续试下一个字段号。
    """
    values = fields.get(number)
    if not values:
        return default
    value = values[0]
    if isinstance(value, int):
        return value
    return default


def get_str(fields: Dict[int, List[Any]], number: int, default: str = "") -> str:
    """取一个字符串字段（UTF-8，容错解码）。

    注意：字段实际是 varint 时**返回 default 而不是把数字转成字符串**。
    字段号漂移时若把数字当文本返回，弹幕内容里会出现 "5" 这种垃圾，
    比直接缺省更难排查。
    """
    values = fields.get(number)
    if not values:
        return default
    value = values[0]
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return default
    return default


def get_bytes(fields: Dict[int, List[Any]], number: int, default: bytes = b"") -> bytes:
    """取一个 bytes 字段。"""
    values = fields.get(number)
    if not values:
        return default
    value = values[0]
    if isinstance(value, bytes):
        return value
    return default


def get_message(fields: Dict[int, List[Any]], number: int) -> Optional[Dict[int, List[Any]]]:
    """取一个嵌套消息字段并递归解析。"""
    raw = get_bytes(fields, number)
    if not raw:
        return None
    try:
        return parse_message(raw)
    except ProtobufError:
        return None


def get_repeated_message(fields: Dict[int, List[Any]], number: int) -> List[Dict[int, List[Any]]]:
    """取 repeated 嵌套消息字段。"""
    out: List[Dict[int, List[Any]]] = []
    for raw in fields.get(number, []):
        if not isinstance(raw, bytes):
            continue
        try:
            out.append(parse_message(raw))
        except ProtobufError:
            continue
    return out


def get_double(fields: Dict[int, List[Any]], number: int, default: float = 0.0) -> float:
    """取一个 double（fixed64）字段。"""
    raw = get_bytes(fields, number)
    if len(raw) != 8:
        return default
    try:
        return struct.unpack("<d", raw)[0]
    except struct.error:
        return default


def get_bool(fields: Dict[int, List[Any]], number: int, default: bool = False) -> bool:
    """取一个 bool 字段。"""
    values = fields.get(number)
    if not values:
        return default
    value = values[0]
    if isinstance(value, int):
        return bool(value)
    return default


# ----------------------------------------------------------------------
# 编码辅助（构造 ack 帧用）
# ----------------------------------------------------------------------


def encode_field_varint(field_number: int, value: int) -> bytes:
    """编码 ``varint`` 字段。"""
    return encode_varint((field_number << 3) | WIRE_VARINT) + encode_varint(value)


def encode_field_bytes(field_number: int, value: bytes) -> bytes:
    """编码 ``length-delimited`` 字段。"""
    return (
        encode_varint((field_number << 3) | WIRE_LENGTH)
        + encode_varint(len(value))
        + value
    )


def encode_field_string(field_number: int, value: str) -> bytes:
    """编码字符串字段。"""
    return encode_field_bytes(field_number, value.encode("utf-8"))


__all__ = [
    "ProtobufError",
    "WIRE_FIXED32",
    "WIRE_FIXED64",
    "WIRE_LENGTH",
    "WIRE_VARINT",
    "encode_field_bytes",
    "encode_field_string",
    "encode_field_varint",
    "encode_varint",
    "get_bool",
    "get_bytes",
    "get_double",
    "get_int",
    "get_message",
    "get_repeated_message",
    "get_str",
    "iter_fields",
    "parse_message",
    "read_varint",
]
