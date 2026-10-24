"""配置类型强转的回归测试。

为什么需要这一层：``settings.json`` 是**给用户手改的**文件，JSON 里把布尔值
写成字符串（``"enabled": "false"``）或把数字写成字符串（``"global_cooldown":
"1.5"``）是极常见的失误。早期 ``InputConfig.from_dict`` 就是
``cls(**_filter_fields(cls, data))``，一个类型都不转：

- ``"enabled": "false"`` → ``bool("false") is True``，**关掉按键输出反而变成
  打开**，静默的相反行为；
- ``"global_cooldown": "1.5"`` → 引擎里 ``if cfg.global_cooldown > 0`` 抛
  ``TypeError: '>' not supported between instances of 'str' and 'int'``，
  一条弹幕就打断整个处理链。

这里锁住"手改坏的配置不会让程序崩溃、也不会静默反转语义"。
"""

from __future__ import annotations

from danmaku_kb.config.models import (
    AppConfig,
    ConnectionConfig,
    DisplayConfig,
    InputConfig,
    Platform,
    WindowGeometry,
)


class TestInputConfigCoercion:
    def test_string_false_is_false(self):
        """"false" 必须转成 False，而不是非空字符串恒真。"""
        cfg = InputConfig.from_dict({"enabled": "false"})
        assert cfg.enabled is False

    def test_string_true_is_true(self):
        assert InputConfig.from_dict({"enabled": "true"}).enabled is True
        assert InputConfig.from_dict({"enabled": "1"}).enabled is True
        assert InputConfig.from_dict({"enabled": "是"}).enabled is True

    def test_numeric_bool(self):
        assert InputConfig.from_dict({"enabled": 1}).enabled is True
        assert InputConfig.from_dict({"enabled": 0}).enabled is False

    def test_unknown_string_uses_default(self):
        # 认不出来的词不能猜成 False —— 默认 enabled 是 True，猜成 False
        # 会让用户"什么都没改却打不出字"。
        assert InputConfig.from_dict({"enabled": "maybe"}).enabled is True

    def test_cooldown_string_becomes_float(self):
        """引擎里要拿它做 ``> 0`` 比较，字符串会直接抛 TypeError。"""
        cfg = InputConfig.from_dict({"global_cooldown": "1.5", "per_user_cooldown": "2"})
        assert cfg.global_cooldown == 1.5
        assert isinstance(cfg.global_cooldown, float)
        assert cfg.per_user_cooldown == 2.0

    def test_cooldown_is_never_negative(self):
        cfg = InputConfig.from_dict({"global_cooldown": -3.0, "per_user_cooldown": -1})
        assert cfg.global_cooldown == 0.0
        assert cfg.per_user_cooldown == 0.0

    def test_cooldown_garbage_falls_back_to_default(self):
        cfg = InputConfig.from_dict({"global_cooldown": "很久", "dedupe_window": None})
        assert cfg.global_cooldown == InputConfig().global_cooldown
        assert cfg.dedupe_window == InputConfig().dedupe_window

    def test_max_actions_clamped(self):
        assert InputConfig.from_dict({"max_actions_per_message": 999}).max_actions_per_message == 10
        assert InputConfig.from_dict({"max_actions_per_message": -5}).max_actions_per_message == 0

    def test_empty_hotkey_falls_back_to_default(self):
        """热键留空会让全局热键永远注册不上，必须回落默认值。"""
        cfg = InputConfig.from_dict({"toggle_hotkey": "   "})
        assert cfg.toggle_hotkey == InputConfig().toggle_hotkey


class TestDisplayConfigCoercion:
    def test_bools_and_ints(self):
        cfg = DisplayConfig.from_dict(
            {"show_timestamp": "false", "max_lines": "123", "font_size": "18"}
        )
        assert cfg.show_timestamp is False
        assert cfg.max_lines == 123
        assert cfg.font_size == 18

    def test_font_size_clamped(self):
        assert DisplayConfig.from_dict({"font_size": 999}).font_size == 72
        assert DisplayConfig.from_dict({"font_size": 1}).font_size == 6

    def test_max_lines_at_least_one(self):
        assert DisplayConfig.from_dict({"max_lines": 0}).max_lines == 1

    def test_opacity_clamped(self):
        assert DisplayConfig.from_dict({"opacity": "2"}).opacity == 1.0
        assert DisplayConfig.from_dict({"opacity": 0.01}).opacity == 0.2


class TestConnectionConfigCoercion:
    def test_platform_and_mode_coerced(self):
        cfg = ConnectionConfig.from_dict({"platform": "抖音", "douyin_mode": "browser"})
        # "抖音" 不是枚举值，回落 B 站；重点是**不能抛异常**
        assert isinstance(cfg.platform, Platform)
        assert cfg.douyin_mode.value == "browser"

    def test_reconnect_numbers_coerced(self):
        cfg = ConnectionConfig.from_dict(
            {
                "auto_reconnect": "no",
                "reconnect_delay": "3.5",
                "max_reconnect_attempts": "7",
                "connection_timeout": "20",
            }
        )
        assert cfg.auto_reconnect is False
        assert cfg.reconnect_delay == 3.5
        assert cfg.max_reconnect_attempts == 7
        assert cfg.connection_timeout == 20.0

    def test_garbage_does_not_raise(self):
        cfg = ConnectionConfig.from_dict(
            {
                "platform": None,
                "room": 12345,
                "auto_reconnect": [],
                "reconnect_delay": {},
                "max_reconnect_attempts": "abc",
            }
        )
        assert cfg.room == "12345"
        assert isinstance(cfg.reconnect_delay, float)


class TestWindowGeometryCoercion:
    def test_minimum_size(self):
        geo = WindowGeometry.from_dict({"width": 10, "height": 10})
        assert geo.width == 640
        assert geo.height == 480

    def test_offscreen_position_clamped(self):
        """窗口坐标不能被写进几万像素外，否则窗口"消失"在屏幕外。"""
        geo = WindowGeometry.from_dict({"x": 999999, "y": -999999})
        assert geo.x == 20000
        assert geo.y == -20000

    def test_garbage_uses_default(self):
        geo = WindowGeometry.from_dict({"x": "abc", "width": None})
        # 无法解析时用 dataclass 默认值，而不是抛异常或变成 0
        assert geo.x == WindowGeometry().x
        assert geo.width == WindowGeometry().width


class TestAppConfigRoundTrip:
    def test_from_dict_does_not_raise_on_garbage(self):
        cfg = AppConfig.from_dict(
            {
                "connection": "不是字典",
                "display": [1, 2, 3],
                "input": {"enabled": "false"},
                "window": None,
            }
        )
        assert isinstance(cfg, AppConfig)
        assert cfg.input.enabled is False

    def test_schema_version_is_not_serialised_as_a_field(self):
        """``SCHEMA_VERSION`` 是 ``ClassVar``，不能出现在 to_dict 的子字典里。"""
        payload = AppConfig().to_dict()
        assert payload["version"] == AppConfig.SCHEMA_VERSION
        assert "SCHEMA_VERSION" not in payload
        assert "SCHEMA_VERSION" not in payload["input"]

    def test_roundtrip_preserves_values(self):
        cfg = AppConfig()
        cfg.input.enabled = False
        cfg.input.global_cooldown = 2.5
        cfg.display.font_size = 20
        again = AppConfig.from_dict(cfg.to_dict())
        assert again.input.enabled is False
        assert again.input.global_cooldown == 2.5
        assert again.display.font_size == 20

    def test_json_serialisable(self):
        import json

        text = json.dumps(AppConfig().to_dict(), ensure_ascii=False)
        assert "bilibili" in text or "douyin" in text
