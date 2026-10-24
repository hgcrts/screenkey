# -*- coding: utf-8 -*-
"""配置数据模型。

参考项目的 ``AppConfig`` 是个扁平 dataclass，所有字段塞在一起：窗口几何、
平台、凭证、显示选项、超时参数。它的字段集合被注释称为"对外契约，不随意增删"，
于是界面杂项状态只能另开一个 ``ui_state.json``，形成两套并行的持久化。

这里把配置拆成四个语义清晰的子配置，各自负责一件事：

- :class:`ConnectionConfig` —— 连哪个平台、哪个房间、用什么凭证
- :class:`DisplayConfig`  —— 弹幕窗口怎么显示
- :class:`InputConfig`    —— 键鼠模拟的全局开关与默认时序
- :class:`AppConfig`      —— 顶层聚合 + 窗口几何

每个子配置都提供 ``from_dict``，对未知键宽容、对缺失键用默认值，
这样旧配置文件与手改坏的 JSON 都不会导致启动失败。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, ClassVar, Dict, Mapping, Type, TypeVar

T = TypeVar("T")

#: 被当作"真"的字符串（JSON 里手写成字符串布尔值很常见）
_TRUE_STRINGS = frozenset({"1", "true", "yes", "on", "是", "开"})
#: 被当作"假"的字符串
_FALSE_STRINGS = frozenset({"0", "false", "no", "off", "", "否", "关", "none", "null"})


def _as_bool(value: Any, default: bool = False) -> bool:
    """把任意输入转成 ``bool``。

    手改 ``settings.json`` 时很容易写成 ``"enabled": "false"``。
    若直接 ``bool("false")`` 会得到 ``True``（非空字符串为真），
    于是"关掉按键输出"变成"打开按键输出"——静默的相反行为。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
        return default
    return default


def _as_int(value: Any, default: int) -> int:
    """把任意输入转成 ``int``；失败用 ``default``。"""
    if isinstance(value, bool):
        return int(value)
    try:
        return int(float(value)) if isinstance(value, str) else int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value: Any, default: float) -> float:
    """把任意输入转成 ``float``；失败用 ``default``。"""
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    """把任意输入转成 ``str``；``None`` 用 ``default``。"""
    if value is None:
        return default
    return str(value)


def _as_bool_map(value: Any, default: Mapping[str, bool]) -> Dict[str, bool]:
    """把 ``{类型: 真假}`` 还原成干净的字典，缺的键用 ``default``。

    手改 ``settings.json`` 时常见两种写法：只写想关掉的那一项（其
    余类型必须保持默认的"开"），或者把值写成 ``"off"``/``"false"``
    这类字符串。直接 ``dict(value)`` 会让第一种情况把其它类型全部
    变成"关"，而 ``bool("false")`` 又会把第二种情况静默反转成"开"。
    """
    merged: Dict[str, bool] = {
        str(key).strip().lower(): bool(flag) for key, flag in dict(default).items()
    }
    if isinstance(value, Mapping):
        for key, raw in value.items():
            merged[str(key).strip().lower()] = _as_bool(raw, merged.get(str(key).strip().lower(), True))
    return merged


class Platform(str, Enum):
    """支持的直播平台。"""

    DOUYIN = "douyin"
    BILIBILI = "bilibili"

    @classmethod
    def coerce(cls, value: Any) -> "Platform":
        """把任意输入转成 ``Platform``，未知值回落到 B 站。"""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except (ValueError, AttributeError):
            return cls.BILIBILI

    @property
    def label(self) -> str:
        """中文显示名。"""
        return {"douyin": "抖音", "bilibili": "B站"}[self.value]

    @property
    def default_room_hint(self) -> str:
        """界面上给用户看的房间号填写示例。"""
        return {
            "douyin": "https://live.douyin.com/123456789",
            "bilibili": "https://live.bilibili.com/12345",
        }[self.value]


class DouyinMode(str, Enum):
    """抖音连接模式。"""

    WEBSOCKET = "websocket"   # 直接走 protobuf WebSocket（默认，真实可用）
    BROWSER = "browser"       # 用 playwright 抓页面（需要额外安装）

    @classmethod
    def coerce(cls, value: Any) -> "DouyinMode":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except (ValueError, AttributeError):
            return cls.WEBSOCKET


def _filter_fields(cls: Type[T], data: Mapping[str, Any]) -> Dict[str, Any]:
    """只保留 ``cls`` 声明过的字段，忽略未知键。

    手改过的配置文件里常有多余键（或旧版本遗留字段），直接
    ``cls(**data)`` 会抛 ``TypeError`` 让整个程序起不来。
    """
    if not isinstance(data, Mapping):
        return {}
    known = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    return {k: v for k, v in data.items() if k in known}


@dataclass
class ConnectionConfig:
    """连接相关配置。"""

    platform: Platform = Platform.BILIBILI
    room: str = ""
    cookie: str = ""
    sessdata: str = ""
    douyin_mode: DouyinMode = DouyinMode.WEBSOCKET

    # 重连策略
    auto_reconnect: bool = True
    reconnect_delay: float = 3.0
    max_reconnect_attempts: int = 5

    # 网络
    connection_timeout: float = 15.0
    heartbeat_interval: float = 10.0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ConnectionConfig":
        """从 dict 还原。

        每个字段都做**显式类型强转**：手改配置写成 ``"auto_reconnect": "false"``
        或 ``"connection_timeout": "15"`` 时，若原样塞进 dataclass，前者的真值
        判断会反转、后者在 ``> 0`` 之类的比较上直接抛 ``TypeError``。
        """
        kwargs = _filter_fields(cls, data)
        default = cls()
        return cls(
            platform=Platform.coerce(kwargs.get("platform", default.platform)),
            room=_as_str(kwargs.get("room"), default.room),
            cookie=_as_str(kwargs.get("cookie"), default.cookie),
            sessdata=_as_str(kwargs.get("sessdata"), default.sessdata),
            douyin_mode=DouyinMode.coerce(kwargs.get("douyin_mode", default.douyin_mode)),
            auto_reconnect=_as_bool(kwargs.get("auto_reconnect"), default.auto_reconnect),
            reconnect_delay=_as_float(kwargs.get("reconnect_delay"), default.reconnect_delay),
            max_reconnect_attempts=_as_int(
                kwargs.get("max_reconnect_attempts"), default.max_reconnect_attempts
            ),
            connection_timeout=_as_float(
                kwargs.get("connection_timeout"), default.connection_timeout
            ),
            heartbeat_interval=_as_float(
                kwargs.get("heartbeat_interval"), default.heartbeat_interval
            ),
        )


#: 弹幕窗口可单独开关的消息类型，顺序即设置对话框里的显示顺序。
#: 和 :class:`screenkey.danmaku.message.MessageType` 的取值一一对应。
DANMAKU_TYPE_ORDER = (
    "danmaku",
    "gift",
    "guard",
    "super_chat",
    "enter",
    "follow",
    "like",
    "stats",
    "system",
)


def _default_show_types() -> Dict[str, bool]:
    """所有消息类型默认都显示。"""
    return {key: True for key in DANMAKU_TYPE_ORDER}


@dataclass
class DisplayConfig:
    """弹幕显示窗口配置。"""

    max_lines: int = 1000
    show_timestamp: bool = True
    show_nickname: bool = True
    show_platform: bool = True
    # 单位：像素（与 QSS 同一套单位）。16px 等于旧版 12 磅的观感 ——
    # 旧版把它当磅用，同一个"12"在弹幕窗是 16px、在选项页是 12px。
    font_size: int = 16
    auto_scroll: bool = True
    # 只看触发了按键的弹幕
    only_triggered: bool = False
    # 弹幕窗口透明度（0.2 - 1.0）
    opacity: float = 0.95
    # 窗口置顶（``Qt.WindowStaysOnTopHint``）。弹幕列表本来就是边玩/边播边瞄的
    # 浮层，默认置顶；不想挡住别的窗口时点工具栏「置顶」取消，状态写回配置。
    always_on_top: bool = True
    # 按消息类型单独开关：键是 ``MessageType`` 的值（"danmaku"/"gift"/...），
    # 缺省全开。没写进配置的类型按"开"处理，所以以后新增消息类型不会因为
    # 旧配置里没有这个键而被默认静音。
    show_types: Dict[str, bool] = field(default_factory=_default_show_types)

    def type_enabled(self, msg_type: Any) -> bool:
        """该消息类型是否显示；类型不认识时按显示处理。"""
        key = getattr(msg_type, "value", msg_type)
        return bool(self.show_types.get(str(key).strip().lower(), True))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DisplayConfig":
        """从 dict 还原（显式类型强转，见 :meth:`ConnectionConfig.from_dict`）。"""
        kwargs = _filter_fields(cls, data)
        default = cls()
        return cls(
            max_lines=max(1, _as_int(kwargs.get("max_lines"), default.max_lines)),
            show_timestamp=_as_bool(kwargs.get("show_timestamp"), default.show_timestamp),
            show_nickname=_as_bool(kwargs.get("show_nickname"), default.show_nickname),
            show_platform=_as_bool(kwargs.get("show_platform"), default.show_platform),
            font_size=min(72, max(6, _as_int(kwargs.get("font_size"), default.font_size))),
            auto_scroll=_as_bool(kwargs.get("auto_scroll"), default.auto_scroll),
            only_triggered=_as_bool(kwargs.get("only_triggered"), default.only_triggered),
            opacity=min(1.0, max(0.2, _as_float(kwargs.get("opacity"), default.opacity))),
            always_on_top=_as_bool(kwargs.get("always_on_top"), default.always_on_top),
            show_types=_as_bool_map(kwargs.get("show_types"), default.show_types),
        )


@dataclass
class FocusFilterConfig:
    """「只在指定窗口为前台时才注入按键」的配置。

    默认关闭：不填任何东西时行为与旧版本完全一致（往当前焦点窗口发）。
    :attr:`field` 用进程名而不是标题往往更耐用——游戏窗口标题常带等级、
    房间名，会变；进程名不会。
    """

    enabled: bool = False
    #: 目标窗口的匹配目标（标题 / 进程名 / 类名，按 :attr:`field` 解释）
    pattern: str = ""
    #: 匹配哪个字段：``"title"`` / ``"process"`` / ``"class"``
    field: str = "title"
    #: 匹配方式：``"contains"`` / ``"exact"`` / ``"regex"``
    match_mode: str = "contains"
    case_sensitive: bool = False

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FocusFilterConfig":
        """从 dict 还原（显式类型强转，见 :meth:`ConnectionConfig.from_dict`）。"""
        if not isinstance(data, Mapping):
            return cls()
        kwargs = _filter_fields(cls, data)
        default = cls()
        return cls(
            enabled=_as_bool(kwargs.get("enabled"), default.enabled),
            pattern=_as_str(kwargs.get("pattern"), default.pattern),
            field=_as_focus_field(kwargs.get("field")),
            match_mode=_as_focus_mode(kwargs.get("match_mode")),
            case_sensitive=_as_bool(kwargs.get("case_sensitive"), default.case_sensitive),
        )


def _as_focus_field(value: Any) -> str:
    """收敛到合法的匹配字段；非法值回落 ``title``。

    这里**不能**直接信配置里的字符串：``field`` 会被拿去查
    :class:`WindowInfo` 的取值分支，拼错一个字母就会静默退化成"永远比对标题"，
    用户看到的则是"填了进程名却怎么都不生效"。
    """
    text = str(value or "").strip().lower()
    return text if text in ("title", "process", "class") else "title"


def _as_focus_mode(value: Any) -> str:
    """收敛到合法的匹配方式；非法值回落 ``contains``。"""
    text = str(value or "").strip().lower()
    return text if text in ("contains", "exact", "regex") else "contains"


@dataclass
class InputConfig:
    """键鼠模拟的全局配置。"""

    enabled: bool = True
    # 游戏模式：用扫描码 + keybd_event，兼容 DirectInput 游戏
    game_mode: bool = False
    # 全局暂停/恢复热键
    toggle_hotkey: str = "ctrl+shift+p"
    # 单条弹幕最多触发几个映射
    max_actions_per_message: int = 1
    # 全局动作间隔下限（秒）：防止弹幕刷屏时按键风暴
    global_cooldown: float = 0.0
    # 同一用户在此秒数内的弹幕只处理第一条（0 = 不限制）
    per_user_cooldown: float = 0.0
    # 去重：相同内容的弹幕在此秒数内只触发一次
    dedupe_window: float = 0.0
    # 焦点窗口过滤：只在指定窗口为前台时才真的注入按键
    focus_filter: FocusFilterConfig = field(default_factory=FocusFilterConfig)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "InputConfig":
        """从 dict 还原。

        **必须显式强转**：这里曾经就是 ``cls(**_filter_fields(cls, data))``。
        手改 ``settings.json`` 写成 ``"global_cooldown": "1.5"`` 时，字段会以
        ``str`` 形态留在配置里，``engine._execute_match`` 里的
        ``if cfg.global_cooldown > 0`` 于是抛
        ``TypeError: '>' not supported between instances of 'str' and 'int'``，
        直接在抓取线程里打断整条弹幕处理链。
        """
        kwargs = _filter_fields(cls, data)
        default = cls()
        # 热键留空 / 只有空白会让全局热键永远注册不上（用户以为开了却按不出反应），
        # 所以先 strip 再判空回落默认值。
        hotkey = _as_str(kwargs.get("toggle_hotkey"), default.toggle_hotkey).strip()
        return cls(
            enabled=_as_bool(kwargs.get("enabled"), default.enabled),
            game_mode=_as_bool(kwargs.get("game_mode"), default.game_mode),
            toggle_hotkey=hotkey or default.toggle_hotkey,
            max_actions_per_message=min(
                10, max(0, _as_int(kwargs.get("max_actions_per_message"), default.max_actions_per_message))
            ),
            global_cooldown=max(0.0, _as_float(kwargs.get("global_cooldown"), default.global_cooldown)),
            per_user_cooldown=max(
                0.0, _as_float(kwargs.get("per_user_cooldown"), default.per_user_cooldown)
            ),
            dedupe_window=max(0.0, _as_float(kwargs.get("dedupe_window"), default.dedupe_window)),
            focus_filter=FocusFilterConfig.from_dict(kwargs.get("focus_filter") or {}),
        )


@dataclass
class WindowGeometry:
    """主窗口几何。"""

    x: int = 100
    y: int = 100
    width: int = 1180
    height: int = 780

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WindowGeometry":
        kwargs = _filter_fields(cls, data)
        default = cls()
        out = cls(
            x=_as_int(kwargs.get("x"), default.x),
            y=_as_int(kwargs.get("y"), default.y),
            width=_as_int(kwargs.get("width"), default.width),
            height=_as_int(kwargs.get("height"), default.height),
        )
        # 防止窗口小到看不见
        out.width = max(640, out.width)
        out.height = max(480, out.height)
        # 防止坐标离谱到完全在屏幕外（换显示器 / 分辨率变小后很常见）。
        # 这里只做一个**粗**边界；精确的"夹回可用屏幕区域"需要 Qt，
        # 由 ``MainWindow._restore_geometry`` 用 QScreen 完成。
        out.x = min(20000, max(-20000, out.x))
        out.y = min(20000, max(-20000, out.y))
        return out


@dataclass
class AppConfig:
    """顶层配置聚合。"""

    #: 配置 schema 版本，供未来迁移使用。
    #:
    #: 必须是 ``ClassVar``：写成普通字段会被 ``asdict()`` 带进 ``settings.json``
    #: （曾经真的写进去过，见 ````selftest/settings.json`` 的历史样本）。
    SCHEMA_VERSION: ClassVar[int] = 3

    version: int = SCHEMA_VERSION
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    input: InputConfig = field(default_factory=InputConfig)
    window: WindowGeometry = field(default_factory=WindowGeometry)

    def to_dict(self) -> Dict[str, Any]:
        """转成可 JSON 序列化的 dict。"""
        data = asdict(self)
        data["connection"]["platform"] = self.connection.platform.value
        data["connection"]["douyin_mode"] = self.connection.douyin_mode.value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AppConfig":
        """从 dict 还原；任何子配置损坏都只影响它自己，其余用默认值。"""
        if not isinstance(data, Mapping):
            return cls()

        def _sub(key: str, sub_cls: Type[T], default: T) -> T:
            raw = data.get(key)
            if not isinstance(raw, Mapping):
                return default
            try:
                return sub_cls.from_dict(raw)  # type: ignore[attr-defined]
            except (TypeError, ValueError):
                return default

        default = cls()
        config = cls(
            version=_as_int(data.get("version"), cls.SCHEMA_VERSION) or cls.SCHEMA_VERSION,
            connection=_sub("connection", ConnectionConfig, default.connection),
            display=_sub("display", DisplayConfig, default.display),
            input=_sub("input", InputConfig, default.input),
            window=_sub("window", WindowGeometry, default.window),
        )
        return config


__all__ = [
    "AppConfig",
    "ConnectionConfig",
    "DANMAKU_TYPE_ORDER",
    "DisplayConfig",
    "DouyinMode",
    "FocusFilterConfig",
    "InputConfig",
    "Platform",
    "WindowGeometry",
]
