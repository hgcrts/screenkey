# -*- coding: utf-8 -*-
"""凭据加密（DPAPI）与配置落盘的测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screenkey.config import secrets as S  # noqa: E402
from screenkey.config.manager import ConfigManager  # noqa: E402
from screenkey.config.models import AppConfig  # noqa: E402


class TestSecretsModule:
    def test_plaintext_passthrough(self):
        assert S.encrypt("") == ""
        assert S.decrypt("") == ""
        assert S.decrypt("SESSDATA=abc") == "SESSDATA=abc"
        assert not S.is_encrypted("SESSDATA=abc")

    def test_prefix_recognized(self):
        assert S.is_encrypted(S.PREFIX + "AAAA")
        assert not S.is_encrypted(S.PREFIX)  # 只有前缀、没有内容不算

    @pytest.mark.skipif(not S.dpapi_available(), reason="需要 Windows DPAPI")
    def test_roundtrip(self):
        plain = "SESSDATA=1%2Cabcdef; bili_jct=xyz"
        cipher = S.encrypt(plain)
        assert S.is_encrypted(cipher)
        assert plain not in cipher
        assert S.decrypt(cipher) == plain

    @pytest.mark.skipif(not S.dpapi_available(), reason="需要 Windows DPAPI")
    def test_encrypt_idempotent(self):
        once = S.encrypt("secret")
        assert S.encrypt(once) == once

    def test_corrupt_cipher_returns_empty(self):
        assert S.decrypt(S.PREFIX + "!!!not-base64!!!") == ""

    def test_foreign_cipher_returns_empty(self):
        # 一段合法 base64、但不是 DPAPI 密文
        assert S.decrypt(S.PREFIX + "YWJjZGVmZ2g=") == ""


class TestManagerIntegration:
    @pytest.fixture(autouse=True)
    def _no_credential_adopt(self, monkeypatch):
        """测试环境里别把宿主机的真实凭据捡进临时目录（``adopt_credentials`` 会扫用户目录）。"""
        monkeypatch.setattr(ConfigManager, "candidate_config_dirs", lambda self: [])

    def _manager(self, tmp_path: Path) -> ConfigManager:
        manager = ConfigManager(config_dir=tmp_path, auto_save=False)
        manager.load()
        return manager

    @pytest.mark.skipif(not S.dpapi_available(), reason="需要 Windows DPAPI")
    def test_save_encrypts_and_load_decrypts(self, tmp_path: Path):
        manager = self._manager(tmp_path)
        manager.config.connection.cookie = "ttwid=abc; odin_tt=def"
        manager.config.connection.sessdata = "SESSDATA=1%2Cxyz"
        assert manager.save()

        raw = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
        dump_cookie = raw["config"]["connection"]["cookie"]
        dump_sess = raw["config"]["connection"]["sessdata"]
        assert dump_cookie.startswith(S.PREFIX)
        assert dump_sess.startswith(S.PREFIX)
        assert "ttwid=abc" not in json.dumps(raw, ensure_ascii=False)

        reloaded = self._manager(tmp_path)
        assert reloaded.config.connection.cookie == "ttwid=abc; odin_tt=def"
        assert reloaded.config.connection.sessdata == "SESSDATA=1%2Cxyz"

    def test_plaintext_config_still_loads(self, tmp_path: Path):
        """旧版本写出的明文配置必须还能读（升级不能把用户登录状态弄丢）。"""
        payload = {
            "version": AppConfig.SCHEMA_VERSION,
            "config": {
                "connection": {"cookie": "ttwid=old", "sessdata": "SESSDATA=old"},
            },
        }
        (tmp_path / "settings.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        manager = self._manager(tmp_path)
        assert manager.config.connection.cookie == "ttwid=old"
        assert manager.config.connection.sessdata == "SESSDATA=old"

    @pytest.mark.skipif(not S.dpapi_available(), reason="需要 Windows DPAPI")
    def test_migrate_credentials_rewrites_file(self, tmp_path: Path):
        payload = {
            "version": AppConfig.SCHEMA_VERSION,
            "config": {
                "connection": {"cookie": "ttwid=plain", "sessdata": "SESSDATA=plain"},
            },
        }
        (tmp_path / "settings.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        manager = self._manager(tmp_path)
        assert manager.migrate_credentials() is True

        raw = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert raw["config"]["connection"]["cookie"].startswith(S.PREFIX)
        assert raw["config"]["connection"]["sessdata"].startswith(S.PREFIX)

        # 迁移后再迁移是空操作，且值仍能读回
        again = self._manager(tmp_path)
        assert again.migrate_credentials() is False
        assert again.config.connection.sessdata == "SESSDATA=plain"

    def test_migrate_noop_without_credentials(self, tmp_path: Path):
        manager = self._manager(tmp_path)
        assert manager.migrate_credentials() is False
