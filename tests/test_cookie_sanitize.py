# -*- coding: utf-8 -*-
"""抖音 Cookie 里的非法字符：所谓"连接不上直播间"，其实是字符串坏了。

真实故障（2026-10-22，用户报「抖音链接报错」）：

用户从浏览器复制了一份 **1024 字符**的 cookie，但中间第 512 个字符被换成了
``…`` —— 复制源把超长值省略了。之后 ``parse_cookie_string``、配置保存、
``check_cookie_text`` 全都没意见（ttwid 还在，界面照样显示"有效"），
一直走到 ``websockets.connect()`` 才炸::

    invalid Cookie header: ttwid=1%7C5SYJ...…...

然后重试 5 次，每次都是同一句 —— 因为它不是网络问题，也不是房间号问题。

这一组测试锁住四件事：

1. 清洗函数认得出「…」截断痕迹，也清得掉其它 HTTP 头放不下的字符；
2. ``check_cookie_text``（界面校验）不再对残缺 cookie 说"有效"；
3. websockets 确实拒绝原始值、接受清洗后的值（这是报错的出处）；
4. 握手期的 header 错误被翻译成人话，并且**不再重试**。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from danmaku_kb.login.douyin import (
    check_cookie_text,
    parse_cookie_string,
    sanitize_cookie_header,
)


#: 一份干净的 cookie（用户的正常形态）
CLEAN = "ttwid=1%7Cabc123%7C1792512465; odin_tt=deadbeef; __ac_nonce=0123"


def truncated_cookie() -> str:
    """用户现场的形状：中间被换成省略号，首尾内容都还在。"""

    return (
        "ttwid=1%7Cabc; odin_tt=deadbeef; "
        + "A" * 40
        + "\u2026"
        + "B" * 40
        + "; sessionid=xyz"
    )


class TestSanitizeCookieHeader:
    def test_clean_cookie_is_left_alone(self):
        result = sanitize_cookie_header(CLEAN)
        assert result.value == CLEAN
        assert result.notes == []
        assert result.truncated is False
        assert result.changed is False

    def test_empty_input(self):
        result = sanitize_cookie_header("")
        assert result.value == ""
        assert result.notes == []

    def test_ellipsis_is_detected_and_removed(self):
        raw = truncated_cookie()
        result = sanitize_cookie_header(raw)

        assert result.truncated is True
        assert "\u2026" not in result.value
        assert any("…" in note for note in result.notes)
        # 首尾内容必须保住：ttwid 还在
        assert parse_cookie_string(result.value).get("ttwid") == "1%7Cabc"
        # 省略号两侧的碎片拼回了一个完整项（原来被 parse 直接丢弃）
        assert {"odin_tt", "sessionid"} <= set(parse_cookie_string(result.value))
        assert len(parse_cookie_string(result.value)) == len(parse_cookie_string(raw)) + 1

    def test_control_and_non_ascii_chars_are_removed(self):
        raw = "ttwid=abc\x00def; odin_tt=中文值; __ac_nonce=x\u200by"
        result = sanitize_cookie_header(raw)

        assert result.truncated is False
        assert all(0x20 <= ord(ch) <= 0x7E or ch == "\t" for ch in result.value)
        assert "ttwid=abcdef" in result.value
        assert any("去掉" in note for note in result.notes)

    def test_fullwidth_punctuation_is_normalized(self):
        result = sanitize_cookie_header("ttwid=abc；odin_tt=def＝ghi")

        assert result.value == "ttwid=abc; odin_tt=def=ghi"
        assert parse_cookie_string(result.value) == {"ttwid": "abc", "odin_tt": "def=ghi"}

    def test_whitespace_and_separators_are_normalized(self):
        result = sanitize_cookie_header("  ttwid=a ;   odin_tt=b  ;  ")
        assert result.value == "ttwid=a; odin_tt=b"


class TestCheckCookieText:
    def test_truncated_cookie_is_rejected_with_actionable_text(self):
        ok, message = check_cookie_text(truncated_cookie())
        assert ok is False
        # 必须明确告诉用户"复制被省略了、要重粘"，而不是只说"无效"
        assert "…" in message
        assert "重新" in message

    def test_clean_cookie_still_passes(self):
        ok, _ = check_cookie_text(CLEAN)
        assert ok is True


class TestWebsocketsGroundTruth:
    """报错出处：websockets 按 HTTP 头规则校验值，非法字符直接抛。"""

    def test_raw_truncated_value_is_rejected(self):
        datastructures = pytest.importorskip("websockets.datastructures")
        with pytest.raises(Exception) as excinfo:
            datastructures.Headers({"Cookie": truncated_cookie()})
        assert "invalid Cookie header" in str(excinfo.value)
        assert excinfo.value.__class__.__name__ in {"InvalidHeader", "InvalidHeaderValue"}

    def test_sanitized_value_is_accepted(self):
        datastructures = pytest.importorskip("websockets.datastructures")
        cleaned = sanitize_cookie_header(truncated_cookie()).value
        assert datastructures.Headers({"Cookie": cleaned})["Cookie"] == cleaned


class TestFetcherHeaders:
    def test_headers_never_contain_illegal_chars(self):
        from danmaku_kb.danmaku.douyin import DouyinFetcher

        value = DouyinFetcher(cookie=truncated_cookie())._headers()["Cookie"]
        assert "\u2026" not in value
        datastructures = pytest.importorskip("websockets.datastructures")
        datastructures.Headers({"Cookie": value})  # 不抛就算过

    def test_clean_cookie_is_passed_through_unchanged(self):
        from danmaku_kb.danmaku.douyin import DouyinFetcher

        assert DouyinFetcher(cookie=CLEAN)._headers()["Cookie"] == CLEAN

    def test_ttwid_extraction_ignores_ellipsis(self):
        from danmaku_kb.danmaku.douyin import DouyinFetcher

        assert DouyinFetcher(cookie=truncated_cookie())._ensure_ttwid() == "1%7Cabc"


class TestHeaderErrorTranslation:
    def test_recognizes_invalid_cookie_header(self):
        from danmaku_kb.danmaku.douyin import _looks_like_header_error

        assert _looks_like_header_error(Exception("invalid Cookie header: ttwid=1"))
        assert not _looks_like_header_error(ConnectionResetError("connection reset"))

    def test_message_is_actionable_and_truncates_long_values(self):
        from danmaku_kb.danmaku.douyin import _header_error_text

        text = _header_error_text(Exception("invalid Cookie header: " + "t" * 500))
        assert "重新完整粘贴" in text
        assert len(text) < 400

    def test_cookie_header_error_is_runtime_error(self):
        from danmaku_kb.danmaku.douyin import CookieHeaderError

        assert issubclass(CookieHeaderError, RuntimeError)


class TestNoPointlessRetry:
    def test_header_error_stops_immediately(self):
        """握手期的 header 错误只报一次：重试一万次也不会变好。"""

        from danmaku_kb.danmaku.douyin import CookieHeaderError, DouyinFetcher

        fetcher = DouyinFetcher(cookie=truncated_cookie())
        calls = {"connect": 0, "errors": [], "status": []}

        async def boom() -> None:
            calls["connect"] += 1
            raise CookieHeaderError("Cookie 有非法字符")

        fetcher._connect_once = boom  # type: ignore[assignment]
        fetcher.on_error(calls["errors"].append)
        fetcher.on_status(lambda connected, text="": calls["status"].append(connected))

        assert asyncio.run(fetcher._run_connection_loop()) is False
        assert calls["connect"] == 1, "不该重试"
        assert len(calls["errors"]) == 1
        assert "非法字符" in calls["errors"][0]
        assert calls["status"] == [], "重试提示不该出现"


class TestStopIsNotReconnect:
    def test_pressing_stop_does_not_report_retry(self):
        """用户按"停止"时不该闪过"正在重试"，会被误读成连不上。"""

        from danmaku_kb.danmaku.douyin import DouyinFetcher

        fetcher = DouyinFetcher(cookie=CLEAN)
        status: list = []

        async def stopped() -> None:
            fetcher._stopping = True
            raise ConnectionResetError("connection closed")

        fetcher._connect_once = stopped  # type: ignore[assignment]
        fetcher.on_status(lambda connected, text="": status.append((connected, text)))

        assert asyncio.run(fetcher._run_connection_loop()) is False
        assert not any("重试" in (text or "") for _, text in status)


@pytest.fixture(scope="module")
def qt():
    """离屏 QApplication；没有 PyQt5 就跳过界面那两条。"""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt5", reason="需要 PyQt5 才能测界面")

    from danmaku_kb.ui.qt_env import configure_qt_environment

    configure_qt_environment()

    from PyQt5.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


class TestCredentialSummaryWarning:
    """主界面那行「凭证状态」必须把 cookie 的毛病摆出来。"""

    def test_truncated_cookie_warning_is_visible(self, qt):
        from types import SimpleNamespace

        from danmaku_kb.config.models import Platform
        from danmaku_kb.ui.main_window import credential_summary

        connection = SimpleNamespace(cookie=truncated_cookie(), sessdata="")
        text = credential_summary(connection, Platform.DOUYIN)

        assert "…" in text
        assert "重新" in text

    def test_clean_cookie_has_no_warning(self, qt):
        from types import SimpleNamespace

        from danmaku_kb.config.models import Platform
        from danmaku_kb.ui.main_window import credential_summary

        connection = SimpleNamespace(cookie=CLEAN, sessdata="")
        text = credential_summary(connection, Platform.DOUYIN)

        assert "⚠" not in text
