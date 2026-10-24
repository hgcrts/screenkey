# -*- coding: utf-8 -*-
"""键鼠映射数据模型与序列化。

参考项目的 ``core/input/mappings.py`` 里，``_mapping_from_dict`` 依赖
``field.type`` 是真实类型对象，因此该模块**不能**加
``from __future__ import annotations``，否则 ``field.type`` 变成字符串，
枚举解析静默失效 —— 这是一个隐蔽的陷阱。

这里改成显式声明哪些字段是枚举（:data:`_KEY_ENUM_FIELDS` /
:data:`_MOUSE_ENUM_FIELDS`），于是本模块可以放心使用延迟注解，
枚举解析逻辑也一眼可见，不再依赖 dataclass 内部实现细节。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, fields
from enum import Enum
from typing import Any, Dict, List, Mapping, Sequence, Type, TypeVar, Union

T = TypeVar("T", bound=Enum)


class KeyAction(str, Enum):
    """键盘动作。"""

    PRESS = "press"                        # 按下不松
    RELEASE = "release"                    # 松开
    PRESS_AND_RELEASE = "press_and_release"  # 按一下（最常用）
    LONG_PRESS = "long_press"              # 长按

    @classmethod
    def coerce(cls, value: Any, default: "KeyAction" = None) -> "KeyAction":  # type: ignore[assignment]
        return _coerce_enum(cls, value, default or cls.PRESS_AND_RELEASE)


class MouseAction(str, Enum):
    """鼠标动作。"""

    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    PRESS = "press"
    RELEASE = "release"
    LONG_PRESS = "long_press"
    MOVE = "move"
    SCROLL_UP = "scroll_up"
    SCROLL_DOWN = "scroll_down"

    @classmethod
    def coerce(cls, value: Any, default: "MouseAction" = None) -> "MouseAction":  # type: ignore[assignment]
        return _coerce_enum(cls, value, default or cls.CLICK)


class MouseButton(str, Enum):
    """鼠标按键。"""

    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"

    @classmethod
    def coerce(cls, value: Any, default: "MouseButton" = None) -> "MouseButton":  # type: ignore[assignment]
        return _coerce_enum(cls, value, default or cls.LEFT)


class InputType(str, Enum):
    """映射目标类型。"""

    KEYBOARD = "keyboard"
    MOUSE = "mouse"


def _coerce_enum(enum_cls: Type[T], value: Any, default: T) -> T:
    """把任意值转成枚举成员，失败返回 ``default``。"""
    if isinstance(value, enum_cls):
        return value
    if value is None:
        return default
    try:
        return enum_cls(str(value).strip().lower())
    except (ValueError, AttributeError):
        return default


# ----------------------------------------------------------------------
# 匹配模式
# ----------------------------------------------------------------------


class MatchMode(str, Enum):
    """弹幕内容与触发词的匹配方式。"""

    EXACT = "exact"        # 内容完全等于触发词
    CONTAINS = "contains"  # 内容包含触发词
    STARTWITH = "startswith"  # 内容以触发词开头
    REGEX = "regex"        # 正则匹配
    SENSING = "sensing"    # 重复字符感应（"666666" 触发 "6"）

    @classmethod
    def coerce(cls, value: Any, default: "MatchMode" = None) -> "MatchMode":  # type: ignore[assignment]
        # 缺省回落到 EXACT —— 必须与 dataclass 字段默认值一致。
        # 这里若写 CONTAINS，`MatchMode.coerce(None)` 与
        # `KeyMapping(trigger="x").match_mode` 就会给出不同答案。
        return _coerce_enum(cls, value, default or cls.EXACT)


@dataclass
class BaseMapping:
    """键鼠映射的公共字段。

    抽出基类是为了让筛选/匹配逻辑只写一遍（见
    :mod:`danmaku_kb.filters.rules`），而不是像参考项目那样
    在键盘映射与鼠标映射之间复制粘贴。
    """
    #: 触发词（弹幕内容）
    trigger: str
    #: 说明，仅用于界面展示
    description: str = ""
    #: 匹配方式
    match_mode: MatchMode = MatchMode.EXACT
    #: 是否启用
    enabled: bool = True
    #: 动作执行前的延迟（秒）
    delay: float = 0.0
    #: 同一触发词的最小触发间隔（秒）
    cooldown: float = 0.5
    #: 是否启用"重复字符感应"（"66666" → 触发 "6"）
    enable_sensing: bool = False
    #: 感应：达到几次算短按
    short_press_count: int = 1
    #: 感应：达到几次算长按
    long_press_count: int = 6
    #: 长按持续时长（秒）；映射编辑器「长按(秒)」列可调，范围 0.05–5.0
    long_press_duration: float = 0.5
    #: 区分大小写（默认不区分）
    case_sensitive: bool = False

    @property
    def input_type(self) -> InputType:
        """该映射的目标类型（子类覆写）。"""
        raise NotImplementedError

    def to_dict(self) -> Dict[str, Any]:
        """转成可 JSON 序列化的 dict（枚举写成 ``value``）。"""
        return _mapping_to_dict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Any:
        """从 dict 还原；宽容策略见 :func:`_mapping_from_dict`。"""
        return _mapping_from_dict(cls, data)

    #: 触发词分隔符：英文逗号与中文逗号都认。
    TRIGGER_SEPARATORS = (",", "，")

    @property
    def triggers(self) -> List[str]:
        """触发词列表（英文或中文逗号分隔，已去空白与空片段）。

        ``trigger="w,上,前"`` → ``["w", "上", "前"]``；``trigger=""`` → ``[]``。

        界面（``ui/mapping_editor.py``）一直承诺"多个触发词用英文逗号分隔"，
        但早先这里返回的是 ``[self.trigger]``，即把整串当成**一个字面量**：
        ``w,上,前`` 只有弹幕原文恰好是 ``w,上,前`` 时才命中，
        发 ``w`` / ``上`` / ``前`` 全都不触发——界面的提示与实现相反。
        现在按分隔符真拆开，与界面承诺一致。
        """
        raw = self.trigger or ""
        for sep in self.TRIGGER_SEPARATORS:
            raw = raw.replace(sep, self.TRIGGER_SEPARATORS[0])
        return [part.strip() for part in raw.split(self.TRIGGER_SEPARATORS[0]) if part.strip()]

    @property
    def normalized_triggers(self) -> List[str]:
        """规整后的触发词列表：去空白、按需转小写、**去重且保持书写顺序**。

        去重是必要的：``"w,w,W"`` 在默认（不区分大小写）下是同一个触发词，
        重复项会让匹配循环白跑，也会让"最长优先"的挑选结果不稳定。
        """
        seen: set = set()
        out: List[str] = []
        for text in self.triggers:
            key = text if self.case_sensitive else text.lower()
            if key and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    def key_id(self) -> str:
        """用于冷却计时的唯一键。

        用**整串** ``trigger`` 而不是拆开后的某一项：冷却属于"这条映射"，
        不该因为用户把 ``w`` 改写成 ``w,上`` 就凭空多出一份独立冷却。
        """
        return f"{self.input_type.value}:{self.trigger}"

    def normalize_trigger(self) -> str:
        """第一个触发词的规整形式。

        只关心"单个触发词"的调用方（例如日志、界面摘要）用它；
        匹配逻辑请改用 :attr:`normalized_triggers`，否则多触发词会漏配。
        """
        normalized = self.normalized_triggers
        return normalized[0] if normalized else ""


@dataclass
class KeyMapping(BaseMapping):
    """弹幕 → 键盘按键。"""

    #: 目标按键（如 ``"w"``、``"space"``、``"f1"``）
    key: str = ""
    #: 键盘动作
    action: KeyAction = KeyAction.PRESS_AND_RELEASE
    #: 是否使用扫描码直发（游戏模式）
    use_scancode: bool = False

    @property
    def input_type(self) -> InputType:
        return InputType.KEYBOARD


@dataclass
class MouseMapping(BaseMapping):
    """弹幕 → 鼠标动作。"""

    button: MouseButton = MouseButton.LEFT
    action: MouseAction = MouseAction.CLICK
    move_x: int = 0
    move_y: int = 0
    scroll_amount: int = 1

    @property
    def input_type(self) -> InputType:
        return InputType.MOUSE


#: 键鼠映射的联合类型。
#:
#: **不要把这个别名命名为 ``Mapping``**：本模块从 ``typing`` 导入了
#: ``Mapping`` 用于 ``isinstance(data, Mapping)`` 校验，一旦在模块级
#: 重新绑定成 ``Any``，那些校验就会抛
#: ``TypeError: typing.Any cannot be used with isinstance()``，
#: 于是**每一份映射配置都静默加载为空**。这个坑真实发生过。
AnyMapping = Union["KeyMapping", "MouseMapping"]

# ----------------------------------------------------------------------
# 枚举字段表（显式声明，替代脆弱的 field.type 内省）
# ----------------------------------------------------------------------

_KEY_ENUM_FIELDS: Dict[str, Type[Enum]] = {
    "action": KeyAction,
    "match_mode": MatchMode,
}

_MOUSE_ENUM_FIELDS: Dict[str, Type[Enum]] = {
    "button": MouseButton,
    "action": MouseAction,
    "match_mode": MatchMode,
}

#: 反序列化时夹到 ``>= 0`` 的数值字段。
#:
#: 注意 ``move_x`` / ``move_y`` **不在这里** —— 它们是位移量，
#: 负值表示向左/向上移动，是合法输入。
_NON_NEGATIVE_FIELDS = frozenset(
    {"delay", "cooldown", "long_press_duration", "scroll_amount"}
)

#: 反序列化时夹到 ``>= 1`` 的数值字段（计数类，0 会让功能静默失效）。
_POSITIVE_FIELDS = frozenset({"short_press_count", "long_press_count"})


def _mapping_to_dict(mapping: Any) -> Dict[str, Any]:
    """把映射 dataclass 转成可 JSON 序列化的 dict。"""
    result: Dict[str, Any] = {}
    for f in fields(mapping):
        value = getattr(mapping, f.name)
        if isinstance(value, Enum):
            result[f.name] = value.value
        else:
            result[f.name] = value
    return result


def _mapping_from_dict(cls: type, data: Mapping[str, Any]) -> Any:
    """从 dict 还原映射。

    宽容策略（与参考项目一致，因为用户会手改配置文件）：

    - 非 dict 直接抛 ``TypeError``（这是调用方的 bug，不该吞）
    - 未知键忽略
    - 缺失键用 dataclass 默认值
    - 枚举字段接受字符串 value；非法值回落默认，**不让整份配置加载失败**
    - 数值字段尝试 ``int``/``float`` 转换，失败保留默认
    """
    if not isinstance(data, Mapping):
        raise TypeError(f"映射数据必须是 dict，实际为 {type(data).__name__}")

    enum_fields = _KEY_ENUM_FIELDS if cls is KeyMapping else _MOUSE_ENUM_FIELDS
    known = {f.name: f for f in fields(cls)}
    kwargs: Dict[str, Any] = {}

    for name, value in data.items():
        if name not in known:
            continue
        if name in enum_fields:
            default = known[name].default
            if default is dataclasses.MISSING:
                default = None
            kwargs[name] = _coerce_enum(enum_fields[name], value, default)
            continue
        target_type = known[name].type
        # 本模块用了延迟注解，所以 type 可能是字符串；两种都处理
        if isinstance(target_type, str):
            target_type = {"int": int, "float": float, "bool": bool, "str": str}.get(target_type)
        if target_type is bool:
            kwargs[name] = bool(value)
        elif target_type in (int, float):
            try:
                converted = target_type(value)
            except (TypeError, ValueError):
                continue
            # 负数在这里是**危险值**而不是"随便写的数"：
            # 负 delay 会让 time.sleep() 抛 ValueError，负 cooldown 让冷却形同虚设，
            # 0 次重复计数让感应永不触发。一律夹到合法下界。
            if name in _NON_NEGATIVE_FIELDS:
                converted = max(0, converted)
            elif name in _POSITIVE_FIELDS:
                converted = max(1, converted)
            kwargs[name] = converted
        else:
            kwargs[name] = value

    try:
        return cls(**kwargs)
    except TypeError:
        # 触发词之类的必填字段缺失：用空触发词补上，让这一条在界面上可见可改，
        # 而不是整份映射列表加载失败
        kwargs.setdefault("trigger", "")
        kwargs.setdefault("key", "")
        return cls(**kwargs)


def key_mapping_to_dict(mapping: KeyMapping) -> Dict[str, Any]:
    """``KeyMapping`` → dict。"""
    return _mapping_to_dict(mapping)


def key_mapping_from_dict(data: Mapping[str, Any]) -> KeyMapping:
    """dict → ``KeyMapping``。"""
    return _mapping_from_dict(KeyMapping, data)


def mouse_mapping_to_dict(mapping: MouseMapping) -> Dict[str, Any]:
    """``MouseMapping`` → dict。"""
    return _mapping_to_dict(mapping)


def mouse_mapping_from_dict(data: Mapping[str, Any]) -> MouseMapping:
    """dict → ``MouseMapping``。"""
    return _mapping_from_dict(MouseMapping, data)


def key_mappings_to_list(mappings: Sequence[KeyMapping]) -> List[Dict[str, Any]]:
    """批量序列化；单条异常跳过，不影响其余。"""
    out: List[Dict[str, Any]] = []
    for mapping in mappings or ():
        try:
            out.append(key_mapping_to_dict(mapping))
        except Exception:  # pragma: no cover - 防御性
            continue
    return out


def key_mappings_from_list(data: Any) -> List[KeyMapping]:
    """批量反序列化；单条损坏跳过。"""
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
        return []
    out: List[KeyMapping] = []
    for item in data:
        try:
            out.append(key_mapping_from_dict(item))
        except (TypeError, ValueError):
            continue
    return out


def mouse_mappings_to_list(mappings: Sequence[MouseMapping]) -> List[Dict[str, Any]]:
    """批量序列化。"""
    out: List[Dict[str, Any]] = []
    for mapping in mappings or ():
        try:
            out.append(mouse_mapping_to_dict(mapping))
        except Exception:  # pragma: no cover - 防御性
            continue
    return out


def mouse_mappings_from_list(data: Any) -> List[MouseMapping]:
    """批量反序列化。"""
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
        return []
    out: List[MouseMapping] = []
    for item in data:
        try:
            out.append(mouse_mapping_from_dict(item))
        except (TypeError, ValueError):
            continue
    return out


# ----------------------------------------------------------------------
# 默认映射
# ----------------------------------------------------------------------


def _k(trigger: str, key: str, **kwargs: Any) -> KeyMapping:
    """构造默认键盘映射的简写。"""
    return KeyMapping(trigger=trigger, key=key, **kwargs)


def _m(trigger: str, button: MouseButton, action: MouseAction, **kwargs: Any) -> MouseMapping:
    """构造默认鼠标映射的简写。"""
    return MouseMapping(trigger=trigger, button=button, action=action, **kwargs)


#: 方向键等：感应模式让"wwwww"也能持续移动
_SENSING = dict(enable_sensing=True, short_press_count=1, long_press_count=6)


def default_key_mappings() -> List[KeyMapping]:
    """默认键盘映射（弹幕文本 → 按键）。

    与参考项目的默认表相比，这里同时提供中文与英文触发词，
    并且给"数字/字母"类都开了感应，这样刷屏式弹幕也有反应。
    """
    return [
        # --- 移动 ---
        _k("w", "up", match_mode=MatchMode.EXACT, description="向上", **_SENSING),
        _k("s", "down", match_mode=MatchMode.EXACT, description="向下", **_SENSING),
        _k("a", "left", match_mode=MatchMode.EXACT, description="向左", **_SENSING),
        _k("d", "right", match_mode=MatchMode.EXACT, description="向右", **_SENSING),
        _k("上", "up", match_mode=MatchMode.EXACT, description="向上", **_SENSING),
        _k("下", "down", match_mode=MatchMode.EXACT, description="向下", **_SENSING),
        _k("左", "left", match_mode=MatchMode.EXACT, description="向左", **_SENSING),
        _k("右", "right", match_mode=MatchMode.EXACT, description="向右", **_SENSING),
        # --- 确认/取消 ---
        _k("空格", "space", match_mode=MatchMode.EXACT, description="空格", **_SENSING),
        _k("space", "space", match_mode=MatchMode.EXACT, description="空格", **_SENSING),
        _k("回车", "enter", match_mode=MatchMode.EXACT, description="回车"),
        _k("enter", "enter", match_mode=MatchMode.EXACT, description="回车"),
        _k("esc", "escape", match_mode=MatchMode.EXACT, description="退出"),
        _k("退出", "escape", match_mode=MatchMode.EXACT, description="退出"),
        # --- 数字技能键 ---
        _k("1", "1", match_mode=MatchMode.EXACT, description="技能1", **_SENSING),
        _k("2", "2", match_mode=MatchMode.EXACT, description="技能2", **_SENSING),
        _k("3", "3", match_mode=MatchMode.EXACT, description="技能3", **_SENSING),
        _k("4", "4", match_mode=MatchMode.EXACT, description="技能4", **_SENSING),
        _k("5", "5", match_mode=MatchMode.EXACT, description="技能5", **_SENSING),
        # --- 常用技能键 ---
        _k("q", "q", match_mode=MatchMode.EXACT, description="技能Q", **_SENSING),
        _k("e", "e", match_mode=MatchMode.EXACT, description="技能E", **_SENSING),
        _k("r", "r", match_mode=MatchMode.EXACT, description="技能R", **_SENSING),
        _k("f", "f", match_mode=MatchMode.EXACT, description="技能F", **_SENSING),
        _k("6", "6", match_mode=MatchMode.EXACT, description="技能6", **_SENSING),
        _k("7", "7", match_mode=MatchMode.EXACT, description="技能7", **_SENSING),
    ]


def default_mouse_mappings() -> List[MouseMapping]:
    """默认鼠标映射（弹幕文本 → 鼠标动作）。"""
    return [
        _m("左键", MouseButton.LEFT, MouseAction.CLICK, description="鼠标左键", **_SENSING),
        _m("左", MouseButton.LEFT, MouseAction.CLICK, description="鼠标左键", **_SENSING),
        _m("右键", MouseButton.RIGHT, MouseAction.CLICK, description="鼠标右键", **_SENSING),
        _m("右", MouseButton.RIGHT, MouseAction.CLICK, description="鼠标右键", **_SENSING),
        _m("中键", MouseButton.MIDDLE, MouseAction.CLICK, description="鼠标中键"),
        _m("上滚", MouseButton.MIDDLE, MouseAction.SCROLL_UP, description="滚轮上"),
        _m("滚轮上", MouseButton.MIDDLE, MouseAction.SCROLL_UP, description="滚轮上"),
        _m("下滚", MouseButton.MIDDLE, MouseAction.SCROLL_DOWN, description="滚轮下"),
        _m("滚轮下", MouseButton.MIDDLE, MouseAction.SCROLL_DOWN, description="滚轮下"),
    ]


__all__ = [
    "AnyMapping",
    "BaseMapping",
    "InputType",
    "KeyAction",
    "KeyMapping",
    "MatchMode",
    "MouseAction",
    "MouseButton",
    "MouseMapping",
    "default_key_mappings",
    "default_mouse_mappings",
    "key_mapping_from_dict",
    "key_mapping_to_dict",
    "key_mappings_from_list",
    "key_mappings_to_list",
    "mouse_mapping_from_dict",
    "mouse_mapping_to_dict",
    "mouse_mappings_from_list",
    "mouse_mappings_to_list",
]
