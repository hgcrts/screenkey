"""映射数据模型与序列化测试。

参考项目在这块有过真实缺陷：鼠标映射完全没有持久化。本套件把
"每种映射都能往返"作为硬契约。
"""

from __future__ import annotations

import pytest

from screenkey.input.mappings import (
    InputType,
    KeyAction,
    KeyMapping,
    MatchMode,
    MouseAction,
    MouseButton,
    MouseMapping,
    default_key_mappings,
    default_mouse_mappings,
    key_mappings_from_list,
    key_mappings_to_list,
    mouse_mappings_from_list,
    mouse_mappings_to_list,
)


class TestDefaults:
    def test_key_defaults_non_empty(self):
        mappings = default_key_mappings()
        assert len(mappings) >= 20
        assert all(isinstance(m, KeyMapping) for m in mappings)

    def test_mouse_defaults_non_empty(self):
        mappings = default_mouse_mappings()
        assert len(mappings) >= 5
        assert all(isinstance(m, MouseMapping) for m in mappings)

    def test_core_wasd_present(self):
        triggers = {m.trigger for m in default_key_mappings()}
        for t in ("w", "s", "a", "d"):
            assert t in triggers, f"缺少 {t} 映射"

    def test_arrows_mapped(self):
        by_trigger = {m.trigger: m for m in default_key_mappings()}
        assert by_trigger["w"].key == "up"
        assert by_trigger["s"].key == "down"
        assert by_trigger["a"].key == "left"
        assert by_trigger["d"].key == "right"

    def test_mouse_buttons_covered(self):
        buttons = {m.button for m in default_mouse_mappings()}
        assert MouseButton.LEFT in buttons
        assert MouseButton.RIGHT in buttons

    def test_any_trigger_is_always_valid(self):
        # 触发词是任意文本，不该被过滤
        for m in default_key_mappings():
            assert m.trigger


class TestEnums:
    def test_coerce_from_value(self):
        assert KeyAction.coerce("press") is KeyAction.PRESS
        assert MouseButton.coerce("right") is MouseButton.RIGHT
        assert MatchMode.coerce("exact") is MatchMode.EXACT

    def test_coerce_unknown_falls_back(self):
        assert KeyAction.coerce("nonsense") is KeyAction.PRESS_AND_RELEASE
        assert MatchMode.coerce(None) is MatchMode.EXACT

    def test_coerce_passthrough(self):
        assert KeyAction.coerce(KeyAction.PRESS) is KeyAction.PRESS

    def test_coerce_from_enum_name(self):
        # 大写名也要认，方便手改配置
        assert KeyAction.coerce("LONG_PRESS") is KeyAction.LONG_PRESS

    def test_input_type_values(self):
        assert InputType.KEYBOARD.value == "keyboard"
        assert InputType.MOUSE.value == "mouse"


class TestKeyMappingSerialization:
    def test_roundtrip(self):
        original = KeyMapping(
            trigger="w",
            key="up",
            action=KeyAction.LONG_PRESS,
            delay=0.3,
            cooldown=1.5,
            enabled=False,
            description="往前走",
            enable_sensing=True,
            short_press_count=2,
            long_press_count=8,
            long_press_duration=0.9,
            match_mode=MatchMode.CONTAINS,
            case_sensitive=True,
            use_scancode=True,
        )
        restored = KeyMapping.from_dict(original.to_dict())
        assert restored == original

    def test_list_roundtrip(self):
        originals = default_key_mappings()
        restored = key_mappings_from_list(key_mappings_to_list(originals))
        assert len(restored) == len(originals)
        assert restored == originals

    def test_unknown_keys_ignored(self):
        data = {"trigger": "w", "key": "up", "future_field": 123}
        mapping = KeyMapping.from_dict(data)
        assert mapping.trigger == "w"

    def test_missing_fields_use_defaults(self):
        mapping = KeyMapping.from_dict({"trigger": "w"})
        assert mapping.key == ""
        assert mapping.cooldown == 0.5
        assert mapping.enabled is True

    def test_invalid_enum_falls_back(self):
        mapping = KeyMapping.from_dict({"trigger": "w", "key": "up", "action": "not-an-action"})
        assert mapping.action is KeyAction.PRESS_AND_RELEASE

    def test_non_dict_raises_typeerror(self):
        with pytest.raises(TypeError):
            KeyMapping.from_dict(["not", "a", "dict"])

    def test_delay_clamped_non_negative(self):
        mapping = KeyMapping.from_dict({"trigger": "w", "delay": -5})
        assert mapping.delay >= 0

    def test_cooldown_clamped_non_negative(self):
        mapping = KeyMapping.from_dict({"trigger": "w", "cooldown": -1})
        assert mapping.cooldown >= 0


class TestMouseMappingSerialization:
    def test_roundtrip(self):
        original = MouseMapping(
            trigger="左键",
            button=MouseButton.RIGHT,
            action=MouseAction.DOUBLE_CLICK,
            delay=0.2,
            cooldown=2.0,
            enabled=True,
            description="右键双击",
            move_x=10,
            move_y=-10,
            scroll_amount=3,
        )
        restored = MouseMapping.from_dict(original.to_dict())
        assert restored == original

    def test_list_roundtrip(self):
        originals = default_mouse_mappings()
        restored = mouse_mappings_from_list(mouse_mappings_to_list(originals))
        assert restored == originals

    def test_move_fields_preserved(self):
        """鼠标映射的移动字段必须能存下来。

        参考项目的鼠标映射从未被持久化，重启后全部丢失。
        """
        mapping = MouseMapping(trigger="移动", action=MouseAction.MOVE, move_x=5, move_y=7)
        restored = MouseMapping.from_dict(mapping.to_dict())
        assert (restored.move_x, restored.move_y) == (5, 7)

    def test_scroll_amount_preserved(self):
        mapping = MouseMapping(trigger="上滚", action=MouseAction.SCROLL_UP, scroll_amount=4)
        restored = MouseMapping.from_dict(mapping.to_dict())
        assert restored.scroll_amount == 4

    def test_input_type_is_mouse(self):
        assert MouseMapping(trigger="x").input_type is InputType.MOUSE

    def test_key_input_type_is_keyboard(self):
        assert KeyMapping(trigger="x").input_type is InputType.KEYBOARD


class TestMappingIdentity:
    def test_key_id_differs_by_input_type(self):
        k = KeyMapping(trigger="same", key="up")
        m = MouseMapping(trigger="same", button=MouseButton.LEFT)
        assert k.key_id() != m.key_id()

    def test_same_mapping_same_key_id(self):
        a = KeyMapping(trigger="w", key="up")
        b = KeyMapping(trigger="w", key="up", cooldown=99.0)
        # key_id 标识"同一条映射"，与冷却等可变参数无关
        assert a.key_id() == b.key_id()

    def test_triggers_property(self):
        assert KeyMapping(trigger="w").triggers == ["w"]

    def test_triggers_splits_on_comma(self):
        """界面承诺「多个触发词用英文逗号分隔」，数据层必须真拆。"""
        assert KeyMapping(trigger="w,上,前").triggers == ["w", "上", "前"]

    def test_triggers_accepts_chinese_comma(self):
        assert KeyMapping(trigger="w，上，前").triggers == ["w", "上", "前"]

    def test_triggers_drops_blank_pieces(self):
        assert KeyMapping(trigger="w, ,上,").triggers == ["w", "上"]
        assert KeyMapping(trigger=",,").triggers == []
        assert KeyMapping(trigger="").triggers == []

    def test_normalized_triggers_dedupes_case_insensitively(self):
        # 默认不区分大小写：w / W 是同一个触发词，只留一个且保持书写顺序
        assert KeyMapping(trigger="w,w,W,上").normalized_triggers == ["w", "上"]

    def test_normalized_triggers_keeps_case_when_sensitive(self):
        assert KeyMapping(trigger="w,W", case_sensitive=True).normalized_triggers == ["w", "W"]

    def test_normalize_trigger_returns_first(self):
        assert KeyMapping(trigger="w,上,前").normalize_trigger() == "w"
        assert KeyMapping(trigger="").normalize_trigger() == ""

    def test_key_id_still_uses_whole_trigger(self):
        """冷却属于"这条映射"：把 w 改写成 w,上 不该凭空多出一份独立冷却。"""
        assert KeyMapping(trigger="w,上,前", key="up").key_id() == "keyboard:w,上,前"

    def test_mouse_triggers_also_split(self):
        m = MouseMapping(trigger="左键,攻击", button=MouseButton.LEFT)
        assert m.triggers == ["左键", "攻击"]


class TestBadInputTolerance:
    def test_none_from_dict(self):
        with pytest.raises(TypeError):
            KeyMapping.from_dict(None)

    def test_list_skips_bad_entries(self):
        data = [{"trigger": "w", "key": "up"}, "garbage", {"trigger": "s", "key": "down"}]
        restored = key_mappings_from_list(data)
        assert len(restored) == 2

    def test_list_of_non_dict_returns_empty(self):
        assert key_mappings_from_list("nonsense") == []
