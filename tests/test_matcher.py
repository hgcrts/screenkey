"""匹配器测试 —— 三级匹配顺序与长按判定。

参考项目在这里有过真实 bug：感应分支优先于精确匹配，导致 "666" 这种
精确映射永远不可达。本测试锁定顺序契约。
"""

from __future__ import annotations

from screenkey.input.mappings import (
    KeyAction,
    KeyMapping,
    MatchMode,
    MouseAction,
    MouseButton,
    MouseMapping,
)
from screenkey.input.matcher import (
    CooldownTracker,
    collapse_repeats,
    detect_repeated_char,
    find_mapping,
    match_content,
)


class TestDetectRepeatedChar:
    def test_short_input_never_repeats(self):
        assert detect_repeated_char("") == (False, "", 0)
        assert detect_repeated_char("6") == (False, "", 0)

    def test_two_same_chars(self):
        assert detect_repeated_char("66") == (True, "6", 2)

    def test_six_same_chars(self):
        assert detect_repeated_char("666666") == (True, "6", 6)

    def test_mixed_content_is_not_repeat(self):
        # 只认"整体重复"，夹杂别的内容不算
        assert detect_repeated_char("666哈哈")[0] is False

    def test_whitespace_ignored(self):
        assert detect_repeated_char(" 6 6 6 ")[0] is False

    def test_chinese_char_repeat(self):
        assert detect_repeated_char("哈哈哈哈") == (True, "哈", 4)


class TestCollapseRepeats:
    def test_collapses(self):
        assert collapse_repeats("666666") == "6"

    def test_keeps_normal(self):
        assert collapse_repeats("abc") == "abc"


class TestMatchExact:
    def test_exact_hit(self):
        mappings = [KeyMapping(trigger="w", key="up")]
        found = match_content("w", mappings)
        assert len(found) == 1
        assert found[0].mapping.key == "up"
        assert found[0].kind == "exact"

    def test_exact_does_not_degrade_to_contains(self):
        """精确模式不得退化成包含匹配。

        这是参考项目的隐性缺陷：一旦精确匹配被当成"包含"，
        弹幕里出现 "awake" 也会触发 w 键。
        """
        mappings = [KeyMapping(trigger="w", key="up", match_mode=MatchMode.EXACT)]
        assert match_content("awake", mappings) == []

    def test_case_insensitive_by_default(self):
        mappings = [KeyMapping(trigger="w", key="up")]
        assert match_content("W", mappings)

    def test_case_sensitive_when_asked(self):
        mappings = [KeyMapping(trigger="W", key="up", case_sensitive=True)]
        assert match_content("W", mappings)
        assert match_content("w", mappings) == []


class TestMatchOrder:
    def test_exact_wins_over_sensing(self):
        """精确匹配必须优先于感应匹配。

        参考项目真实 bug：感应分支写在前面，导致 "666" 精确映射永远不可达。
        """
        mappings = [
            KeyMapping(trigger="6", key="1", action=KeyAction.PRESS_AND_RELEASE, enable_sensing=True),
            KeyMapping(trigger="666", key="2", action=KeyAction.PRESS_AND_RELEASE),
        ]
        found = match_content("666", mappings)
        assert found, "666 应该命中精确映射"
        assert found[0].mapping.key == "2"
        assert found[0].kind == "exact"

    def test_sensing_used_when_no_exact(self):
        mappings = [KeyMapping(trigger="6", key="1", enable_sensing=True, long_press_count=6)]
        found = match_content("666666", mappings)
        assert found and found[0].kind == "sensing"
        assert found[0].is_long_press is True

    def test_longer_trigger_wins(self):
        mappings = [
            KeyMapping(trigger="a", key="1"),
            KeyMapping(trigger="ab", key="2"),
            KeyMapping(trigger="abc", key="3"),
        ]
        found = match_content("abc", mappings)
        assert found[0].mapping.key == "3"


class TestMatchModes:
    def test_contains(self):
        mappings = [KeyMapping(trigger="上", key="up", match_mode=MatchMode.CONTAINS)]
        assert match_content("往上走", mappings)

    def test_startwith(self):
        mappings = [KeyMapping(trigger="!", key="1", match_mode=MatchMode.STARTWITH)]
        assert match_content("!go", mappings)
        assert match_content("go!", mappings) == []

    def test_regex(self):
        mappings = [KeyMapping(trigger=r"\d{3}", key="1", match_mode=MatchMode.REGEX)]
        assert match_content("abc123", mappings)

    def test_bad_regex_is_skipped_not_raised(self):
        mappings = [KeyMapping(trigger="[unclosed", key="1", match_mode=MatchMode.REGEX)]
        assert match_content("anything", mappings) == []


class TestSensingThresholds:
    def test_below_short_threshold_no_match(self):
        mappings = [
            KeyMapping(trigger="6", key="1", enable_sensing=True, short_press_count=3, long_press_count=6)
        ]
        assert match_content("66", mappings) == []

    def test_short_press(self):
        mappings = [
            KeyMapping(trigger="6", key="1", enable_sensing=True, short_press_count=3, long_press_count=6)
        ]
        found = match_content("666", mappings)
        assert found and found[0].is_long_press is False

    def test_long_press(self):
        mappings = [
            KeyMapping(trigger="6", key="1", enable_sensing=True, short_press_count=3, long_press_count=6)
        ]
        found = match_content("666666", mappings)
        assert found and found[0].is_long_press is True


class TestDisabledAndLimits:
    def test_disabled_mapping_not_matched(self):
        mappings = [KeyMapping(trigger="w", key="up", enabled=False)]
        assert match_content("w", mappings) == []

    def test_max_results(self):
        mappings = [KeyMapping(trigger=f"k{i}", key="a") for i in range(5)]
        # 同一内容只能精确命中一条，用包含模式制造多命中
        mappings = [KeyMapping(trigger="k", key=f"k{i}", match_mode=MatchMode.CONTAINS) for i in range(5)]
        assert len(match_content("kkkk", mappings, max_results=2)) == 2

    def test_empty_content(self):
        assert match_content("", [KeyMapping(trigger="w", key="up")]) == []

    def test_empty_mappings(self):
        assert match_content("w", []) == []


class TestMouseMatching:
    def test_mouse_mapping_matched(self):
        mappings = [MouseMapping(trigger="左键", button=MouseButton.LEFT, action=MouseAction.CLICK)]
        found = match_content("左键", mappings)
        assert found and found[0].mapping.button == MouseButton.LEFT

    def test_mixed_key_and_mouse(self):
        mappings = [
            KeyMapping(trigger="w", key="up"),
            MouseMapping(trigger="左键", button=MouseButton.LEFT),
        ]
        assert match_content("左键", mappings)[0].mapping.trigger == "左键"
        assert match_content("w", mappings)[0].mapping.trigger == "w"


class TestFindMapping:
    def test_returns_first(self):
        mappings = [KeyMapping(trigger="w", key="up")]
        got = find_mapping("w", mappings)
        assert got is not None and got.trigger == "w"

    def test_none_when_no_match(self):
        assert find_mapping("zzz", [KeyMapping(trigger="w", key="up")]) is None


class TestMultiTrigger:
    """一条映射用逗号写多个触发词（界面一直这么承诺，实现曾经相反）。

    界面 ``ui/mapping_editor.py`` 的提示原文是「多个触发词用英文逗号分隔，
    例如 w,上,前」，而 ``BaseMapping.triggers`` 早先返回 ``[self.trigger]``：
    整串被当成**一个字面量**，发 ``w`` / ``上`` / ``前`` 全都不触发，
    只有弹幕原文恰好是 ``w,上,前`` 才命中。这组测试锁住"任一个触发词命中即命中"。
    """

    def test_every_trigger_matches_exact(self):
        mappings = [KeyMapping(trigger="w,上,前", key="up")]
        for probe in ("w", "上", "前"):
            found = match_content(probe, mappings)
            assert found, f"{probe!r} 应该命中"
            assert found[0].kind == "exact"
            assert found[0].trigger == probe

    def test_full_string_is_not_a_literal_anymore(self):
        """逗号是分隔符，整串不再等于任何一个触发词（语义变更，刻意如此）。"""
        mappings = [KeyMapping(trigger="w,上,前", key="up")]
        assert match_content("w,上,前", mappings) == []

    def test_matched_trigger_is_recorded_for_sorting(self):
        """命中结果要记下**命中的那个触发词**，排序才按它的长度算。"""
        mappings = [KeyMapping(trigger="回车,车", key="enter", match_mode=MatchMode.CONTAINS)]
        found = match_content("我要回车", mappings)
        assert found and found[0].trigger == "回车"
        assert found[0].mapping.trigger == "回车,车"

    def test_longer_matched_trigger_wins(self):
        """同为 CONTAINS 时，命中的触发词更长的排前面。"""
        mappings = [
            KeyMapping(trigger="abcd", key="a", match_mode=MatchMode.CONTAINS),
            KeyMapping(trigger="ab", key="b", match_mode=MatchMode.CONTAINS),
        ]
        found = match_content("xxabcdxx", mappings, max_results=3)
        assert [item.mapping.key for item in found] == ["a", "b"]

    def test_sensing_matches_each_trigger(self):
        mappings = [KeyMapping(trigger="6,9", key="r", enable_sensing=True, long_press_count=6)]
        for probe, char in (("666666", "6"), ("999999", "9")):
            found = match_content(probe, mappings)
            assert found and found[0].is_long_press
            assert found[0].trigger == char

    def test_contains_matches_second_trigger(self):
        mappings = [KeyMapping(trigger="666,大佬", key="q", match_mode=MatchMode.CONTAINS)]
        found = match_content("大佬真棒", mappings)
        assert found and found[0].kind == "contains" and found[0].trigger == "大佬"

    def test_startwith_matches_each_trigger(self):
        mappings = [KeyMapping(trigger="上,前", key="w", match_mode=MatchMode.STARTWITH)]
        assert match_content("上啊", mappings)[0].trigger == "上"
        assert match_content("前面", mappings)[0].trigger == "前"
        assert match_content("啊上", mappings) == []

    def test_regex_skips_bad_candidate_and_keeps_good_one(self):
        """多个候选正则里写错一个，只跳过它，不影响其它候选。"""
        mappings = [KeyMapping(trigger="[unclosed,a+b", key="q", match_mode=MatchMode.REGEX)]
        found = match_content("xxaaabbb", mappings)
        assert found and found[0].kind == "regex" and found[0].trigger == "a+b"

    def test_mouse_mapping_also_splits(self):
        mappings = [MouseMapping(trigger="左键,攻击", button=MouseButton.LEFT, action=MouseAction.CLICK)]
        found = match_content("攻击", mappings)
        assert found and found[0].trigger == "攻击"
        assert found[0].mapping.button == MouseButton.LEFT

    def test_case_insensitive_multi_trigger(self):
        mappings = [KeyMapping(trigger="W,上", key="up")]
        assert match_content("w", mappings)[0].trigger == "w"
        assert match_content("上", mappings)[0].trigger == "上"

    def test_default_mappings_have_no_comma_triggers(self):
        """默认映射里没有含逗号的触发词 —— 所以多触发词不会改变既有行为。"""
        from screenkey.input.mappings import default_key_mappings, default_mouse_mappings

        all_mappings = list(default_key_mappings()) + list(default_mouse_mappings())
        assert [m.trigger for m in all_mappings if "," in m.trigger or "，" in m.trigger] == []

    def test_default_666666_still_long_presses(self):
        """回归：默认映射下 666666 仍命中 "6" 长按。"""
        from screenkey.input.mappings import default_key_mappings

        found = match_content("666666", default_key_mappings(), max_results=3)
        assert found and found[0].trigger == "6" and found[0].is_long_press


class TestCooldownTracker:
    def test_ready_then_blocked(self):
        tracker = CooldownTracker()
        assert tracker.ready("k", 1.0, now=100.0) is True
        tracker.mark("k", now=100.0)
        assert tracker.ready("k", 1.0, now=100.5) is False
        assert tracker.ready("k", 1.0, now=101.5) is True

    def test_zero_cooldown_always_ready(self):
        tracker = CooldownTracker()
        tracker.mark("k", now=100.0)
        assert tracker.ready("k", 0.0, now=100.0) is True

    def test_now_optional_defaults_to_monotonic(self):
        """``now`` 必须是可选的。

        参考项目要求调用方处处传时间戳，结果引擎忘了传，
        每次弹幕都抛 TypeError、冷却静默失效、按键全废。
        """
        tracker = CooldownTracker()
        assert tracker.ready("k", 1.0) is True
        tracker.mark("k")
        assert tracker.ready("k", 1.0) is False

    def test_per_user(self):
        tracker = CooldownTracker()
        assert tracker.ready_for_user(1, 5.0, now=0.0) is True
        tracker.mark_user(1, now=0.0)
        assert tracker.ready_for_user(1, 5.0, now=1.0) is False
        assert tracker.ready_for_user(2, 5.0, now=1.0) is True

    def test_prune_removes_old(self):
        tracker = CooldownTracker()
        tracker.mark("old", now=0.0)
        tracker.prune(now=1000.0, max_age=10.0)
        assert tracker.ready("old", 100.0, now=1000.0) is True
