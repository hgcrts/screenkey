"""protobuf_lite 测试 —— 零依赖 wire-format 解析器。"""

from __future__ import annotations

import pytest

from danmaku_kb.danmaku.protobuf_lite import (
    ProtobufError,
    encode_field_bytes,
    encode_field_string,
    encode_field_varint,
    encode_varint,
    get_bool,
    get_bytes,
    get_int,
    get_message,
    get_repeated_message,
    get_str,
    iter_fields,
    parse_message,
    read_varint,
)


class TestVarint:
    def test_single_byte(self):
        assert read_varint(b"\x08", 0) == (8, 1)

    def test_multi_byte(self):
        value, pos = read_varint(b"\xac\x02", 0)
        assert value == 300
        assert pos == 2

    def test_encode_roundtrip(self):
        for n in (0, 1, 127, 128, 300, 2**31, 2**63 - 1):
            data = encode_varint(n)
            got, pos = read_varint(data, 0)
            assert got == n, n
            assert pos == len(data)

    def test_encode_negative_uses_twos_complement(self):
        data = encode_varint(-1)
        # 负数按 64 位补码编码，占 10 字节
        assert len(data) == 10

    def test_truncated_raises(self):
        with pytest.raises(ProtobufError):
            read_varint(b"\x80", 0)

    def test_overlong_raises(self):
        # 超过 63 位移位应当报错而不是静默回绕
        with pytest.raises(ProtobufError):
            read_varint(b"\xff" * 12 + b"\x01", 0)


class TestIterFields:
    def test_varint_field(self):
        fields = list(iter_fields(encode_field_varint(1, 42)))
        assert fields == [(1, 0, 42)]

    def test_string_field(self):
        fields = list(iter_fields(encode_field_string(3, "hi")))
        assert fields == [(3, 2, b"hi")]

    def test_bytes_field(self):
        fields = list(iter_fields(encode_field_bytes(8, b"\x00\x01")))
        assert fields == [(8, 2, b"\x00\x01")]

    def test_multiple_fields(self):
        data = encode_field_varint(1, 1) + encode_field_string(2, "x")
        fields = list(iter_fields(data))
        assert fields == [(1, 0, 1), (2, 2, b"x")]

    def test_truncated_length_delimited_raises(self):
        # 声明长度 5 但只有 2 字节
        with pytest.raises(ProtobufError):
            list(iter_fields(b"\x0a\x05ab"))

    def test_unknown_wire_type_raises(self):
        # wire type 3 = START_GROUP，本解析器不支持
        with pytest.raises(ProtobufError):
            list(iter_fields(b"\x0b"))


class TestParseMessage:
    def test_repeated_same_field(self):
        data = encode_field_varint(1, 1) + encode_field_varint(1, 2)
        parsed = parse_message(data)
        assert parsed[1] == [1, 2]

    def test_get_helpers(self):
        data = encode_field_varint(1, 7) + encode_field_string(2, "hello") + encode_field_bytes(3, b"\xff")
        parsed = parse_message(data)
        assert get_int(parsed, 1) == 7
        assert get_str(parsed, 2) == "hello"
        assert get_bytes(parsed, 3) == b"\xff"
        assert get_bool(parsed, 1) is True

    def test_missing_field_default(self):
        parsed = parse_message(encode_field_varint(1, 1))
        assert get_int(parsed, 99, -1) == -1
        assert get_str(parsed, 99, "def") == "def"

    def test_get_message(self):
        inner = encode_field_string(1, "in")
        outer = encode_field_bytes(5, inner)
        parsed = parse_message(outer)
        got = get_message(parsed, 5)
        assert get_str(got, 1) == "in"

    def test_get_repeated_message(self):
        a = encode_field_bytes(1, encode_field_varint(1, 10))
        b = encode_field_bytes(1, encode_field_varint(1, 20))
        parsed = parse_message(a + b)
        items = get_repeated_message(parsed, 1)
        assert [get_int(x, 1) for x in items] == [10, 20]

    def test_wrong_type_returns_default(self):
        # 字段是 varint，却用 get_str 取 —— 不应抛异常
        parsed = parse_message(encode_field_varint(1, 5))
        assert get_str(parsed, 1, "fallback") == "fallback"

    def test_empty_input(self):
        assert parse_message(b"") == {}

    def test_utf8_invalid_replaced(self):
        parsed = parse_message(encode_field_bytes(1, b"\xff\xfe"))
        assert get_str(parsed, 1) == "\ufffd\ufffd"


class TestDouyinEnvelope:
    """用真实形状的信封验证三层解包：PushFrame → Response → Message。"""

    def _push_frame(self, payload: bytes, ptype: str = "msg", encoding: str = "gzip", log_id: int = 0):
        data = b""
        if log_id:
            data += encode_field_varint(2, log_id)
        data += encode_field_string(6, encoding)
        data += encode_field_string(7, ptype)
        data += encode_field_bytes(8, payload)
        return data

    def test_push_frame_roundtrip(self):
        from danmaku_kb.danmaku.douyin import parse_push_frame

        frame = self._push_frame(b"inner", log_id=12345)
        parsed = parse_push_frame(frame)
        assert parsed["type"] == "msg"
        assert parsed["encoding"] == "gzip"
        assert parsed["payload"] == b"inner"
        assert parsed["log_id"] == 12345

    def test_response_with_messages(self):
        from danmaku_kb.danmaku.douyin import parse_response

        msg1 = encode_field_string(1, "WebcastChatMessage")
        msg2 = encode_field_string(1, "WebcastGiftMessage")
        response = (
            encode_field_bytes(1, msg1)
            + encode_field_bytes(1, msg2)
            + encode_field_string(2, "cursor-1")
            + encode_field_string(5, "ext-1")
            + encode_field_varint(8, 15)
            + encode_field_varint(9, 1)
        )
        parsed = parse_response(response)
        assert len(parsed["messages"]) == 2
        assert parsed["cursor"] == "cursor-1"
        assert parsed["internal_ext"] == "ext-1"
        assert parsed["heartbeat"] == 15
        assert parsed["need_ack"] is True

    def test_build_ack_has_type_and_payload(self):
        from danmaku_kb.danmaku.douyin import build_ack, parse_push_frame

        ack = build_ack("internal_src:pushserver|seq:1", log_id=999)
        parsed = parse_push_frame(ack)
        assert parsed["type"] == "ack"
        assert parsed["payload"] == b"internal_src:pushserver|seq:1"
        assert parsed["log_id"] == 999

    def test_gzip_payload_decoded(self):
        import gzip as gz

        from danmaku_kb.danmaku.douyin import decode_payload

        raw = encode_field_string(1, "hello")
        assert decode_payload(gz.compress(raw), "gzip") == raw

    def test_uncompressed_payload_passthrough(self):
        from danmaku_kb.danmaku.douyin import decode_payload

        # 服务端并非所有帧都压缩，解压失败必须回落原样返回
        assert decode_payload(b"\x0a\x01x", "gzip") == b"\x0a\x01x"
