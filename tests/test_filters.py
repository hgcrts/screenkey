# -*- coding: utf-8 -*-
"""筛选规则引擎测试。

重点覆盖参考项目做不到、而 V2 刻意设计成这样的几条行为：

* 规则**有序**，第一条命中即返回（能表达"或"，也能给不同规则配不同动作）。
* 无规则命中时**默认放行**（"不加规则就等于不过滤"的直觉）。
* 条件之间是**与**，需要"或"就写多条规则。
* 用户把正则写错时视为不匹配，**不能因此丢掉整条弹幕流**。
"""

from __future__ import annotations

import pytest

from screenkey.danmaku.message import DanmakuMessage, MessageType
from screenkey.filters.rules import (
    CompareOp,
    Condition,
    FieldMatch,
    FilterAction,
    FilterEngine,
    FilterResult,
    FilterRule,
    keyword_block_rule,
    length_rule,
    prefix_rewrite_rule,
    user_block_rule,
)


def msg(content="你好", **kwargs) -> DanmakuMessage:
    kwargs.setdefault("nickname", "观众")
    kwargs.setdefault("platform", "bilibili")
    return DanmakuMessage(content=content, **kwargs)


# ----------------------------------------------------------------------
# Condition
# ----------------------------------------------------------------------


class TestConditionExtract:
    """字段取值。"""

    def test_content(self):
        assert Condition(field=FieldMatch.CONTENT, value="你").test(msg("你好"))

    def test_nickname(self):
        condition = Condition(field=FieldMatch.NICKNAME, value="观众")
        assert condition.test(msg("任何内容"))

    def test_user_id(self):
        condition = Condition(field=FieldMatch.USER_ID, op=CompareOp.EQUALS, value="42")
        assert condition.test(msg("x", user_id="42"))
        assert not condition.test(msg("x", user_id="43"))

    def test_platform(self):
        condition = Condition(field=FieldMatch.PLATFORM, op=CompareOp.EQUALS, value="bilibili")
        assert condition.test(msg("x", platform="bilibili"))
        assert not condition.test(msg("x", platform="douyin"))

    def test_msg_type(self):
        condition = Condition(field=FieldMatch.MSG_TYPE, op=CompareOp.EQUALS, value="gift")
        assert condition.test(msg("x", msg_type=MessageType.GIFT))
        assert not condition.test(msg("x", msg_type=MessageType.DANMAKU))


class TestConditionCompare:
    def test_equals_is_case_insensitive_by_default(self):
        condition = Condition(op=CompareOp.EQUALS, value="ABC")
        assert condition.test(msg("abc"))

    def test_case_sensitive_flag(self):
        condition = Condition(op=CompareOp.EQUALS, value="ABC", case_sensitive=True)
        assert not condition.test(msg("abc"))
        assert condition.test(msg("ABC"))

    def test_contains_empty_needle_is_false(self):
        """空 needle 不能命中一切 —— 否则一条空规则会吞掉所有弹幕。"""
        assert not Condition(op=CompareOp.CONTAINS, value="").test(msg("随便"))
        assert Condition(op=CompareOp.NOT_CONTAINS, value="").test(msg("随便"))

    def test_startswith_and_endswith(self):
        assert Condition(op=CompareOp.STARTWITH, value="按").test(msg("按下w"))
        assert Condition(op=CompareOp.ENDSWITH, value="w").test(msg("按下w"))
        assert not Condition(op=CompareOp.STARTWITH, value="w").test(msg("按下w"))

    def test_in_and_not_in_accept_comma_string(self):
        condition = Condition(op=CompareOp.IN, value="a, b ,c")
        assert condition.test(msg("b"))
        assert not condition.test(msg("d"))
        assert Condition(op=CompareOp.NOT_IN, value="a,b").test(msg("d"))

    def test_in_accepts_list(self):
        assert Condition(op=CompareOp.IN, value=["x", "y"]).test(msg("y"))

    def test_in_ignores_whitespace_pieces(self):
        """``"a,,b"`` 里的空片段要丢掉，不能把空串当成一个成员。"""
        condition = Condition(op=CompareOp.IN, value="a,,b")
        assert condition.test(msg("a"))
        assert not condition.test(msg(""))

    def test_length_ops(self):
        assert Condition(op=CompareOp.LENGTH_GT, value=2).test(msg("abc"))
        assert not Condition(op=CompareOp.LENGTH_GT, value=2).test(msg("ab"))
        assert Condition(op=CompareOp.LENGTH_LT, value=2).test(msg("a"))
        assert Condition(op=CompareOp.LENGTH_EQ, value=2).test(msg("ab"))

    def test_length_ops_with_garbage_value_default_to_zero(self):
        """长度值写错时按 0 处理（``_as_int`` 的兜底），不是抛异常。

        注意 ``LENGTH_GT(0)`` 对**非空**内容是成立的（"长度大于 0"），
        所以断言要用空串来验证"0"这一侧，别误以为它恒为假。
        """
        # 非空内容长度 > 0，所以 LENGTH_GT("abc"→0) 为真
        assert Condition(op=CompareOp.LENGTH_GT, value="abc").test(msg("a"))
        # 空串长度 == 0：既不大于 0 也不小于 0，但等于 0
        assert not Condition(op=CompareOp.LENGTH_GT, value="abc").test(msg(""))
        assert not Condition(op=CompareOp.LENGTH_LT, value="abc").test(msg(""))
        assert Condition(op=CompareOp.LENGTH_EQ, value="abc").test(msg(""))

    def test_negate_flips_result(self):
        assert Condition(op=CompareOp.CONTAINS, value="坏", negate=True).test(msg("好"))
        assert not Condition(op=CompareOp.CONTAINS, value="坏", negate=True).test(msg("很坏"))


class TestConditionRegex:
    def test_regex_match(self):
        assert Condition(op=CompareOp.REGEX, value=r"\d{3}").test(msg("666"))

    def test_regex_is_ignored_for_empty_pattern(self):
        """空正则必须视为"不匹配"，否则 ``re.search("", x)`` 永远为真。"""
        assert not Condition(op=CompareOp.REGEX, value="").test(msg("任何"))

    def test_broken_regex_is_swallowed_not_raised(self):
        """用户写错正则：视为不匹配，不能抛异常打断弹幕流。"""
        condition = Condition(op=CompareOp.REGEX, value="([unclosed")
        assert condition.test(msg("任何")) is False

    def test_not_regex_with_broken_pattern(self):
        """
        坏正则对 NOT_REGEX 也返回 False（而不是 True）。

        这是刻意的保守选择：写错的规则不该反过来命中一切。
        """
        condition = Condition(op=CompareOp.NOT_REGEX, value="([unclosed")
        assert condition.test(msg("任何")) is False

    def test_not_regex_normal(self):
        assert Condition(op=CompareOp.NOT_REGEX, value="坏").test(msg("好"))


# ----------------------------------------------------------------------
# FilterRule
# ----------------------------------------------------------------------


class TestFilterRuleApply:
    def test_allow(self):
        message, actionable = FilterRule(action=FilterAction.ALLOW).apply(msg())
        assert message is not None
        assert actionable is True

    def test_drop_returns_none(self):
        message, actionable = FilterRule(action=FilterAction.DROP).apply(msg())
        assert message is None
        assert actionable is False

    def test_display_only_keeps_message_but_blocks_keys(self):
        message, actionable = FilterRule(action=FilterAction.DISPLAY_ONLY).apply(msg())
        assert message is not None
        assert message.content == "你好"
        assert actionable is False

    def test_rewrite_strips_prefix(self):
        rule = FilterRule(action=FilterAction.REWRITE, strip_prefix="按")
        message, actionable = rule.apply(msg("按w"))
        assert message is not None
        assert message.content == "w"
        assert actionable is True

    def test_rewrite_to_replaces_content(self):
        rule = FilterRule(action=FilterAction.REWRITE, rewrite_to="666")
        message, _ = rule.apply(msg("随便说点啥"))
        assert message is not None
        assert message.content == "666"

    def test_rewrite_prefix_absent_leaves_content(self):
        rule = FilterRule(action=FilterAction.REWRITE, strip_prefix="按")
        message, _ = rule.apply(msg("没这个前缀"))
        assert message is not None
        assert message.content == "没这个前缀"

    def test_rewrite_does_not_mutate_original(self):
        """消息是不可变的，改写必须产生新对象。"""
        original = msg("按w")
        FilterRule(action=FilterAction.REWRITE, strip_prefix="按").apply(original)
        assert original.content == "按w"


class TestFilterRuleMatches:
    def test_disabled_rule_never_matches(self):
        assert not FilterRule(action=FilterAction.DROP, enabled=False).matches(msg())

    def test_empty_conditions_match_everything(self):
        """空条件 = 无条件命中（兜底规则靠这个）。"""
        assert FilterRule().matches(msg("任何"))

    def test_conditions_are_anded(self):
        rule = FilterRule(
            conditions=[
                Condition(op=CompareOp.CONTAINS, value="a"),
                Condition(op=CompareOp.CONTAINS, value="b"),
            ]
        )
        assert rule.matches(msg("ab"))
        assert not rule.matches(msg("a"))
        assert not rule.matches(msg("b"))

    def test_match_any_ors_conditions(self):
        """``match_any=True`` 时任一条件成立即命中。

        这条存在的原因是：``length_rule`` 这类"太短或太长都该丢"的规则，
        两个条件在"与"语义下**永远不可能同时成立**，规则会彻底失效。
        """
        rule = FilterRule(
            match_any=True,
            conditions=[
                Condition(op=CompareOp.CONTAINS, value="a"),
                Condition(op=CompareOp.CONTAINS, value="b"),
            ],
        )
        assert rule.matches(msg("a"))
        assert rule.matches(msg("b"))
        assert rule.matches(msg("ab"))
        assert not rule.matches(msg("c"))

    def test_match_any_still_respects_enabled_and_filters(self):
        """或语义不能绕过 enabled / msg_types / platforms 这些前置闸门。"""
        cond = [Condition(op=CompareOp.CONTAINS, value="a")]
        assert not FilterRule(match_any=True, enabled=False, conditions=cond).matches(msg("a"))
        assert not FilterRule(
            match_any=True, msg_types=[MessageType.GIFT], conditions=cond
        ).matches(msg("a"))
        assert not FilterRule(
            match_any=True, platforms=["bilibili"], conditions=cond
        ).matches(msg("a", platform="douyin"))

    def test_match_any_with_no_conditions_still_matches_everything(self):
        assert FilterRule(match_any=True).matches(msg("任何"))

    def test_msg_type_filter(self):
        rule = FilterRule(msg_types=[MessageType.GIFT])
        assert rule.matches(msg("x", msg_type=MessageType.GIFT))
        assert not rule.matches(msg("x", msg_type=MessageType.DANMAKU))

    def test_platform_filter_is_case_insensitive(self):
        rule = FilterRule(platforms=["BILIBILI"])
        assert rule.matches(msg("x", platform="bilibili"))
        assert not rule.matches(msg("x", platform="douyin"))

    def test_empty_platform_list_means_all_platforms(self):
        assert FilterRule(platforms=[]).matches(msg("x", platform="douyin"))


# ----------------------------------------------------------------------
# FilterEngine
# ----------------------------------------------------------------------


class TestFilterEngineOrdering:
    """有序 + 第一条命中即返回 —— 这是 V2 相对参考项目的核心改动。"""

    def test_first_matching_rule_wins(self):
        engine = FilterEngine(
            [
                FilterRule(name="先丢弃", action=FilterAction.DROP, conditions=[Condition(value="坏")]),
                FilterRule(name="后放行", action=FilterAction.ALLOW),
            ]
        )
        result = engine.evaluate(msg("很坏"))
        assert result.dropped
        assert result.rule == "先丢弃"

    def test_order_matters_for_same_message(self):
        """把兜底规则放前面，后面的拒绝规则就永远轮不到 —— 顺序是有意义的。"""
        engine = FilterEngine(
            [
                FilterRule(name="兜底", action=FilterAction.ALLOW),
                FilterRule(name="拒绝", action=FilterAction.DROP, conditions=[Condition(value="坏")]),
            ]
        )
        assert engine.evaluate(msg("很坏")).rule == "兜底"

    def test_no_rule_matches_defaults_to_allow(self):
        """没写任何规则 = 不过滤。这条直觉必须保住。"""
        engine = FilterEngine([FilterRule(name="只挡礼物", msg_types=[MessageType.GIFT])])
        result = engine.evaluate(msg("普通弹幕"))
        assert not result.dropped
        assert result.actionable is True
        assert result.rule == ""

    def test_empty_engine_allows_everything(self):
        assert FilterEngine([]).evaluate(msg("任何")).actionable is True

    def test_display_only_rule_keeps_message_but_not_actionable(self):
        engine = FilterEngine([FilterRule(action=FilterAction.DISPLAY_ONLY, msg_types=[MessageType.ENTER])])
        result = engine.evaluate(msg("来了", msg_type=MessageType.ENTER))
        assert result.message is not None
        assert result.actionable is False

    def test_hit_counts_accumulate(self):
        engine = FilterEngine([FilterRule(name="挡", action=FilterAction.DROP, conditions=[Condition(value="坏")])])
        engine.evaluate(msg("坏"))
        engine.evaluate(msg("坏"))
        assert engine.hit_counts["挡"] == 2

    def test_unnamed_rule_gets_positional_key(self):
        engine = FilterEngine([FilterRule(action=FilterAction.DROP, conditions=[Condition(value="坏")])])
        assert engine.evaluate(msg("坏")).rule == "rule@0"

    def test_filter_helper_returns_tuple(self):
        engine = FilterEngine([FilterRule(action=FilterAction.DROP, conditions=[Condition(value="坏")])])
        assert engine.filter(msg("坏")) == (None, False)
        message, actionable = engine.filter(msg("好"))
        assert message is not None and actionable is True


class TestFilterEngineManagement:
    def test_add_rule_appends_by_default(self):
        engine = FilterEngine()
        engine.add_rule(FilterRule(name="a"))
        engine.add_rule(FilterRule(name="b"))
        assert [r.name for r in engine.rules] == ["a", "b"]

    def test_add_rule_at_index(self):
        engine = FilterEngine([FilterRule(name="a")])
        engine.add_rule(FilterRule(name="b"), index=0)
        assert [r.name for r in engine.rules] == ["b", "a"]

    def test_add_rule_out_of_range_index_appends(self):
        engine = FilterEngine([FilterRule(name="a")])
        engine.add_rule(FilterRule(name="b"), index=99)
        assert [r.name for r in engine.rules] == ["a", "b"]

    def test_remove_rule(self):
        engine = FilterEngine([FilterRule(name="a"), FilterRule(name="b")])
        assert engine.remove_rule(0) is True
        assert [r.name for r in engine.rules] == ["b"]
        assert engine.remove_rule(5) is False

    def test_move_rule(self):
        engine = FilterEngine([FilterRule(name="a"), FilterRule(name="b"), FilterRule(name="c")])
        assert engine.move_rule(0, 1) is True
        assert [r.name for r in engine.rules] == ["b", "a", "c"]
        assert engine.move_rule(0, -1) is False

    def test_clear(self):
        engine = FilterEngine([FilterRule(name="a")])
        engine.evaluate(msg("x"))
        engine.clear()
        assert engine.rules == []
        assert engine.hit_counts == {}


class TestFilterResult:
    def test_dropped_and_bool(self):
        assert FilterResult(message=None).dropped is True
        assert bool(FilterResult(message=None)) is False
        assert bool(FilterResult(message=msg())) is True


class TestDefaultEngine:
    def test_noise_types_are_display_only(self):
        engine = FilterEngine.default()
        for kind in (MessageType.ENTER, MessageType.FOLLOW, MessageType.LIKE, MessageType.STATS):
            result = engine.evaluate(msg("噪音", msg_type=kind))
            assert result.message is not None, kind
            assert result.actionable is False, kind

    def test_real_danmaku_is_actionable(self):
        """默认规则必须让真实弹幕通过，否则装了程序什么都不触发。"""
        result = FilterEngine.default().evaluate(msg("666"))
        assert result.actionable is True
        assert result.message is not None

    def test_default_never_drops(self):
        """默认规则不该悄悄丢消息 —— 用户没配规则就该全看到。"""
        engine = FilterEngine.default()
        for kind in MessageType:
            result = engine.evaluate(msg("内容", msg_type=kind))
            assert result.message is not None, kind


# ----------------------------------------------------------------------
# 序列化
# ----------------------------------------------------------------------


class TestSerialization:
    def test_condition_roundtrip(self):
        original = Condition(
            field=FieldMatch.NICKNAME,
            op=CompareOp.STARTWITH,
            value="管理",
            case_sensitive=True,
            negate=True,
        )
        restored = Condition.from_dict(original.to_dict())
        assert restored == original

    def test_condition_from_dict_is_lenient(self):
        condition = Condition.from_dict({"field": "不存在的字段", "op": "也不存在"})
        assert condition.field is FieldMatch.CONTENT
        assert condition.op is CompareOp.CONTAINS
        assert Condition.from_dict("垃圾").field is FieldMatch.CONTENT

    def test_rule_roundtrip(self):
        original = FilterRule(
            name="测试规则",
            action=FilterAction.REWRITE,
            conditions=[Condition(value="按", op=CompareOp.STARTWITH)],
            msg_types=[MessageType.DANMAKU],
            platforms=["douyin"],
            strip_prefix="按",
            rewrite_to="",
            description="说明",
        )
        restored = FilterRule.from_dict(original.to_dict())
        assert restored.name == original.name
        assert restored.action is original.action
        assert restored.conditions == original.conditions
        assert restored.msg_types == original.msg_types
        assert restored.platforms == original.platforms
        assert restored.strip_prefix == original.strip_prefix
        assert restored.description == original.description

    def test_rule_from_dict_tolerates_garbage(self):
        rule = FilterRule.from_dict(None)
        assert rule.action is FilterAction.ALLOW
        assert rule.conditions == []
        assert rule.enabled is True

    def test_rule_from_dict_is_lenient_with_broken_conditions(self):
        """宽容反序列化：垃圾项不会触发 `continue`，而是各自变成一条默认条件。

        `Condition.from_dict` 对非 Mapping 输入（字符串、None）返回**默认
        Condition** 而不抛异常，所以这里得到的是 3 条条件而不是 2 条。
        这是刻意的宽容策略——坏数据不该让整份配置加载失败。
        """
        rule = FilterRule.from_dict({"name": "x", "conditions": [{"field": "content"}, "垃圾", None]})
        assert len(rule.conditions) == 3
        assert all(c.field is FieldMatch.CONTENT for c in rule.conditions)

    def test_engine_roundtrip(self):
        engine = FilterEngine.default()
        restored = FilterEngine.from_list(engine.to_list())
        assert len(restored.rules) == len(engine.rules)
        assert [r.name for r in restored.rules] == [r.name for r in engine.rules]

    def test_engine_from_list_garbage_falls_back_to_default(self):
        """整份规则文件损坏时给出默认规则，而不是空引擎。"""
        assert len(FilterEngine.from_list("垃圾").rules) == 2
        assert len(FilterEngine.from_list(None).rules) == 2

    def test_engine_from_list_empty_falls_back_to_default(self):
        assert len(FilterEngine.from_list([]).rules) == 2


# ----------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------


class TestKeywordBlockRule:
    def test_blocks_any_listed_keyword(self):
        rule = keyword_block_rule(["广告", "加群"])
        assert rule.action is FilterAction.DROP
        assert rule.matches(msg("来看看广告啊"))
        assert rule.matches(msg("加群领福利"))
        assert not rule.matches(msg("正常发言"))

    def test_keywords_are_escaped(self):
        """关键词里有正则元字符时不能被当成元字符。"""
        rule = keyword_block_rule(["a.c"])
        assert rule.matches(msg("x a.c y"))
        assert not rule.matches(msg("x abc y"))

    def test_empty_keyword_list_disables_rule(self):
        """空关键词若照常生成规则，正则为空会命中一切 → 整条弹幕流被吞掉。"""
        rule = keyword_block_rule([])
        assert rule.enabled is False
        assert not rule.matches(msg("任何内容"))


class TestPrefixRewriteRule:
    def test_strips_prefix_then_allows(self):
        rule = prefix_rewrite_rule("按")
        assert rule.matches(msg("按w"))
        message, actionable = rule.apply(msg("按w"))
        assert message is not None
        assert message.content == "w"
        assert actionable is True

    def test_non_matching_message_untouched(self):
        rule = prefix_rewrite_rule("按")
        assert not rule.matches(msg("w"))


class TestUserBlockRule:
    def test_blocks_listed_users(self):
        rule = user_block_rule(["1001", "1002"])
        assert rule.matches(msg("x", user_id="1001"))
        assert not rule.matches(msg("x", user_id="2002"))

    def test_comparison_is_string_based(self):
        """user_id 是字符串，``1001`` 和 ``1001`` 要能对上。"""
        rule = user_block_rule([1001])
        assert rule.matches(msg("x", user_id="1001"))

    def test_empty_list_disables_rule(self):
        assert user_block_rule([]).enabled is False


class TestLengthRule:
    def test_min_length_drops_too_short(self):
        rule = length_rule(min_length=2)
        assert rule.matches(msg("a"))
        assert not rule.matches(msg("ab"))

    def test_max_length_drops_too_long(self):
        rule = length_rule(max_length=3)
        assert rule.matches(msg("abcd"))
        assert not rule.matches(msg("abc"))

    def test_both_bounds(self):
        rule = length_rule(min_length=2, max_length=4)
        assert rule.matches(msg("a"))
        assert rule.matches(msg("abcde"))
        assert not rule.matches(msg("abc"))

    def test_both_bounds_requires_or_semantics(self):
        """回归：两个边界必须用「或」连接。

        历史缺陷有两层——条件带了 ``negate`` 让语义整体倒过来；去掉 negate 后
        若仍是「与」，则"太短"与"太长"不可能同时成立，规则永不命中。
        这里同时锁住 ``match_any`` 与实际的丢弃行为。
        """
        rule = length_rule(min_length=2, max_length=4)
        assert rule.match_any is True
        assert rule.matches(msg("a"))      # 太短 → 丢
        assert rule.matches(msg("abcde"))  # 太长 → 丢
        assert not rule.matches(msg("abc"))  # 在区间内 → 留

    def test_match_any_survives_serialization(self):
        """``match_any`` 必须随规则一起存盘，否则重启后长度规则又失效。"""
        rule = length_rule(min_length=2, max_length=4)
        assert rule.to_dict()["match_any"] is True
        assert FilterRule.from_dict(rule.to_dict()).match_any is True
        engine = FilterEngine.from_list(FilterEngine([rule]).to_list())
        assert engine.rules[0].match_any is True

    def test_zero_means_unlimited(self):
        rule = length_rule(min_length=0, max_length=0)
        assert rule.enabled is False
        assert not rule.matches(msg("a"))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
