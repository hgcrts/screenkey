# -*- coding: utf-8 -*-
"""通用小工具。

与参考项目 ``utils/helpers.py`` 相比的变化：

- ``extract_room_id`` 只做纯提取（返回 str），平台判断交给调用方，
  避免一个函数里塞三套正则互相干扰
- 时间相关工具统一走 ``datetime``，不再混用 float 时间戳与字符串
- 限流不放这里：触发频率由 :class:`~screenkey.input.matcher.CooldownTracker`
  统一管（按键级 / 用户级 / 全局三级），这里只留纯函数
"""

from __future__ import annotations

import random
import re
import string
from datetime import datetime
from typing import Any, List, Optional

# ----------------------------------------------------------------------
# 文本
# ----------------------------------------------------------------------

#: 控制字符（C0/C1 区），弹幕里偶有混入，直接落进 Qt 会显示成方块
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_WHITESPACE = re.compile(r"\s+")


def clean_text(text: Any, max_length: int = 0) -> str:
    """清理文本：去控制字符、压缩空白、去首尾空格。

    :param text: 任意对象，会被 ``str()``
    :param max_length: 大于 0 时截断到该长度（超长弹幕会拖垮界面渲染）
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    # 保留 \n \t 之外的空白处理交给 _WHITESPACE，所以先把 \t\n\r 也一起压缩
    cleaned = _CONTROL_CHARS.sub("", text)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    if max_length > 0 and len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    return cleaned


def truncate(text: str, length: int, suffix: str = "…") -> str:
    """按字符数截断（比 ``clean_text`` 的 max_length 更显式）。"""
    if length <= 0 or len(text) <= length:
        return text
    return text[: max(0, length - len(suffix))] + suffix


def is_valid_url(url: str) -> bool:
    """粗略校验 http(s) URL。"""
    if not isinstance(url, str):
        return False
    pattern = re.compile(
        r"^https?://"
        r"(?:\d{1,3}\.){3}\d{1,3}"      # IP
        r"|^https?://[^\s/$.?#].[^\s]*$"  # 域名
    )
    return bool(pattern.match(url.strip()))


# ----------------------------------------------------------------------
# 房间号提取
# ----------------------------------------------------------------------

_DOUYIN_PATTERNS = (
    re.compile(r"live\.douyin\.com/(\d+)"),
    re.compile(r"live\.douyin\.com/([A-Za-z0-9_\-]+)"),
    # PC 客户端 / 网页版分享出来的「主站」链接：
    #   https://www.douyin.com/root/live/373609674107
    #   https://www.douyin.com/follow/live/123456
    #   https://www.douyin.com/live/live/123456
    re.compile(r"douyin\.com/(?:root/|follow/)?live/(?:live/)?(\d+)"),
    re.compile(r"douyin\.com/live/(\d+)"),
    # 兜底：任何 douyin.com 链接里形如 /<6 位以上数字> 的末段
    # （例如 https://www.douyin.com/room/1234567890）
    re.compile(r"douyin\.com/\S*?/(\d{6,})"),
    re.compile(r"^(\d{6,})$"),
)

_BILI_PATTERNS = (
    re.compile(r"live\.bilibili\.com/(?:blanc/)?(\d+)"),
    re.compile(r"bilibili\.com/live/(\d+)"),
    re.compile(r"^(\d+)$"),
)


def extract_douyin_room_id(value: str) -> Optional[str]:
    """从抖音直播链接或纯数字里提取 web_rid。"""
    if not value:
        return None
    text = value.strip()
    for pattern in _DOUYIN_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def extract_bilibili_room_id(value: str) -> Optional[str]:
    """从 B 站直播链接或纯数字里提取真实房间号。

    注意：这里返回的是 URL 里写的短号，不是 ``get_info`` 得到的真实房间号；
    短号交给 blivedm 处理即可，它会自己解析。
    """
    if not value:
        return None
    text = value.strip()
    for pattern in _BILI_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def extract_room_id(value: str, platform: str = "") -> Optional[str]:
    """按平台提取房间号；平台未知时先试 B 站再试抖音。

    B 站优先是因为抖音的 ``live.douyin.com/<rid>`` 正则更宽松，
    纯数字输入在两边都合法，而 B 站房间号纯数字最常见。
    """
    platform = (platform or "").lower()
    if platform in ("bilibili", "bili", "b站"):
        return extract_bilibili_room_id(value)
    if platform in ("douyin", "dy", "抖音"):
        return extract_douyin_room_id(value)
    return extract_bilibili_room_id(value) or extract_douyin_room_id(value)


# ----------------------------------------------------------------------
# 杂项
# ----------------------------------------------------------------------


def generate_random_string(length: int = 32) -> str:
    """生成随机字母数字串（用于签名 nonce 等）。"""
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choices(alphabet, k=max(1, length)))


def parse_key_combo(combo: str) -> List[str]:
    """把 ``"Ctrl + Shift + Q"`` 解析成 ``["ctrl", "shift", "q"]``。

    参考项目的热键 bug 就出在这里：注册时按书写顺序存，
    查询时按字母序查，两者不一致导致热键永不触发。
    所以本函数**统一排序**，让"书写顺序"这个变量彻底消失。
    """
    if not combo:
        return []
    parts = [p.strip().lower() for p in str(combo).split("+")]
    return sorted(p for p in parts if p)


def normalize_key_name(key: str) -> str:
    """统一按键别名，便于比较。"""
    if not key:
        return ""
    alias = {
        "control": "ctrl",
        "return": "enter",
        "esc": "escape",
        "page_up": "pageup",
        "page_down": "pagedown",
        " ": "space",
        "lwin": "win",
        "rwin": "win",
    }
    lowered = str(key).strip().lower()
    return alias.get(lowered, lowered)


# ----------------------------------------------------------------------
# 时间
# ----------------------------------------------------------------------


def timestamp_to_datetime(timestamp: Any) -> datetime:
    """把秒级/毫秒级时间戳或 ISO 字符串转成 ``datetime``。"""
    if isinstance(timestamp, datetime):
        return timestamp
    if isinstance(timestamp, str):
        try:
            return datetime.fromisoformat(timestamp)
        except ValueError:
            pass
        try:
            timestamp = float(timestamp)
        except ValueError:
            return datetime.now()
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return datetime.now()
    # 13 位以上按毫秒处理
    if value > 1e11:
        value /= 1000.0
    try:
        return datetime.fromtimestamp(value)
    except (OSError, OverflowError, ValueError):
        return datetime.now()


def format_duration(seconds: float) -> str:
    """把秒数格式化成人读字符串。"""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}秒"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}分{secs}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}小时{minutes}分"


__all__ = [
    "clean_text",
    "extract_bilibili_room_id",
    "extract_douyin_room_id",
    "extract_room_id",
    "format_duration",
    "generate_random_string",
    "is_valid_url",
    "normalize_key_name",
    "parse_key_combo",
    "timestamp_to_datetime",
    "truncate",
]
