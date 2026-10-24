# -*- coding: utf-8 -*-
"""node 运行时查找：随包内置优先，其次环境变量，最后 PATH。

打包版把官方 ``node.exe`` 放在 ``_internal/node/`` 下；源码运行没有这个目录，
退化到 ``SCREENKEY_NODE`` 或 PATH 里的 node。这里用临时目录伪造三种布局。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from screenkey.danmaku import signature


@pytest.fixture()
def clean_env(tmp_path, monkeypatch):
    monkeypatch.delenv(signature.ENV_NODE, raising=False)
    monkeypatch.delenv(signature.ENV_NODE_LEGACY, raising=False)
    monkeypatch.setattr(signature.shutil, "which", lambda name: None)
    # 让源码态的 build/node-runtime 探测落空（临时目录里没有 build/）
    monkeypatch.setattr(signature, "_PACKAGE_DATA", tmp_path / "pkg" / "data")


def _fake_node(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ fake node")
    return path


def test_onedir_bundle_is_used(tmp_path, monkeypatch, clean_env):
    exe = tmp_path / "ScreenKey.exe"
    exe.write_bytes(b"MZ")
    node = _fake_node(tmp_path / "_internal" / "node" / "node.exe")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))

    assert signature.find_node() == str(node)


def test_bundle_beats_env_override(tmp_path, monkeypatch, clean_env):
    """用户机器上装着旧版 node 时，不应改变随包运行时的行为。"""
    exe = tmp_path / "ScreenKey.exe"
    exe.write_bytes(b"MZ")
    node = _fake_node(tmp_path / "_internal" / "node" / "node.exe")
    other = _fake_node(tmp_path / "other-node.exe")
    monkeypatch.setenv(signature.ENV_NODE, str(other))

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))

    assert signature.find_node() == str(node)


def test_onefile_meipass_layout(tmp_path, monkeypatch, clean_env):
    node = _fake_node(tmp_path / "node" / "node.exe")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert signature.find_node() == str(node)


def test_env_override_without_bundle(tmp_path, monkeypatch, clean_env):
    other = _fake_node(tmp_path / "custom" / "node.exe")
    monkeypatch.setenv(signature.ENV_NODE, str(other))

    assert signature.find_node() == str(other)


def test_legacy_env_var_still_works(tmp_path, monkeypatch, clean_env):
    other = _fake_node(tmp_path / "legacy" / "node.exe")
    monkeypatch.setenv(signature.ENV_NODE_LEGACY, str(other))

    assert signature.find_node() == str(other)


def test_nothing_available_returns_none(tmp_path, monkeypatch, clean_env):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "ScreenKey.exe"))

    assert signature.find_node() is None


def test_signature_available_uses_bundled_node(tmp_path, monkeypatch, clean_env):
    """整体判定：内置运行时 + 内置 sign.js 应当直接可用。"""
    exe = tmp_path / "ScreenKey.exe"
    exe.write_bytes(b"MZ")
    _fake_node(tmp_path / "_internal" / "node" / "node.exe")
    sign_js = tmp_path / "data" / "sign.js"
    sign_js.parent.mkdir(parents=True)
    sign_js.write_text("function get_sign(){return 'x'}", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(signature, "_BUNDLED_SIGN_JS", sign_js)

    assert signature.signature_available() is True
