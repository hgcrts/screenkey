# -*- coding: utf-8 -*-
"""抖音 Cookie 文本解析的测试。

这个模块此前**一条测试都没有**，而 ``parse_cookie_string`` 是"用户能不能连上
抖音"的唯一入口。实测确认过两个真实失效的格式：

* DevTools 表格粘贴 ``ttwid\\t1%7Cabc`` → ``{"ttwid": ""}``（名字留下、值丢失）
* 请求头整行 ``Cookie: ttwid=xxx`` → ``{}``（整行被丢弃）

而界面引导（``DOUYIN_GUIDE_STEPS``）恰恰教用户从 DevTools 复制 —— 等于把
用户直接导向这两个坑。下面每条用例都对应一种用户真的会粘进来的形态。
"""

from __future__ import annotations

from danmaku_kb.login.douyin import (
    IMPORTANT_COOKIE_NAMES,
    build_cookie_header,
    check_cookie_text,
    detect_cookie_source,
    extract_ttwid,
    parse_cookie_string,
    validate_cookie_string,
)


class TestParseCookieStringBasicFormats:
    def test_semicolon_separated(self):
        assert parse_cookie_string("a=1; b=2") == {"a": "1", "b": "2"}

    def test_multiline(self):
        assert parse_cookie_string("a=1\nb=2") == {"a": "1", "b": "2"}

    def test_value_may_contain_equals(self):
        """只在第一个 ``=`` 处切分，值里的 ``=`` 要保留。"""
        assert parse_cookie_string("ttwid=abc==") == {"ttwid": "abc=="}

    def test_empty_text(self):
        assert parse_cookie_string("") == {}

    def test_whitespace_only(self):
        assert parse_cookie_string("   \n\t \n") == {}


class TestParseDevtoolsTablePaste:
    """DevTools → Application → Cookies 表格复制出来的 ``name<TAB>value``。"""

    def test_single_row(self):
        # 这条以前返回 {"ttwid": ""}：值被 TAB 切走当成了裸名字
        assert parse_cookie_string("ttwid\t1%7Cabc") == {"ttwid": "1%7Cabc"}

    def test_multi_row(self):
        text = "ttwid\t1%7Cabc\nsessionid\txyz\nmsToken\tabc="
        assert parse_cookie_string(text) == {
            "ttwid": "1%7Cabc",
            "sessionid": "xyz",
            "msToken": "abc=",
        }

    def test_quoted_name_and_value(self):
        """从表格复制时可能带引号，两侧引号都要剥掉。"""
        assert parse_cookie_string('"ttwid"\t"abc"') == {"ttwid": "abc"}

    def test_equals_in_first_cell_falls_back_to_normal_path(self):
        """首列本身含 ``=`` 时不是表格形态，走普通解析（``a=1<TAB>b=2``）。"""
        assert parse_cookie_string("a=1\tb=2") == {"a": "1", "b": "2"}


class TestParseHeaderLinePaste:
    """用户常把整行请求头粘进来（``Cookie: ...``）。"""

    def test_cookie_header_prefix_is_stripped(self):
        assert parse_cookie_string("Cookie: ttwid=xxx; sessionid=yyy") == {
            "ttwid": "xxx",
            "sessionid": "yyy",
        }

    def test_set_cookie_prefix_case_insensitive(self):
        assert parse_cookie_string("SET-COOKIE: ttwid=xxx") == {"ttwid": "xxx"}

    def test_header_with_surrounding_spaces(self):
        assert parse_cookie_string("  cookie :  a=1; b=2  ") == {"a": "1", "b": "2"}


class TestDetectCookieSource:
    def test_header(self):
        assert detect_cookie_source("Cookie: a=1") == "请求头"

    def test_devtools_table(self):
        assert detect_cookie_source("a\t1") == "DevTools 表格"

    def test_plain_text(self):
        assert detect_cookie_source("a=1; b=2") == "cookie 文本"

    def test_empty(self):
        assert detect_cookie_source("") == ""


class TestValidateCookieString:
    def test_source_is_recorded(self):
        """``source`` 以前恒为空字符串，用户看不出程序怎么理解了他的粘贴。"""
        info = validate_cookie_string("ttwid\t1%7Cabc")
        assert info.source == "DevTools 表格"
        assert info.cookies["ttwid"] == "1%7Cabc"

    def test_missing_lists_every_important_cookie(self):
        info = validate_cookie_string("ttwid=abc")
        assert info.ok is True
        assert "sessionid" in info.missing
        assert "ttwid" not in info.missing
        assert set(info.missing) | {"ttwid"} == set(IMPORTANT_COOKIE_NAMES)

    def test_ok_requires_ttwid(self):
        assert validate_cookie_string("sessionid=abc").ok is False
        assert validate_cookie_string("ttwid=abc").ok is True


class TestCheckCookieText:
    def test_empty_text_rejected(self):
        ok, message = check_cookie_text("")
        assert ok is False
        assert "没有解析到" in message

    def test_missing_ttwid_rejected(self):
        ok, message = check_cookie_text("sessionid=abc")
        assert ok is False
        assert "ttwid" in message

    def test_empty_ttwid_value_has_hint(self):
        """只复制了名字没复制值，要给出针对性提示而不是笼统报错。"""
        ok, message = check_cookie_text("ttwid\t")
        assert ok is False
        assert "空的" in message

    def test_valid_with_session(self):
        ok, message = check_cookie_text("ttwid=a; sessionid=b")
        assert ok is True
        assert "有效" in message

    def test_valid_without_session(self):
        ok, message = check_cookie_text("ttwid=a")
        assert ok is True
        assert "未登录" in message

    def test_message_mentions_detected_source(self):
        """格式识别结果要回显给用户，粘错格式时能立刻发现。"""
        ok, message = check_cookie_text("ttwid\tabc")
        assert ok is True
        assert "DevTools 表格" in message


class TestBuildCookieHeader:
    def test_skips_empty_values(self):
        """空值项不能拼出 ``ttwid=;`` 这种无效头。"""
        assert build_cookie_header({"a": "1", "b": ""}) == "a=1"

    def test_empty_dict(self):
        assert build_cookie_header({}) == ""


class TestExtractTtwid:
    def test_from_plain_cookie(self):
        assert extract_ttwid("a=1; ttwid=xyz") == "xyz"

    def test_from_header_line(self):
        assert extract_ttwid("Cookie: ttwid=xyz") == "xyz"

    def test_from_devtools_table(self):
        assert extract_ttwid("ttwid\txyz") == "xyz"

    def test_absent(self):
        assert extract_ttwid("a=1") is None

    def test_empty_value_returns_none(self):
        assert extract_ttwid("ttwid=") is None
