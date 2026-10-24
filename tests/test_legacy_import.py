# -*- coding: utf-8 -*-
"""``ConfigManager.import_legacy`` 的回归测试。

为什么单独一个文件：这个函数此前**零测试覆盖**，而且它对参考项目的
**真实**配置文件格式完全无效——

参考项目 ``userdata/配置文件.json`` 是一个**裸 list**（12 条映射），
而旧实现写的是 ``if not isinstance(data, Mapping): return (0, 0)``，
于是"导入旧配置"永远返回 ``(0, 0)``：用户看到的是"导入成功但一条都没进来"。

本文件同时守住另外两条历史结论：

1. ``overwrite=False`` 时**只在当前映射为空**才导入（这是文档承诺的语义）
2. 三种旧格式都要认：裸 list、``{"keyboard"/"mouse"}``、``{"mappings"/"mouse_mappings"}``
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from danmaku_kb.config.manager import ConfigManager
from danmaku_kb.input.mappings import (
    KeyMapping,
    MouseMapping,
    default_key_mappings,
)

#: 参考项目真实文件的绝对路径（本机存在时才跑那条"真文件"用例）
REFERENCE_PROJECT = Path(r"E:\DSHwork\抖音加B站弹幕键盘")
REFERENCE_CONFIG = REFERENCE_PROJECT / "userdata" / "配置文件.json"

#: 与参考项目真实文件同构的一条键盘映射（字段名逐个照抄）
REAL_STYLE_KEY = {
    "trigger": "j",
    "key": "k",
    "action": "press_and_release",
    "delay": 0.0,
    "cooldown": 0.15,
    "enabled": True,
    "description": "攻击",
    "enable_sensing": False,
    "short_press_count": 1,
    "long_press_count": 6,
    "long_press_duration": 1,
}

#: 与参考项目真实文件同构的一条鼠标映射
REAL_STYLE_MOUSE = {
    "trigger": "左键",
    "button": "left",
    "action": "click",
    "delay": 0.0,
    "cooldown": 0.5,
    "enabled": True,
    "description": "左键点击",
}


def _write(path: Path, payload: object) -> Path:
    """把 payload 写成 UTF-8 JSON（``ensure_ascii=False`` 才配得上中文触发词）。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class TestBareListFormat:
    """裸 list —— 参考项目 ``配置文件.json`` 的真实格式。"""

    def test_bare_list_is_imported(self, tmp_path, manager):
        """核心回归：裸 list 必须真的导入进来（旧实现返回 (0, 0)）。"""

        legacy = tmp_path / "legacy"
        _write(legacy / "配置文件.json", [REAL_STYLE_KEY, REAL_STYLE_KEY | {"trigger": "c"}])

        imported = manager.import_legacy(legacy, overwrite=True)

        assert imported == (2, 0), f"裸 list 导入应得 (2, 0)，实际 {imported}"
        assert len(manager.key_mappings) == 2
        assert isinstance(manager.key_mappings[0], KeyMapping)
        assert [m.trigger for m in manager.key_mappings] == ["j", "c"]
        assert manager.key_mappings[0].key == "k"
        assert manager.key_mappings[0].description == "攻击"
        assert manager.key_mappings[0].cooldown == pytest.approx(0.15)

    def test_bare_list_preserves_every_field(self, tmp_path, manager):
        """逐字段核对，而不是只看条数（条数对了但字段错了同样是坏）。"""

        legacy = tmp_path / "legacy"
        _write(legacy / "配置文件.json", [REAL_STYLE_KEY])
        manager.import_legacy(legacy, overwrite=True)

        m = manager.key_mappings[0]
        assert m.trigger == "j"
        assert m.key == "k"
        assert m.delay == pytest.approx(0.0)
        assert m.cooldown == pytest.approx(0.15)
        assert m.enabled is True
        assert m.description == "攻击"
        assert m.enable_sensing is False
        assert m.short_press_count == 1
        assert m.long_press_count == 6
        assert m.long_press_duration == pytest.approx(1.0)

    def test_bare_list_finds_userdata_subdir(self, tmp_path, manager):
        """``<root>/userdata/配置文件.json`` 是参考项目的实际位置。"""

        legacy = tmp_path / "legacy"
        _write(legacy / "userdata" / "配置文件.json", [REAL_STYLE_KEY])
        assert manager.import_legacy(legacy, overwrite=True) == (1, 0)

    def test_empty_bare_list_imports_nothing(self, tmp_path, manager):
        """空 list 不是错误，只是没东西可导入。"""

        legacy = tmp_path / "legacy"
        _write(legacy / "配置文件.json", [])
        assert manager.import_legacy(legacy, overwrite=True) == (0, 0)


class TestDictFormats:
    """两种 dict 形状（新式 ``keyboard``/``mouse``、旧式 ``mappings``/``mouse_mappings``）。"""

    def test_keyboard_and_mouse_keys(self, tmp_path, manager):
        legacy = tmp_path / "legacy"
        _write(
            legacy / "配置文件.json",
            {"keyboard": [REAL_STYLE_KEY], "mouse": [REAL_STYLE_MOUSE]},
        )

        imported = manager.import_legacy(legacy, overwrite=True)

        assert imported == (1, 1)
        assert isinstance(manager.key_mappings[0], KeyMapping)
        assert isinstance(manager.mouse_mappings[0], MouseMapping)
        assert manager.mouse_mappings[0].button.value == "left"

    def test_mappings_and_mouse_mappings_keys(self, tmp_path, manager):
        legacy = tmp_path / "legacy"
        _write(
            legacy / "配置文件.json",
            {"mappings": [REAL_STYLE_KEY], "mouse_mappings": [REAL_STYLE_MOUSE]},
        )
        assert manager.import_legacy(legacy, overwrite=True) == (1, 1)

    def test_key_mappings_key_also_accepted(self, tmp_path, manager):
        """``key_mappings`` 是第三种可接受的键名。"""

        legacy = tmp_path / "legacy"
        _write(legacy / "配置文件.json", {"key_mappings": [REAL_STYLE_KEY]})
        assert manager.import_legacy(legacy, overwrite=True) == (1, 0)

    def test_keyboard_only(self, tmp_path, manager):
        legacy = tmp_path / "legacy"
        _write(legacy / "配置文件.json", {"keyboard": [REAL_STYLE_KEY]})
        assert manager.import_legacy(legacy, overwrite=True) == (1, 0)


class TestOverwriteSemantics:
    """``overwrite`` 的两种语义，文档里承诺过。"""

    def test_skips_when_mappings_not_empty(self, tmp_path, manager):
        """默认 ``overwrite=False``：已有映射时**一条都不动**。"""

        manager.key_mappings = default_key_mappings()
        before = len(manager.key_mappings)

        legacy = tmp_path / "legacy"
        _write(legacy / "配置文件.json", [REAL_STYLE_KEY])

        assert manager.import_legacy(legacy) == (0, 0)
        assert len(manager.key_mappings) == before
        assert manager.key_mappings[0].trigger == "w", "默认映射不该被覆盖"

    def test_imports_when_empty(self, tmp_path, manager):
        """当前为空时默认参数就能导入（不需要 overwrite=True）。"""

        assert manager.key_mappings == []
        legacy = tmp_path / "legacy"
        _write(legacy / "配置文件.json", [REAL_STYLE_KEY])
        assert manager.import_legacy(legacy) == (1, 0)

    def test_overwrite_replaces_existing(self, tmp_path, manager):
        manager.key_mappings = default_key_mappings()
        legacy = tmp_path / "legacy"
        _write(legacy / "配置文件.json", [REAL_STYLE_KEY])

        assert manager.import_legacy(legacy, overwrite=True) == (1, 0)
        assert len(manager.key_mappings) == 1
        assert manager.key_mappings[0].trigger == "j"


class TestMalformedInput:
    """坏输入不能抛异常 —— 导入旧配置只是便利功能，不该拦住启动。"""

    def test_missing_dir(self, tmp_path, manager):
        assert manager.import_legacy(tmp_path / "does-not-exist", overwrite=True) == (0, 0)

    def test_dir_without_config_file(self, tmp_path, manager):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert manager.import_legacy(empty, overwrite=True) == (0, 0)

    @pytest.mark.parametrize("payload", ["just a string", 42, True, None])
    def test_unrecognised_toplevel_types(self, tmp_path, manager, payload):
        legacy = tmp_path / "legacy"
        _write(legacy / "配置文件.json", payload)
        assert manager.import_legacy(legacy, overwrite=True) == (0, 0)

    def test_corrupt_json_does_not_raise(self, tmp_path, manager):
        """损坏的 JSON 走 ``_read_json`` 的备份分支，返回 ``None``。"""

        legacy = tmp_path / "legacy"
        legacy.mkdir()
        (legacy / "配置文件.json").write_text("{not json", encoding="utf-8")

        assert manager.import_legacy(legacy, overwrite=True) == (0, 0)
        assert (legacy / "配置文件.json.bad").exists(), "损坏文件应被备份"

    def test_garbage_entries_are_skipped(self, tmp_path, manager):
        """list 里混垃圾：能解析的进、解析不了的跳过，不整体失败。"""

        legacy = tmp_path / "legacy"
        _write(legacy / "配置文件.json", [REAL_STYLE_KEY, "garbage", 123, None])
        assert manager.import_legacy(legacy, overwrite=True) == (1, 0)


class TestFindLegacyFile:
    """``_find_legacy_file`` 的候选顺序。"""

    def test_prefers_root_over_userdata(self, tmp_path):
        root_cfg = _write(tmp_path / "配置文件.json", [REAL_STYLE_KEY])
        _write(tmp_path / "userdata" / "配置文件.json", [])
        assert ConfigManager._find_legacy_file(tmp_path) == root_cfg

    def test_falls_back_to_userdata(self, tmp_path):
        userdata_cfg = _write(tmp_path / "userdata" / "配置文件.json", [REAL_STYLE_KEY])
        assert ConfigManager._find_legacy_file(tmp_path) == userdata_cfg

    def test_falls_back_to_src(self, tmp_path):
        src_cfg = _write(tmp_path / "src" / "配置文件.json", [REAL_STYLE_KEY])
        assert ConfigManager._find_legacy_file(tmp_path) == src_cfg

    def test_nonexistent_root(self, tmp_path):
        assert ConfigManager._find_legacy_file(tmp_path / "nope") is None


@pytest.mark.skipif(not REFERENCE_CONFIG.is_file(), reason="本机没有参考项目，跳过")
class TestAgainstRealReferenceProject:
    """直接拿参考项目的**真实文件**跑一遍 —— 这是这个缺陷最初的证据。"""

    def test_real_file_is_bare_list(self):
        data = json.loads(REFERENCE_CONFIG.read_text(encoding="utf-8-sig"))
        assert isinstance(data, list), "参考项目真实格式就是裸 list"
        assert len(data) > 0
        assert {"trigger", "key", "action"} <= set(data[0])

    def test_real_file_imports_all_entries(self, manager):
        expected = len(json.loads(REFERENCE_CONFIG.read_text(encoding="utf-8-sig")))
        imported = manager.import_legacy(REFERENCE_PROJECT, overwrite=True)

        assert imported[0] == expected, (
            f"参考项目有 {expected} 条，只导入了 {imported[0]} 条 —— "
            "裸 list 又没被认出来了"
        )
        assert len(manager.key_mappings) == expected

    def test_real_file_fields_survive(self, manager):
        manager.import_legacy(REFERENCE_PROJECT, overwrite=True)
        raw = json.loads(REFERENCE_CONFIG.read_text(encoding="utf-8-sig"))

        by_trigger = {m.trigger: m for m in manager.key_mappings}
        for entry in raw:
            m = by_trigger[entry["trigger"]]
            assert m.key == entry["key"]
            assert m.description == entry["description"]
            assert m.cooldown == pytest.approx(entry["cooldown"])
            assert m.long_press_duration == pytest.approx(entry["long_press_duration"])
