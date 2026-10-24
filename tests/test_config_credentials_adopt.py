# -*- coding: utf-8 -*-
"""配置目录回落时的凭证找回。

真实故障（2026-10-21~22，用户现场）：

* 程序正常启动读 ``C:\\Users\\<用户>\\.screenkey\\settings.json``（里面存着他的
  抖音 Cookie）；
* 但在受限环境启动时 ``probe_writable()`` 判主目录不可写，程序**静默**换成
  ``<项目>\\.screenkey``，从此读的是另一份（空）配置；
* 用户打开程序看到输入框空空，于是判定"cookie 没了"。

这里锁住两层补救：

1. 换目录时若新目录还没有 settings.json，把旧的那份整体复制过去；
2. 新目录已有文件但两条凭证都为空时，只把凭证从别的目录捡回来。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _write_settings(folder: Path, *, cookie: str = "", sessdata: str = "", room: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "settings.json"
    payload = {
        "version": 1,
        "config": {
            "connection": {
                "platform": "bilibili",
                "room": room,
                "sessdata": sessdata,
                "cookie": cookie,
            }
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class TestAdoptSettings:
    """换目录：新目录还没有配置文件时，把旧配置整体搬过去。"""

    def test_copies_when_target_missing(self, tmp_path):
        from screenkey.config.manager import adopt_settings

        source = tmp_path / "home"
        target = tmp_path / "portable"
        _write_settings(source, cookie="ttwid=1%7Cabc")
        target.mkdir()

        assert adopt_settings(source, target) is True
        copied = json.loads((target / "settings.json").read_text(encoding="utf-8"))
        assert copied["config"]["connection"]["cookie"] == "ttwid=1%7Cabc"

    def test_never_overwrites_existing_target(self, tmp_path):
        from screenkey.config.manager import adopt_settings

        source = tmp_path / "home"
        target = tmp_path / "portable"
        _write_settings(source, cookie="old=1")
        _write_settings(target, cookie="new=2")

        assert adopt_settings(source, target) is False
        kept = json.loads((target / "settings.json").read_text(encoding="utf-8"))
        assert kept["config"]["connection"]["cookie"] == "new=2"

    def test_missing_source_is_noop(self, tmp_path):
        from screenkey.config.manager import adopt_settings

        target = tmp_path / "portable"
        assert adopt_settings(tmp_path / "home", target) is False
        assert not (target / "settings.json").exists()

    def test_writable_config_dir_fallback_carries_settings(self, tmp_path, monkeypatch):
        """回落到便携目录的那一刻就要把旧配置带过去。"""
        from screenkey.config import manager as manager_mod

        home = tmp_path / "home"
        portable = tmp_path / "portable"
        _write_settings(home, cookie="ttwid=1%7Cfromhome")
        portable.mkdir()

        monkeypatch.delenv(manager_mod.ENV_CONFIG_DIR, raising=False)
        monkeypatch.setattr(manager_mod, "default_config_dir", lambda: home)
        monkeypatch.setattr(manager_mod, "portable_config_dir", lambda: portable)
        monkeypatch.setattr(manager_mod, "_probe_writable", lambda path: Path(path) == portable)

        assert manager_mod.writable_config_dir() == portable
        copied = portable / "settings.json"
        assert copied.is_file(), "换目录后凭证必须跟过来，否则用户以为 cookie 丢了"
        assert "fromhome" in copied.read_text(encoding="utf-8")


class TestAdoptCredentials:
    """新目录已有文件但没凭证时，只补凭证，不动其他设置。"""

    def test_credentials_recovered_from_other_dir(self, tmp_path, monkeypatch):
        from screenkey.config.manager import ConfigManager

        other = tmp_path / "other"
        _write_settings(other, cookie="ttwid=1%7Crescue", sessdata="sess-1", room="https://live.douyin.com/123")

        manager = ConfigManager(config_dir=tmp_path / "local", auto_save=False)
        monkeypatch.setattr(manager, "candidate_config_dirs", lambda: [other])
        manager.load()

        conn = manager.config.connection
        assert conn.cookie == "ttwid=1%7Crescue"
        assert conn.sessdata == "sess-1"
        # 房间号不在"捡回"范围内：备用目录里的设置优先
        assert conn.room == ""

    def test_local_credentials_win(self, tmp_path, monkeypatch):
        from screenkey.config.manager import ConfigManager

        other = tmp_path / "other"
        _write_settings(other, cookie="ttwid=other")
        local = tmp_path / "local"
        _write_settings(local, cookie="ttwid=local")

        manager = ConfigManager(config_dir=local, auto_save=False)
        monkeypatch.setattr(manager, "candidate_config_dirs", lambda: [other])
        manager.load()

        assert manager.config.connection.cookie == "ttwid=local"

    def test_no_candidate_has_credentials(self, tmp_path, monkeypatch):
        from screenkey.config.manager import ConfigManager

        other = tmp_path / "other"
        other.mkdir(parents=True, exist_ok=True)

        manager = ConfigManager(config_dir=tmp_path / "local", auto_save=False)
        monkeypatch.setattr(manager, "candidate_config_dirs", lambda: [other])
        assert manager.adopt_credentials() is False
        assert manager.config.connection.cookie == ""

    def test_broken_json_is_ignored(self, tmp_path):
        from screenkey.config.manager import ConfigManager

        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / "settings.json").write_text("{not json", encoding="utf-8")

        assert ConfigManager._read_credentials(broken / "settings.json") is None

    def test_candidate_dirs_exclude_self(self, tmp_path, monkeypatch):
        from screenkey.config import manager as manager_mod

        monkeypatch.setattr(manager_mod, "default_config_dir", lambda: tmp_path / "cfg")
        monkeypatch.setattr(manager_mod, "portable_config_dir", lambda: tmp_path / "portable")

        manager = manager_mod.ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
        dirs = manager.candidate_config_dirs()
        assert (tmp_path / "cfg") not in dirs
        assert tmp_path / "portable" in dirs


@pytest.fixture(scope="module")
def qt():
    """离屏 QApplication；没有 PyQt5 就跳过。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt5", reason="需要 PyQt5 才能测界面")

    from screenkey.ui.qt_env import configure_qt_environment

    configure_qt_environment()

    from PyQt5.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt, tmp_path):
    from screenkey.config.manager import ConfigManager
    from screenkey.ui.main_window import MainWindow

    manager = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
    manager.load()
    window = MainWindow(config=manager.config, config_manager=manager)
    try:
        yield window
    finally:
        window.close()


class TestConfigPathHint:
    """界面上必须写明程序现在读的是哪一份 settings.json。"""

    def test_path_is_shown(self, window, tmp_path):
        text = window.config_path_label.text()
        assert "settings.json" in text
        assert str(tmp_path / "cfg") in text

    def test_fallback_is_called_out(self, window, monkeypatch):
        """用的是回落目录时必须写出来，否则用户以为配置被清空了。"""
        from screenkey.config import manager as manager_mod

        monkeypatch.setattr(manager_mod, "default_config_dir", lambda: Path("C:/Users/someone/.screenkey"))
        window._refresh_config_path()  # noqa: SLF001
        assert "已回落到程序目录" in window.config_path_label.text()

    def test_no_fallback_note_in_normal_dir(self, window, tmp_path, monkeypatch):
        from screenkey.config import manager as manager_mod

        monkeypatch.setattr(manager_mod, "default_config_dir", lambda: tmp_path / "cfg")
        window._refresh_config_path()  # noqa: SLF001
        assert "已回落到程序目录" not in window.config_path_label.text()

    def test_unsaved_marker(self, window):
        """文件还没落盘时给出提示，而不是让用户去猜。"""
        assert "尚未保存过" in window.config_path_label.text()

    def test_marker_disappears_after_save(self, window):
        window.manager.save()
        window._refresh_config_path()  # noqa: SLF001 - 直接测刷新逻辑
        assert "尚未保存过" not in window.config_path_label.text()

    def test_open_dir_failure_is_reported(self, window, monkeypatch):
        from screenkey.ui import main_window as mw_mod

        class _NoOp:
            @staticmethod
            def openUrl(_url):  # noqa: N802 - 模拟 Qt 静态方法
                return False

        monkeypatch.setattr(mw_mod, "QDesktopServices", _NoOp)
        window._open_config_dir()  # noqa: SLF001
        assert "手动打开" in window.log_view.toPlainText()
