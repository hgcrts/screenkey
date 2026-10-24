"""筛选规则持久化 + 规则引擎行为的回归测试。

为什么需要这一层：``FilterEngine`` 一直有完整的 ``to_list``/``from_list``，
但 ``ConfigManager.save()`` 的 payload 里从来没有 ``filters`` 键，``load()``
也从不读它 —— 用户在「筛选规则」对话框里辛苦配的规则**重启即丢**，界面上
那句"本次运行有效；如需永久保存请写入配置文件"就是这个洞的自白。

这里锁住三件事：
1. ``save()``/``load()`` 能完整往返规则（含条件、msg_types、platforms、
   ``match_any``、``enabled`` 等全部字段）；
2. 用户把规则全删光（空列表）会被尊重，不被默认规则集覆盖；
3. 配置文件里那一节写坏时不炸、回落默认。
"""

from __future__ import annotations

import json

from danmaku_kb.config.manager import ConfigManager
from danmaku_kb.filters.rules import (
    CompareOp,
    Condition,
    FieldMatch,
    FilterAction,
    FilterEngine,
    FilterRule,
)


def _rich_rule() -> FilterRule:
    """一条把所有字段都填满的规则，用来验证往返不丢字段。"""
    return FilterRule(
        name="挡广告",
        action=FilterAction.REWRITE,
        conditions=[
            Condition(field=FieldMatch.CONTENT, op=CompareOp.CONTAINS, value="加群"),
            Condition(
                field=FieldMatch.NICKNAME,
                op=CompareOp.REGEX,
                value="^广告.*",
                case_sensitive=True,
                negate=True,
            ),
        ],
        msg_types=["danmaku"],
        platforms=["bilibili", "douyin"],
        strip_prefix="!",
        rewrite_to="（已过滤）",
        enabled=False,
        description="测试用",
        match_any=True,
    )


class TestFilterPersistence:
    def test_save_writes_filters_key(self, tmp_path):
        manager = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
        manager.filters = [_rich_rule()]
        assert manager.save() is True
        payload = json.loads(manager.settings_path.read_text(encoding="utf-8"))
        assert "filters" in payload, "save() 必须把筛选规则写进 settings.json"
        assert payload["filters"][0]["name"] == "挡广告"

    def test_roundtrip_preserves_every_field(self, tmp_path):
        manager = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
        manager.filters = [_rich_rule()]
        assert manager.save() is True

        again = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
        assert again.load() is True
        assert len(again.filters) == 1
        rule = again.filters[0]
        assert rule.name == "挡广告"
        assert rule.action is FilterAction.REWRITE
        assert rule.enabled is False
        assert rule.match_any is True
        assert rule.strip_prefix == "!"
        assert rule.rewrite_to == "（已过滤）"
        assert rule.description == "测试用"
        # 构造时故意塞字符串（界面/手改 JSON 都可能这样），序列化不能炸，
        # 反序列化后应被规整成 MessageType 枚举
        from danmaku_kb.danmaku.message import MessageType

        assert rule.msg_types == [MessageType.DANMAKU]
        assert set(rule.platforms) == {"bilibili", "douyin"}
        assert len(rule.conditions) == 2
        assert rule.conditions[0].value == "加群"
        assert rule.conditions[1].op is CompareOp.REGEX
        assert rule.conditions[1].case_sensitive is True
        assert rule.conditions[1].negate is True

    def test_rule_order_is_preserved(self, tmp_path):
        """规则是有序列表，第一条命中生效 —— 顺序丢了语义就变了。"""
        manager = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
        manager.filters = [
            FilterRule(name="一", action=FilterAction.DROP),
            FilterRule(name="二", action=FilterAction.ALLOW),
            FilterRule(name="三", action=FilterAction.DISPLAY_ONLY),
        ]
        manager.save()
        again = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
        again.load()
        assert [r.name for r in again.filters] == ["一", "二", "三"]

    def test_empty_list_is_respected(self, tmp_path):
        """用户把规则全删了，重启后不该又冒出默认规则。"""
        manager = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
        manager.filters = []
        manager.save()
        again = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
        again.load()
        assert again.filters == []

    def test_missing_key_falls_back_to_defaults(self, tmp_path):
        """老配置文件里没有 filters 节 —— 用默认规则集，不是空。"""
        manager = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
        manager.ensure_dir()
        manager.settings_path.write_text(
            json.dumps({"version": 2, "config": {}, "key_mappings": []}),
            encoding="utf-8",
        )
        assert manager.load() is True
        assert len(manager.filters) == len(FilterEngine.default().rules)

    def test_malformed_section_falls_back_without_raising(self, tmp_path):
        manager = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
        manager.ensure_dir()
        manager.settings_path.write_text(
            json.dumps({"version": 2, "config": {}, "filters": "不是列表"}),
            encoding="utf-8",
        )
        assert manager.load() is True
        assert len(manager.filters) == len(FilterEngine.default().rules)

    def test_garbage_entries_do_not_wipe_everything(self, tmp_path):
        """整节是垃圾字典时 ``from_list`` 回落默认，不能抛异常。"""
        manager = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
        manager.ensure_dir()
        manager.settings_path.write_text(
            json.dumps({"version": 2, "config": {}, "filters": [{"name": 1, "action": "??"}]}),
            encoding="utf-8",
        )
        assert manager.load() is True
        assert len(manager.filters) >= 1

    def test_reset_restores_default_rules(self, tmp_path):
        manager = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
        manager.filters = [FilterRule(name="只剩一条")]
        manager.reset()
        assert len(manager.filters) == len(FilterEngine.default().rules)

    def test_loaded_rules_actually_work(self, tmp_path):
        """持久化的规则要能真的参与过滤，而不只是"存下来了"。"""
        manager = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
        manager.filters = [
            FilterRule(
                name="挡加群",
                action=FilterAction.DROP,
                conditions=[Condition(field=FieldMatch.CONTENT, op=CompareOp.CONTAINS, value="加群")],
            )
        ]
        manager.save()

        again = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
        again.load()
        engine = FilterEngine(rules=again.filters)
        from danmaku_kb.danmaku.message import DanmakuMessage

        blocked = engine.evaluate(DanmakuMessage(content="快来加群", nickname="u", user_id="1"))
        assert blocked.dropped is True
        passed = engine.evaluate(DanmakuMessage(content="你好", nickname="u", user_id="1"))
        assert passed.dropped is False


class TestHitCountKeys:
    """``FilterEngine.evaluate`` 的命中计数键。"""

    def test_same_name_rules_do_not_share_counter(self):
        """两条同名规则以前都用 ``rule.name`` 当键，命中数会合并成一格。"""
        engine = FilterEngine(
            rules=[
                FilterRule(
                    name="同名",
                    action=FilterAction.DROP,
                    conditions=[Condition(field=FieldMatch.CONTENT, op=CompareOp.CONTAINS, value="a")],
                ),
                FilterRule(
                    name="同名",
                    action=FilterAction.DROP,
                    conditions=[Condition(field=FieldMatch.CONTENT, op=CompareOp.CONTAINS, value="b")],
                ),
            ]
        )
        from danmaku_kb.danmaku.message import DanmakuMessage

        engine.evaluate(DanmakuMessage(content="a", nickname="u", user_id="1"))
        engine.evaluate(DanmakuMessage(content="b", nickname="u", user_id="1"))
        # 两条规则各命中一次，必须是两个独立的键，且都只记 1 次
        assert len(engine.hit_counts) == 2, engine.hit_counts
        assert sorted(engine.hit_counts.values()) == [1, 1]

    def test_unique_name_keeps_plain_key(self):
        """名字唯一时统计键保持原名（界面上直接显示规则名）。"""
        engine = FilterEngine(
            rules=[
                FilterRule(
                    name="挡",
                    action=FilterAction.DROP,
                    conditions=[Condition(field=FieldMatch.CONTENT, op=CompareOp.CONTAINS, value="x")],
                )
            ]
        )
        from danmaku_kb.danmaku.message import DanmakuMessage

        engine.evaluate(DanmakuMessage(content="x", nickname="u", user_id="1"))
        assert engine.hit_counts == {"挡": 1}

    def test_unnamed_rule_uses_positional_key(self):
        engine = FilterEngine(
            rules=[
                FilterRule(
                    name="",
                    action=FilterAction.DROP,
                    conditions=[Condition(field=FieldMatch.CONTENT, op=CompareOp.CONTAINS, value="x")],
                )
            ]
        )
        from danmaku_kb.danmaku.message import DanmakuMessage

        result = engine.evaluate(DanmakuMessage(content="x", nickname="u", user_id="1"))
        assert result.rule == "rule@0"
