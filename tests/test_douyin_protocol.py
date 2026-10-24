"""抖音协议回归测试 —— 锁定实测得出的字段号与解压规则。

这些断言全部来自**真实抓包**（`E:\\DSHwork\\.tmp` 下的实测脚本），
不是从网上抄的 proto 文件。每一条都对应一个曾经让"连接正常但永远 0 条弹幕"
或"连上就被拒绝"的真实缺陷，所以改动时不要凭直觉调整。
"""

from __future__ import annotations

import gzip

from danmaku_kb.danmaku.douyin import (
    HEADER_COMPRESS_TYPE,
    HEADER_INTERNAL_EXT,
    MSG_METHOD,
    MSG_MSG_ID,
    MSG_PAYLOAD,
    build_ack,
    decode_payload,
    extract_room_info,
    parse_headers_list,
    parse_push_frame,
    parse_response,
)
from danmaku_kb.danmaku.protobuf_lite import (
    encode_field_bytes,
    encode_field_string,
    encode_field_varint,
    parse_message,
)


def _headers_list(items) -> bytes:
    """构造 PushFrame.headersList 的字节（每个元素是 HeadersList{1:key,2:value}）。"""
    out = b""
    for key, value in items:
        item = encode_field_string(1, key) + encode_field_string(2, value)
        out += encode_field_bytes(5, item)
    return out


def _push_frame(ptype="msg", payload=b"", log_id=0, headers=b"") -> bytes:
    frame = b""
    if log_id:
        frame += encode_field_varint(2, log_id)
    if headers:
        frame += headers
    if ptype:
        frame += encode_field_string(7, ptype)
    if payload:
        frame += encode_field_bytes(8, payload)
    return frame


class TestHeaderList:
    """headersList 是实测的关键：压缩方式与 ack 要用的 internal_ext 都在这里。"""

    def test_parses_key_value_pairs(self):
        raw = _push_frame(
            headers=_headers_list(
                [
                    ("compress_type", "gzip"),
                    ("im-internal_ext", "internal_src:pushserver|seq:1"),
                ]
            )
        )
        frame = parse_push_frame(raw)
        assert frame is not None
        assert frame["headers"][HEADER_COMPRESS_TYPE] == "gzip"
        assert frame["headers"][HEADER_INTERNAL_EXT] == "internal_src:pushserver|seq:1"

    def test_parse_headers_list_ignores_garbage_entries(self):
        """非 bytes 的项要跳过而不是抛异常。"""
        assert parse_headers_list({5: [123, "notbytes"]}) == {}
        assert parse_headers_list({}) == {}


class TestDecodePayload:
    """压缩方式的判定不能只看 payloadEncoding —— 实测它是空的。"""

    def test_gzip_via_headers_when_encoding_empty(self):
        """真实帧的 payloadEncoding 为空，压缩只在 headersList 里声明。"""
        original = b"hello danmaku" * 20
        frame = parse_push_frame(
            _push_frame(
                payload=gzip.compress(original),
                headers=_headers_list([("compress_type", "gzip")]),
            )
        )
        assert frame is not None
        # 关键：encoding 为空（"none"），但靠 headers 仍要解压成功
        assert frame["encoding"] in ("none", "")
        assert decode_payload(frame["payload"], frame["encoding"], frame["headers"]) == original

    def test_gzip_detected_by_magic_bytes_even_without_any_declaration(self):
        """两个声明都缺失时，靠 gzip 魔数 1f8b 仍要识别出来。"""
        original = b"x" * 500
        data = gzip.compress(original)
        assert data[:2] == b"\x1f\x8b"
        assert decode_payload(data, "", {}) == original

    def test_gzip_via_encoding_for_backward_compat(self):
        original = b"legacy path" * 10
        assert decode_payload(gzip.compress(original), "gzip", {}) == original

    def test_plain_payload_is_passed_through(self):
        """没声明压缩、也没有 gzip 魔数 → 原样返回。"""
        raw = b"\x08\x01\x12\x03abc"
        assert decode_payload(raw, "", {}) == raw
        assert decode_payload(raw, "none", {}) == raw

    def test_corrupt_gzip_returns_raw_not_empty(self):
        """坏数据返回原始字节：返回空会让上层误以为收到空帧。"""
        broken = b"\x1f\x8b" + b"\x00" * 3
        assert decode_payload(broken, "gzip", {}) == broken

    def test_empty_payload(self):
        assert decode_payload(b"", "", {}) == b""


class TestMessageEnvelopeFieldNumbers:
    """信封字段号是实测值；写错会让每条消息被静默丢弃。"""

    def test_payload_is_field_2(self):
        assert MSG_PAYLOAD == (2,)
        envelope = encode_field_string(1, "WebcastChatMessage") + encode_field_bytes(2, b"\x08\x01")
        fields = parse_message(envelope)
        assert fields[2][0] == b"\x08\x01"
        assert fields[1][0] == b"WebcastChatMessage"

    def test_method_is_field_1_and_msgid_is_field_3(self):
        assert MSG_METHOD == (1,)
        assert MSG_MSG_ID == (3,)

    def test_field_6_and_8_are_not_the_payload(self):
        """6/8 分别是 needWrdsStore(bool) 与 wrdsSubKey(string) —— 不是消息体。

        这是曾经的真实缺陷：MSG_PAYLOAD 写成 (6, 8)，于是
        「WebcastRoomDataSyncMessage」这类消息全都取不到 payload。
        """
        assert MSG_PAYLOAD == (2,)
        assert 6 not in MSG_PAYLOAD
        assert 8 not in MSG_PAYLOAD


class TestParseResponse:
    def test_extracts_messages_cursor_and_ext(self):
        inner = b"\x08\x01"
        payload = (
            encode_field_bytes(1, inner)
            + encode_field_string(2, "t-123_r-1_d-1_u-1")
            + encode_field_string(5, "internal_src:pushserver|seq:1")
            + encode_field_varint(8, 15)
            + encode_field_varint(9, 1)
        )
        response = parse_response(payload)
        assert response["messages"] == [inner]
        assert response["cursor"] == "t-123_r-1_d-1_u-1"
        assert response["internal_ext"] == "internal_src:pushserver|seq:1"
        assert response["heartbeat"] == 15
        assert response["need_ack"] is True

    def test_garbage_returns_empty_shape(self):
        response = parse_response(b"\xff\xff\xff")
        assert response["messages"] == []
        assert response["need_ack"] is False


class TestBuildAck:
    """ack 帧结构是实测 hex：10<logId> 3a03"ack" 42<len>internalExt 原文。"""

    def test_structure_has_no_extra_response_wrapper(self):
        ack = build_ack("internal_src:pushserver|seq:1", log_id=686646978616963919)
        fields = parse_message(ack)
        assert fields[2][0] == 686646978616963919  # logId 原样回传
        assert fields[7][0] == b"ack"
        assert fields[8][0] == b"internal_src:pushserver|seq:1"
        # 关键：不能多包一层 field 5（Response 的 internalExt）
        assert 5 not in fields

    def test_matches_observed_server_frame_prefix(self):
        ack = build_ack("internal_src:x", log_id=1)
        assert ack.startswith(b"\x10\x01")  # field2 wire0 varint 1
        assert b"\x3a\x03ack" in ack  # field7 len3 "ack"

    def test_without_log_id_and_without_ext(self):
        ack = build_ack("", 0)
        fields = parse_message(ack)
        assert fields[7][0] == b"ack"
        assert 2 not in fields
        assert 8 not in fields


class TestPushFrameType:
    def test_msg_and_hb_types(self):
        frame = parse_push_frame(_push_frame(ptype="msg", payload=b"abc"))
        assert frame is not None
        assert frame["type"] == "msg"
        assert frame["payload"] == b"abc"

        hb = parse_push_frame(b"\x3a\x02hb")
        assert hb is not None
        assert hb["type"] == "hb"
        assert hb["payload"] == b""

    def test_log_id_is_field_2(self):
        frame = parse_push_frame(_push_frame(ptype="msg", payload=b"x", log_id=99))
        assert frame is not None
        assert frame["log_id"] == 99

    def test_unparsable_returns_none(self):
        assert parse_push_frame(b"\xff\xff") is None


class TestExtractRoomInfo:
    """抖音页面内联 JSON 被转义过，正则必须容忍反斜杠。"""

    def test_escaped_form(self):
        """页面里的真实形式是**一个**反斜杠 + 引号（实测网页源码）。

        注意 raw 字符串里要写 ``\\"`` 才是"一个反斜杠跟着一个引号"；
        写成 ``\\\\"`` 是两个反斜杠，那不是抖音页面的形式。
        """
        html = r'{"roomId\":\"7694959653804935955\",\"user_unique_id\":\"7694971917535479339\"}'
        info = extract_room_info(html)
        assert info["room_id"] == "7694959653804935955"
        assert info["user_unique_id"] == "7694971917535479339"

    def test_unescaped_form(self):
        html = '{"roomId":"7694902743525264166","user_unique_id":"1234567890123456789"}'
        info = extract_room_info(html)
        assert info["room_id"] == "7694902743525264166"
        assert info["user_unique_id"] == "1234567890123456789"

    def test_missing_returns_empty(self):
        info = extract_room_info("<html>验证</html>")
        assert info["room_id"] == ""
        assert info["user_unique_id"] == ""


class TestCookieHygiene:
    """Cookie 头被 Morsel 文本污染会导致握手被拒（HTTP 200 / InvalidStatus）。"""

    def test_cookie_map_skips_set_cookie_attributes(self):
        from danmaku_kb.danmaku.douyin import DouyinFetcher

        fetcher = DouyinFetcher()
        fetcher.cookie = (
            "ttwid=1%7Cabc%7C1791625631%7Cdeadbeef; "
            "Domain=.douyin.com; Path=/; expires=Tue, 05 Oct 2027 09:46:42 GMT; "
            "SameSite=None; Secure; UIFID=xyz"
        )
        mapping = fetcher._cookie_map()
        assert mapping["ttwid"] == "1%7Cabc%7C1791625631%7Cdeadbeef"
        assert mapping["UIFID"] == "xyz"
        for attribute in ("Domain", "Path", "expires", "SameSite", "Secure"):
            assert attribute not in mapping

    def test_ensure_ttwid_rejects_polluted_value(self):
        """被污染的 ttwid（含 ':' 或空格）必须视为"没有"，否则会跳过暖场。

        真实症状：`ttwid=Set-Cookie: ttwid=1%7C...` 被当成合法值 ->
        跳过首页暖场 -> 带着畸形 Cookie 握手 -> 服务端 HTTP 200 拒绝升级。
        """
        from danmaku_kb.danmaku.douyin import DouyinFetcher

        fetcher = DouyinFetcher()
        fetcher.cookie = "ttwid=Set-Cookie: ttwid=1%7Cabc; Domain=douyin.com"
        assert fetcher._ensure_ttwid() == ""

    def test_ensure_ttwid_accepts_real_value(self):
        from danmaku_kb.danmaku.douyin import DouyinFetcher

        fetcher = DouyinFetcher()
        fetcher.cookie = "UIFID=abc; ttwid=1%7CB7xt%7C1791625601%7C27c2c8f0; UIFID_TEMP=z"
        assert fetcher._ensure_ttwid() == "1%7CB7xt%7C1791625601%7C27c2c8f0"

    def test_ensure_ttwid_absent(self):
        from danmaku_kb.danmaku.douyin import DouyinFetcher

        fetcher = DouyinFetcher()
        fetcher.cookie = "UIFID=abc"
        assert fetcher._ensure_ttwid() == ""


class TestSignatureFieldOrder:
    """签名 13 字段的顺序是协议的一部分，改顺序会算错签名。"""

    def test_sign_md5_fields_exact_order(self):
        from danmaku_kb.danmaku.signature import SIGN_MD5_FIELDS

        assert SIGN_MD5_FIELDS == (
            "live_id",
            "aid",
            "version_code",
            "webcast_sdk_version",
            "room_id",
            "sub_room_id",
            "sub_channel_id",
            "did_rule",
            "user_unique_id",
            "device_platform",
            "device_type",
            "ac",
            "identity",
        )

    def test_build_md5_param_missing_fields_become_empty(self):
        from danmaku_kb.danmaku.signature import build_md5_param

        text = build_md5_param({"live_id": "1", "aid": "6383", "room_id": "123"})
        assert text.startswith("live_id=1,aid=6383,")
        assert "sub_room_id=," in text
        assert text.endswith("device_type=,ac=,identity=")

    def test_md5_hex_of_real_capture(self):
        """实测抓到的签名输入串，锁死 md5 结果。"""
        from danmaku_kb.danmaku.signature import build_md5_param, md5_hex

        params = {
            "live_id": "1",
            "aid": "6383",
            "version_code": "180800",
            "webcast_sdk_version": "1.0.14-beta.0",
            "room_id": "7694959653804935955",
            "did_rule": "3",
            "user_unique_id": "7319483754668557238",
            "device_platform": "web",
            "identity": "audience",
        }
        expected = (
            "live_id=1,aid=6383,version_code=180800,webcast_sdk_version=1.0.14-beta.0,"
            "room_id=7694959653804935955,sub_room_id=,sub_channel_id=,did_rule=3,"
            "user_unique_id=7319483754668557238,device_platform=web,device_type=,ac=,identity=audience"
        )
        assert build_md5_param(params) == expected
        # 实测抓包值（room_id=7694959653804935955）——改动字段顺序或取值会打破它
        assert md5_hex(expected) == "777e47759bbd505c1b453acdb47b8e29"


class TestWsUrlParams:
    """URL 参数表必须逐字段对齐浏览器；四个空值字段也必须出现。"""

    def _url_params(self):
        from urllib.parse import parse_qs, urlparse

        from danmaku_kb.danmaku.douyin import DouyinFetcher

        fetcher = DouyinFetcher()
        # 注意：DouyinFetcher.room_id 是覆盖后的只读 property（读 _douyin_room_id），
        # 给 _room_id 赋值不会影响它，internal_ext 会是空 room_id。
        fetcher._douyin_room_id = "7694959653804935955"
        fetcher._room_id = "7694959653804935955"
        fetcher.user_unique_id = "7319483754668557238"
        fetcher.device_id = "7319483754668557238"
        url = fetcher._build_ws_url()
        # keep_blank_values=True 是必须的：这四个字段的值就是空串，
        # 默认 False 会把它们整个丢掉，测试就查不到"存在性"了。
        return parse_qs(urlparse(url).query, keep_blank_values=True)

    def test_signature_sensitive_empty_fields_are_present(self):
        """这四个是签名 13 字段之一，缺了服务端复算签名会对不上。"""
        params = self._url_params()
        for name in ("sub_room_id", "sub_channel_id", "device_type", "ac"):
            assert name in params, name
            assert params[name] == [""]

    def test_no_extra_debug_device_id_web_rid_params(self):
        """多传参数会被服务端拒绝，这三个是曾经多传的。"""
        params = self._url_params()
        for name in ("debug", "device_id", "web_rid"):
            assert name not in params, name

    def test_internal_ext_never_empty(self):
        """internal_ext 可以为"假值以外的任意串"，但**绝不能留空**。

        实测：空 internal_ext 约 21 秒后 1006 断开；服务端不校验里面的
        first_req_ms/fetch_time 是否新鲜，所以固定值即可。
        注意分隔符是冒号（``wss_push_room_id:123``），不是等号。
        """
        params = self._url_params()
        assert params["internal_ext"][0]
        ext = params["internal_ext"][0]
        assert "wss_push_room_id:7694959653804935955" in ext
        assert ext.startswith("internal_src:")

    def test_cursor_has_fallback(self):
        params = self._url_params()
        assert params["cursor"][0]

    def test_host_is_default(self):
        from danmaku_kb.danmaku.douyin import DEFAULT_WS_HOST

        assert "webcast100-ws-web-lq.douyin.com" in DEFAULT_WS_HOST
        assert DEFAULT_WS_HOST.endswith("/webcast/im/push/v2/")


class TestAckSelection:
    """internal_ext 在 Response 与 headersList 两处都可能出现，只认一个会漏 ack。"""

    def test_headers_internal_ext_used_when_response_lacks_it(self):
        from danmaku_kb.danmaku.douyin import DouyinFetcher

        fetcher = DouyinFetcher()
        inner = encode_field_string(2, "t-1")
        payload = encode_field_bytes(1, inner) + encode_field_varint(9, 1)
        raw = _push_frame(
            payload=payload,
            log_id=7,
            headers=_headers_list([("im-internal_ext", "internal_src:fromheaders")]),
        )
        fetcher._handle_frame_sync(raw)
        pending = fetcher._pending_ack
        assert pending is not None
        fields = parse_message(pending)
        assert fields[8][0] == b"internal_src:fromheaders"
        assert fields[2][0] == 7

    def test_response_internal_ext_wins_when_present(self):
        from danmaku_kb.danmaku.douyin import DouyinFetcher

        fetcher = DouyinFetcher()
        payload = encode_field_string(5, "internal_src:fromresponse") + encode_field_varint(9, 1)
        raw = _push_frame(
            payload=payload,
            log_id=3,
            headers=_headers_list([("im-internal_ext", "internal_src:fromheaders")]),
        )
        fetcher._handle_frame_sync(raw)
        fields = parse_message(fetcher._pending_ack)
        assert fields[8][0] == b"internal_src:fromresponse"

    def test_no_internal_ext_means_no_ack(self):
        from danmaku_kb.danmaku.douyin import DouyinFetcher

        fetcher = DouyinFetcher()
        raw = _push_frame(payload=encode_field_string(2, "t-1"), log_id=1)
        fetcher._handle_frame_sync(raw)
        assert fetcher._pending_ack is None


class TestEndToEndFrameDecode:
    """一个压缩帧从字节到消息体的完整链路（模拟真实抓包结构）。"""

    def test_gzip_frame_decodes_to_chat_message(self):
        # ChatMessage{2:User{3:nickname}, 3:content}
        user = encode_field_string(3, "我也不是至尊宝") + encode_field_varint(1, 12345)
        chat = encode_field_bytes(2, user) + encode_field_string(3, "第一")
        envelope = encode_field_string(1, "WebcastChatMessage") + encode_field_bytes(2, chat)
        response = encode_field_bytes(1, envelope) + encode_field_varint(9, 1)
        raw = _push_frame(
            payload=gzip.compress(response),
            headers=_headers_list([("compress_type", "gzip")]),
        )

        frame = parse_push_frame(raw)
        assert frame is not None
        decoded = decode_payload(frame["payload"], frame["encoding"], frame["headers"])
        response_fields = parse_response(decoded)
        assert len(response_fields["messages"]) == 1

        envelope_fields = parse_message(response_fields["messages"][0])
        assert envelope_fields[MSG_METHOD[0]][0] == b"WebcastChatMessage"
        chat_fields = parse_message(envelope_fields[MSG_PAYLOAD[0]][0])
        assert chat_fields[3][0].decode("utf-8") == "第一"
        nickname = parse_message(chat_fields[2][0])[3][0].decode("utf-8")
        assert nickname == "我也不是至尊宝"
