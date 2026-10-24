# -*- coding: utf-8 -*-
"""弹幕筛选规则引擎。

参考项目的筛选是"一个大类 ``DanmakuFilter`` 带 6 个平行列表"：
``keywords`` / ``prefixes`` / ``regex_patterns`` / ``blocked_users`` /
``min_length`` / ``max_length``。问题是这些条件**只能全部叠加**，
用户没法表达"包含 A 或 包含 B"、"命中正则就不看关键词"这类组合，
也没法给不同规则配不同动作（比如"只显示不按键"）。

这里改成**有序规则列表**：每条规则自带条件（全部满足才算命中）与动作
（允许通过 / 丢弃 / 改写内容 / 只显示不触发按键）。规则按顺序求值，
第一条给出结论的规则胜出，末尾的默认规则兜底。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..danmaku.message import DanmakuMessage, MessageType

# ----------------------------------------------------------------------
# 动作
# ----------------------------------------------------------------------


class FilterAction(str, Enum):
    """规则命中后的处置方式。"""

    ALLOW = "allow"            # 放行，参与按键映射
    DROP = "drop"              # 丢弃，界面也不显示
    DISPLAY_ONLY = "display_only"  # 界面显示，但不触发按键
    REWRITE = "rewrite"        # 改写内容后放行（配合 rewrite_to / strip_prefix）

    @classmethod
    def coerce(cls, value: Any, default: "FilterAction" = None) -> "FilterAction":  # type: ignore[assignment]
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except (ValueError, AttributeError):
            return default or cls.ALLOW


class FieldMatch(str, Enum):
    """条件针对哪个字段。"""

    CONTENT = "content"
    NICKNAME = "nickname"
    USER_ID = "user_id"
    PLATFORM = "platform"
    MSG_TYPE = "msg_type"

    @classmethod
    def coerce(cls, value: Any, default: "FieldMatch" = None) -> "FieldMatch":  # type: ignore[assignment]
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except (ValueError, AttributeError):
            return default or cls.CONTENT


class CompareOp(str, Enum):
    """比较方式。"""

    EQUALS = "equals"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    STARTWITH = "startswith"
    ENDSWITH = "endswith"
    REGEX = "regex"
    NOT_REGEX = "not_regex"
    IN = "in"                  # 值在给定列表里
    NOT_IN = "not_in"
    LENGTH_GT = "length_gt"
    LENGTH_LT = "length_lt"
    LENGTH_EQ = "length_eq"

    @classmethod
    def coerce(cls, value: Any, default: "CompareOp" = None) -> "CompareOp":  # type: ignore[assignment]
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except (ValueError, AttributeError):
            return default or cls.CONTAINS


# ----------------------------------------------------------------------
# 条件
# ----------------------------------------------------------------------


@dataclass
class Condition:
    """一条判断条件。

    :param field: 针对哪个字段
    :param op: 比较方式
    :param value: 比较值；``IN``/``NOT_IN`` 时可以是列表
    :param case_sensitive: 是否区分大小写
    :param negate: 整体取反
    """

    field: FieldMatch = FieldMatch.CONTENT
    op: CompareOp = CompareOp.CONTAINS
    value: Any = ""
    case_sensitive: bool = False
    negate: bool = False

    # 注意：本类有个字段叫 ``field``，它在类体内会遮蔽 ``dataclasses.field``。
    # 因此这里**不能**再写 ``field(default=...)``，否则拿到的是 FieldMatch 枚举成员。
    # 需要默认值的可变字段请改用 ``default_factory`` 语义之外的方式，
    # 或把此类拆成两个 dataclass。当前所有字段都是不可变默认值，故无此需求。

    def test(self, message: DanmakuMessage) -> bool:
        """判断消息是否满足本条件。"""
        raw = self._extract(message)
        result = self._compare(raw)
        return (not result) if self.negate else result

    def _extract(self, message: DanmakuMessage) -> str:
        """取出待比较的字段值（统一成字符串）。"""
        if self.field is FieldMatch.CONTENT:
            return message.content or ""
        if self.field is FieldMatch.NICKNAME:
            return message.nickname or ""
        if self.field is FieldMatch.USER_ID:
            return message.user_id or ""
        if self.field is FieldMatch.PLATFORM:
            return message.platform or ""
        if self.field is FieldMatch.MSG_TYPE:
            return message.msg_type.value
        return ""

    def _compare(self, raw: str) -> bool:
        """执行比较。"""
        op = self.op
        expected = self.value

        # 长度类条件直接算，不涉及字符串比较
        if op is CompareOp.LENGTH_GT:
            return len(raw) > self._as_int(expected)
        if op is CompareOp.LENGTH_LT:
            return len(raw) < self._as_int(expected)
        if op is CompareOp.LENGTH_EQ:
            return len(raw) == self._as_int(expected)

        # 集合类条件
        if op in (CompareOp.IN, CompareOp.NOT_IN):
            items = self._as_list(expected)
            if not self.case_sensitive:
                haystack = raw.lower()
                items = [str(i).lower() for i in items]
            else:
                haystack = raw
                items = [str(i) for i in items]
            found = haystack in items
            return found if op is CompareOp.IN else not found

        # 字符串比较
        if op in (CompareOp.REGEX, CompareOp.NOT_REGEX):
            pattern = str(expected or "")
            if not pattern:
                return False
            try:
                flags = 0 if self.case_sensitive else re.IGNORECASE
                matched = re.search(pattern, raw, flags) is not None
            except re.error:
                # 用户写错正则：视为不匹配，并静默跳过（不能因此丢掉整条弹幕流）
                return False
            return matched if op is CompareOp.REGEX else not matched

        text = raw if self.case_sensitive else raw.lower()
        needle = str(expected if self.case_sensitive else str(expected).lower())

        if op is CompareOp.EQUALS:
            return text == needle
        if op is CompareOp.CONTAINS:
            return needle in text if needle else False
        if op is CompareOp.NOT_CONTAINS:
            return needle not in text if needle else True
        if op is CompareOp.STARTWITH:
            return text.startswith(needle) if needle else False
        if op is CompareOp.ENDSWITH:
            return text.endswith(needle) if needle else False
        return False

    @staticmethod
    def _as_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _as_list(value: Any) -> List[Any]:
        if isinstance(value, (list, tuple, set)):
            return list(value)
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return [value]

    def to_dict(self) -> Dict[str, Any]:
        """序列化。"""
        return {
            "field": self.field.value,
            "op": self.op.value,
            "value": self.value,
            "case_sensitive": self.case_sensitive,
            "negate": self.negate,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Condition":
        """反序列化（宽容）。"""
        if not isinstance(data, Mapping):
            return cls()
        return cls(
            field=FieldMatch.coerce(data.get("field")),
            op=CompareOp.coerce(data.get("op")),
            value=data.get("value", ""),
            case_sensitive=bool(data.get("case_sensitive", False)),
            negate=bool(data.get("negate", False)),
        )


# ----------------------------------------------------------------------
# 规则
# ----------------------------------------------------------------------


@dataclass
class FilterRule:
    """一条筛选规则。

    ``conditions`` 之间默认是**与**关系（全部满足才命中）；需要"或"就写多条
    规则，或把 ``match_any`` 设为 ``True``。空 ``conditions`` 表示无条件命中
    （用于兜底规则）。

    ``match_any`` 存在的理由：像"长度必须落在 [min, max] 区间内"这种**一侧太短、
    另一侧太长**的约束，用 DROP 动作表达时要写成"太短 或 太长 → 丢弃"，
    这是两个条件的**或**，单靠"与"永远无法命中（历史上 ``length_rule`` 就
    因此完全失效）。
    """

    name: str = ""
    action: FilterAction = FilterAction.ALLOW
    conditions: List[Condition] = field(default_factory=list)
    #: 需要放行的消息类型；空表示不限
    msg_types: List[MessageType] = field(default_factory=list)
    #: 只对某些平台生效；空表示不限
    platforms: List[str] = field(default_factory=list)
    #: ``REWRITE`` 动作：剥掉的前缀
    strip_prefix: str = ""
    #: ``REWRITE`` 动作：替换成的内容（为空则只做 strip_prefix）
    rewrite_to: str = ""
    #: 是否启用
    enabled: bool = True
    #: 规则说明
    description: str = ""
    #: ``conditions`` 之间是"或"（任一命中即算命中）。默认"与"。
    match_any: bool = False

    def matches(self, message: DanmakuMessage) -> bool:
        """消息是否命中本规则。"""
        if not self.enabled:
            return False

        if self.msg_types and message.msg_type not in self.msg_types:
            return False

        if self.platforms:
            allowed = {str(p).strip().lower() for p in self.platforms}
            if (message.platform or "").lower() not in allowed:
                return False

        if not self.conditions:
            return True

        if self.match_any:
            return any(condition.test(message) for condition in self.conditions)

        for condition in self.conditions:
            if not condition.test(message):
                return False
        return True

    def apply(self, message: DanmakuMessage) -> Tuple[Optional[DanmakuMessage], bool]:
        """应用规则。

        返回 ``(消息或 None, 是否允许触发按键)``：

        - ``DROP``         → ``(None, False)``
        - ``DISPLAY_ONLY`` → ``(message, False)``
        - ``REWRITE``      → ``(改写后的消息, True)``
        - ``ALLOW``        → ``(message, True)``
        """
        if self.action is FilterAction.DROP:
            return (None, False)
        if self.action is FilterAction.DISPLAY_ONLY:
            return (message, False)
        if self.action is FilterAction.REWRITE:
            return (self._rewrite(message), True)
        return (message, True)

    def _rewrite(self, message: DanmakuMessage) -> DanmakuMessage:
        """按规则改写消息内容。"""
        content = message.content
        if self.strip_prefix and content.startswith(self.strip_prefix):
            content = content[len(self.strip_prefix):].strip()
        if self.rewrite_to:
            content = self.rewrite_to
        return message.with_content(content)

    def to_dict(self) -> Dict[str, Any]:
        """序列化。"""
        return {
            "name": self.name,
            "action": self.action.value,
            "conditions": [c.to_dict() for c in self.conditions],
            # msg_types 理论上都是 MessageType 枚举，但界面/手改 JSON 可能塞进
            # 字符串。早期这里直接 ``t.value``，一条字符串就让整个 save() 抛
            # AttributeError —— 用户改了一条规则导致所有配置都存不下来。
            "msg_types": [
                t.value if isinstance(t, MessageType) else str(t) for t in self.msg_types
            ],
            "platforms": list(self.platforms),
            "strip_prefix": self.strip_prefix,
            "rewrite_to": self.rewrite_to,
            "enabled": self.enabled,
            "description": self.description,
            "match_any": self.match_any,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FilterRule":
        """反序列化（宽容）。"""
        if not isinstance(data, Mapping):
            return cls()

        raw_conditions = data.get("conditions")
        conditions: List[Condition] = []
        if isinstance(raw_conditions, Sequence) and not isinstance(raw_conditions, (str, bytes)):
            for item in raw_conditions:
                try:
                    conditions.append(Condition.from_dict(item))
                except Exception:  # pragma: no cover - 防御性
                    continue

        raw_types = data.get("msg_types")
        msg_types: List[MessageType] = []
        if isinstance(raw_types, Sequence) and not isinstance(raw_types, (str, bytes)):
            msg_types = [MessageType.coerce(t) for t in raw_types]

        raw_platforms = data.get("platforms")
        platforms: List[str] = []
        if isinstance(raw_platforms, Sequence) and not isinstance(raw_platforms, (str, bytes)):
            platforms = [str(p) for p in raw_platforms]

        return cls(
            name=str(data.get("name", "") or ""),
            action=FilterAction.coerce(data.get("action")),
            conditions=conditions,
            msg_types=msg_types,
            platforms=platforms,
            strip_prefix=str(data.get("strip_prefix", "") or ""),
            rewrite_to=str(data.get("rewrite_to", "") or ""),
            enabled=bool(data.get("enabled", True)),
            description=str(data.get("description", "") or ""),
            match_any=bool(data.get("match_any", False)),
        )


# ----------------------------------------------------------------------
# 规则集
# ----------------------------------------------------------------------


@dataclass
class FilterResult:
    """筛选结果。"""

    #: 通过筛选后的消息；被丢弃时为 None
    message: Optional[DanmakuMessage]
    #: 是否允许触发按键映射
    actionable: bool = True
    #: 命中的规则名
    rule: str = ""
    #: 丢弃原因（用于调试）
    reason: str = ""

    @property
    def dropped(self) -> bool:
        return self.message is None

    def __bool__(self) -> bool:
        return self.message is not None


class FilterEngine:
    """有序规则集。

    规则按顺序求值，**第一条命中即返回**。所以顺序有意义：
    把"屏蔽关键词"这类拒绝规则放前面，"默认放行"兜底规则放最后。
    """

    def __init__(self, rules: Optional[Sequence[FilterRule]] = None) -> None:
        self.rules: List[FilterRule] = list(rules or [])
        #: 统计：每条规则命中次数
        self.hit_counts: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # 规则管理
    # ------------------------------------------------------------------

    def add_rule(self, rule: FilterRule, index: int = -1) -> None:
        """插入一条规则；``index < 0`` 表示追加到末尾。"""
        if index < 0 or index >= len(self.rules):
            self.rules.append(rule)
        else:
            self.rules.insert(index, rule)

    def remove_rule(self, index: int) -> bool:
        """删除指定位置的规则。"""
        if 0 <= index < len(self.rules):
            self.rules.pop(index)
            return True
        return False

    def move_rule(self, index: int, offset: int) -> bool:
        """上下移动规则（界面用）。"""
        target = index + offset
        if not (0 <= index < len(self.rules) and 0 <= target < len(self.rules)):
            return False
        self.rules[index], self.rules[target] = self.rules[target], self.rules[index]
        return True

    def clear(self) -> None:
        """清空规则。"""
        self.rules.clear()
        self.hit_counts.clear()

    # ------------------------------------------------------------------
    # 求值
    # ------------------------------------------------------------------

    def _hit_key(self, rule: FilterRule, position: int) -> str:
        """命中计数的键。

        未命名规则用位置键 ``rule@N``。**同名规则也必须区分开**：两条都叫
        「挡广告」时若都用名字当键，两行的命中数会合并成一格，界面上看不出
        到底是哪一条在生效。
        """
        if not rule.name:
            return f"rule@{position}"
        duplicates = sum(1 for other in self.rules if other.name == rule.name)
        return f"{rule.name}@{position}" if duplicates > 1 else rule.name

    def evaluate(self, message: DanmakuMessage) -> FilterResult:
        """对一条消息应用规则集。"""
        # 用 enumerate 而不是 ``self.rules.index(rule)``：后者按 ``==`` 比较，
        # 两条内容完全相同的规则会拿到同一个下标（统计串台），而且是 O(n)，
        # 规则多时每条弹幕都要做一遍 O(n²) 的线性查找。
        for position, rule in enumerate(self.rules):
            if not rule.matches(message):
                continue
            key = self._hit_key(rule, position)
            self.hit_counts[key] = self.hit_counts.get(key, 0) + 1
            new_message, actionable = rule.apply(message)
            if new_message is None:
                return FilterResult(message=None, actionable=False, rule=key, reason="被规则丢弃")
            return FilterResult(message=new_message, actionable=actionable, rule=key)

        # 没有规则命中：默认放行（保持"不加规则就等于不过滤"的直觉）
        return FilterResult(message=message, actionable=True, rule="")

    def filter(self, message: DanmakuMessage) -> Tuple[Optional[DanmakuMessage], bool]:
        """便捷接口：返回 ``(消息或 None, 是否可触发按键)``。"""
        result = self.evaluate(message)
        return (result.message, result.actionable)

    # ------------------------------------------------------------------
    # 预设规则
    # ------------------------------------------------------------------

    @classmethod
    def default(cls) -> "FilterEngine":
        """构造一套开箱可用的默认规则。

        默认**不丢弃任何弹幕**（``allow_all`` 兜底），只是把
        "进入直播间"这类噪音标记为不触发按键，避免观众进出就乱按键。
        """
        return cls(
            [
                FilterRule(
                    name="进入直播间不触发按键",
                    action=FilterAction.DISPLAY_ONLY,
                    msg_types=[MessageType.ENTER, MessageType.FOLLOW, MessageType.LIKE, MessageType.STATS],
                    description="观众进出、点赞、关注只在界面显示，不模拟按键",
                ),
                FilterRule(
                    name="默认放行",
                    action=FilterAction.ALLOW,
                    description="未命中任何规则时放行",
                ),
            ]
        )

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_list(self) -> List[Dict[str, Any]]:
        """序列化规则列表。"""
        return [rule.to_dict() for rule in self.rules]

    @classmethod
    def from_list(cls, data: Any) -> "FilterEngine":
        """从列表反序列化；单条损坏跳过。"""
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
            return cls.default()
        rules: List[FilterRule] = []
        for item in data:
            try:
                rules.append(FilterRule.from_dict(item))
            except Exception:  # pragma: no cover - 防御性
                continue
        return cls(rules) if rules else cls.default()


# ----------------------------------------------------------------------
# 常用规则工厂
# ----------------------------------------------------------------------


def keyword_block_rule(keywords: Sequence[str], name: str = "屏蔽关键词") -> FilterRule:
    """构造"包含任一关键词就丢弃"的规则。

    多个关键词用多条 ``Condition`` + ``negate`` 表达"或"比较绕，
    这里直接用一条 ``IN`` 之外的做法：为每个关键词生成一条规则会更清晰，
    但为兼容"一条规则多个关键词"，用正则的 ``|`` 合并。
    """
    cleaned = [k for k in (str(k).strip() for k in keywords) if k]
    if not cleaned:
        return FilterRule(name=name, action=FilterAction.DROP, description="无关键词，永不命中", enabled=False)
    pattern = "|".join(re.escape(k) for k in cleaned)
    return FilterRule(
        name=name,
        action=FilterAction.DROP,
        conditions=[Condition(field=FieldMatch.CONTENT, op=CompareOp.REGEX, value=pattern)],
        description=f"包含以下任一关键词则丢弃：{', '.join(cleaned)}",
    )


def prefix_rewrite_rule(prefix: str, name: str = "剥掉指令前缀") -> FilterRule:
    """构造"以某前缀开头就剥掉前缀后放行"的规则。"""
    return FilterRule(
        name=name,
        action=FilterAction.REWRITE,
        conditions=[Condition(field=FieldMatch.CONTENT, op=CompareOp.STARTWITH, value=prefix)],
        strip_prefix=prefix,
        description=f"剥掉前缀 {prefix!r} 后参与按键匹配",
    )


def user_block_rule(user_ids: Sequence[str], name: str = "屏蔽用户") -> FilterRule:
    """构造"指定用户发言直接丢弃"的规则。"""
    ids = [str(u).strip() for u in user_ids if str(u).strip()]
    if not ids:
        return FilterRule(name=name, action=FilterAction.DROP, enabled=False)
    return FilterRule(
        name=name,
        action=FilterAction.DROP,
        conditions=[Condition(field=FieldMatch.USER_ID, op=CompareOp.IN, value=ids)],
        description=f"屏蔽用户：{', '.join(ids)}",
    )


def length_rule(min_length: int = 0, max_length: int = 0, name: str = "长度限制") -> FilterRule:
    """构造长度限制规则（0 表示不限制）。

    规则的动作是 ``DROP``，所以 ``conditions`` 要**直接表达"该丢"的条件**：

    * ``min_length`` → 丢 ``len < min_length`` 的
    * ``max_length`` → 丢 ``len > max_length`` 的

    两个条件之间是**或**（``match_any=True``）：一侧太短、另一侧太长，任一侧
    越界都该丢。历史上这里有两个 bug 叠在一起，导致该规则完全失效：

    1. 两个条件都带着 ``negate=True``，而 ``Condition.test`` 会取反，
       于是语义整体倒过来——``min_length=2`` 反而丢掉"长度 >= 2"的弹幕；
    2. 即使去掉 ``negate``，条件之间默认是**与**，而"长度 < 2"与"长度 > 4"
       不可能同时成立，规则永远不命中。

    所以这里必须同时满足：条件不加 ``negate``，且 ``match_any=True``。
    """
    conditions: List[Condition] = []
    if min_length > 0:
        conditions.append(Condition(field=FieldMatch.CONTENT, op=CompareOp.LENGTH_LT, value=min_length))
    if max_length > 0:
        conditions.append(Condition(field=FieldMatch.CONTENT, op=CompareOp.LENGTH_GT, value=max_length))
    return FilterRule(
        name=name,
        action=FilterAction.DROP,
        conditions=conditions,
        match_any=True,
        enabled=bool(conditions),
        description=f"内容长度需在 {min_length}-{max_length or '∞'} 之间",
    )


__all__ = [
    "CompareOp",
    "Condition",
    "FieldMatch",
    "FilterAction",
    "FilterEngine",
    "FilterResult",
    "FilterRule",
    "keyword_block_rule",
    "length_rule",
    "prefix_rewrite_rule",
    "user_block_rule",
]
