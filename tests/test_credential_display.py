# -*- coding: utf-8 -*-
"""凭证展示：两个平台的凭证分开存，界面必须让人分得清。

真实场景（2026-10-21）：用户在「获取凭证」里保存了抖音 Cookie，但主界面
平台停在 B 站，凭证框显示的是空的 SESSDATA —— 看起来像"cookie 没了"，
其实 settings.json 里一直存着。这里锁住两件事：

1. 起始状态就要显示「抖音 Cookie 已保存」，不能只留一个空框；
2. 切换平台后，输入框里必须是那个平台各自的凭证，互不串台。
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="module")
def qt():
    """离屏 QApplication；没有 PyQt5 就跳过整组。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt5", reason="需要 PyQt5 才能测界面")

    from screenkey.ui.qt_env import configure_qt_environment

    configure_qt_environment()

    from PyQt5.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt, tmp_path):
    """和用户现场一致：平台停在 B 站，抖音 Cookie 已保存。"""
    from screenkey.config.manager import ConfigManager
    from screenkey.config.models import Platform
    from screenkey.ui.main_window import MainWindow

    manager = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
    manager.load()
    conn = manager.config.connection
    conn.platform = Platform.BILIBILI
    conn.sessdata = ""
    conn.cookie = "ttwid=1%7Cabc123; odin_tt=def456"
    conn.room = "https://live.bilibili.com/21414905"

    window = MainWindow(config=manager.config, config_manager=manager)
    try:
        yield window
    finally:
        window.close()


class TestCredentialSummary:
    def test_douyin_cookie_is_reported_while_on_bilibili(self, window):
        """B 站平台下也必须看到"抖音 Cookie 已保存"，否则就像丢了。"""
        summary = window.cred_saved.text()
        assert "抖音 Cookie 已保存" in summary
        assert "2 项" in summary
        assert "含 ttwid" in summary
        assert "B 站 SESSDATA 未保存" in summary

    def test_other_platform_hint_is_shown(self, window):
        """当前平台没凭证、另一个平台有时，要明确说"切换平台即可看到"。"""
        summary = window.cred_saved.text()
        assert "切换平台" in summary
        # 输入框本身仍只显示当前平台（B 站）的凭证，也就是空
        assert window.cred_edit.text() == ""

    def test_missing_ttwid_is_called_out(self, window):
        window.config.connection.cookie = "sessionid=onlysession"
        window._refresh_cred_summary()
        assert "缺少 ttwid" in window.cred_saved.text()


class TestPlatformSwitch:
    def test_switching_to_douyin_shows_its_cookie(self, window):
        from screenkey.config.models import Platform

        window._select_platform(Platform.DOUYIN)
        assert window._current_platform() == Platform.DOUYIN
        assert window.cred_edit.text() == "ttwid=1%7Cabc123; odin_tt=def456"
        assert "含 ttwid" in window.cred_saved.text()

    def test_switching_back_restores_empty_bilibili_credential(self, window):
        from screenkey.config.models import Platform

        window._select_platform(Platform.DOUYIN)
        window._select_platform(Platform.BILIBILI)
        assert window.cred_edit.text() == ""
        # 抖音那份还在，状态行继续提示
        assert "抖音 Cookie 已保存" in window.cred_saved.text()


class TestCredentialSummaryFunction:
    """不依赖 Qt 的纯函数：文案只由 config 内容决定，方便直接回归。"""

    @staticmethod
    def _conn(cookie: str = "", sessdata: str = ""):
        from types import SimpleNamespace

        return SimpleNamespace(cookie=cookie, sessdata=sessdata)

    def test_only_douyin_cookie_on_bilibili_platform(self):
        from screenkey.config.models import Platform
        from screenkey.ui.main_window import credential_summary

        text = credential_summary(self._conn(cookie="ttwid=abc"), Platform.BILIBILI)
        assert "抖音 Cookie 已保存（1 项，含 ttwid）" in text
        assert "B 站 SESSDATA 未保存" in text
        assert "切换平台即可看到" in text

    def test_both_saved_has_no_switch_hint(self):
        from screenkey.config.models import Platform
        from screenkey.ui.main_window import credential_summary

        text = credential_summary(
            self._conn(cookie="ttwid=abc", sessdata="SESSDATA=x"), Platform.BILIBILI
        )
        assert "B 站 SESSDATA 已保存" in text
        assert "抖音 Cookie 已保存" in text
        assert "切换平台" not in text

    def test_only_sessdata_on_douyin_platform(self):
        from screenkey.config.models import Platform
        from screenkey.ui.main_window import credential_summary

        text = credential_summary(self._conn(sessdata="SESSDATA=x"), Platform.DOUYIN)
        assert "抖音 Cookie 未保存" in text
        assert "B 站的凭证已保存" in text

    def test_nothing_saved_has_no_hint(self):
        from screenkey.config.models import Platform
        from screenkey.ui.main_window import credential_summary

        text = credential_summary(self._conn(), Platform.BILIBILI)
        assert "B 站 SESSDATA 未保存" in text
        assert "抖音 Cookie 未保存" in text
        assert "切换平台" not in text

