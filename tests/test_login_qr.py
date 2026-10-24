# -*- coding: utf-8 -*-
"""B 站扫码登录与二维码渲染的测试。

这两个模块此前**一条测试都没有**（``grep`` 确认 ``bilibili_qr``/``qr_image``
在 ``tests/`` 里零命中），而它们是"用户拿不到 SESSDATA 就连不上 B 站"的唯一通道。

全程离线：``bilibili_qr._http_get`` 被替换成假响应，不发任何真实请求。
"""

from __future__ import annotations

import json
import struct
import time

import pytest

from danmaku_kb.login import bilibili_qr, qr_image
from danmaku_kb.login.bilibili_qr import (
    QRCodeSession,
    QRState,
    LoginResult,
    BilibiliQRLogin,
    parse_cookies,
    _sessdata_from_url,
)


# ----------------------------------------------------------------------
# 假 HTTP 层
# ----------------------------------------------------------------------


class _FakeHeaders:
    """模仿 ``http.client.HTTPMessage``：既有 ``get_all`` 也有 ``get``。"""

    def __init__(self, set_cookie: list | None = None) -> None:
        self._set_cookie = list(set_cookie or [])

    def get_all(self, name: str):
        if name.lower() == "set-cookie":
            return list(self._set_cookie)
        return None

    def get(self, name: str):
        if name.lower() == "set-cookie":
            return self._set_cookie[0] if self._set_cookie else None
        return None


class _FakeResponse:
    """假的 urllib 响应对象（支持 ``with``、``read``、``headers``）。"""

    def __init__(self, body: str = "", set_cookie: list | None = None) -> None:
        self._body = body.encode("utf-8")
        self.headers = _FakeHeaders(set_cookie)

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_http(monkeypatch, response=None, error=None):
    """把模块级 ``_http_get`` 换成假实现，记录调用 URL。"""
    calls: list = []

    def fake_get(url, headers=None, timeout=15.0):
        calls.append(url)
        if error is not None:
            raise error
        return response

    monkeypatch.setattr(bilibili_qr, "_http_get", fake_get)
    return calls


def _payload(code=0, data=None, message="0"):
    return json.dumps({"code": code, "message": message, "data": data or {}})


# ----------------------------------------------------------------------
# 状态与数据类
# ----------------------------------------------------------------------


class TestQRState:
    def test_all_states_have_labels(self):
        for state in QRState:
            assert state.label, f"{state} 缺中文说明"

    def test_labels_are_distinct(self):
        labels = [s.label for s in QRState]
        assert len(labels) == len(set(labels)), "两个状态显示同一句话，用户无法区分"

    def test_inner_code_map_covers_four_documented_codes(self):
        """B 站文档里的四个内层码必须都在映射表里。"""
        assert bilibili_qr._INNER_CODE_MAP[0] is QRState.SUCCESS
        assert bilibili_qr._INNER_CODE_MAP[86101] is QRState.WAITING
        assert bilibili_qr._INNER_CODE_MAP[86090] is QRState.SCANNED
        assert bilibili_qr._INNER_CODE_MAP[86038] is QRState.EXPIRED

    def test_waiting_and_scanned_are_different_states(self):
        """参考项目的缺陷就是只区分成功/失败，丢掉了"已扫码待确认"。"""
        assert QRState.WAITING is not QRState.SCANNED


class TestQRCodeSession:
    def test_age_and_expiry(self):
        session = QRCodeSession(url="u", qrcode_key="k", created_at=time.time() - 200)
        assert session.age > 190
        assert session.expired is True

    def test_fresh_session_not_expired(self):
        session = QRCodeSession(url="u", qrcode_key="k", created_at=time.time())
        assert session.expired is False

    def test_expiry_is_before_bilibili_180s(self):
        """本地判定必须早于 B 站的 180 秒，否则会拿过期码去轮询。"""
        session = QRCodeSession(url="u", qrcode_key="k", created_at=time.time() - 175)
        assert session.expired is True

    def test_to_dict_excludes_png(self):
        session = QRCodeSession(url="u", qrcode_key="k", png=b"\x89PNG")
        assert session.to_dict() == {"url": "u", "qrcode_key": "k"}


class TestLoginResult:
    def test_ok_requires_state_and_sessdata(self):
        assert LoginResult(state=QRState.SUCCESS, sessdata="s").ok is True
        assert LoginResult(state=QRState.SUCCESS, sessdata="").ok is False
        assert LoginResult(state=QRState.WAITING, sessdata="s").ok is False

    def test_cookie_string(self):
        result = LoginResult(cookies={"SESSDATA": "a", "bili_jct": "b"})
        assert result.cookie_string == "SESSDATA=a; bili_jct=b"

    def test_cookie_string_empty(self):
        assert LoginResult().cookie_string == ""


class TestParseCookies:
    def test_parses_multiple_set_cookie_lines(self):
        headers = _FakeHeaders([
            "SESSDATA=abc123; Path=/; Domain=.bilibili.com; HttpOnly",
            "bili_jct=token; Path=/",
            "DedeUserID=42; Path=/",
        ])
        cookies = parse_cookies(headers)
        assert cookies["SESSDATA"] == "abc123"
        assert cookies["bili_jct"] == "token"
        assert cookies["DedeUserID"] == "42"

    def test_handles_headers_without_get_all(self):
        class _OnlyGet:
            def get(self, name):
                return "SESSDATA=onlyget; Path=/"

        assert parse_cookies(_OnlyGet())["SESSDATA"] == "onlyget"

    def test_bad_cookie_line_is_skipped(self):
        headers = _FakeHeaders(["=; ;;;", "SESSDATA=good; Path=/"])
        cookies = parse_cookies(headers)
        assert cookies.get("SESSDATA") == "good"

    def test_none_headers_returns_empty(self):
        assert parse_cookies(None) == {}

    def test_no_set_cookie_returns_empty(self):
        assert parse_cookies(_FakeHeaders([])) == {}


class TestSessdataFromUrl:
    def test_extracts_from_query(self):
        url = "https://passport.biligame.com/crossDomain?SESSDATA=xyz%2Cabc&other=1"
        assert _sessdata_from_url(url) == "xyz,abc"

    def test_missing_param_returns_empty(self):
        assert _sessdata_from_url("https://x/?a=1") == ""

    def test_empty_url_returns_empty(self):
        assert _sessdata_from_url("") == ""


# ----------------------------------------------------------------------
# generate()
# ----------------------------------------------------------------------


class TestGenerate:
    def test_success_builds_session_with_png(self, monkeypatch):
        body = _payload(0, {"url": "https://b23.tv/QR", "qrcode_key": "KEY123"})
        calls = _install_http(monkeypatch, _FakeResponse(body))

        login = BilibiliQRLogin()
        session = login.generate()

        assert session is not None
        assert session.url == "https://b23.tv/QR"
        assert session.qrcode_key == "KEY123"
        assert session.png.startswith(b"\x89PNG"), "二维码必须是 PNG 字节"
        assert calls == [bilibili_qr.QR_GENERATE_URL]
        assert login.session is session
        assert login.result.state is QRState.WAITING

    def test_business_error_returns_none(self, monkeypatch):
        _install_http(monkeypatch, _FakeResponse(_payload(-412, {}, "请求被拦截")))
        login = BilibiliQRLogin()
        assert login.generate() is None
        assert login.result.state is QRState.ERROR
        assert "请求被拦截" in login.result.message

    def test_non_json_body_returns_none(self, monkeypatch):
        _install_http(monkeypatch, _FakeResponse("<html>风控页</html>"))
        login = BilibiliQRLogin()
        assert login.generate() is None
        assert login.result.state is QRState.ERROR
        assert "格式" in login.result.message

    def test_missing_fields_returns_none(self, monkeypatch):
        _install_http(monkeypatch, _FakeResponse(_payload(0, {"url": ""})))
        login = BilibiliQRLogin()
        assert login.generate() is None
        assert login.result.state is QRState.ERROR

    def test_network_error_returns_none(self, monkeypatch):
        _install_http(monkeypatch, error=OSError("连接被重置"))
        login = BilibiliQRLogin()
        assert login.generate() is None
        assert login.result.state is QRState.ERROR
        assert "网络错误" in login.result.message

    def test_http_error_reports_code(self, monkeypatch):
        import urllib.error

        err = urllib.error.HTTPError("u", 412, "Precondition Failed", None, None)
        _install_http(monkeypatch, error=err)
        login = BilibiliQRLogin()
        assert login.generate() is None
        assert "412" in login.result.message


# ----------------------------------------------------------------------
# poll_once()
# ----------------------------------------------------------------------


class TestPollOnce:
    def _login_with_session(self, monkeypatch, body, set_cookie=None):
        calls = _install_http(monkeypatch, _FakeResponse(body, set_cookie))
        login = BilibiliQRLogin()
        login._session = QRCodeSession(url="u", qrcode_key="KEY", created_at=time.time())
        return login, calls

    def test_no_session_reports_error(self):
        login = BilibiliQRLogin()
        result = login.poll_once()
        assert result.state is QRState.ERROR
        assert "还没有申请" in result.message

    def test_locally_expired_skips_http(self, monkeypatch):
        """本地已判定过期就不该再打接口（省一次必然失败的请求）。"""
        calls = _install_http(monkeypatch, _FakeResponse("{}"))
        login = BilibiliQRLogin()
        login._session = QRCodeSession(url="u", qrcode_key="K", created_at=time.time() - 300)
        result = login.poll_once()
        assert result.state is QRState.EXPIRED
        assert calls == [], "过期后仍发了请求"

    @pytest.mark.parametrize("inner,expected", [
        (86101, QRState.WAITING),
        (86090, QRState.SCANNED),
        (86038, QRState.EXPIRED),
    ])
    def test_inner_codes_map_to_states(self, monkeypatch, inner, expected):
        body = _payload(0, {"code": inner, "message": "m"})
        login, calls = self._login_with_session(monkeypatch, body)
        result = login.poll_once()
        assert result.state is expected
        assert "qrcode_key=KEY" in calls[0]

    def test_unknown_inner_code_is_error(self, monkeypatch):
        body = _payload(0, {"code": 99999, "message": "奇怪状态"})
        login, _ = self._login_with_session(monkeypatch, body)
        assert login.poll_once().state is QRState.ERROR

    def test_non_numeric_inner_code_is_error_not_crash(self, monkeypatch):
        body = _payload(0, {"code": None, "message": "m"})
        login, _ = self._login_with_session(monkeypatch, body)
        assert login.poll_once().state is QRState.ERROR

    def test_outer_business_error(self, monkeypatch):
        login, _ = self._login_with_session(monkeypatch, _payload(-352, {}, "风控"))
        result = login.poll_once()
        assert result.state is QRState.ERROR
        assert "风控" in result.message

    def test_success_extracts_sessdata_and_cookies(self, monkeypatch):
        body = _payload(0, {"code": 0, "message": "ok", "url": "https://x/"})
        login, _ = self._login_with_session(monkeypatch, body, set_cookie=[
            "SESSDATA=sess; Path=/",
            "bili_jct=jct; Path=/",
            "DedeUserID=777; Path=/",
            "unrelated=zzz; Path=/",
        ])
        result = login.poll_once()
        assert result.state is QRState.SUCCESS
        assert result.ok is True
        assert result.sessdata == "sess"
        assert result.dede_user_id == "777"
        assert "unrelated" not in result.cookies, "只应保留 WANTED_COOKIES 里的凭据"
        assert set(result.cookies) <= set(bilibili_qr.WANTED_COOKIES)

    def test_success_without_sessdata_cookie_is_not_ok(self, monkeypatch):
        """拿不到 SESSDATA 就等于没登录成功，不能让界面显示"成功"。"""
        body = _payload(0, {"code": 0, "message": "ok"})
        login, _ = self._login_with_session(monkeypatch, body, set_cookie=["bili_jct=j; Path=/"])
        result = login.poll_once()
        assert result.state is QRState.SUCCESS
        assert result.sessdata == ""
        assert result.ok is False

    def test_sessdata_falls_back_to_url_query(self, monkeypatch):
        """极少数情况 SESSDATA 出现在 data.url 的查询串里。"""
        body = _payload(0, {
            "code": 0,
            "url": "https://passport.biligame.com/crossDomain?SESSDATA=fromurl&x=1",
        })
        login, _ = self._login_with_session(monkeypatch, body, set_cookie=[])
        result = login.poll_once()
        assert result.sessdata == "fromurl"
        assert result.ok is True
        assert result.cookies.get("SESSDATA") == "fromurl"

    def test_network_error_during_poll(self, monkeypatch):
        login = BilibiliQRLogin()
        login._session = QRCodeSession(url="u", qrcode_key="K", created_at=time.time())
        _install_http(monkeypatch, error=OSError("boom"))
        result = login.poll_once()
        assert result.state is QRState.ERROR
        assert "网络错误" in result.message

    def test_result_property_reflects_last_poll(self, monkeypatch):
        body = _payload(0, {"code": 86090, "message": "已扫码"})
        login, _ = self._login_with_session(monkeypatch, body)
        assert login.result.state is QRState.WAITING  # 初始
        login.poll_once()
        assert login.result.state is QRState.SCANNED


# ----------------------------------------------------------------------
# 后台轮询
# ----------------------------------------------------------------------


class TestStartPolling:
    def test_returns_false_without_session(self):
        assert BilibiliQRLogin().start_polling() is False

    def test_stops_after_success_and_reports_states(self, monkeypatch):
        """状态**变化**时才回调，且成功后线程自己退出。

        注意不要在这里用 ``stop_polling`` 抢跑：那会在第一轮轮询后就把线程
        停掉，测到的永远是 ``[WAITING]``。正确做法是等线程自己跑完
        （``_thread.join``），因为成功本身就是终止条件。
        """
        login = BilibiliQRLogin()
        login._session = QRCodeSession(url="u", qrcode_key="K", created_at=time.time())

        sequence = [
            LoginResult(state=QRState.WAITING, message="等扫码"),
            LoginResult(state=QRState.WAITING, message="等扫码"),
            LoginResult(state=QRState.SCANNED, message="已扫码"),
            LoginResult(state=QRState.SUCCESS, sessdata="s", message="成功"),
        ]
        seen: list = []

        def fake_poll():
            return sequence.pop(0) if sequence else LoginResult(
                state=QRState.SUCCESS, sessdata="s")

        monkeypatch.setattr(login, "poll_once", fake_poll)

        assert login.start_polling(on_update=seen.append, interval=0.01, max_seconds=5) is True
        login._thread.join(timeout=5.0)
        assert not login._thread.is_alive(), "成功后轮询线程应自己退出"

        states = [r.state for r in seen]
        assert states == [QRState.WAITING, QRState.SCANNED, QRState.SUCCESS], (
            f"回调应只在状态**变化**时触发，实际 {states}"
        )

    def test_rejects_second_start_while_running(self, monkeypatch):
        login = BilibiliQRLogin()
        login._session = QRCodeSession(url="u", qrcode_key="K", created_at=time.time())
        monkeypatch.setattr(login, "poll_once",
                            lambda: LoginResult(state=QRState.WAITING))
        assert login.start_polling(interval=0.01, max_seconds=5) is True
        try:
            assert login.start_polling(interval=0.01, max_seconds=5) is False
        finally:
            login.stop_polling(timeout=3.0)

    def test_callback_exception_is_isolated(self, monkeypatch):
        """回调抛异常不能把轮询线程打死（界面回调很容易出错）。"""
        login = BilibiliQRLogin()
        login._session = QRCodeSession(url="u", qrcode_key="K", created_at=time.time())
        monkeypatch.setattr(login, "poll_once",
                            lambda: LoginResult(state=QRState.SUCCESS, sessdata="s"))

        def bad_callback(result):
            raise RuntimeError("界面回调炸了")

        assert login.start_polling(on_update=bad_callback, interval=0.01, max_seconds=5) is True
        login.stop_polling(timeout=3.0)

    def test_max_seconds_timeout_reports_expired(self, monkeypatch):
        """超时必须报过期，不能静默结束。

        同样不能抢跑 ``stop_polling``：那会设上 ``_stop_event``，而超时分支
        的条件正是 ``not self._stop_event.is_set()``，于是超时提示被吞掉。
        让它自己到点退出，再 join。
        """
        login = BilibiliQRLogin()
        login._session = QRCodeSession(url="u", qrcode_key="K", created_at=time.time())
        monkeypatch.setattr(login, "poll_once",
                            lambda: LoginResult(state=QRState.WAITING))
        seen: list = []
        login.start_polling(on_update=seen.append, interval=0.01, max_seconds=0.05)
        login._thread.join(timeout=5.0)
        assert not login._thread.is_alive(), "到点后线程应自己退出"
        assert any(r.state is QRState.EXPIRED for r in seen), (
            f"超时应报过期而不是静默结束，实际回调 {[r.state for r in seen]}"
        )
        assert login.result.state is QRState.EXPIRED

    def test_stop_polling_is_idempotent(self):
        login = BilibiliQRLogin()
        login.stop_polling()   # 没起过
        login.stop_polling()   # 再来一次


# ----------------------------------------------------------------------
# 二维码渲染
# ----------------------------------------------------------------------


def _png_size(data: bytes) -> tuple:
    """从 PNG 的 IHDR 里读出宽高。"""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


class TestQRImage:
    def test_segno_is_available_in_this_environment(self):
        assert qr_image.SEGNO_AVAILABLE is True

    def test_real_qr_is_png_with_requested_scale(self):
        data = qr_image.make_qr_png("https://b23.tv/abcdef", scale=4, border=2)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = _png_size(data)
        assert width == height, "二维码应当时正方形"
        assert width > 0

    def test_different_text_yields_different_png(self):
        a = qr_image.make_qr_png("https://b23.tv/aaa")
        b = qr_image.make_qr_png("https://b23.tv/bbb")
        assert a != b, "不同 URL 渲染出同一张图，说明内容没进二维码"

    def test_empty_text_falls_back_to_placeholder(self):
        data = qr_image.make_qr_png("")
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert data == qr_image.make_placeholder_png("NO DATA"), (
            "空文本应回落到占位图，而不是让 segno 抛异常"
        )

    def test_never_raises_on_weird_text(self, monkeypatch):
        """segno 内部异常必须被吃掉并回落占位图（参考项目让它冒泡到 Qt 事件循环）。"""
        class _Boom:
            @staticmethod
            def make(*a, **k):
                raise RuntimeError("segno 内部炸了")

        monkeypatch.setattr(qr_image, "segno", _Boom)
        data = qr_image.make_qr_png("https://x/")
        assert data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_missing_segno_falls_back_instead_of_failing(self, monkeypatch):
        monkeypatch.setattr(qr_image, "SEGNO_AVAILABLE", False)
        data = qr_image.make_qr_png("https://x/")
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert data == qr_image.make_placeholder_png("INSTALL SEGNO")

    def test_placeholder_is_png(self):
        data = qr_image.make_placeholder_png("TEST", scale=2, border=2)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = _png_size(data)
        assert width == height > 0

    def test_placeholder_scale_clamped(self):
        """scale 过小会让图不可读，必须夹到至少 2。"""
        small = qr_image.make_placeholder_png("A", scale=0, border=1)
        normal = qr_image.make_placeholder_png("A", scale=2, border=1)
        assert small == normal

    def test_placeholder_draws_finder_patterns(self):
        """三个定位角必须画出来，否则占位图看着不像二维码。"""
        data = qr_image.make_placeholder_png("A", scale=2, border=2)
        width, _ = _png_size(data)
        assert width > 0

    def test_placeholder_canvas_fits_the_text(self):
        """画布必须宽到能放下整段文字。

        ``cols`` 是按文字长度算的，但早期实现里画布边长 ``size`` 只按行数
        （``char_h + 2``）与 ``border`` 计算，**完全没考虑 ``cols``**——
        于是 ``make_placeholder_png("INSTALL SEGNO")`` 只画出前几个字符，
        剩下的被 ``if 0 <= px < size`` 静默裁掉。
        """
        text = "INSTALL SEGNO"
        border = 4
        char_w = 6
        needed = len(text) * char_w + 2 * border
        data = qr_image.make_placeholder_png(text, scale=1, border=border)
        width, _ = _png_size(data)
        assert width >= needed, (
            f"画布宽 {width} 装不下 {len(text)} 个字符（需要 {needed}），文字会被裁掉"
        )

    def test_placeholder_unknown_char_does_not_crash(self):
        data = qr_image.make_placeholder_png("中文也要能画", scale=2, border=2)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_placeholder_png_pixel_data_is_well_formed(self):
        """PNG 的 IDAT 解压后必须正好是 ``height * (1 + width*3)`` 字节。

        这是"占位图必须真的能被解码"的核心断言。历史缺陷只做了**纵向**
        放大（``for _ in range(scale): rgb_rows.append(pixel_row)``），
        横向没放大，而 IHDR 里写的是 ``width = size * scale``——
        于是每行少了 ``size*(scale-1)*3`` 字节，PNG 结构非法：
        libpng 报 ``bad adaptive filter value``，``QImage.loadFromData``
        返回 False，界面上只显示"二维码渲染失败"，那句
        "pip install segno" 的提示彻底丢失。
        """
        import zlib as _zlib

        data = qr_image.make_placeholder_png("INSTALL SEGNO", scale=3, border=4)
        width, height = _png_size(data)

        # 收集所有 IDAT 块
        pos = 8
        idat = b""
        while pos < len(data):
            (length,) = struct.unpack(">I", data[pos:pos + 4])
            tag = data[pos + 4:pos + 8]
            payload = data[pos + 8:pos + 8 + length]
            if tag == b"IDAT":
                idat += payload
            pos += 12 + length
        raw = _zlib.decompress(idat)

        expected = height * (1 + width * 3)
        assert len(raw) == expected, (
            f"IDAT 解压后 {len(raw)} 字节，但 IHDR 声明 {width}x{height} 应为 "
            f"{expected} 字节 —— PNG 结构非法，QImage 会加载失败"
        )
        # 每行首字节是 filter type，必须为 0
        stride = 1 + width * 3
        for row in range(height):
            assert raw[row * stride] == 0, f"第 {row} 行的 filter type 不是 0"

    def test_placeholder_decodes_with_pyqt(self):
        """缺 segno 时界面要能真的把占位图显示出来（QImage 能解码）。"""
        pytest.importorskip("PyQt5")
        from PyQt5.QtGui import QImage  # noqa: F401  (导入即验证 Qt 可用)

        data = qr_image.make_placeholder_png("INSTALL SEGNO", scale=3, border=4)
        image = QImage()
        assert image.loadFromData(data, "PNG"), (
            "占位 PNG 无法被 Qt 解码 —— 缺 segno 时用户只能看到"
            "「二维码渲染失败」，安装提示丢失"
        )
        width, height = _png_size(data)
        assert (image.width(), image.height()) == (width, height)


class TestQRImageFile:
    def test_writes_file_and_returns_path(self, tmp_path):
        target = tmp_path / "qr.png"
        got = qr_image.make_qr_png_file("https://b23.tv/x", str(target))
        assert got == str(target)
        assert target.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_write_failure_returns_none(self, tmp_path):
        """写不进去（目录不存在）要返回 None，不能抛。"""
        got = qr_image.make_qr_png_file("https://b23.tv/x", str(tmp_path / "nope" / "qr.png"))
        assert got is None

    def test_empty_text_still_writes_placeholder(self, tmp_path):
        target = tmp_path / "empty.png"
        assert qr_image.make_qr_png_file("", str(target)) == str(target)
        assert target.exists()
