# -*- coding: utf-8 -*-
"""改名迁移测试：``~/.danmaku_kb`` → ``~/.screenkey``。

v3.0.0 把配置目录从 ``.danmaku_kb`` 换成 ``.screenkey``，老用户升级后
不能丢登录凭证。这里用临时目录冒充用户主目录，验证：

1. 新目录不存在、旧目录有配置 → 复制（不是移动，旧目录保留）；
2. 新目录已存在 → 不覆盖（否则用户在新版本里的改动会被旧内容顶掉）；
3. 只有旧目录时才复制；两者都不存在时什么都不做；
4. 环境变量 ``DANMAKU_KB_CONFIG_DIR``（改名前的名字）仍然被读取。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from screenkey.config import manager as config_manager


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("SCREENKEY_CONFIG_DIR", raising=False)
    monkeypatch.delenv("DANMAKU_KB_CONFIG_DIR", raising=False)
    return home


def _write_settings(folder: Path, sessdata: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / config_manager.SETTINGS_FILE_NAME
    path.write_text(
        json.dumps({"version": 3, "config": {"connection": {"sessdata": sessdata}}}),
        encoding="utf-8",
    )
    return path


def test_migrates_legacy_dir_and_keeps_original(fake_home):
    legacy = fake_home / config_manager.LEGACY_DIR_NAME
    _write_settings(legacy, "OLD-SESSDATA")

    migrated = config_manager.migrate_legacy_config_dir()

    current = fake_home / config_manager.DEFAULT_DIR_NAME
    assert migrated == current
    assert (current / config_manager.SETTINGS_FILE_NAME).is_file()
    assert "OLD-SESSDATA" in (current / config_manager.SETTINGS_FILE_NAME).read_text(encoding="utf-8")
    # 旧目录必须保留：用户可能还要回滚到旧版本
    assert (legacy / config_manager.SETTINGS_FILE_NAME).is_file()


def test_does_not_overwrite_existing_new_dir(fake_home):
    _write_settings(fake_home / config_manager.LEGACY_DIR_NAME, "OLD")
    _write_settings(fake_home / config_manager.DEFAULT_DIR_NAME, "NEW")

    assert config_manager.migrate_legacy_config_dir() is None
    current = fake_home / config_manager.DEFAULT_DIR_NAME / config_manager.SETTINGS_FILE_NAME
    assert "NEW" in current.read_text(encoding="utf-8")


def test_no_legacy_dir_is_noop(fake_home):
    assert config_manager.migrate_legacy_config_dir() is None
    assert not (fake_home / config_manager.DEFAULT_DIR_NAME).exists()


def test_manager_uses_new_dir_after_migration(fake_home):
    _write_settings(fake_home / config_manager.LEGACY_DIR_NAME, "OLD-SESSDATA")

    manager = config_manager.ConfigManager()

    assert manager.config_dir == fake_home / config_manager.DEFAULT_DIR_NAME
    assert manager.load() is True
    assert manager.config.connection.sessdata == "OLD-SESSDATA"


def test_legacy_env_var_still_read(fake_home, tmp_path, monkeypatch):
    legacy_override = tmp_path / "custom-config"
    legacy_override.mkdir()
    monkeypatch.setenv("DANMAKU_KB_CONFIG_DIR", str(legacy_override))

    assert config_manager.default_config_dir() == legacy_override


def test_new_env_var_wins_over_legacy(fake_home, tmp_path, monkeypatch):
    new_override = tmp_path / "new-config"
    legacy_override = tmp_path / "old-config"
    monkeypatch.setenv("SCREENKEY_CONFIG_DIR", str(new_override))
    monkeypatch.setenv("DANMAKU_KB_CONFIG_DIR", str(legacy_override))

    assert config_manager.default_config_dir() == new_override
