# -*- coding: utf-8 -*-
"""弹幕 → 映射的触发匹配。

这是"筛选符合弹幕"的第二道关（第一道是
:mod:`screenkey.filters.rules` 的规则过滤）：把已经通过的弹幕文本
拿去和映射表比对，决定该触发哪些动作。

参考项目 ``core/input/matcher.py`` 的三级顺序（精确 → 感应 → 模糊）
是对的，但它把感应放在精确之后却仍在**同一个函数里返回第一个命中**，
且感应分支构造动作时只传了 4 个字段（``dataclasses.replace`` 的 bug），
丢掉 ``cooldown`` / ``delay``。这里用统一的 :class:`MatchResult`
承载"命中了哪条映射 + 命中的强度"，构造动作时始终从原映射派生。

匹配优先级（先命中先返回）：

1. **精确** —— 规整后完全相等
2. **感应** —— 重复字符压缩后与触发词相等（"66666" 命中 "6"）
3. **包含 / 前缀 / 正则** —— 按映射声明的 ``match_mode``

长按判定：感应模式下重复次数 ≥ ``long_press_count`` 视为长按。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .mappings import BaseMapping, MatchMode

# ----------------------------------------------------------------------
# 重复字符检测
# ----------------------------------------------------------------------


def detect_repeated_char(content: str) -> Tuple[bool, str, int]:
    """检测"整条弹幕是否由同一个字符重复组成"。

    返回 ``(是否重复, 字符, 重复次数)``。长度小于 2 一律返回
    ``(False, "", 0)`` —— 单个字符不算"重复"，它应当走精确匹配。

    与参考项目一致：只认"整体重复"，不做"部分重复"检测。
    这样 "66666" 命中 "6"，而 "666哈哈哈" 不命中，避免误触。
    """
    if not content or len(content) < 2:
        return (False, "", 0)

    first = content[0]
    for char in content[1:]:
        if char != first:
            return (False, "", 0)
    return (True, first, len(content))


def collapse_repeats(content: str) -> str:
    """把连续重复字符压缩成一个：``"66666"`` → ``"6"``。"""
    if not content:
        return ""
    out: List[str] = []
    for char in content:
        if not out or out[-1] != char:
            out.append(char)
    return "".join(out)


# ----------------------------------------------------------------------
# 匹配结果
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class MatchResult:
    """一条匹配结果。"""

    mapping: BaseMapping
    #: 命中方式
    kind: str
    #: 是否为长按（感应模式下重复次数达到阈值）
    is_long_press: bool = False
    #: 感应模式下的重复次数
    repeat_count: int = 0
    #: 实际命中的那个触发词。一条映射可以用逗号写多个触发词
    #: （``w,上,前``），排序与日志都该用命中的那一个，
    #: 而不是整串；空串表示回落成 ``mapping.trigger``。
    matched_trigger: str = ""

    @property
    def trigger(self) -> str:
        """命中的触发词；没有记录时回落到映射的整串触发词。"""
        return self.matched_trigger or self.mapping.trigger

    @property
    def delay(self) -> float:
        return float(self.mapping.delay)

    @property
    def cooldown(self) -> float:
        return float(self.mapping.cooldown)

    @property
    def duration(self) -> float:
        """长按时应持续的秒数。"""
        return float(self.mapping.long_press_duration)

    def __str__(self) -> str:  # pragma: no cover
        return f"<{self.kind} {self.mapping.trigger!r} long={self.is_long_press}>"


# ----------------------------------------------------------------------
# 单条匹配
# ----------------------------------------------------------------------


def _norm(text: str, case_sensitive: bool) -> str:
    """按映射的大小写设置规整文本。"""
    text = (text or "").strip()
    return text if case_sensitive else text.lower()


def _match_one(
    content: str,
    mapping: BaseMapping,
    repeat: Tuple[bool, str, int],
) -> Optional[MatchResult]:
    """尝试用一条映射匹配内容。

    一条映射可以用逗号写多个触发词（``w,上,前``），任一个命中即算命中；
    因此这里遍历 :attr:`BaseMapping.normalized_triggers`，
    并在结果里记下**实际命中的那个触发词**（``matched_trigger``），
    供排序（长触发词优先）与日志使用。
    """
    if not mapping.enabled:
        return None

    triggers = mapping.normalized_triggers
    if not triggers:
        return None

    normalized = _norm(content, mapping.case_sensitive)
    mode = mapping.match_mode
    case_sensitive = mapping.case_sensitive

    # --- 1. 精确 ---
    for trigger in triggers:
        if normalized == trigger:
            return MatchResult(mapping=mapping, kind="exact", matched_trigger=trigger)

    # --- 2. 感应（重复字符） ---
    # 仅当映射开了 enable_sensing，或映射显式声明为 SENSING 模式
    sensing_on = mapping.enable_sensing or mode is MatchMode.SENSING
    if sensing_on and repeat[0]:
        repeated_char = repeat[1] if case_sensitive else repeat[1].lower()
        count = repeat[2]
        # short_press_count 是"触发所需的最少重复次数"下限。
        # 低于它整条不匹配 —— 否则用户设的阈值形同虚设，
        # 弹幕里随便两个重复字符都会按出键来。
        min_count = max(1, int(mapping.short_press_count))
        if count < min_count:
            return None
        long_threshold = max(1, int(mapping.long_press_count))
        collapsed = collapse_repeats(repeated_char)
        for trigger in triggers:
            if repeated_char == trigger or collapsed == collapse_repeats(trigger):
                return MatchResult(
                    mapping=mapping,
                    kind="sensing",
                    is_long_press=count >= long_threshold,
                    repeat_count=count,
                    matched_trigger=trigger,
                )

    # --- 3. 声明式匹配 ---
    if mode is MatchMode.CONTAINS:
        for trigger in triggers:
            if trigger in normalized:
                return MatchResult(mapping=mapping, kind="contains", matched_trigger=trigger)
    elif mode is MatchMode.STARTWITH:
        for trigger in triggers:
            if normalized.startswith(trigger):
                return MatchResult(mapping=mapping, kind="startswith", matched_trigger=trigger)
    elif mode is MatchMode.REGEX:
        # 正则模式下"多个触发词"= 多个候选正则，任一个匹配即可。
        # 正则用未规整的原串（大小写敏感性与用户写的字符类有关）。
        flags = 0 if case_sensitive else re.IGNORECASE
        for pattern in mapping.triggers:
            try:
                if re.search(pattern, content, flags):
                    return MatchResult(mapping=mapping, kind="regex", matched_trigger=pattern)
            except re.error:
                # 单个正则写错只跳过它，不影响这条映射的其它触发词。
                continue
        return None
    elif mode is MatchMode.EXACT:
        # 精确模式已经在上面的第 1 步处理过；此处不再放宽，
        # 否则 "精确" 会退化成 "包含"，参考项目曾因此让短触发词误触。
        return None
    elif mode is MatchMode.SENSING:
        # 感应模式只认重复字符，不认包含
        return None

    return None


# ----------------------------------------------------------------------
# 批量匹配
# ----------------------------------------------------------------------


def match_content(
    content: str,
    mappings: Sequence[BaseMapping],
    *,
    max_results: int = 1,
) -> List[MatchResult]:
    """返回内容命中的全部映射，按优先级排序。

    排序键：精确 > 感应 > 包含/前缀/正则；同级内触发词更长的优先
    （长触发词更具体，"回车" 应当比 "车" 先命中）。

    :param max_results: 最多返回几条；0 表示不限
    """
    if not content or not mappings:
        return []

    repeat = detect_repeated_char(content)
    exact: List[MatchResult] = []
    sensing: List[MatchResult] = []
    loose: List[MatchResult] = []

    for mapping in mappings:
        result = _match_one(content, mapping, repeat)
        if result is None:
            continue
        if result.kind == "exact":
            exact.append(result)
        elif result.kind == "sensing":
            sensing.append(result)
        else:
            loose.append(result)

    def _sort_key(item: MatchResult) -> Tuple[int, int]:
        # 触发词越长越具体，排前面
        return (-len(item.trigger), 0 if item.is_long_press else 1)

    exact.sort(key=_sort_key)
    sensing.sort(key=_sort_key)
    loose.sort(key=_sort_key)

    ordered = exact + sensing + loose
    if max_results and max_results > 0:
        return ordered[:max_results]
    return ordered


def find_mapping(
    content: str,
    mappings: Sequence[BaseMapping],
) -> Optional[MatchResult]:
    """返回最佳匹配（没有则 ``None``）。"""
    results = match_content(content, mappings, max_results=1)
    return results[0] if results else None


# ----------------------------------------------------------------------
# 冷却判定
# ----------------------------------------------------------------------


class CooldownTracker:
    """按映射键记录上次触发时间，实现 ``cooldown``。

    参考项目把 ``last_trigger_time`` 字典在界面层与匹配器之间来回传递，
    容易漏更新。这里封成一个对象，并额外支持"每用户冷却"。
    """

    def __init__(self) -> None:
        self._last: Dict[str, float] = {}
        self._last_user: Dict[str, float] = {}

    @staticmethod
    def _now(now: Optional[float] = None) -> float:
        """``now`` 缺省时取单调时钟。

        显式传 ``now`` 是为了测试能注入假时间；
        生产代码直接省略即可 —— 参考项目要求调用方处处传时间戳，
        结果就是有人忘了传、冷却静默失效。
        """
        return time.monotonic() if now is None else float(now)

    def ready(self, key: str, cooldown: float, now: Optional[float] = None) -> bool:
        """该键是否已过冷却期（不刷新时间）。"""
        if cooldown <= 0:
            return True
        last = self._last.get(key)
        if last is None:
            return True
        return (self._now(now) - last) >= cooldown

    def mark(self, key: str, now: Optional[float] = None) -> None:
        """记录一次触发。"""
        self._last[key] = self._now(now)

    def ready_for_user(self, user_id: str, cooldown: float, now: Optional[float] = None) -> bool:
        """该用户是否已过冷却期。"""
        if cooldown <= 0 or not user_id:
            return True
        last = self._last_user.get(user_id)
        if last is None:
            return True
        return (self._now(now) - last) >= cooldown

    def mark_user(self, user_id: str, now: Optional[float] = None) -> None:
        """记录某用户的一次触发。"""
        if user_id:
            self._last_user[user_id] = self._now(now)

    def reset(self) -> None:
        """清空全部记录。"""
        self._last.clear()
        self._last_user.clear()

    def prune(self, now: Optional[float] = None, max_age: float = 600.0) -> None:
        """清理过期记录，防止长时间运行内存增长。"""
        cutoff = self._now(now) - max(60.0, max_age)
        for store in (self._last, self._last_user):
            for key in [k for k, ts in store.items() if ts < cutoff]:
                store.pop(key, None)


__all__ = [
    "CooldownTracker",
    "MatchResult",
    "collapse_repeats",
    "detect_repeated_char",
    "find_mapping",
    "match_content",
]
