# -*- coding: utf-8 -*-
"""B 站 WBI 签名回归测试（全部离线）。

锁住 2026-10 实测到的那个坑：``getDanmuInfo`` 对**未签名**请求一律回
``{"code": -352}``，而 blivedm 0.1.1 的 ``_init_host_server()`` 不带签名，
于是 ``init_room()`` 抛 ``InitError``，B 站弹幕 100% 连不上。

测试分三层：

1. 纯函数：置换表顺序、参数过滤、``wts``/``w_rid`` 生成（用 B 站文档里
   ``7cd084941338484aae1ad9425b84077c`` + ``4932caff0ff746eab6f01bf08b70ac45``
   → ``ea1db124af3c7062474693fa704f4ff8`` 这个公开样例做向量）。
2. 客户端子类：签名成功走签名数据；签名失败退回默认服务器列表且返回 True
   （旧行为在 ``init_room()`` 里紧接着抛异常，降级从未生效）。
3. 抓取器接线：``connect()`` 必须使用本地子类而不是裸 ``BLiveClient``。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

BILIBILI_PY = (
    Path(__file__).resolve().parents[1] / "src" / "screenkey" / "danmaku" / "bilibili.py"
)


@pytest.fixture(scope="module")
def wbi():
    from screenkey.danmaku import bilibili_wbi

    return bilibili_wbi


@pytest.fixture(scope="module")
def module():
    from screenkey.danmaku import bilibili

    return bilibili


class TestMixinKey:
    def test_known_vector(self, wbi):
        """B 站公开样例：置换表取错一位这里就会红。"""
        key = wbi.mixin_key(
            "7cd084941338484aae1ad9425b84077c",
            "4932caff0ff746eab6f01bf08b70ac45",
        )
        assert key == "ea1db124af3c7062474693fa704f4ff8"

    def test_key_length(self, wbi):
        assert len(wbi.mixin_key("a" * 32, "b" * 32)) == 32


class TestSignParams:
    def test_filters_forbidden_chars(self, wbi):
        signed = wbi.sign_params({"q": "a!b'c(d)e*f", "id": 103}, "KEY", now=1000.0)
        # 值里的 !'()* 必须剔掉，否则服务端算出的 w_rid 不一致
        assert signed["q"] == "abcdef"
        # 非字符串值会被转成字符串参与签名（aiohttp 也这么编码）
        assert str(signed["id"]) == "103"

    def test_adds_wts_and_w_rid(self, wbi):
        signed = wbi.sign_params({"id": 103, "type": 0}, "KEY", now=1700000000.0)
        assert signed["wts"] == 1700000000
        assert len(signed["w_rid"]) == 32
        int(signed["w_rid"], 16)  # 必须是十六进制

    def test_deterministic_for_same_time(self, wbi):
        a = wbi.sign_params({"id": 103}, "KEY", now=1700000000.0)
        b = wbi.sign_params({"id": 103}, "KEY", now=1700000000.0)
        assert a == b

    def test_mixin_key_changes_signature(self, wbi):
        a = wbi.sign_params({"id": 103}, "KEY-A", now=1700000000.0)
        b = wbi.sign_params({"id": 103}, "KEY-B", now=1700000000.0)
        assert a["w_rid"] != b["w_rid"]


class TestSignedClient:
    """``_SignedHostServerClient._init_host_server()`` 的三条路径。"""

    def _make(self, module):
        obj = module._SignedHostServerClient.__new__(module._SignedHostServerClient)
        obj._session = None
        obj._room_id = 34348
        obj._host_server_list = None
        obj._host_server_token = None
        return obj

    def test_signed_success(self, module, monkeypatch):
        servers = [{"host": "tx-live.example", "port": 2243, "wss_port": 443}]

        async def fake(session, room_id):
            return {"host_list": servers, "token": "tok"}

        monkeypatch.setattr(module, "_fetch_signed_danmu_info", fake)
        obj = self._make(module)
        assert asyncio.run(obj._init_host_server()) is True
        assert obj._host_server_list == servers
        assert obj._host_server_token == "tok"

    def test_fallback_to_default_servers(self, module, monkeypatch):
        """签名失败时退回默认列表并返回 True——否则 init_room 直接抛 InitError。"""
        defaults = [{"host": "broadcastlv.chat.bilibili.com", "port": 2243, "wss_port": 443}]

        async def fake(session, room_id):
            return None

        monkeypatch.setattr(module, "_fetch_signed_danmu_info", fake)
        monkeypatch.setattr(module, "_DEFAULT_SERVERS", defaults)
        obj = self._make(module)
        assert asyncio.run(obj._init_host_server()) is True
        assert obj._host_server_list == defaults
        assert obj._host_server_token is None

    def test_no_default_servers_means_failure(self, module, monkeypatch):
        async def fake(session, room_id):
            return {}

        monkeypatch.setattr(module, "_fetch_signed_danmu_info", fake)
        monkeypatch.setattr(module, "_DEFAULT_SERVERS", [])
        obj = self._make(module)
        assert asyncio.run(obj._init_host_server()) is False


class _FakeJsonResponse:
    def __init__(self, payload):
        self.status = 200
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, *args, **kwargs):
        return self._payload


class _FakeRoomSession:
    """只应答 room_init 的假 session（记录请求参数）。"""

    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeJsonResponse(self._payload)


class TestRoomInit:
    def _make(self, module, payload):
        obj = module._SignedHostServerClient.__new__(module._SignedHostServerClient)
        obj._session = _FakeRoomSession(payload)
        obj._tmp_room_id = 103
        obj._room_id = None
        obj._room_short_id = None
        obj._room_owner_uid = None
        obj._host_server_list = None
        obj._host_server_token = None
        obj._ssl = True
        return obj

    def test_room_init_uses_id_param(self, module):
        """``getInfoByRoom?room_id=`` 一律 -352；必须换 ``room_init?id=``。"""
        obj = self._make(module, {
            "code": 0,
            "data": {"room_id": 34348, "short_id": 103, "uid": 1871001},
        })
        assert asyncio.run(obj._init_room_id_and_owner()) is True
        url, kwargs = obj._session.calls[0]
        assert "room_init" in url
        assert kwargs.get("params") == {"id": 103}
        assert obj._room_id == 34348
        assert obj._room_owner_uid == 1871001

    def test_room_init_failure_is_not_fatal(self, module, monkeypatch):
        """房间信息拿不到时用用户给的号继续，别抛 InitError。"""
        obj = self._make(module, {"code": -352, "message": "-352"})
        servers = [{"host": "tx-live.example", "port": 2243, "wss_port": 443}]

        async def fake(session, room_id):
            return {"host_list": servers, "token": "tok"}

        monkeypatch.setattr(module, "_fetch_signed_danmu_info", fake)
        assert asyncio.run(obj.init_room()) is True
        assert obj._room_id == 103
        assert obj._host_server_list == servers


class TestFetcherWiring:
    def test_client_class_is_local_subclass(self, module):
        import blivedm

        assert module._SignedHostServerClient is not None
        assert issubclass(module._SignedHostServerClient, blivedm.BLiveClient)
        # 必须真的覆盖了那个方法，否则等于没改
        assert (
            module._SignedHostServerClient._init_host_server
            is not blivedm.BLiveClient._init_host_server
        )

    def test_connect_uses_signed_client(self):
        source = BILIBILI_PY.read_text(encoding="utf-8")
        assert "_SignedHostServerClient or getattr(blivedm" in source

    def test_diagnosis_prefers_signed_call(self):
        """诊断必须先走签名接口，否则永远只会报 -352。"""
        source = BILIBILI_PY.read_text(encoding="utf-8")
        start = source.index("async def _diagnose_ws_failure")
        end = source.index("async def _wait_ws_ready")
        body = source[start:end]
        assert "_fetch_signed_danmu_info" in body
        assert body.index("_fetch_signed_danmu_info") < body.index("getDanmuInfo?id=")
